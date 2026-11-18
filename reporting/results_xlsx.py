"""Write one issued report version as an Excel worksheet of literal text cells."""

import re
from io import BytesIO

from django.core.exceptions import ValidationError
from xlsxwriter import Workbook

from .results_csv import STANDARD_COLUMNS, issued_result_rows

# XlsxWriter escapes literal OOXML tokens one at a time. Two tokens can
# share an underscore, including the escape it generates for a control code.
AMBIGUOUS_ESCAPE = re.compile(
    r"_x[0-9A-Fa-f]{4}(?:_x[0-9A-Fa-f]{4}_|[\x00-\x08\x0b-\x1f\ufffe\uffff])"
)


def _write_text(sheet, row, col, value, cell_format):
    text = "" if value is None else str(value)
    # Excel permits at most 253 line feeds in a cell. XlsxWriter checks the
    # length in UTF-16 units and reports if a cell would be truncated.
    if text.count("\n") > 253:
        raise ValidationError(
            f"Row {row + 1}, column {col + 1} has too many line breaks for Excel. "
            "Download these results as CSV."
        )
    # The library interprets this wrapper as rich text even in write_string().
    if (text.startswith("<r>") and text.endswith("</r>")) or AMBIGUOUS_ESCAPE.search(text):
        raise ValidationError(
            f"Row {row + 1}, column {col + 1} contains text the Excel export cannot "
            "preserve exactly. Download these results as CSV."
        )
    if sheet.write_string(row, col, text, cell_format):
        raise ValidationError(
            f"Row {row + 1}, column {col + 1} exceeds an Excel worksheet limit. "
            "Download these results as CSV."
        )


def export_results_xlsx(snapshot, *, columns=None):
    """Return a workbook with the selected fields, keeping issued values as text."""
    columns = STANDARD_COLUMNS if columns is None else tuple(columns)
    output = BytesIO()
    with Workbook(output, {"in_memory": True}) as book:
        sheet = book.add_worksheet("Results")
        header = book.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#2F6E3E"})
        body = book.add_format({"num_format": "@"})
        for col, (_, heading) in enumerate(columns):
            _write_text(sheet, 0, col, heading, header)
        last_row = 0
        for last_row, cells in enumerate(issued_result_rows(snapshot), start=1):
            for col, (key, _) in enumerate(columns):
                _write_text(sheet, last_row, col, cells[key], body)
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, last_row, len(columns) - 1)
        sheet.autofit(300)
    return output.getvalue()
