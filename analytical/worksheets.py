"""Calculated results: work an entry from its readings, keep the worksheet, work it again.

`work` turns the readings an analyst typed into the reported text and reporting limit, using
the test's CalculationRule. `record` stores the worksheet beside the new revision. `reproduce`
works a stored worksheet again from its own readings, calculation version and rounding
snapshot, which is how a reported value is shown to be reproducible.

Error keys are form field names: a reading with key `gross` reports under `reading_gross`,
and a reading the entry itself records (percent solids) under that entry field.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from . import bod, calculations
from .calculations import Rounding, plain, value_text
from .models import CalculationRule, ResultKind, ResultWorksheet, WorksheetReading
from .parsing import MAX_TEXT_LENGTH, parse_decimal, parse_dilution, parse_result_text

READING_PREFIX = "reading_"


def reading_field(key):
    return f"{READING_PREFIX}{key}"


def token(calculation):
    """Names the calculation a page asked its readings for. The entry service refuses readings
    typed for another one: the same reading names can mean other units (grams, milligrams)."""
    return f"{calculation.key}:{calculation.version}"


def shown(token_text):
    """The calculation a posted token names, or None for a blank, malformed or unknown one."""
    key, _, version = (token_text or "").partition(":")
    return calculations.registered(key, int(version)) if version.isdigit() else None


def active_rule(definition_id):
    """The calculation rule manual entry must use for a test, or None. Call after the method
    policy row is locked: configuration changes take the same lock."""
    return CalculationRule.objects.filter(definition_id=definition_id, is_active=True).first()


def active_rules(definition_ids):
    """{definition id: rule} for the entry pages. The service reads its own, under lock."""
    return {
        rule.definition_id: rule
        for rule in CalculationRule.objects.filter(definition_id__in=definition_ids, is_active=True)
    }


def sets_limit(rule):
    """Whether the rule's calculation works out the reporting limit applied itself."""
    calculation = calculations.current(rule.calculation)
    return bool(calculation and calculation.sets_limit)


@dataclass
class Worked:
    """An entry worked from readings, ready for the ordinary entry checks."""

    calculation: object
    readings: list  # (Reading, typed text, kind, exact value or None)
    supplied_text: str  # the reported text: the rounded value, or ND
    limit_text: str  # reporting limit applied
    unrounded: Decimal | None
    rounding: dict = field(default_factory=dict)
    reported_as_nd: bool = False
    rule_basis: str = ""
    unrounded_limit: Decimal | None = None  # where the calculation rounds the limit it sets

    bottle_data: list | None = None

    def audit(self):
        return {
            **({"bottle_data": self.bottle_data} if self.bottle_data is not None else {}),
            "calculation": self.calculation.key,
            "calculation_version": self.calculation.version,
            "readings": {reading.key: text for reading, text, _, _ in self.readings},
            "unrounded_value": None
            if self.unrounded is None
            else plain(calculations.unrounded(self.unrounded)),
            "rounding": self.rounding,
            "reported_as_nd": self.reported_as_nd,
            **(
                {}
                if self.unrounded_limit is None
                else {"unrounded_limit": plain(calculations.unrounded(self.unrounded_limit))}
            ),
        }


def _parse_readings(calculation, readings, entry):
    """Read each input exactly as typed. `entry` holds the entry's own fields, for the
    readings a calculation takes from them (Reading.entry_field)."""
    errors, parsed, values = {}, [], {}
    supplied = dict(readings or {})
    for reading in calculation.inputs:
        if reading.entry_field:
            text, name = (entry or {}).get(reading.entry_field), reading.entry_field
        else:
            text, name = supplied.pop(reading.key, ""), reading_field(reading.key)
        try:
            if isinstance(text, float):
                raise ValidationError(
                    f"Supply {reading.label[0].lower()}{reading.label[1:]} as exact decimal "
                    "text, not a float."
                )
            text = "" if text is None else str(text)
            if reading.entry_field and not text.strip():
                raise ValidationError(f"{reading.label} is required by this calculation.")
            if len(text) > MAX_TEXT_LENGTH:
                raise ValidationError(
                    f"{reading.label} must be at most {MAX_TEXT_LENGTH} characters."
                )
            if reading.allows_nd:
                kind, value = parse_result_text(text, reading.label)
            else:
                kind, value = ResultKind.DETECTED, parse_decimal(text, reading.label)
            if value is not None and reading.positive and value <= 0:
                raise ValidationError(f"{reading.label} must be greater than zero.")
            if value is not None and reading.non_negative and value < 0:
                raise ValidationError(f"{reading.label} cannot be negative.")
        except ValidationError as exc:
            errors[name] = exc.messages
            continue
        parsed.append((reading, text, kind, value))
        values[reading.key] = value
    if supplied:
        errors.setdefault("supplied_text", []).append(
            "These readings do not belong to this test's calculation. Reload the page."
        )
    return parsed, values, errors


def _dilution(text, calculation, errors):
    try:
        dilution = parse_dilution(text)
    except ValidationError as exc:
        errors["dilution_factor"] = exc.messages
        return None
    if not calculation.uses_dilution and dilution != 1:
        errors["dilution_factor"] = [
            "This calculation applies no dilution factor. Enter 1; a diluted portion needs a "
            "calculation that uses the factor."
        ]
    return dilution


def _storable(text, label, key):
    """The same exactness rule as a typed value: at most the stored scale, never rounded."""
    try:
        return parse_decimal(text, label)
    except ValidationError:
        raise ValidationError(
            {
                key: [
                    f"{label} works out to {text}, which cannot be stored exactly (results "
                    f"keep at most {calculations.MAX_PLACES} decimal places). Check the "
                    "readings and the rounding rule."
                ]
            }
        ) from None


def work(
    *,
    rule,
    definition,
    readings,
    dilution_factor,
    reporting_limit_applied,
    report_nd=False,
    has_qualifier=False,
    expected=None,
    result_basis="",
    entry=None,
    bottle_data=None,
):
    """Work one manual entry. Every problem is a ValidationError keyed by form field.
    `expected` is the token of the calculation the page showed, when a page supplied one.
    `result_basis` and `entry` (the entry's own fields, such as percent solids) are what the
    analyst recorded with the result; some calculations read them."""
    calculation = calculations.current(rule.calculation)
    if calculation is None:
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    f"The calculation '{rule.calculation}' set for this test is not part of "
                    "this release. An administrator must choose another under QC configuration."
                ]
            }
        )
    if not calculation.gives(definition.unit):
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    f"The calculation gives {calculation.result_unit}; the test's unit is now "
                    f"{definition.unit}. An administrator must correct the calculation rule."
                ]
            }
        )
    soil = definition.matrix == "soil"
    if calculation.soil_only and not soil:
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    "The calculation corrects a soil result to dry weight; this test is not a "
                    "soil test. An administrator must correct the calculation rule."
                ]
            }
        )
    if calculation.liquid_only and soil:
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    "The calculation takes a volume of sample; this is a soil test. An "
                    "administrator must correct the calculation rule."
                ]
            }
        )
    if expected is not None and expected != token(calculation):
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    "The calculation set for this test changed after this page was opened; it "
                    f"is now {calculation.label}. Nothing was saved. Reload the page and enter "
                    "the readings it asks for."
                ]
            }
        )
    if calculation.supports_bottles:
        bottle_data, bottles = bod.parse_bottles(bottle_data)
    elif bottle_data is not None:
        raise ValidationError({NON_FIELD_ERRORS: ["This calculation does not take bottle rows."]})
    parsed, values, errors = _parse_readings(calculation, readings, entry)
    if calculation.supports_bottles:
        values["bottles"] = bottles
    if soil and calculation.soil_basis and result_basis != calculation.soil_basis:
        errors["result_basis"] = [calculation.soil_basis_reason]
    dilution = _dilution(dilution_factor, calculation, errors)
    limit_text = (reporting_limit_applied or "").strip()
    if calculation.sets_limit and limit_text:
        starts_from = next(r.label for r in calculation.inputs if r.key == "base_limit")
        errors["reporting_limit_applied"] = [
            "This calculation sets the reporting limit applied. Leave it blank and give the "
            f"{starts_from[0].lower()}{starts_from[1:]} with the readings."
        ]
    if errors:
        raise ValidationError(errors)
    try:
        outcome = calculation.run(values, dilution)
        # Rounding and the record of the unrounded values need a size that can be worked.
        rounded_here = [outcome.value, outcome.limit if calculation.rounds_limit else None]
        for number in rounded_here:
            if number is not None:
                rule.rounding.apply(number)
                calculations.unrounded(number)
    except calculations.CalculationError as exc:
        at = reading_field(exc.field) if exc.field else NON_FIELD_ERRORS
        raise ValidationError({at: [str(exc)]}) from None
    except ArithmeticError:
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    "These readings work out to a number too large to round and store. Check "
                    "the readings and their units."
                ]
            }
        ) from None
    worked = Worked(
        calculation, parsed, "ND", limit_text, None, rule_basis=rule.basis, bottle_data=bottle_data
    )
    limit_band = None
    if calculation.sets_limit:
        limit, limit_field = outcome.limit, reading_field("base_limit")
        if calculation.rounds_limit:
            limit, limit_band = rule.rounding.apply(outcome.limit)
            if limit <= 0:
                raise ValidationError(
                    {
                        limit_field: [
                            "The reporting limit applied works out to "
                            f"{plain(calculations.unrounded(outcome.limit))}, which this test's "
                            f"rounding ({rule.rounding.describe()}) takes to zero. An "
                            "administrator must set a rounding that keeps the limit."
                        ]
                    }
                )
            worked.unrounded_limit = outcome.limit
            # Kept even when a reading is ND: the limit was still rounded by this rule.
            worked.rounding = _rounding_snapshot(rule, limit_band=limit_band)
        worked.limit_text = limit_text = plain(limit)
        limit = _storable(limit_text, "The reporting limit applied", limit_field)
    else:
        try:
            limit = parse_decimal(limit_text, "Reporting limit applied")
        except ValidationError:
            limit = None  # the ordinary entry check reports a missing or malformed limit
    if outcome.value is None:
        return worked  # a reading was ND: reported as ND at the limit, no value to round
    rounded, band = rule.rounding.apply(outcome.value)
    text = value_text(rounded)
    _storable(text, "The calculated value", NON_FIELD_ERRORS)
    worked.unrounded = outcome.value
    worked.rounding = _rounding_snapshot(rule, band, limit_band)
    below = limit is not None and limit > 0 and rounded < limit
    if report_nd:
        if not below:
            raise ValidationError(
                {
                    "report_nd": [
                        f"The calculated value {text} {definition.unit} is not below the "
                        f"reporting limit applied ({limit_text or 'not given'}), so it cannot "
                        "be reported as ND."
                    ]
                }
            )
        worked.reported_as_nd = True
        return worked
    if below and not has_qualifier:
        raise ValidationError(
            {
                "qualifiers": [
                    f"The calculated value {text} {definition.unit} is below the reporting "
                    f"limit applied ({limit_text}). Add a qualifier to report the value, or "
                    "choose Report as ND."
                ]
            }
        )
    worked.supplied_text = text
    return worked


def _rounding_snapshot(rule, band=None, limit_band=None):
    """The rule as applied: `band` is the range the value fell in, `limit_band` the range the
    limit fell in where the calculation rounds the limit it sets."""
    snapshot = rule.rounding.as_json(band)
    if limit_band:
        snapshot["limit_band"] = limit_band
    return snapshot


def record(revision, worked):
    """Store the worksheet of a new calculated revision. Caller holds the transaction."""
    calculation = worked.calculation
    worksheet = ResultWorksheet.objects.create(
        revision=revision,
        bottle_data=worked.bottle_data,
        calculation=calculation.key,
        version=calculation.version,
        title=calculation.title,
        formula=calculation.formula,
        source=calculation.source,
        unrounded_value=None
        if worked.unrounded is None
        else calculations.unrounded(worked.unrounded),
        rounding=worked.rounding,
        reported_as_nd=worked.reported_as_nd,
        rule_basis=worked.rule_basis,
        unrounded_limit=None
        if worked.unrounded_limit is None
        else calculations.unrounded(worked.unrounded_limit),
    )
    WorksheetReading.objects.bulk_create(
        WorksheetReading(
            worksheet=worksheet,
            position=position,
            key=reading.key,
            label=reading.label,
            unit=calculation.unit_of(reading, revision.unit),
            supplied_text=text,
            kind=kind,
            value=value,
        )
        for position, (reading, text, kind, value) in enumerate(worked.readings, start=1)
    )
    return worksheet


@dataclass
class Reproduction:
    """A stored worksheet worked again. `available` is False when this release no longer has
    the calculation version; the stored value then stands on its own record."""

    available: bool
    value_text: str = ""  # the rounded value the readings give now
    limit_text: str = ""
    matches: bool = False


def reproduce(worksheet):
    """Work a stored worksheet again from its own readings, version and rounding snapshot.
    A record that cannot be worked at all is reported as not matching, never as an error."""
    calculation = calculations.registered(worksheet.calculation, worksheet.version)
    if calculation is None:
        return Reproduction(available=False)
    revision = worksheet.revision
    values = {reading.key: reading.value for reading in worksheet.readings.all()}
    # What the calculation read from the entry is on the revision too, and the basis it means
    # is the basis the revision declares: the worksheet and its result must say the same.
    record_ok = all(
        values.get(reading.key) == getattr(revision, reading.entry_field)
        for reading in calculation.inputs
        if reading.entry_field
    ) and not (
        revision.matrix == "soil"
        and calculation.soil_basis
        and revision.result_basis != calculation.soil_basis
    )
    try:
        if calculation.supports_bottles:
            values["bottles"] = bod.parse_bottles(worksheet.bottle_data)[1]
        elif worksheet.bottle_data is not None:
            return Reproduction(available=True)
        outcome = calculation.run(values, revision.dilution_factor)
        limit_ok, limit_text = True, ""
        if calculation.sets_limit:
            limit = outcome.limit
            if calculation.rounds_limit:
                limit, limit_band = Rounding.from_json(worksheet.rounding).apply(outcome.limit)
                limit_ok = (
                    limit_band == worksheet.rounding.get("limit_band")
                    and calculations.unrounded(outcome.limit) == worksheet.unrounded_limit
                )
            limit_text = plain(limit)
            limit_ok = limit_ok and limit == revision.reporting_limit_applied
        limit_ok = limit_ok and record_ok
        if outcome.value is None:
            return Reproduction(True, "ND", limit_text, limit_ok and revision.is_nondetect)
        rounded, band = Rounding.from_json(worksheet.rounding).apply(outcome.value)
    except ArithmeticError, KeyError, TypeError, ValidationError, calculations.CalculationError:
        return Reproduction(available=True)
    text = value_text(rounded)
    same_unrounded = calculations.unrounded(outcome.value) == worksheet.unrounded_value
    if worksheet.reported_as_nd:
        reported = revision.is_nondetect and rounded < revision.reporting_limit_applied
    else:
        reported = revision.supplied_text == text and revision.value == rounded
    return Reproduction(
        True,
        text,
        limit_text,
        limit_ok and same_unrounded and reported and band == worksheet.rounding.get("band"),
    )
