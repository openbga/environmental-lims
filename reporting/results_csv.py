"""Spreadsheet download of the sample results in a single issued snapshot."""

import csv
import io
from decimal import InvalidOperation

from django.core.exceptions import ValidationError

from analytical.parsing import parse_decimal
from core.csv_cells import spreadsheet_text

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
NUMERIC_COLUMNS = {"result", "reporting_limit", "percent_solids"}


def _spreadsheet_cell(value, *, numeric):
    if numeric and isinstance(value, str) and value.startswith(("+", "-")):
        try:
            parse_decimal(value)
        except ValidationError, InvalidOperation:
            pass
        else:
            return value
    return spreadsheet_text(value)


def export_results_csv(snapshot):
    """Return UTF-8 spreadsheet text with the issued result spelling and timestamp offsets."""
    content = snapshot["content"]
    synthetic = content.get("synthetic_notice")
    common = {
        "report_number": content["report"]["number"],
        "report_version": snapshot["release"]["version"],
        "synthetic_data": str(synthetic).lower() if synthetic is not None else "",
    }
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
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
            external = result.get("external", {})
            qualifiers = (
                result.get("external_qualifiers")
                if result.get("source") == "external"
                else ", ".join(result.get("qualifiers", []))
            )
            cells = {
                **sample_cells,
                "analyte": result["analyte"],
                "result": result["reported"],
                "unit": result["unit"],
                "qualifiers": qualifiers,
                "reporting_limit": result["reporting_limit"],
                "result_basis": basis_label(result, sample["matrix"]),
                "percent_solids": result.get("percent_solids"),
                "solids_reference": result.get("solids_reference"),
                "method_code": result["method_code"],
                "method_version": result.get("method_version"),
                "analysis_started_at": result.get("analysis_started_at"),
                "subcontract_lab": external.get("laboratory"),
                "subcontract_report_reference": external.get("report_reference"),
                "subcontract_sample_id": external.get("external_sample_id"),
            }
            writer.writerow(
                {
                    name: _spreadsheet_cell(value, numeric=name in NUMERIC_COLUMNS)
                    for name, value in cells.items()
                }
            )
    return output.getvalue()
