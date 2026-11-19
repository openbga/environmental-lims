"""Monthly concentration summaries from a project's current immutable report versions."""

import json
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Context, Decimal, Inexact, localcontext
from fractions import Fraction

from django.db import connection

from analytical.parsing import MAX_TEXT_LENGTH

from .daily_load import issued_concentration_mg_l
from .models import Report

MAX_MONTHLY_ROWS = 1000

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
               jsonb_agg(jsonb_build_array(sample_data, result_data, within_month)
                         ORDER BY sample_position, result_position) AS sources
        FROM matches GROUP BY report_id, version_id, report_version, report_number, synthetic_notice
    )
    SELECT rows.report_id, rows.report_number, rows.report_version, rows.synthetic_notice,
           rows.sources::text, (file.snapshot #> '{{content,batches}}')::text,
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


def monthly_concentrations(*, client_id, project_id, location, analyte, matrix, month):
    """Calculate only when each represented day has exactly one eligible issued result."""
    if type(month) is not date or month.day != 1:
        raise ValueError("Supply the first calendar day of the collection month.")
    summary = {
        "days": 0,
        "rows": [],
        "mean": None,
        "maximum": None,
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

    rows = summary["rows"]
    values = []
    for report_id, number, version, synthetic, sources, batches, pdf_available in versions:
        summary["synthetic_notice"] |= synthetic
        sources = json.loads(sources)
        if len(rows) + len(sources) > MAX_MONTHLY_ROWS:
            return summary | {
                "rows": [],
                "days": None,
                "reason": f"The worksheet exceeds the {MAX_MONTHLY_ROWS:,} source result limit.",
            }
        content = {"batches": json.loads(batches) if batches else None}
        if not number or not number.strip():
            number = Report(pk=report_id).number
        for sample, result, within_month in sources:
            row, value = _source_row(sample, result, within_month, content)
            row.update(
                report_id=report_id,
                report_number=number,
                report_version=version,
                pdf_available=pdf_available,
            )
            rows.append(row)
            if value is not None:
                values.append(value)

    rows.sort(key=lambda row: (row["day"] is None, row["day"]))
    counts = Counter()
    sample_days = defaultdict(set)
    for row in rows:
        if row["day"] is not None:
            counts[row["day"]] += 1
            if row["lab_sample_id"] is not None:
                sample_days[row["lab_sample_id"]].add(row["day"])
    summary["days"] = len(counts)
    for row in rows:
        reasons = [row["reason"]]
        if counts[row["day"]] > 1:
            reasons.append("Multiple issued results on this sampling day.")
        if len(sample_days[row["lab_sample_id"]]) > 1:
            reasons.append("The same lab sample ID appears on different sampling days.")
        row["reason"] = " ".join(filter(None, reasons))

    if not rows:
        summary["reason"] = "No matching issued results were found for this month."
    elif any(row["reason"] for row in rows):
        summary["reason"] = "Check the status on each issued source result."
    else:
        summary["mean"] = _mean(values)
        summary["maximum"] = format(max(values), "f")
    return summary
