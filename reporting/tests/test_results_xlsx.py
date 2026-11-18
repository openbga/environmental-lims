from io import BytesIO
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from django.core.exceptions import ValidationError

from analytical.workbook_import import read_workbook
from reporting.results_csv import COLUMNS
from reporting.results_xlsx import export_results_xlsx


@pytest.fixture
def snapshot():
    return {
        "release": {"version": 2},
        "content": {
            "report": {"number": "RPT-000012"},
            "samples": [
                {
                    "lab_id": "00123",
                    "client_sample_id": "=A1",
                    "matrix": "Soil",
                    "collection_location": "https://example.invalid/sample",
                    "results": [
                        {
                            "analyte": "Trace <µ> & solids",
                            "reported": "+1.20000000000000001e-3",
                            "unit": "µg/kg",
                            "reporting_limit": "0.0100",
                            "method_code": " 01 ",
                        },
                        {
                            "source": "external",
                            "analyte": "Synthetic nondetect",
                            "reported": "ND",
                            "unit": "µg/kg",
                            "reporting_limit": "0.020",
                            "method_code": "EXT",
                            "external_qualifiers": "J, as reported",
                            "external": {"report_reference": "00000000000000067"},
                        },
                    ],
                }
            ],
        },
    }


def test_standard_workbook_preserves_issued_text_and_legacy_fields(snapshot):
    content = export_results_xlsx(snapshot)
    table = read_workbook(content, worksheet="Results")
    headings, internal_row, external_row = table.rows
    assert tuple(headings) == COLUMNS
    assert table.numeric_cells == set()
    internal = dict(zip(headings, internal_row, strict=True))
    assert internal["report_version"] == "2"
    assert internal["lab_sample_id"] == "00123"
    assert internal["client_sample_id"] == "=A1"
    assert internal["collection_location"] == "https://example.invalid/sample"
    assert internal["analyte"] == "Trace <µ> & solids"
    assert internal["result"] == "+1.20000000000000001e-3"
    assert internal["reporting_limit"] == "0.0100"
    assert internal["method_code"] == " 01 "
    assert internal["result_basis"] == "Basis not recorded"
    assert internal["method_version"] == internal["synthetic_data"] == ""
    external = dict(zip(headings, external_row, strict=True))
    assert external["result"] == "ND"
    assert external["qualifiers"] == "J, as reported"
    assert external["subcontract_report_reference"] == "00000000000000067"

    with ZipFile(BytesIO(content)) as package:
        worksheet = ElementTree.fromstring(package.read("xl/worksheets/sheet1.xml"))
    assert worksheet.find(".//{*}f") is None
    assert worksheet.find(".//{*}hyperlinks") is None
    assert worksheet.find(".//{*}autoFilter").get("ref") == "A1:V3"
    assert worksheet.find(".//{*}pane").get("topLeftCell") == "A2"


def test_custom_workbook_keeps_selected_headings_order_and_literal_text(snapshot):
    columns = [("unit", "Units"), ("analyte", " Analyte 🧪 "), ("result", "Result")]
    snapshot["content"]["samples"][0]["results"][0]["analyte"] = (
        "Sample <r>Fe</r> _x000A_ & μ\rfield"
    )
    content = export_results_xlsx(snapshot, columns=columns)
    assert read_workbook(content, worksheet="Results").rows == [
        ["Units", " Analyte 🧪 ", "Result"],
        ["µg/kg", "Sample <r>Fe</r> _x000A_ & μ\rfield", "+1.20000000000000001e-3"],
        ["µg/kg", "Synthetic nondetect", "ND"],
    ]


@pytest.mark.parametrize(
    "unrepresentable",
    [
        "a" * 32_768,
        "🧪" * 16_384,
        "\n" * 254,
        "<r>Sample & μ</r>",
        "_x000A_x00B5_",
        "Sample _x0041\r",
    ],
)
def test_workbook_rejects_unrepresentable_values_without_truncation(snapshot, unrepresentable):
    snapshot["content"]["samples"][0]["results"][0]["analyte"] = unrepresentable
    with pytest.raises(ValidationError, match=r"Row 2, column 1 .*Download these results as CSV"):
        export_results_xlsx(snapshot, columns=[("analyte", "Analyte")])
    # Only selected fields need to fit into a cell.
    rows = read_workbook(
        export_results_xlsx(snapshot, columns=[("result", "Result")]), worksheet="Results"
    ).rows
    assert rows[1] == ["+1.20000000000000001e-3"]
