"""Client location results from the current issued versions of the project's reports."""

import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Page, Paginator
from django.db import connection

from analytical.models import ResultBasis, ResultKind
from analytical.parsing import MAX_TEXT_LENGTH
from core.grids import PAGE_SIZES

from .results_csv import issued_result_values

# Keep the count and page in one statement so they see the same set of issued versions.
# Expand the frozen arrays in PostgreSQL and return only the selected page's result objects.
_HISTORY_SQL = """
    WITH matches AS (
        SELECT report.id AS report_id, version.id AS version_id, version.number AS report_version,
               content.report ->> 'number' AS report_number,
               content.synthetic_notice IS NOT DISTINCT FROM 'true'::jsonb AS synthetic_notice,
               sample.data ->> 'lab_id' AS lab_sample_id,
               sample.data ->> 'client_sample_id' AS client_sample_id,
               sample.data ->> 'matrix' AS matrix,
               sample.data ->> 'disposition' AS receipt_disposition,
               NULLIF(sample.data ->> 'collected_at', '') AS collected_at,
               sample.position AS sample_position, result.position AS result_position,
               result.data AS result_data
        FROM receipt_project AS project
        JOIN reporting_report AS report ON report.project_id = project.id
        JOIN reporting_reportversion AS version
          ON version.report_id = report.id AND version.number = report.latest_version
        JOIN reporting_reportfile AS file ON file.version_id = version.id
        CROSS JOIN LATERAL jsonb_to_record(file.snapshot -> 'content')
          AS content(report jsonb, samples jsonb, synthetic_notice jsonb)
        CROSS JOIN LATERAL jsonb_array_elements(content.samples)
          WITH ORDINALITY AS sample(data, position)
        CROSS JOIN LATERAL jsonb_array_elements(sample.data -> 'results')
          WITH ORDINALITY AS result(data, position)
        WHERE project.client_id = %(client_id)s AND project.id = %(project_id)s
          AND report.latest_version > 0
          AND (sample.data ->> 'collection_location') COLLATE "C" = %(location)s
          AND (%(analyte)s = '' OR strpos(lower(result.data ->> 'analyte'), lower(%(analyte)s)) > 0)
          {comparison_filter}
    ), totals AS (
        SELECT count(*) AS total,
               greatest(1, (count(*) + %(per_page)s - 1) / %(per_page)s) AS last_page
        FROM matches
    ), page AS (
        SELECT total, CASE WHEN %(page)s < 1 THEN last_page
                           ELSE least(last_page, %(page)s) END AS number
        FROM totals
    ), selected_rows AS (
        SELECT rows.* FROM page
        CROSS JOIN LATERAL (
            SELECT * FROM matches
            ORDER BY collected_at::timestamptz {direction} NULLS LAST,
                     report_id, sample_position, result_position
            LIMIT %(per_page)s OFFSET (page.number - 1) * %(per_page)s
        ) AS rows
    )
    {qualified_qc}
    SELECT page.total, page.number, rows.report_id, rows.report_number, rows.report_version,
           rows.synthetic_notice, rows.lab_sample_id, rows.client_sample_id, rows.matrix,
           rows.collected_at, rows.sample_position, rows.result_position,
           rows.result_data::text AS result_data
           {qualification_column}
    FROM page LEFT JOIN selected_rows AS rows ON true
    ORDER BY rows.collected_at::timestamptz {direction} NULLS LAST,
             rows.report_id, rows.sample_position, rows.result_position
"""


# Restrict the series before counting or paginating it. For an unknown basis the
# complementary predicate retains only rows that also have no usable basis.
_COMPARISON_FILTER = """
    AND result.data -> 'analyte' = to_jsonb(%(comparison_analyte)s::text)
    AND result.data -> 'unit' = to_jsonb(%(comparison_unit)s::text)
    AND sample.data -> 'matrix' = to_jsonb(%(comparison_matrix)s::text)
    AND (
        (result.data -> 'result_basis') IS DISTINCT FROM 'null'::jsonb
        AND COALESCE(result.data ->> 'result_basis', '') = ANY(%(basis_codes)s::text[])
        AND (%(comparison_matrix)s <> 'Soil' OR NOT (result.data ? 'source'))
    ) = %(known_basis)s
"""

# Read QC once per displayed issue, only when applying a limit. The identity is
# the PDF's batch and test definition, or batch and analyte in pre-definition snapshots.
_QUALIFIED_QC = """
    , qualified_qc AS MATERIALIZED (
        SELECT file.version_id, batch ->> 'number' AS batch_number, qc.key AS analyte,
               NULLIF(qc.value -> 'test_definition_id', 'null'::jsonb) AS definition
        FROM (SELECT DISTINCT version_id FROM selected_rows) AS visible
        JOIN reporting_reportfile AS file ON file.version_id = visible.version_id
        CROSS JOIN LATERAL jsonb_array_elements(file.snapshot -> 'content' -> 'batches') AS batch
        CROSS JOIN LATERAL jsonb_each(batch -> 'qc') AS qc
        WHERE (qc.value -> 'issues') @> '[{"disposition":"accept_qualified"}]'::jsonb
    )
"""

_QUALIFICATION_COLUMN = """
    , (rows.receipt_disposition = 'Qualify'
       OR rows.result_data #>> ARRAY['preservation','decision','decision'] = 'qualify'
       OR EXISTS (
           SELECT 1 FROM qualified_qc AS qc
           WHERE qc.version_id = rows.version_id
             AND qc.batch_number IN (rows.result_data ->> 'batch',
                                     rows.result_data -> 'preparation' ->> 'batch')
             AND (qc.definition = rows.result_data -> 'test_definition_id'
                  OR (qc.definition IS NULL AND rows.result_data ->> 'test_definition_id' IS NULL
                      AND qc.analyte COLLATE "C" = rows.result_data ->> 'analyte'))
       )) AS report_qualified
"""

# Keys are the raw basis codes in issued snapshots. Historic aqueous snapshots
# omit the basis, as do subcontracted results. Soil requires an explicit basis.
_AQUEOUS_BASIS = {"": ResultBasis.NA, "na": ResultBasis.NA}
_MATRIX_BASES = {
    "Water": _AQUEOUS_BASIS,
    "Wastewater": _AQUEOUS_BASIS,
    "Soil": {"dry": ResultBasis.DRY, "wet": ResultBasis.WET},
}
_REPORTED_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def comparison_context(result, matrix):
    """Identify the series for a result using only its frozen report fields."""
    if not isinstance(result, dict):
        return None
    analyte, unit = result.get("analyte"), result.get("unit")
    if not all(isinstance(value, str) and value.strip() for value in (analyte, unit, matrix)):
        return None
    raw_basis = result.get("result_basis", "")
    basis = ResultBasis.UNKNOWN
    if isinstance(raw_basis, str) and (matrix != "Soil" or "source" not in result):
        basis = _MATRIX_BASES.get(matrix, {}).get(raw_basis, ResultBasis.UNKNOWN)
    return {
        "analyte": analyte,
        "unit": unit,
        "matrix": matrix,
        "basis": basis,
        "basis_label": basis.label,
    }


def _compare_result(result, basis, upper_limit, report_qualified):
    if basis == ResultBasis.UNKNOWN:
        return "Not compared · Reporting basis unavailable"
    if result.get("kind") == ResultKind.NON_DETECT:
        return "Not compared · Non-detect"
    if result.get("kind") != ResultKind.DETECTED or not isinstance(result.get("qualifiers"), list):
        return "Not compared · Result information incomplete"
    if report_qualified or result["qualifiers"]:
        return "Not compared · Qualified result"
    if "source" in result:
        if result["source"] != "external" or not isinstance(result.get("external_qualifiers"), str):
            return "Not compared · Result information incomplete"
        if result["external_qualifiers"].strip():
            return "Not compared · Qualified result"

    text = result.get("reported")
    if (
        not isinstance(text, str)
        or len(text) > MAX_TEXT_LENGTH
        or not _REPORTED_NUMBER.fullmatch(text)
    ):
        return "Not compared · No numeric result"
    try:
        value = Decimal(text)
    except InvalidOperation:
        return "Not compared · No numeric result"
    if not value.is_finite():
        return "Not compared · No numeric result"
    if value > upper_limit:
        return "Above limit"
    if value < upper_limit:
        return "Below limit"
    return "At limit"


def location_history(
    *,
    client_id,
    project_id,
    location,
    analyte="",
    page=1,
    descending=True,
    per_page=25,
    comparison=None,
    upper_limit=None,
):
    """Return one page of exact-location matches, optionally comparing one issued series."""
    if upper_limit is not None and (
        comparison is None or not isinstance(upper_limit, Decimal) or not upper_limit.is_finite()
    ):
        raise ValueError("Provide a comparison series and a finite decimal upper limit.")
    if per_page not in PAGE_SIZES:
        raise ValueError("Invalid page size. Choose 25, 50 or 100.")
    if not location or not location.strip():
        return Paginator([], per_page).get_page(page)

    # Like Paginator.get_page, a malformed page goes to the first, and an out-of-bounds
    # number goes to the last. Bound the supplied integer before binding it as a bigint.
    try:
        requested_page = max(0, min(int(page), sys.maxsize))
    except OverflowError, TypeError, ValueError:
        requested_page = 1

    params = {
        "client_id": client_id,
        "project_id": project_id,
        "location": location,
        "analyte": analyte,
        "page": requested_page,
        "per_page": per_page,
    }
    if comparison is not None:
        matrix, basis = comparison["matrix"], comparison["basis"]
        known_basis = basis != ResultBasis.UNKNOWN
        params.update(
            comparison_analyte=comparison["analyte"],
            comparison_unit=comparison["unit"],
            comparison_matrix=matrix,
            known_basis=known_basis,
            basis_codes=[
                code
                for code, group in _MATRIX_BASES.get(matrix, {}).items()
                if not known_basis or group == basis
            ],
        )
    with connection.cursor() as cursor:
        cursor.execute(
            _HISTORY_SQL.format(
                direction="DESC" if descending else "ASC",
                comparison_filter=_COMPARISON_FILTER if comparison is not None else "",
                qualified_qc=_QUALIFIED_QC if upper_limit is not None else "",
                qualification_column=_QUALIFICATION_COLUMN if upper_limit is not None else "",
            ),
            params,
        )
        columns = [column.name for column in cursor.description[2:]]
        raw_rows = cursor.fetchall()

    total, number = raw_rows[0][:2]  # the LEFT JOIN preserves this record on an empty page
    rows = []
    for _, _, *values in raw_rows:
        row = dict(zip(columns, values, strict=True))
        if row["report_id"] is None:
            continue
        collected_at = row["collected_at"]
        if collected_at:
            collected_at = datetime.fromisoformat(collected_at)
            if collected_at.tzinfo is None:
                collected_at = collected_at.replace(tzinfo=UTC)
        row["collected_at"] = collected_at
        result = json.loads(row.pop("result_data"))
        # Keep incomplete snapshot fields visible as missing. The comparison always
        # uses the original result, including whether qualifier evidence was present.
        display = dict.fromkeys(("analyte", "unit", "reported", "reporting_limit", "method_code"))
        display.update(result)
        qualifiers = display.get("qualifiers", [])
        if not isinstance(qualifiers, list) or not all(
            isinstance(code, str) for code in qualifiers
        ):
            display["qualifiers"] = ["Unavailable"]
        if not isinstance(display.get("external", {}), dict):
            display["external"] = {}
        row.update(issued_result_values(display, row["matrix"]))
        row["qualifiers"] = row["qualifiers"] or ""
        row["source"] = result.get("source", "")
        row["comparison_available"] = comparison_context(result, row["matrix"]) is not None
        if upper_limit is not None:
            row["comparison"] = _compare_result(
                result, comparison["basis"], upper_limit, row.pop("report_qualified")
            )
        rows.append(row)

    # PostgreSQL already sliced the rows; the paginator supplies navigation for the full total.
    paginator = Paginator(rows, per_page)
    paginator.count = total
    return Page(rows, number, paginator)
