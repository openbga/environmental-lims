"""Client location results from the current issued versions of the project's reports."""

import json
import sys
from datetime import UTC, datetime

from django.core.paginator import Page, Paginator
from django.db import connection

from core.grids import PAGE_SIZES

from .results_csv import issued_result_values

# Keep the count and page in one statement so they see the same set of issued versions.
# Expand the frozen arrays in PostgreSQL and return only the selected page's result objects.
_HISTORY_SQL = """
    WITH matches AS (
        SELECT report.id AS report_id, version.number AS report_version,
               content.report ->> 'number' AS report_number,
               content.synthetic_notice IS NOT DISTINCT FROM 'true'::jsonb AS synthetic_notice,
               sample.data ->> 'lab_id' AS lab_sample_id,
               sample.data ->> 'client_sample_id' AS client_sample_id,
               sample.data ->> 'matrix' AS matrix,
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
    ), totals AS (
        SELECT count(*) AS total,
               greatest(1, (count(*) + %(per_page)s - 1) / %(per_page)s) AS last_page
        FROM matches
    ), page AS (
        SELECT total, CASE WHEN %(page)s < 1 THEN last_page
                           ELSE least(last_page, %(page)s) END AS number
        FROM totals
    )
    SELECT page.total, page.number, rows.report_id, rows.report_number, rows.report_version,
           rows.synthetic_notice, rows.lab_sample_id, rows.client_sample_id, rows.matrix,
           rows.collected_at,
           rows.result_data::text AS result_data
    FROM page
    LEFT JOIN LATERAL (
        SELECT * FROM matches
        ORDER BY collected_at::timestamptz {direction} NULLS LAST,
                 report_id, sample_position, result_position
        LIMIT %(per_page)s OFFSET (page.number - 1) * %(per_page)s
    ) AS rows ON true
    ORDER BY rows.collected_at::timestamptz {direction} NULLS LAST,
             rows.report_id, rows.sample_position, rows.result_position
"""


def location_history(
    *, client_id, project_id, location, analyte="", page=1, descending=True, per_page=25
):
    """Return one page of exact-location matches, retaining each issued report appearance."""
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

    with connection.cursor() as cursor:
        cursor.execute(
            _HISTORY_SQL.format(direction="DESC" if descending else "ASC"),
            {
                "client_id": client_id,
                "project_id": project_id,
                "location": location,
                "analyte": analyte,
                "page": requested_page,
                "per_page": per_page,
            },
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
        row.update(issued_result_values(result, row["matrix"]))
        row["qualifiers"] = row["qualifiers"] or ""
        row["source"] = result.get("source", "")
        rows.append(row)

    # PostgreSQL already sliced the rows; the paginator supplies navigation for the full total.
    paginator = Paginator(rows, per_page)
    paginator.count = total
    return Page(rows, number, paginator)
