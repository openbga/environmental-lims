"""Spreadsheet download of the sample results in a single issued snapshot."""

from decimal import InvalidOperation

from django.core.exceptions import ValidationError

from analytical.parsing import parse_decimal
from core.csv_cells import spreadsheet_text
from core.csv_templates import MAX_TEMPLATE_BYTES as MAX_TEMPLATE_BYTES
from core.csv_templates import example_csv_template as _example_csv_template
from core.csv_templates import parse_csv_template as _parse_csv_template
from core.csv_templates import spreadsheet_writer

from .content import basis_label

COLUMNS = (
    "report_number",
    "report_version",
    "synthetic_data",
    "lab_sample_id",
    "client_sample_id",
    "matrix",
    "collected_at",
    "collection_location",
    "analyte",
    "result",
    "unit",
    "qualifiers",
    "reporting_limit",
    "result_basis",
    "percent_solids",
    "solids_reference",
    "method_code",
    "method_version",
    "analysis_started_at",
    "subcontract_lab",
    "subcontract_report_reference",
    "subcontract_sample_id",
)
STANDARD_COLUMNS = tuple((key, key) for key in COLUMNS)
NUMERIC_COLUMNS = {"result", "reporting_limit", "percent_solids"}


def parse_csv_template(upload):
    """Read two CSV rows of headings and field keys into ordered (key, heading) pairs.

    Keys and duplicate headings are matched exactly. Headings must be single-line
    text without control characters or spreadsheet formula prefixes.
    """
    return _parse_csv_template(upload, allowed_keys=COLUMNS, field_label="result")


def example_csv_template():
    """Return a starter template containing every supported result field key."""
    return _example_csv_template(COLUMNS)


def _spreadsheet_cell(value, *, numeric):
    if numeric and isinstance(value, str) and value.startswith(("+", "-")):
        try:
            parse_decimal(value)
        except ValidationError, InvalidOperation:
            pass
        else:
            return value
    return spreadsheet_text(value)


def issued_result_rows(snapshot):
    """Yield unescaped fields for each sample result in the issued version."""
    content = snapshot["content"]
    synthetic = content.get("synthetic_notice")
    common = {
        "report_number": content["report"]["number"],
        "report_version": snapshot["release"]["version"],
        "synthetic_data": str(synthetic).lower() if synthetic is not None else "",
    }
    for sample in content["samples"]:
        sample_cells = {
            **common,
            "lab_sample_id": sample["lab_id"],
            "client_sample_id": sample["client_sample_id"],
            "matrix": sample["matrix"],
            "collected_at": sample.get("collected_at"),
            "collection_location": sample.get("collection_location"),
        }
        for result in sample["results"]:
            yield {**sample_cells, **issued_result_values(result, sample["matrix"])}


def issued_result_values(result, matrix):
    """Read the common display fields from one frozen result."""
    external = result.get("external", {})
    qualifiers = (
        result.get("external_qualifiers")
        if result.get("source") == "external"
        else ", ".join(result.get("qualifiers", []))
    )
    return {
        "analyte": result["analyte"],
        "result": result["reported"],
        "unit": result["unit"],
        "qualifiers": qualifiers,
        "reporting_limit": result["reporting_limit"],
        "result_basis": basis_label(result, matrix),
        "percent_solids": result.get("percent_solids"),
        "solids_reference": result.get("solids_reference"),
        "method_code": result["method_code"],
        "method_version": result.get("method_version"),
        "analysis_started_at": result.get("analysis_started_at"),
        "subcontract_lab": external.get("laboratory"),
        "subcontract_report_reference": external.get("report_reference"),
        "subcontract_sample_id": external.get("external_sample_id"),
    }


def export_results_csv(snapshot, *, columns=None):
    """Return issued result text in standard columns or ordered (key, heading) pairs."""
    columns = STANDARD_COLUMNS if columns is None else tuple(columns)
    output, writer = spreadsheet_writer()
    writer.writerow(spreadsheet_text(heading) for _, heading in columns)
    for cells in issued_result_rows(snapshot):
        writer.writerow(
            _spreadsheet_cell(cells[key], numeric=key in NUMERIC_COLUMNS) for key, _ in columns
        )
    return output.getvalue()
