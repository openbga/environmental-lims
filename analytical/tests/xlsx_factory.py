"""Small synthetic SpreadsheetML tables for import and browser regression journeys."""

from io import BytesIO
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZipFile


def workbook(sheets):
    """Strings are inline text; (type, text) explicitly supplies a stored cell type/value."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = "application/vnd.openxmlformats-officedocument.spreadsheetml."
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{ct}sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                f'ContentType="{ct}worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="book" '
            f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
            + "".join(
                f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="s{i}"/>'
                for i, name in enumerate(sheets, 1)
            )
            + "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{pkg}">'
            + "".join(
                f'<Relationship Id="s{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Relationships>",
        )
        for index, rows in enumerate(sheets.values(), 1):
            records = []
            for row_number, values in enumerate(rows, 1):
                cells = []
                for column, value in enumerate(values, 1):
                    if value is None:
                        continue
                    number, letters = column, ""
                    while number:
                        number, digit = divmod(number - 1, 26)
                        letters = chr(65 + digit) + letters
                    coordinate = f"{letters}{row_number}"
                    if isinstance(value, tuple):
                        kind, text = value
                        payload = (
                            f"<f>{escape(text)}</f><v>1</v>"
                            if kind == "formula"
                            else f"<v>{escape(text)}</v>"
                        )
                        cell_type = "n" if kind == "formula" else kind
                        cells.append(f'<c r="{coordinate}" t="{cell_type}">{payload}</c>')
                    else:
                        cells.append(
                            f'<c r="{coordinate}" t="inlineStr"><is><t xml:space="preserve">'
                            f"{escape(value)}</t></is></c>"
                        )
                records.append(f'<row r="{row_number}">{"".join(cells)}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<worksheet xmlns="{ns}"><sheetData>{"".join(records)}</sheetData></worksheet>',
            )
    return output.getvalue()
