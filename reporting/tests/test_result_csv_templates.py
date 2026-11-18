import csv
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from reporting.results_csv import (
    COLUMNS,
    MAX_TEMPLATE_BYTES,
    example_csv_template,
    export_results_csv,
    parse_csv_template,
)


def parse(data):
    return parse_csv_template(SimpleUploadedFile("template.csv", data))


def test_parse_template_preserves_safe_headings_and_column_order():
    assert parse(
        '\ufeff" Concentración, µg/L ","Muestra ""A""",Fecha\r\n'
        "result,client_sample_id,collected_at\r\n".encode()
    ) == [
        ("result", " Concentración, µg/L "),
        ("client_sample_id", 'Muestra "A"'),
        ("collected_at", "Fecha"),
    ]


@pytest.mark.parametrize(
    ("data", "expected_error"),
    [
        (b"\xff\nresult", "UTF-8"),
        (b'"Unclosed\nresult', "properly quoted"),
        (b"Reading\n", "exactly two CSV rows"),
        (b"\nresult\n", "exactly two CSV rows"),
        (b"Reading\nresult\n\n", "exactly two CSV rows"),
        (b"Reading\nresult\nExtra", "exactly two CSV rows"),
        (b"Reading,Unit\nresult", "same column"),
        (b"Reading, \nresult,unit", "output heading in column 2"),
        (b"Reading,Unit\nresult,", "field key from the example template in column 2"),
        (b"Reading\nResult", "field key from the example template"),
        (b"Reading\nresult ", "field key from the example template"),
        (b"Reading\nnot_a_field", "field key from the example template"),
        (b"Reading,Reading\nresult,unit", "different output heading in column 2"),
        (b"Reading,Value\nresult,result", "different result field key in column 2"),
    ],
)
def test_invalid_templates_describe_the_required_change(data, expected_error):
    with pytest.raises(ValidationError, match=expected_error):
        parse(data)


@pytest.mark.parametrize(
    "heading",
    [
        "=name",
        "+name",
        "-name",
        "@name",
        "  ＝name",
        "\u200b@name",
        "\tname",
        "Re\x00sult",
        "Re\nsult",
        "Re\u2028sult",
    ],
)
def test_templates_reject_unsafe_headings(heading):
    with pytest.raises(ValidationError, match="plain text heading in column 1"):
        parse(f'"{heading}"\nresult'.encode())


def test_template_size_limit_counts_upload_bytes():
    suffix = b"\nresult"
    heading = "é" * ((MAX_TEMPLATE_BYTES - len(suffix)) // 2) + "a"
    data = heading.encode() + suffix
    assert len(data) == MAX_TEMPLATE_BYTES
    assert parse(data) == [("result", heading)]
    with pytest.raises(ValidationError, match="at most 16 KiB"):
        parse(b"a" + data)


def test_example_is_a_valid_template_with_all_standard_keys():
    example = example_csv_template()
    assert example.startswith("\ufeff")
    columns = parse(example.encode())
    assert tuple(key for key, _ in columns) == COLUMNS
    assert dict(columns)["client_sample_id"] == "Client sample ID"


def test_mapped_export_keeps_frozen_values_and_spreadsheet_protection():
    snapshot = {
        "release": {"version": 2},
        "content": {
            "report": {"number": "RPT-000001"},
            "samples": [
                {
                    "lab_id": "EX-001",
                    "client_sample_id": " ＝sample",
                    "matrix": "Water",
                    "collected_at": "2026-11-08T08:00:00-05:00",
                    "results": [
                        {
                            "analyte": "Synthetic analyte",
                            "reported": "-4.800e-2",
                            "unit": "µg/L",
                            "reporting_limit": "0.0100",
                            "method_code": "EXAMPLE",
                        },
                        {
                            "analyte": "Synthetic external analyte",
                            "reported": "ND",
                            "unit": "µg/L",
                            "source": "external",
                            "external_qualifiers": "J: estimated, as reported",
                            "reporting_limit": "0.0010",
                            "method_code": "EXAMPLE",
                        },
                    ],
                }
            ],
        },
    }
    columns = parse(
        b'"Reading, as issued",Limit,Sample,Version,Collected,Qualifiers,Lab\n'
        b"result,reporting_limit,client_sample_id,report_version,collected_at,qualifiers,"
        b"subcontract_lab"
    )

    exported = export_results_csv(snapshot, columns=columns)

    assert exported.startswith('\ufeff"Reading, as issued",Limit,Sample,Version,Collected,')
    assert list(csv.reader(StringIO(exported.removeprefix("\ufeff"), newline=""))) == [
        ["Reading, as issued", "Limit", "Sample", "Version", "Collected", "Qualifiers", "Lab"],
        ["-4.800e-2", "0.0100", "' ＝sample", "2", "2026-11-08T08:00:00-05:00", "", ""],
        [
            "ND",
            "0.0010",
            "' ＝sample",
            "2",
            "2026-11-08T08:00:00-05:00",
            "J: estimated, as reported",
            "",
        ],
    ]
