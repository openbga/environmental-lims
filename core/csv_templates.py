"""Read small, two-row templates for configurable CSV downloads."""

import csv
import io
import unicodedata

from django.core.exceptions import ValidationError

from .csv_cells import spreadsheet_text

MAX_TEMPLATE_BYTES = 16 * 1024


def parse_csv_template(upload, *, allowed_keys, field_label):
    """Read CSV headings and field keys into ordered (key, heading) pairs."""
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
        raise ValidationError(
            f"Use exactly two CSV rows: output headings, then {field_label} field keys."
        )
    headings, keys = rows
    if len(headings) != len(keys):
        raise ValidationError(
            f"Give every output heading a {field_label} field key in the same column."
        )
    return validate_csv_columns(
        zip(keys, headings, strict=True), allowed_keys=allowed_keys, field_label=field_label
    )


def validate_csv_columns(columns, *, allowed_keys, field_label):
    """Validate a projection, preserving exact key and heading text."""
    validated = []
    seen_headings = set()
    seen_keys = set()
    for index, pair in enumerate(columns, start=1):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValidationError(f"Give column {index} a field key and output heading.")
        key, heading = pair
        if not isinstance(heading, str) or not heading.strip():
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
        if key not in allowed_keys:
            raise ValidationError(
                f"Enter a {field_label} field key from the example template in column {index} "
                "of the second row."
            )
        if key in seen_keys:
            raise ValidationError(f"Use a different {field_label} field key in column {index}.")
        seen_headings.add(heading)
        seen_keys.add(key)
        validated.append((key, heading))
    if not validated:
        raise ValidationError("Select at least one field to export.")
    return validated


def spreadsheet_writer():
    output = io.StringIO(newline="")
    output.write("\ufeff")
    return output, csv.writer(output, lineterminator="\r\n")


def example_csv_template(keys):
    """Return a UTF-8 BOM-prefixed template containing every supported key."""
    output, writer = spreadsheet_writer()
    writer.writerow(key.replace("_", " ").capitalize().replace(" id", " ID") for key in keys)
    writer.writerow(keys)
    return output.getvalue()
