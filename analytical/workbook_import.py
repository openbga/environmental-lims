"""Bounded OOXML table reading without interpreting or rounding numeric observations."""

import io
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from django.core.exceptions import ValidationError

MAX_COMPRESSED_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 200
MAX_ROWS = 10_000
MAX_COLUMNS = 256
MAX_CELLS = 100_000
MAX_SHARED_STRINGS = 100_000
MAX_TEXT = 32_767

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
WORKBOOK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORKSHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
STRINGS_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
REL_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
CELL_COORDINATE = re.compile(r"([A-Z]+)([1-9][0-9]{0,4})", re.ASCII)
INTEGER = re.compile(r"[0-9]{1,10}", re.ASCII)
STRING_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", re.ASCII)


@dataclass
class WorkbookTable:
    worksheet: str
    rows: list[list[str]]
    numeric_cells: set[tuple[int, int]]


def _fail(message):
    raise ValidationError(f"Excel workbook: {message}")


def _tag(name):
    return f"{{{SHEET_NS}}}{name}"


def _xml(data, expected):
    """DTD-free parsing with limits independent of ZIP metadata and XML declarations."""
    try:
        depth = count = 0
        parser = ElementTree.iterparse(
            io.BytesIO(data),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        for event, _element in parser:
            if event == "start":
                depth += 1
                count += 1
                if depth > 64 or count > 500_000:
                    _fail("XML structure exceeds the supported limits.")
            else:
                depth -= 1
        root = parser.root
    except (ParseError, DefusedXmlException, ValueError, LookupError) as exc:
        raise ValidationError("Excel workbook: invalid or unsafe XML.") from exc
    if root.tag != expected:
        _fail("unexpected XML root or namespace; use a transitional OOXML .xlsx workbook.")
    return root


def _safe_name(name):
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(ord(char) < 32 for char in name)
        or any(char in name for char in ":%?#")
        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
    ):
        _fail("unsafe package member name or relationship target.")
    return name


def _target(source, target):
    if (
        not target
        or "\\" in target
        or any(char in target for char in ":%?#")
        or any(ord(char) < 32 for char in target)
    ):
        _fail("unsafe relationship target.")
    # Package-absolute targets are permitted by OPC; relative targets stay in the package.
    joined = (
        target[1:] if target.startswith("/") else posixpath.join(posixpath.dirname(source), target)
    )
    normalized = posixpath.normpath(joined)
    _safe_name(normalized)
    if normalized.endswith("/") or normalized == ".":
        _fail("invalid relationship target.")
    return normalized


def _read_package(data):
    if len(data) > MAX_COMPRESSED_BYTES:
        _fail("the compressed file exceeds 1 MiB; split the file.")
    if not data.startswith(b"PK\x03\x04"):
        _fail("not an unencrypted .xlsx ZIP package.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                _fail(f"more than {MAX_MEMBERS} ZIP members.")
            names = set()
            declared = 0
            for member in members:
                _safe_name(member.filename)
                if member.filename != member.orig_filename or member.filename in names:
                    _fail("duplicate or malformed ZIP member names.")
                names.add(member.filename)
                if member.flag_bits & 1:
                    _fail("encrypted ZIP members are not supported.")
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    _fail("unsupported ZIP compression; save as a standard .xlsx workbook.")
                if member.external_attr >> 16 & 0o170000 == 0o120000:
                    _fail("symbolic links are not supported.")
                declared += member.file_size
                if declared > MAX_EXPANDED_BYTES:
                    _fail("expanded workbook exceeds 20 MiB.")
            parts, expanded = {}, 0
            for member in members:
                chunks = []
                with archive.open(member) as stream:
                    while chunk := stream.read(min(65_536, MAX_EXPANDED_BYTES - expanded + 1)):
                        expanded += len(chunk)
                        if expanded > MAX_EXPANDED_BYTES:
                            _fail("expanded workbook exceeds 20 MiB.")
                        chunks.append(chunk)
                content = b"".join(chunks)
                if len(content) != member.file_size:
                    _fail("ZIP member size does not match its contents.")
                if not member.is_dir():
                    parts[member.filename] = content
            return parts
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        zlib.error,
        UnicodeDecodeError,
        NotImplementedError,
        ValueError,
    ) as exc:
        raise ValidationError("Excel workbook: malformed or damaged ZIP package.") from exc


def _part(parts, name):
    if name not in parts:
        _fail(f"required package part '{name}' is missing.")
    return parts[name]


def _content_types(parts):
    root = _xml(_part(parts, "[Content_Types].xml"), f"{{{CONTENT_NS}}}Types")
    defaults, overrides = {}, {}
    for entry in root:
        kind = entry.tag
        content_type = entry.get("ContentType", "")
        if not content_type:
            _fail("missing package content type.")
        if any(word in content_type.lower() for word in ("macro", "vba")):
            _fail("macros are not supported; use a macro-free .xlsx workbook.")
        if kind == f"{{{CONTENT_NS}}}Default":
            key, table = entry.get("Extension", ""), defaults
            if not key or any(char in key for char in "/\\."):
                _fail("invalid default content type.")
            key = key.lower()
        elif kind == f"{{{CONTENT_NS}}}Override":
            name = entry.get("PartName", "")
            if not name.startswith("/"):
                _fail("invalid content type part name.")
            key, table = _safe_name(name[1:]), overrides
            if key not in parts:
                _fail("content type names a missing package part.")
        else:
            _fail("unexpected content type entry or namespace.")
        if key in table:
            _fail("duplicate package content types.")
        table[key] = content_type
    if any("vbaproject" in name.lower() for name in parts):
        _fail("macros are not supported.")

    def check(name, expected):
        actual = overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].lower()))
        if actual != expected:
            _fail(f"unexpected content type for '{name}'.")

    return check


def _relationships(parts, check_type):
    relationships = {}
    for name, data in parts.items():
        if not name.endswith(".rels"):
            continue
        check_type(name, REL_TYPE)
        root = _xml(data, f"{{{REL_NS}}}Relationships")
        if name == "_rels/.rels":
            source = ""
        else:
            directory, filename = posixpath.split(name)
            if posixpath.basename(directory) != "_rels":
                _fail("invalid relationship part location.")
            source = posixpath.join(posixpath.dirname(directory), filename[:-5])
            _part(parts, source)
        entries = {}
        for entry in root:
            if entry.tag != f"{{{REL_NS}}}Relationship":
                _fail("unexpected relationship element or namespace.")
            rid, kind, target = (entry.get(attr, "") for attr in ("Id", "Type", "Target"))
            if not rid or not kind or rid in entries:
                _fail("missing or duplicate relationship identifier.")
            if entry.get("TargetMode", "Internal") != "Internal":
                _fail("external relationships are not supported.")
            if any(word in kind.lower() for word in ("external", "vba", "macro")):
                _fail("external links and macros are not supported.")
            resolved = _target(source, target)
            _part(parts, resolved)
            entries[rid] = (kind, resolved)
        relationships[source] = entries
    if "" not in relationships:
        _fail("package root relationships are missing.")
    return relationships


def _one_relationship(entries, kind, *, required=True):
    matches = [target for actual, target in entries.values() if actual == f"{DOC_REL_NS}/{kind}"]
    if len(matches) > 1 or (required and not matches):
        _fail(f"expected one {kind} relationship.")
    return matches[0] if matches else None


def _decode_text(value):
    """Decode ST_Xstring once per <t>; escaped underscores never start a second pass."""
    # A UTF-16 surrogate pair takes fourteen source characters for one decoded character.
    if len(value) > 14 * MAX_TEXT:
        _fail(f"cell text exceeds {MAX_TEXT} characters.")
    pieces, cursor = [], 0
    while match := STRING_ESCAPE.search(value, cursor):
        pieces.append(value[cursor : match.start()])
        codepoint = int(match[1], 16)
        cursor = match.end()
        if 0xD800 <= codepoint <= 0xDBFF:
            following = STRING_ESCAPE.match(value, cursor)
            if following is None or not 0xDC00 <= int(following[1], 16) <= 0xDFFF:
                _fail("unsupported unpaired UTF-16 surrogate in cell text.")
            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + int(following[1], 16) - 0xDC00
            cursor = following.end()
        elif 0xDC00 <= codepoint <= 0xDFFF:
            _fail("unsupported unpaired UTF-16 surrogate in cell text.")
        pieces.append(chr(codepoint))
    pieces.append(value[cursor:])
    text = "".join(pieces)
    if len(text) > MAX_TEXT:
        _fail(f"cell text exceeds {MAX_TEXT} characters.")
    if any((ord(char) < 32 and char not in "\t\n\r") or 0x7F <= ord(char) <= 0x9F for char in text):
        _fail("unsupported control character in cell text; use ordinary text, tabs or newlines.")
    return text


def _text(container):
    pieces, plain, rich = [], False, False
    for child in container:
        if child.tag == _tag("t"):
            if len(child):
                _fail("invalid string text element.")
            plain = True
            pieces.append(_decode_text(child.text or ""))
        elif child.tag == _tag("r"):
            rich = True
            text_nodes = child.findall(_tag("t"))
            if (
                len(text_nodes) != 1
                or any(item.tag not in (_tag("rPr"), _tag("t")) for item in child)
                or len(text_nodes[0])
            ):
                _fail("invalid rich text run.")
            pieces.append(_decode_text(text_nodes[0].text or ""))
        elif child.tag not in (_tag("rPh"), _tag("phoneticPr")):
            _fail("unsupported string element or namespace.")
    if plain and (rich or len(pieces) > 1):
        _fail("contradictory plain and rich string content.")
    text = "".join(pieces)
    if len(text) > MAX_TEXT:
        _fail(f"cell text exceeds {MAX_TEXT} characters.")
    return text


def _shared_strings(parts, name, check_type):
    if name is None:
        return []
    check_type(name, STRINGS_TYPE)
    root = _xml(_part(parts, name), _tag("sst"))
    strings = []
    for entry in root:
        if entry.tag != _tag("si"):
            _fail("unexpected shared string entry or namespace.")
        if len(strings) >= MAX_SHARED_STRINGS:
            _fail(f"more than {MAX_SHARED_STRINGS} shared strings.")
        strings.append(_text(entry))
    unique = root.get("uniqueCount")
    if unique is not None and (not INTEGER.fullmatch(unique) or int(unique) != len(strings)):
        _fail("shared string count does not match its contents.")
    return strings


def _cell_value(cell, strings, coordinate):
    if cell.find(_tag("f")) is not None or cell.get("t") == "str":
        _fail(f"formula or cached formula at {coordinate}; replace formulas with literal values.")
    kind = cell.get("t", "n")
    if kind not in ("n", "s", "inlineStr"):
        _fail(f"unsupported cell type '{kind}' at {coordinate}; use literal text or numbers.")
    children = list(cell)
    if any(child.tag not in (_tag("v"), _tag("is")) for child in children):
        _fail(f"unsupported cell content at {coordinate}.")
    values, inline = cell.findall(_tag("v")), cell.findall(_tag("is"))
    if len(values) > 1 or len(inline) > 1 or (values and inline):
        _fail(f"contradictory cell content at {coordinate}.")
    if kind == "inlineStr":
        if values or len(inline) != 1:
            _fail(f"invalid inline string at {coordinate}.")
        return _text(inline[0]), False
    if inline or (values and len(values[0])):
        _fail(f"invalid cell value at {coordinate}.")
    value = (values[0].text or "") if values else ""
    if len(value) > MAX_TEXT:
        _fail(f"cell text exceeds {MAX_TEXT} characters at {coordinate}.")
    if kind == "s":
        if not INTEGER.fullmatch(value) or int(value) >= len(strings):
            _fail(f"invalid shared string index at {coordinate}.")
        return strings[int(value)], False
    if not values:  # A styled empty cell is not a numeric observation.
        return "", False
    if not NUMBER.fullmatch(value):
        _fail(f"invalid numeric value at {coordinate}.")
    return value, True


def _table(parts, name, worksheet, strings):
    root = _xml(_part(parts, name), _tag("worksheet"))
    # Refuse foreign-namespace cell constructs and alternate representations.
    if any(not element.tag.startswith(f"{{{SHEET_NS}}}") for element in root.iter()):
        _fail("unsupported worksheet namespace or alternate content.")
    if root.find(f".//{_tag('f')}") is not None:
        _fail("formulas are not supported in the selected worksheet; use literal values.")
    if root.find(_tag("mergeCells")) is not None:
        _fail("merged cells are not supported in the selected worksheet.")
    sheet_data = root.findall(_tag("sheetData"))
    if len(sheet_data) != 1:
        _fail("expected one worksheet data table.")
    rows, numeric, width, count, last_row = [], set(), 0, 0, 0
    for row in sheet_data[0]:
        number = row.get("r", "")
        if row.tag != _tag("row") or not INTEGER.fullmatch(number):
            _fail("every worksheet row must have an explicit row number.")
        number = int(number)
        if not last_row < number <= MAX_ROWS:
            _fail(f"duplicate, out-of-order or out-of-range row; maximum is {MAX_ROWS}.")
        last_row = number
        while len(rows) < number:
            rows.append([])
        last_column = -1
        for cell in row:
            coordinate = cell.get("r", "")
            match = CELL_COORDINATE.fullmatch(coordinate)
            if cell.tag != _tag("c") or match is None:
                _fail("every worksheet cell must have an explicit coordinate.")
            letters, cell_row = match.groups()
            column = 0
            for letter in letters:
                column = column * 26 + ord(letter) - ord("A") + 1
                if column > MAX_COLUMNS:
                    _fail(f"worksheet exceeds {MAX_COLUMNS} columns.")
            column -= 1
            if int(cell_row) != number or column <= last_column:
                _fail(f"duplicate, out-of-order or mismatched cell coordinate {coordinate}.")
            last_column = column
            count += 1
            if count > MAX_CELLS:
                _fail(f"worksheet exceeds {MAX_CELLS} cells.")
            value, is_numeric = _cell_value(cell, strings, coordinate)
            rows[number - 1].extend([""] * (column + 1 - len(rows[number - 1])))
            rows[number - 1][column] = value
            width = max(width, column + 1)
            if is_numeric:
                numeric.add((number, column))
    if len(root.findall(f".//{_tag('c')}")) != count:
        _fail("cell content appears outside the worksheet data table.")
    for row in rows:
        row.extend([""] * (width - len(row)))
    return WorkbookTable(worksheet=worksheet, rows=rows, numeric_cells=numeric)


def read_workbook(data: bytes, *, worksheet: str) -> WorkbookTable:
    """Read one explicitly selected visible sheet; coordinates retain physical row numbers.

    Numeric values are the original XML lexemes, never floats or formatted display values.
    Callers must use ``numeric_cells`` to require text for identifiers and timestamps.
    """
    parts = _read_package(data)
    check_type = _content_types(parts)
    relationships = _relationships(parts, check_type)
    workbook = _one_relationship(relationships[""], "officeDocument")
    check_type(workbook, WORKBOOK_TYPE)
    root = _xml(_part(parts, workbook), _tag("workbook"))
    if any(not element.tag.startswith(f"{{{SHEET_NS}}}") for element in root.iter()):
        _fail("unsupported workbook namespace or alternate content.")
    sheets = root.findall(_tag("sheets"))
    if len(sheets) != 1:
        _fail("expected one workbook sheet list.")
    entries, visible, names, ids, sheet_ids = {}, [], set(), set(), set()
    workbook_rels = relationships.get(workbook, {})
    for sheet in sheets[0]:
        name, rid, sid = (
            sheet.get("name", ""),
            sheet.get(f"{{{DOC_REL_NS}}}id"),
            sheet.get("sheetId"),
        )
        if (
            not 1 <= len(name) <= 31
            or any(char in name for char in "/\\?*:[]")
            or name.startswith("'")
            or name.endswith("'")
        ):
            _fail(
                "worksheet names must use 1 to 31 characters, exclude / \\ ? * : [ ], "
                "and must not begin or end with an apostrophe."
            )
        if (
            sheet.tag != _tag("sheet")
            or not name
            or name in names
            or not rid
            or rid in ids
            or not sid
            or not INTEGER.fullmatch(sid)
            or int(sid) < 1
            or int(sid) in sheet_ids
        ):
            _fail("missing or duplicate worksheet name or identifier.")
        names.add(name)
        ids.add(rid)
        sheet_ids.add(int(sid))
        state = sheet.get("state", "visible")
        if state not in ("visible", "hidden", "veryHidden"):
            _fail("invalid worksheet visibility.")
        relation = workbook_rels.get(rid)
        if relation is None:
            _fail("worksheet relationship is missing.")
        entries[name] = (state, relation)
        if state == "visible" and relation[0] == f"{DOC_REL_NS}/worksheet":
            visible.append(name)
    if worksheet not in entries or entries[worksheet][0] != "visible":
        available = ", ".join(repr(name) for name in visible[:20]) or "none"
        if len(visible) > 20:
            available += f", and {len(visible) - 20} more"
        _fail(f"choose an exact visible worksheet name. Available visible worksheets: {available}.")
    _state, (kind, sheet_part) = entries[worksheet]
    if kind != f"{DOC_REL_NS}/worksheet":
        _fail("the selected sheet is not a worksheet.")
    check_type(sheet_part, WORKSHEET_TYPE)
    strings_part = _one_relationship(workbook_rels, "sharedStrings", required=False)
    strings = _shared_strings(parts, strings_part, check_type)
    return _table(parts, sheet_part, worksheet, strings)
