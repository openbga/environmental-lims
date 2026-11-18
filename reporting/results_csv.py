"""Spreadsheet download of the sample results in a single issued snapshot."""

import csv
import io
import unicodedata
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
MAX_TEMPLATE_BYTES = 16 * 1024


def parse_csv_template(upload):
    """Read two CSV rows of headings and field keys into ordered (key, heading) pairs.

    Keys and duplicate headings are matched exactly. Headings must be single-line
    text without control characters or spreadsheet formula prefixes.
    """
    data = upload.read(MAX_TEMPLATE_BYTES + 1)
    if len(data) > MAX_TEMPLATE_BYTES:
        raise ValidationError(f"Upload a CSV file of at most {MAX_TEMPLATE_BYTES // 1024} KiB.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("Save the template as UTF-8 CSV and upload it again.") from exc
    try:
        rows = list(csv.reader(io.StringIO(content, newline=""), strict=True))
    except csv.Error as exc:
        raise ValidationError("Use comma-separated CSV with properly quoted headings.") from exc
    if len(rows) != 2 or not all(rows):
        raise ValidationError("Use exactly two CSV rows: output headings, then result field keys.")
    headings, keys = rows
    if len(headings) != len(keys):
        raise ValidationError("Give every output heading a result field key in the same column.")

    columns = []
    seen_headings = set()
    seen_keys = set()
    for index, (key, heading) in enumerate(zip(keys, headings, strict=True), start=1):
        if not heading.strip():
            raise ValidationError(f"Enter an output heading in column {index} of the first row.")
        if spreadsheet_text(heading) != heading or any(
            unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in heading
        ):
            raise ValidationError(
                f"Use a plain text heading in column {index}, without formula prefixes, "
                "line breaks or control characters."
            )
        if heading in seen_headings:
            raise ValidationError(f"Use a different output heading in column {index}.")
        if key not in COLUMNS:
            raise ValidationError(
                f"Enter a result field key from the example template in column {index} "
                "of the second row."
            )
        if key in seen_keys:
            raise ValidationError(f"Use a different result field key in column {index}.")
        seen_headings.add(heading)
        seen_keys.add(key)
        columns.append((key, heading))
    return columns


def _spreadsheet_writer():
    output = io.StringIO(newline="")
    output.write("\ufeff")
    return output, csv.writer(output, lineterminator="\r\n")


def example_csv_template():
    """Return a starter template containing every supported result field key."""
    output, writer = _spreadsheet_writer()
    writer.writerow(key.replace("_", " ").capitalize().replace(" id", " ID") for key in COLUMNS)
    writer.writerow(COLUMNS)
    return output.getvalue()


def _spreadsheet_cell(value, *, numeric):
    if numeric and isinstance(value, str) and value.startswith(("+", "-")):
        try:
            parse_decimal(value)
        except ValidationError, InvalidOperation:
            pass
        else:
            return value
    return spreadsheet_text(value)


def export_results_csv(snapshot, *, columns=None):
    """Return issued result text in standard columns or ordered (key, heading) pairs."""
    content = snapshot["content"]
    synthetic = content.get("synthetic_notice")
    common = {
        "report_number": content["report"]["number"],
        "report_version": snapshot["release"]["version"],
        "synthetic_data": str(synthetic).lower() if synthetic is not None else "",
    }
    if columns is None:
        columns = ((key, key) for key in COLUMNS)
    columns = tuple(columns)
    output, writer = _spreadsheet_writer()
    writer.writerow(spreadsheet_text(heading) for _, heading in columns)
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
                _spreadsheet_cell(cells[key], numeric=key in NUMERIC_COLUMNS) for key, _ in columns
            )
    return output.getvalue()
