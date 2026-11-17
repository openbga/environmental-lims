"""Synthetic OOXML fixtures exercise exact values and the upload trust boundary."""

import io
import struct
import warnings
import zipfile

import pytest
from django.core.exceptions import ValidationError

from analytical import workbook_import as reader

S = reader.SHEET_NS
R = reader.DOC_REL_NS
P = reader.REL_NS
C = reader.CONTENT_NS


def sheet(rows="", extra=""):
    return f'<worksheet xmlns="{S}"><sheetData>{rows}</sheetData>{extra}</worksheet>'


def text(coordinate, value):
    return f'<c r="{coordinate}" t="inlineStr"><is><t>{value}</t></is></c>'


def number(coordinate, value, attrs=""):
    return f'<c r="{coordinate}" {attrs}><v>{value}</v></c>'


def package_parts(rows="", *, extra="", shared=None):
    entries = {
        "[Content_Types].xml": (
            f'<Types xmlns="{C}">'
            f'<Default Extension="rels" ContentType="{reader.REL_TYPE}"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/custom/book.xml" ContentType="{reader.WORKBOOK_TYPE}"/>'
            f'<Override PartName="/custom/tables/first.xml" ContentType="{reader.WORKSHEET_TYPE}"/>'
            f'<Override PartName="/custom/tables/data.xml" ContentType="{reader.WORKSHEET_TYPE}"/>'
            '<Override PartName="/custom/tables/hidden.xml" '
            f'ContentType="{reader.WORKSHEET_TYPE}"/>'
            + (
                f'<Override PartName="/custom/strings.xml" ContentType="{reader.STRINGS_TYPE}"/>'
                if shared is not None
                else ""
            )
            + "</Types>"
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{P}"><Relationship Id="book" '
            f'Type="{R}/officeDocument" Target="custom/book.xml"/></Relationships>'
        ),
        "custom/book.xml": (
            f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
            '<sheet name="Cover" sheetId="1" r:id="first"/>'
            '<sheet name="Results" sheetId="2" r:id="data"/>'
            '<sheet name="Hidden" sheetId="3" state="hidden" r:id="hidden"/>'
            "</sheets></workbook>"
        ),
        "custom/_rels/book.xml.rels": (
            f'<Relationships xmlns="{P}">'
            f'<Relationship Id="first" Type="{R}/worksheet" Target="tables/first.xml"/>'
            f'<Relationship Id="data" Type="{R}/worksheet" Target="tables/data.xml"/>'
            f'<Relationship Id="hidden" Type="{R}/worksheet" Target="tables/hidden.xml"/>'
            + (
                f'<Relationship Id="strings" Type="{R}/sharedStrings" Target="strings.xml"/>'
                if shared is not None
                else ""
            )
            + "</Relationships>"
        ),
        "custom/tables/first.xml": sheet('<row r="1">' + text("A1", "Cover only") + "</row>"),
        "custom/tables/data.xml": sheet(rows, extra),
        "custom/tables/hidden.xml": sheet(),
    }
    if shared is not None:
        entries["custom/strings.xml"] = f'<sst xmlns="{S}">{shared}</sst>'
    return entries


def zipped(entries, *, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def read(rows="", **kwargs):
    return reader.read_workbook(zipped(package_parts(rows, **kwargs)), worksheet="Results")


def test_exact_numeric_xml_and_numeric_markers():
    table = read(
        '<row r="1">'
        + number("A1", "0.123456789012345678")
        + number("B1", "1.2300e-4", 't="n"')
        + text("C1", "000012")
        + "</row>"
    )
    assert table.worksheet == "Results"
    assert table.rows == [["0.123456789012345678", "1.2300e-4", "000012"]]
    assert table.numeric_cells == {(1, 0), (1, 1)}


def test_shared_inline_and_rich_text_exclude_phonetics_and_preserve_spaces():
    shared = (
        '<si><t xml:space="preserve"> sample &amp; analyte </t></si>'
        "<si><r><rPr><b/></rPr><t>Cop</t></r><r><t>per</t></r>"
        '<rPh sb="0" eb="3"><t>unwanted</t></rPh></si>'
    )
    rows = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="inlineStr"><is><r><t>mg</t></r><r><t>/L</t></r>'
        '<rPh sb="0" eb="2"><t>ignore</t></rPh></is></c></row>'
    )
    table = read(rows, shared=shared)
    assert table.rows == [[" sample & analyte ", "Copper", "mg/L"]]
    assert table.numeric_cells == set()


def test_selects_nonfirst_sheet_and_retains_sparse_physical_coordinates():
    rows = (
        '<row r="3" hidden="1">' + text("B3", "Sample") + "</row>"
        '<row r="6">' + number("D6", "0.0100") + '<c r="F6" s="1"/></row>'
    )
    table = read(rows, extra='<cols><col min="2" max="2" hidden="1"/></cols>')
    assert table.rows == [
        ["", "", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["", "Sample", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["", "", "", "0.0100", "", ""],
    ]
    assert table.numeric_cells == {(6, 3)}


@pytest.mark.parametrize("name", ["", "results", "Missing", "Hidden"])
def test_requires_exact_visible_sheet_selection(name):
    with pytest.raises(ValidationError, match="Available visible worksheets: 'Cover', 'Results'"):
        reader.read_workbook(zipped(package_parts()), worksheet=name)


@pytest.mark.parametrize(
    "cell",
    [
        '<c r="A1" t="b"><v>1</v></c>',
        '<c r="A1" t="e"><v>#DIV/0!</v></c>',
        '<c r="A1" t="d"><v>2026-11-15T12:30:00</v></c>',
        '<c r="A1"><f>1+1</f><v>2</v></c>',
        '<c r="A1"><f t="shared" si="0"/><v>2</v></c>',
        '<c r="A1" t="str"><v>cached formula</v></c>',
        '<c r="A1" t="n"><v>NaN</v></c>',
        '<c r="A1" t="n"><v>INF</v></c>',
        '<c r="A1" t="n"><v>1,23</v></c>',
        '<c r="A1" t="n"><v/></c>',
        '<c r="A1" t="inlineStr"><v>1</v></c>',
        '<c r="A1" t="s"><v>0</v></c>',
        '<c r="A1"><v>1</v><v>2</v></c>',
        '<c r="A1"><v>1</v><is><t>2</t></is></c>',
    ],
)
def test_refuses_unsafe_or_contradictory_cell_values(cell):
    with pytest.raises(ValidationError):
        read(f'<row r="1">{cell}</row>')


def test_text_iso_timestamp_remains_literal_text():
    value = "2026-11-15T12:30:00-05:00"
    assert read('<row r="1">' + text("A1", value) + "</row>").rows == [[value]]


def test_refuses_merge_even_if_no_mapped_cell_would_use_it():
    with pytest.raises(ValidationError, match="merged cells"):
        read(extra='<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>')


@pytest.mark.parametrize(
    "rows",
    [
        '<row r="1"/><row r="1"/>',
        '<row r="2"/><row r="1"/>',
        "<row/>",
        '<row r="0"/>',
        '<row r="10001"/>',
        '<row r="1"><c r="A2"/></row>',
        '<row r="1"><c r="A1"/><c r="A1"/></row>',
        '<row r="1"><c r="B1"/><c r="A1"/></row>',
        '<row r="1"><c/></row>',
        '<row r="1"><c r="a1"/></row>',
        '<row r="1"><c r="A01"/></row>',
        '<row r="1"><c r="IW1"/></row>',
    ],
)
def test_invalid_order_duplicate_and_out_of_range_coordinates(rows):
    with pytest.raises(ValidationError):
        read(rows)


@pytest.mark.parametrize("part", ["_rels/.rels", "custom/book.xml", "custom/tables/data.xml"])
def test_dtd_and_entities_are_rejected(part):
    entries = package_parts()
    entries[part] = '<!DOCTYPE malicious [<!ENTITY x "boom">]>' + entries[part]
    with pytest.raises(ValidationError, match="unsafe XML"):
        reader.read_workbook(zipped(entries), worksheet="Results")


@pytest.mark.parametrize(
    "target",
    [
        "https://example.invalid/file.xml",
        "../../../escape.xml",
        "file:///etc/passwd",
        "tables\\data.xml",
        "tables/%64ata.xml",
        "tables/data.xml#fragment",
    ],
)
def test_dangerous_targets_rejected(target):
    entries = package_parts()
    entries["custom/_rels/book.xml.rels"] = entries["custom/_rels/book.xml.rels"].replace(
        'Target="tables/data.xml"', f'Target="{target}"'
    )
    with pytest.raises(ValidationError):
        reader.read_workbook(zipped(entries), worksheet="Results")


def test_external_relationships_refused_even_on_unselected_sheet():
    entries = package_parts()
    entries["custom/tables/_rels/first.xml.rels"] = (
        f'<Relationships xmlns="{P}"><Relationship Id="link" Type="{R}/hyperlink" '
        'Target="https://example.invalid/" TargetMode="External"/></Relationships>'
    )
    with pytest.raises(ValidationError, match="external relationships"):
        reader.read_workbook(zipped(entries), worksheet="Results")


@pytest.mark.parametrize(
    "change",
    [
        "macro",
        "wrong_type",
        "wrong_namespace",
        "missing_part",
        "duplicate_relationship",
        "duplicate_sheet",
    ],
)
def test_malformed_or_unsupported_packages(change):
    entries = package_parts()
    if change == "macro":
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            reader.WORKBOOK_TYPE, "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
        )
    elif change == "wrong_type":
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
            reader.WORKSHEET_TYPE, "application/xml"
        )
    elif change == "wrong_namespace":
        entries["custom/tables/data.xml"] = entries["custom/tables/data.xml"].replace(S, "wrong")
    elif change == "missing_part":
        del entries["custom/tables/data.xml"]
    elif change == "duplicate_relationship":
        entries["custom/_rels/book.xml.rels"] = entries["custom/_rels/book.xml.rels"].replace(
            'Id="first"', 'Id="data"'
        )
    elif change == "duplicate_sheet":
        entries["custom/book.xml"] = entries["custom/book.xml"].replace(
            'name="Cover"', 'name="Results"'
        )
    with pytest.raises(ValidationError):
        reader.read_workbook(zipped(entries), worksheet="Results")


def test_duplicate_zip_member_names():
    payload = io.BytesIO(zipped(package_parts()))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(payload, "a") as archive:
            archive.writestr("custom/tables/data.xml", sheet())
    with pytest.raises(ValidationError, match="duplicate"):
        reader.read_workbook(payload.getvalue(), worksheet="Results")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../b", "a\\b"])
def test_dangerous_zip_member_names(name):
    entries = package_parts() | {name: "bad"}
    with pytest.raises(ValidationError, match="unsafe package"):
        reader.read_workbook(zipped(entries), worksheet="Results")


@pytest.mark.parametrize("payload", [b"", b"not a zip", b"PK\x03\x04garbage"])
def test_malformed_archive_is_validation_error(payload):
    with pytest.raises(ValidationError):
        reader.read_workbook(payload, worksheet="Results")


def test_encrypted_zip_flag_refused():
    payload = bytearray(zipped(package_parts()))
    position = payload.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", payload, position + 8)[0]
    struct.pack_into("<H", payload, position + 8, flags | 1)
    with pytest.raises(ValidationError, match="encrypted"):
        reader.read_workbook(bytes(payload), worksheet="Results")


def test_compressed_upload_limit():
    with pytest.raises(ValidationError, match="compressed file exceeds"):
        reader.read_workbook(
            b"PK\x03\x04" + b"x" * reader.MAX_COMPRESSED_BYTES, worksheet="Results"
        )


def test_expanded_archive_limit_uses_all_members():
    entries = package_parts() | {"extra.bin": b"x" * (reader.MAX_EXPANDED_BYTES + 1)}
    with pytest.raises(ValidationError, match="expanded workbook"):
        reader.read_workbook(zipped(entries), worksheet="Results")


def test_member_count_limit():
    entries = package_parts() | {f"extra/{n}.txt": "" for n in range(reader.MAX_MEMBERS)}
    with pytest.raises(ValidationError, match="ZIP members"):
        reader.read_workbook(zipped(entries), worksheet="Results")


def test_cell_and_shared_string_limits(monkeypatch):
    monkeypatch.setattr(reader, "MAX_CELLS", 1)
    with pytest.raises(ValidationError, match="exceeds 1 cells"):
        read('<row r="1"><c r="A1"/><c r="B1"/></row>')
    monkeypatch.setattr(reader, "MAX_SHARED_STRINGS", 1)
    with pytest.raises(ValidationError, match="more than 1 shared strings"):
        read(shared="<si><t>a</t></si><si><t>b</t></si>")


def test_text_length_limit():
    with pytest.raises(ValidationError, match="text exceeds"):
        read('<row r="1">' + text("A1", "x" * (reader.MAX_TEXT + 1)) + "</row>")


def test_xml_structure_depth_limit():
    with pytest.raises(ValidationError, match="XML structure"):
        read(extra="<extLst>" * 65 + "</extLst>" * 65)


@pytest.mark.parametrize(
    "rows",
    [
        '<row r="' + "1" * 5000 + '"/>',
        '<row r="1"><c r="A' + "1" * 5000 + '"/></row>',
        '<row r="1"><c r="A1" t="s"><v>' + "1" * 5000 + "</v></c></row>",
    ],
)
def test_pathological_integer_attributes_are_validation_errors(rows):
    with pytest.raises(ValidationError):
        read(rows)


def test_cells_outside_the_data_table_are_refused():
    with pytest.raises(ValidationError, match="outside the worksheet data"):
        read(extra=number("A1", "1"))


def test_xml_crc_damage_is_validation_error():
    payload = bytearray(zipped(package_parts(), compression=zipfile.ZIP_STORED))
    position = payload.index(b"<Types")
    payload[position + 1] = ord("X")
    with pytest.raises(ValidationError, match="damaged ZIP"):
        reader.read_workbook(bytes(payload), worksheet="Results")


def test_absolute_package_relationship_target_and_parent_relative_target():
    entries = package_parts()
    entries["_rels/.rels"] = entries["_rels/.rels"].replace(
        'Target="custom/book.xml"', 'Target="/custom/book.xml"'
    )
    entries["custom/_rels/book.xml.rels"] = entries["custom/_rels/book.xml.rels"].replace(
        'Target="tables/data.xml"', 'Target="../custom/tables/data.xml"'
    )
    assert reader.read_workbook(zipped(entries), worksheet="Results").rows == []


@pytest.mark.parametrize("damage", ["unsupported_version", "negative_seek", "invalid_utf8"])
def test_malformed_zip_metadata_remains_validation_error(damage):
    payload = bytearray(zipped(package_parts()))
    central = payload.index(b"PK\x01\x02")
    if damage == "unsupported_version":
        struct.pack_into("<H", payload, central + 6, 235)
    elif damage == "negative_seek":
        end = payload.rfind(b"PK\x05\x06")
        struct.pack_into("<I", payload, end + 16, 0xFFFFFFFF)
    else:
        flags = struct.unpack_from("<H", payload, central + 8)[0]
        struct.pack_into("<H", payload, central + 8, flags | 0x800)
        payload[central + 46] = 0xFF
    with pytest.raises(ValidationError):
        reader.read_workbook(bytes(payload), worksheet="Results")


@pytest.mark.parametrize("part", ["[Content_Types].xml", "custom/tables/data.xml"])
def test_unknown_xml_encoding_is_validation_error(part):
    entries = package_parts()
    entries[part] = '<?xml version="1.0" encoding="unrecognized-charset"?>' + entries[part]
    with pytest.raises(ValidationError, match="invalid or unsafe XML"):
        reader.read_workbook(zipped(entries), worksheet="Results")


@pytest.mark.parametrize("source", ["shared", "inline", "rich_shared", "rich_inline"])
@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ("first_x000D_\nsecond", "first\r\nsecond"),
        ("_x005F_x0041_", "_x0041_"),
        ("_x005f_x0041_", "_x0041_"),
        ("_x005F_x005F_x005F_x0041_", "_x005F_x0041_"),
        ("_x005F_x0000_", "_x0000_"),
        ("_x0041__x00e9_", "Aé"),
        ("_xD83D__xDE00_", "😀"),
        ("_x0009__x000A__x000D_", "\t\n\r"),
    ],
)
def test_st_xstring_decoding_is_exact_and_runs_once(source, encoded, expected):
    content = f"<t>{encoded}</t>"
    if source.startswith("rich"):
        content = f"<r>{content}</r>"
    if source.endswith("shared"):
        table = read('<row r="1"><c r="A1" t="s"><v>0</v></c></row>', shared=f"<si>{content}</si>")
    else:
        table = read(f'<row r="1"><c r="A1" t="inlineStr"><is>{content}</is></c></row>')
    assert table.rows == [[expected]]
    assert table.numeric_cells == set()


@pytest.mark.parametrize("shared", [False, True])
def test_escapes_are_not_created_by_concatenating_rich_text_runs(shared):
    content = "<r><t>_x00</t></r><r><t>41_</t></r>"
    if shared:
        table = read('<row r="1"><c r="A1" t="s"><v>0</v></c></row>', shared=f"<si>{content}</si>")
    else:
        table = read(f'<row r="1"><c r="A1" t="inlineStr"><is>{content}</is></c></row>')
    assert table.rows == [["_x0041_"]]


@pytest.mark.parametrize(
    "encoded",
    [
        "_x0000_",
        "_x0001_",
        "_x000B_",
        "_x007F_",
        "_x0085_",
        "_x009F_",
        "_xD83D_",
        "_xDE00_",
        "_xD83D_a_xDE00_",
        "_xD83D__x0041_",
    ],
)
def test_unsupported_escaped_controls_and_unpaired_surrogates_are_refused(encoded):
    with pytest.raises(ValidationError, match="unsupported"):
        read('<row r="1">' + text("A1", encoded) + "</row>")


def test_surrogate_pair_split_between_rich_runs_is_refused():
    content = "<r><t>_xD83D_</t></r><r><t>_xDE00_</t></r>"
    with pytest.raises(ValidationError, match="surrogate"):
        read(f'<row r="1"><c r="A1" t="inlineStr"><is>{content}</is></c></row>')


def test_decoded_text_limit_applies_to_content_after_decoding():
    encoded = "_x0041_" * reader.MAX_TEXT
    assert read('<row r="1">' + text("A1", encoded) + "</row>").rows == [["A" * reader.MAX_TEXT]]
    with pytest.raises(ValidationError, match="text exceeds"):
        read('<row r="1">' + text("A1", encoded + "_x0041_") + "</row>")


@pytest.mark.parametrize("length", [31, 32])
def test_worksheet_name_length_boundary(length):
    name = "N" * length
    entries = package_parts()
    entries["custom/book.xml"] = entries["custom/book.xml"].replace(
        'name="Results"', f'name="{name}"'
    )
    payload = zipped(entries)
    if length == 31:
        assert reader.read_workbook(payload, worksheet=name).worksheet == name
    else:
        with pytest.raises(ValidationError, match="worksheet names must use 1 to 31"):
            reader.read_workbook(payload, worksheet=name)


def test_enormous_worksheet_name_yields_short_fixed_error():
    name = "N" * 100_000
    entries = package_parts()
    entries["custom/book.xml"] = entries["custom/book.xml"].replace(
        'name="Results"', f'name="{name}"'
    )
    with pytest.raises(ValidationError) as error:
        reader.read_workbook(zipped(entries), worksheet="Results")
    assert len(str(error.value)) < 250
    assert "worksheet names must use 1 to 31" in str(error.value)
    assert "N" * 32 not in str(error.value)


@pytest.mark.parametrize(
    "name",
    [
        "bad/name",
        "bad\\name",
        "bad?name",
        "bad*name",
        "bad:name",
        "bad[name",
        "bad]name",
        "'badname",
        "badname'",
    ],
)
def test_invalid_excel_worksheet_names_are_refused(name):
    entries = package_parts()
    entries["custom/book.xml"] = entries["custom/book.xml"].replace(
        'name="Results"', f'name="{name}"'
    )
    with pytest.raises(ValidationError, match="worksheet names must use 1 to 31"):
        reader.read_workbook(zipped(entries), worksheet=name)


def test_internal_apostrophe_is_valid_in_worksheet_name():
    name = "Lab's results"
    entries = package_parts()
    entries["custom/book.xml"] = entries["custom/book.xml"].replace(
        'name="Results"', f'name="{name}"'
    )
    assert reader.read_workbook(zipped(entries), worksheet=name).worksheet == name


def test_worksheet_suggestions_are_bounded_and_later_sheets_remain_selectable():
    entries = package_parts()
    names, sheets, relationships, overrides = [], [], [], []
    for number in range(1, 26):
        name = f"Sheet{number:02}"
        names.append(name)
        sheets.append(f'<sheet name="{name}" sheetId="{number}" r:id="sheet{number}"/>')
        part = f"custom/tables/sheet{number}.xml"
        relationships.append(
            f'<Relationship Id="sheet{number}" Type="{R}/worksheet" '
            f'Target="tables/sheet{number}.xml"/>'
        )
        overrides.append(f'<Override PartName="/{part}" ContentType="{reader.WORKSHEET_TYPE}"/>')
        entries[part] = sheet('<row r="1">' + text("A1", name) + "</row>")
    entries["custom/book.xml"] = (
        f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>' + "".join(sheets) + "</sheets></workbook>"
    )
    entries["custom/_rels/book.xml.rels"] = (
        f'<Relationships xmlns="{P}">' + "".join(relationships) + "</Relationships>"
    )
    entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace(
        "</Types>", "".join(overrides) + "</Types>"
    )
    payload = zipped(entries)
    with pytest.raises(ValidationError) as error:
        reader.read_workbook(payload, worksheet="Missing")
    message = str(error.value)
    assert "'Sheet01'" in message and "'Sheet20'" in message
    assert "'Sheet21'" not in message and "'Sheet25'" not in message
    assert "and 5 more" in message
    assert len(message) < 1000
    assert reader.read_workbook(payload, worksheet=names[-1]).rows == [["Sheet25"]]
