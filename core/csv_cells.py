"""Keep frozen text from being interpreted as a spreadsheet formula."""

import unicodedata

FORMULA_PREFIXES = ("=", "+", "-", "@", "＝", "＋", "－", "＠")


def spreadsheet_text(value):
    if not isinstance(value, str):
        return value
    leading = value.lstrip()
    if leading.startswith(FORMULA_PREFIXES) or any(
        unicodedata.category(char) in {"Cc", "Cf"}
        for char in value[: len(value) - len(leading) + 1]
    ):
        return "'" + value
    return value
