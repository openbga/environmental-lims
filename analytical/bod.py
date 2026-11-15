"""Version 1 BOD5 bottle arithmetic. Selection is an analyst decision, not a QC verdict.

Keep these inputs, validation and arithmetic unchanged for stored version 1 worksheets.
A different bottle contract or equation needs a new registered version and its own parser.
"""

from decimal import localcontext

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

MAX_BOTTLES = 12
NUMBERS = {
    "initial_do": ("Initial DO", "mg/L"),
    "final_do": ("Final DO", "mg/L"),
    "sample_volume": ("Sample volume", "mL"),
    "bottle_volume": ("Filled bottle volume", "mL"),
    "seed_correction": ("Entered seed correction", "mg/L"),
}
TEXT = {"identifier": 60, "seed_reference": 240, "reason": 240}


def parse_bottles(data):
    """Validate the bounded snapshot, retaining original strings and explicit selection.

    Error keys also identify the bottle form, so direct service callers and the entry page
    share the same checks. Excluded bottles remain complete observations in this increment.
    """
    from .parsing import MAX_TEXT_LENGTH, parse_decimal

    if not isinstance(data, list) or not 1 <= len(data) <= MAX_BOTTLES:
        raise ValidationError({NON_FIELD_ERRORS: ["Record from 1 to 12 complete bottle rows."]})
    errors, canonical, parsed, identifiers = {}, [], [], set()
    for i, row in enumerate(data):
        prefix = f"bottle_{i}_"
        if not isinstance(row, dict) or set(row) != {*NUMBERS, *TEXT, "selected"}:
            errors[prefix + "identifier"] = ["The bottle row is incomplete or has unknown fields."]
            continue
        clean, values = {}, {}
        for key, maximum in {**dict.fromkeys(NUMBERS, MAX_TEXT_LENGTH), **TEXT}.items():
            text = row[key]
            if not isinstance(text, str) or len(text) > maximum:
                errors[prefix + key] = [f"Supply text of at most {maximum} characters."]
                continue
            clean[key] = text
            if key in NUMBERS:
                try:
                    value = parse_decimal(text, NUMBERS[key][0])
                    if value < 0 or (key in ("sample_volume", "bottle_volume") and value == 0):
                        raise ValidationError(
                            "Enter a positive volume or a nonnegative DO/correction."
                        )
                    values[key] = value
                except ValidationError as exc:
                    errors[prefix + key] = exc.messages
                except ArithmeticError:
                    errors[prefix + key] = [
                        f"{NUMBERS[key][0]} is outside the range that can be stored exactly. "
                        "Check the number and its exponent."
                    ]
        identifier = clean.get("identifier", "").strip().casefold()
        if not identifier or identifier in identifiers:
            errors[prefix + "identifier"] = ["Enter a unique, nonblank bottle identifier."]
        identifiers.add(identifier)
        selected = row["selected"]
        if type(selected) is not bool:
            errors[prefix + "selected"] = ["Choose Include or Exclude explicitly."]
        clean["selected"] = selected
        if selected is False and not clean.get("reason", "").strip():
            errors[prefix + "reason"] = ["Give the reason this bottle is excluded."]
        if values.get("seed_correction", 0) != 0 and not clean.get("seed_reference", "").strip():
            errors[prefix + "seed_reference"] = [
                "Reference the record supporting this seed correction."
            ]
        if set(values) == set(NUMBERS):
            if values["sample_volume"] > values["bottle_volume"]:
                errors[prefix + "sample_volume"] = [
                    "Sample volume cannot exceed filled bottle volume."
                ]
            with localcontext() as context:
                context.prec = 50
                depletion = values["initial_do"] - values["final_do"]
                corrected = depletion - values["seed_correction"]
                if selected is True and corrected < 0:
                    errors[prefix + "selected"] = [
                        "Corrected depletion is negative. Resolve the readings or exclude this "
                        "bottle with a reason."
                    ]
                values |= {
                    "depletion": depletion,
                    "corrected_depletion": corrected,
                    "value": corrected * values["bottle_volume"] / values["sample_volume"],
                }
        canonical.append(clean)
        parsed.append({**clean, **values})
    if not any(row.get("selected") is True for row in canonical):
        errors[NON_FIELD_ERRORS] = ["Include at least one bottle to calculate a result."]
    if errors:
        raise ValidationError(errors)
    return canonical, parsed


def work(readings, dilution):
    """Called within the registry's Decimal context; round only the final result."""
    from .calculations import Outcome

    selected = [row["value"] for row in readings["bottles"] if row["selected"]]
    return Outcome(sum(selected) * dilution / len(selected))


def display(data):
    """Derived values for the immutable worksheet; no mutation of the snapshot."""
    try:
        canonical, parsed = parse_bottles(data)
        return [
            {**raw, **{key: row[key] for key in ("depletion", "corrected_depletion", "value")}}
            for raw, row in zip(canonical, parsed, strict=True)
        ]
    except ValidationError:
        return []
