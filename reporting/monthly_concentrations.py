"""Monthly concentration summaries from a project's current immutable report versions."""

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Context, Decimal, Inexact, localcontext
from fractions import Fraction
from hashlib import sha256

from django.db import connection

from analytical.parsing import MAX_TEXT_LENGTH

from .daily_load import FLOW_DECIMAL_PLACES, FLOW_MAX_DIGITS, issued_concentration_mg_l
from .models import Report

MAX_MONTHLY_ROWS = 1000
# Leave space in Django's 1,000-field POST limit for the series token and CSRF field.
MAX_MONTHLY_FLOWS = 998

# New issues store local ISO datetimes with offsets. Compare their calendar month as
# issued, even at a UTC month boundary. Unusable endpoints leave the interval open.
# Only use canonical calendar timestamps to rule a sample out in the database.
_ISSUED_MONTH = """
    CASE WHEN {value} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}([T ]|$)'
              AND pg_input_is_valid({value}, 'timestamp with time zone')
         THEN left({value}, 7) END
"""
_MONTH_SQL = """
    WITH matches AS (
        SELECT report.id AS report_id, version.id AS version_id, version.number AS report_version,
               CASE WHEN jsonb_typeof(content.data #> '{{report,number}}') = 'string'
                    THEN content.data #>> '{{report,number}}' END AS report_number,
               (content.data -> 'synthetic_notice') IS NOT DISTINCT FROM 'true'::jsonb
                 AS synthetic_notice,
               sample.position AS sample_position, result.position AS result_position,
               CASE WHEN jsonb_typeof(sample.data) = 'object'
                    THEN sample.data - 'results' - 'containers' END AS sample_data,
               result.data AS result_data,
               (months.first = %(month)s AND months.last = %(month)s) AS within_month
        FROM receipt_project AS project
        JOIN reporting_report AS report ON report.project_id = project.id
        JOIN reporting_reportversion AS version
          ON version.report_id = report.id AND version.number = report.latest_version
        LEFT JOIN reporting_reportfile AS file ON file.version_id = version.id
        CROSS JOIN LATERAL (SELECT file.snapshot -> 'content' AS data) AS content
        LEFT JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(content.data -> 'samples') = 'array'
                 THEN content.data -> 'samples' END
        ) WITH ORDINALITY AS sample(data, position) ON true
        CROSS JOIN LATERAL (
            SELECT sample.data ->> 'collected_at' AS ended,
                   CASE WHEN sample.data ->> 'collection_type' = 'Grab'
                             AND sample.data ->> 'collection_started_at' IS NULL
                        THEN sample.data ->> 'collected_at'
                        ELSE sample.data ->> 'collection_started_at' END AS started
        ) AS period
        CROSS JOIN LATERAL (SELECT {start_month} AS first, {end_month} AS last) AS months
        LEFT JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(sample.data -> 'results') = 'array'
                 THEN sample.data -> 'results' END
        ) WITH ORDINALITY AS result(data, position) ON true
        WHERE project.client_id = %(client_id)s AND project.id = %(project_id)s
          AND report.latest_version > 0
          AND (jsonb_typeof(sample.data) IS DISTINCT FROM 'object'
               OR (sample.data -> 'collection_location' = to_jsonb(%(location)s::text)
                   AND sample.data -> 'matrix' = to_jsonb(%(matrix)s::text)))
          AND (jsonb_typeof(result.data -> 'analyte') IS DISTINCT FROM 'string'
               OR trim(result.data ->> 'analyte') = ''
               OR result.data -> 'analyte' = to_jsonb(%(analyte)s::text))
          AND CASE WHEN months.first IS NOT NULL AND months.last IS NOT NULL
                   THEN %(month)s BETWEEN least(months.first, months.last)
                                      AND greatest(months.first, months.last)
                   ELSE (months.first IS NULL OR months.first <= %(month)s)
                    AND (months.last IS NULL OR months.last >= %(month)s)
              END
        LIMIT %(row_limit)s
    ), by_version AS (
        SELECT report_id, version_id, report_version, report_number, synthetic_notice,
               jsonb_agg(jsonb_build_array(sample_data, result_data, within_month,
                                           sample_position, result_position)
                         ORDER BY sample_position, result_position) AS sources
        FROM matches GROUP BY report_id, version_id, report_version, report_number, synthetic_notice
    )
    SELECT rows.report_id, rows.report_number, rows.report_version, rows.version_id,
           rows.synthetic_notice, rows.sources::text,
           (file.snapshot #> '{{content,batches}}')::text,
           file.version_id IS NOT NULL AS pdf_available
    FROM by_version AS rows
    LEFT JOIN reporting_reportfile AS file ON file.version_id = rows.version_id
    ORDER BY rows.report_id
""".format(
    start_month=_ISSUED_MONTH.format(value="period.started"),
    end_month=_ISSUED_MONTH.format(value="period.ended"),
)


def _issued_datetime(text):
    try:
        stamp = datetime.fromisoformat(text)
        if text[:10] == stamp.date().isoformat() and text[10:11] in ("", "T", " "):
            return stamp
        return None
    except TypeError, ValueError:
        return None


def issued_collection_date(text):
    """Read the local calendar day printed on the issue, preserving the frozen offset."""
    stamp = _issued_datetime(text)
    return stamp.date() if stamp else None


def _sampling_day(sample):
    end = _issued_datetime(sample.get("collected_at"))
    if end is None:
        return None, "The issued sample has no usable collection date."
    kind = sample.get("collection_type")
    if kind == "Grab" and sample.get("collection_started_at") is None:
        return end.date(), ""
    if kind != "Composite":
        return None, "The issued collection type and interval do not identify one sampling day."
    start = _issued_datetime(sample.get("collection_started_at"))
    if start is None:
        return None, "The composite has no usable collection start."
    if start.date() != end.date():
        return None, "The composite spans calendar days and cannot be assigned to one day."
    if (start.tzinfo is None) != (end.tzinfo is None) or start > end:
        return None, "The composite collection interval is inconsistent."
    return end.date(), ""


def _source_row(sample, result, within_month, content):
    sample = sample or {}
    result = result if isinstance(result, dict) else {}
    day, reason = _sampling_day(sample)
    lab_id = sample.get("lab_id")
    if not isinstance(lab_id, str) or not lab_id.strip():
        lab_id = None
    if not sample:
        day, reason = None, "The issued report has incomplete sample information."
    elif not isinstance(result.get("analyte"), str) or not result["analyte"].strip():
        day, reason = None, "The issued sample has incomplete result or analyte information."
    elif day is not None and not within_month:
        day, reason = None, "The issued collection interval has no usable calendar month."
    elif not reason and lab_id is None:
        reason = "The issued result has no lab sample ID."
    value = None
    if not reason:
        value, reason = issued_concentration_mg_l(content, sample, result)
    return {
        "lab_sample_id": lab_id,
        "collected_at": sample.get("collected_at"),
        "collection_started_at": sample.get("collection_started_at"),
        "sample_source": sample.get("source"),
        "sample_type": sample.get("sample_type", "Field sample"),
        "day": day,
        "reported": result.get("reported"),
        "unit": result.get("unit"),
        "method_code": result.get("method_code"),
        "method_version": result.get("method_version"),
        "flow_key": None,
        "reason": reason,
    }, value


def _mean(values):
    # An issued coefficient and exponent each have at most MAX_TEXT_LENGTH digits
    # of precision and magnitude respectively. A microgram conversion adds three
    # fractional places. Summing up to 31 days can add two whole digits.
    with localcontext(Context(prec=3 * MAX_TEXT_LENGTH + 10)) as context:
        context.traps[Inexact] = True
        total = sum(values, Decimal(0))
        try:
            return format(total / len(values), "f")
        except Inexact:
            return str(Fraction(total) / len(values))


def _exact_text(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    # A terminating rational needs fewer decimal digits than the total number of
    # binary digits in its numerator and denominator.
    precision = value.numerator.bit_length() + value.denominator.bit_length()
    with localcontext(Context(prec=precision)) as context:
        context.traps[Inexact] = True
        try:
            return format(Decimal(value.numerator) / Decimal(value.denominator), "f")
        except Inexact:
            return str(value)


def _summarize_sources(summary, sources):
    rows = summary["rows"]
    if not rows:
        summary["reason"] = "No matching issued results were found for this month."
        return summary

    sample_counts = Counter(row["lab_sample_id"] for row in rows if row["lab_sample_id"])
    by_day = defaultdict(list)
    for row, concentration, collection_type, identity in sources:
        if sample_counts[row["lab_sample_id"]] > 1:
            row["reason"] = " ".join(
                filter(None, (row["reason"], "The same lab sample ID appears more than once."))
            )
        if row["day"] is not None:
            by_day[row["day"]].append((row, concentration, collection_type, identity))
    summary["days"] = len(by_day)

    multi_result_days = [entries for entries in by_day.values() if len(entries) > 1]
    for entries in multi_result_days:
        for row, _, kind, _ in entries:
            if (
                not row["reason"]
                and kind == "Grab"
                and datetime.fromisoformat(row["collected_at"]).tzinfo is None
            ):
                row["reason"] = (
                    "An issued collection time with a time zone offset is required to match "
                    "this grab to its flow."
                )
        if any(row["reason"] or kind != "Grab" for row, _, kind, _ in entries):
            for row, *_ in entries:
                row["reason"] = " ".join(
                    filter(
                        None,
                        (
                            row["reason"],
                            "Multiple issued results on this sampling day require distinct "
                            "eligible grab samples.",
                        ),
                    )
                )

    if any(row["reason"] for row in rows):
        summary["reason"] = "Check the status on each issued source result."
    elif not multi_result_days:
        values = [concentration for _, concentration, _, _ in sources]
        summary["mean"] = _mean(values)
        summary["maximum"] = format(max(values), "f")
    elif sum(map(len, multi_result_days)) > MAX_MONTHLY_FLOWS:
        summary["reason"] = f"The worksheet exceeds the {MAX_MONTHLY_FLOWS:,} grab flow limit."
    else:
        summary["reason"] = (
            "Enter the flow measured at each listed grab sample to calculate the monthly figures."
        )
        summary["_daily_sources"] = daily_sources = {}
        for day, entries in sorted(by_day.items()):
            daily_sources[day] = []
            for row, concentration, _, identity in entries:
                key = None
                if len(entries) > 1:
                    key = "flow_" + "_".join(map(str, identity))
                    summary["flow_keys"][key] = f"{row['lab_sample_id']} ({row['collected_at']})"
                    row["flow_key"] = key
                daily_sources[day].append((key, concentration))
    return summary


def monthly_concentrations(*, client_id, project_id, location, analyte, matrix, month):
    """Summarize eligible issued concentrations, collecting per-grab flows where needed."""
    if type(month) is not date or month.day != 1:
        raise ValueError("Supply the first calendar day of the collection month.")
    summary = {
        "days": 0,
        "rows": [],
        "mean": None,
        "maximum": None,
        "daily_values": [],
        "flow_keys": {},
        "series_token": "",
        "reason": "",
        "synthetic_notice": False,
    }
    if matrix not in ("Water", "Wastewater") or not all(
        isinstance(field, str) and field.strip() for field in (location, analyte)
    ):
        summary["reason"] = "Select an issued aqueous analyte with a sampling location."
        return summary

    with connection.cursor() as cursor:
        cursor.execute(
            _MONTH_SQL,
            {
                "client_id": client_id,
                "project_id": project_id,
                "location": location,
                "analyte": analyte,
                "matrix": matrix,
                "month": month.isoformat()[:7],
                "row_limit": MAX_MONTHLY_ROWS + 1,
            },
        )
        versions = cursor.fetchall()

    sources = []
    for (
        report_id,
        number,
        version,
        version_id,
        synthetic,
        payload,
        batches,
        pdf_available,
    ) in versions:
        summary["synthetic_notice"] |= synthetic
        issued_sources = json.loads(payload)
        if len(sources) + len(issued_sources) > MAX_MONTHLY_ROWS:
            return summary | {
                "days": None,
                "reason": f"The worksheet exceeds the {MAX_MONTHLY_ROWS:,} source result limit.",
            }
        content = {"batches": json.loads(batches) if batches else None}
        if not number or not number.strip():
            number = Report(pk=report_id).number
        for sample, result, within_month, sample_position, result_position in issued_sources:
            row, value = _source_row(sample, result, within_month, content)
            row.update(
                report_id=report_id,
                report_number=number,
                report_version=version,
                pdf_available=pdf_available,
            )
            identity = (report_id, version_id, sample_position, result_position)
            sources.append((row, value, (sample or {}).get("collection_type"), identity))

    if sources:
        identities = json.dumps([identity for _, _, _, identity in sources], separators=(",", ":"))
        summary["series_token"] = sha256(identities.encode()).hexdigest()
    summary["rows"] = sorted(
        (row for row, *_ in sources), key=lambda row: (row["day"] is None, row["day"])
    )
    return _summarize_sources(summary, sources)


def apply_grab_flows(summary, flows):
    """Calculate daily and monthly concentrations using every required measured flow."""
    expected = summary["flow_keys"]
    if not expected or not isinstance(flows, Mapping) or flows.keys() != expected.keys():
        raise ValueError("Supply one flow for each listed grab sample.")
    for flow in flows.values():
        if not isinstance(flow, Decimal) or not flow.is_finite() or flow <= 0:
            raise ValueError("Each flow must be a positive decimal number.")
        if (
            flow.as_tuple().exponent < -FLOW_DECIMAL_PLACES
            or flow.adjusted() >= FLOW_MAX_DIGITS - FLOW_DECIMAL_PLACES
        ):
            raise ValueError("Flow can have at most 14 whole digits and 10 decimal places.")

    daily = {}
    for day, entries in summary["_daily_sources"].items():
        if len(entries) == 1:
            daily[day] = entries[0][1]
            continue
        weighted_total = flow_total = Fraction(0)
        for key, concentration in entries:
            flow = Fraction(flows[key])
            weighted_total += Fraction(concentration) * flow
            flow_total += flow
        daily[day] = weighted_total / flow_total
    average = sum(Fraction(value) for value in daily.values()) / len(daily)
    return summary | {
        "mean": _exact_text(average),
        "maximum": _exact_text(max(daily.values(), key=Fraction)),
        "daily_values": [{"day": day, "value": _exact_text(value)} for day, value in daily.items()],
        "reason": "",
    }
