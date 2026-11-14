"""Calculated results: work an entry from its readings, keep the worksheet, work it again.

`work` turns the readings an analyst typed into the reported text and reporting limit, using
the test's CalculationRule. `record` stores the worksheet beside the new revision. `reproduce`
works a stored worksheet again from its own readings, calculation version and rounding
snapshot, which is how a reported value is shown to be reproducible.

Error keys are form field names: a reading with key `gross` reports under `reading_gross`.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from . import calculations
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

    def audit(self):
        return {
            "calculation": self.calculation.key,
            "calculation_version": self.calculation.version,
            "readings": {reading.key: text for reading, text, _, _ in self.readings},
            "unrounded_value": None
            if self.unrounded is None
            else plain(calculations.unrounded(self.unrounded)),
            "rounding": self.rounding,
            "reported_as_nd": self.reported_as_nd,
        }


def _parse_readings(calculation, readings):
    errors, parsed, values = {}, [], {}
    supplied = dict(readings or {})
    for reading in calculation.inputs:
        text = supplied.pop(reading.key, "")
        text = "" if text is None else str(text)
        name = reading_field(reading.key)
        try:
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
):
    """Work one manual entry. Every problem is a ValidationError keyed by form field.
    `expected` is the token of the calculation the page showed, when a page supplied one."""
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
    if calculation.result_unit and calculation.result_unit != definition.unit:
        raise ValidationError(
            {
                NON_FIELD_ERRORS: [
                    f"The calculation gives {calculation.result_unit}; the test's unit is now "
                    f"{definition.unit}. An administrator must correct the calculation rule."
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
    parsed, values, errors = _parse_readings(calculation, readings)
    dilution = _dilution(dilution_factor, calculation, errors)
    limit_text = (reporting_limit_applied or "").strip()
    if calculation.sets_limit and limit_text:
        errors["reporting_limit_applied"] = [
            "This calculation sets the reporting limit applied. Leave it blank and give the "
            "limit before dilution with the readings."
        ]
    if errors:
        raise ValidationError(errors)
    outcome = calculation.run(values, dilution)
    if calculation.sets_limit:
        limit_text = plain(outcome.limit)
        limit = _storable(limit_text, "The reporting limit applied", reading_field("base_limit"))
    else:
        try:
            limit = parse_decimal(limit_text, "Reporting limit applied")
        except ValidationError:
            limit = None  # the ordinary entry check reports a missing or malformed limit
    worked = Worked(calculation, parsed, "ND", limit_text, None, rule_basis=rule.basis)
    if outcome.value is None:
        return worked  # a reading was ND: reported as ND at the limit, nothing to round
    rounded, band = rule.rounding.apply(outcome.value)
    text = value_text(rounded)
    _storable(text, "The calculated value", NON_FIELD_ERRORS)
    worked.unrounded = outcome.value
    worked.rounding = rule.rounding.as_json(band)
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


def record(revision, worked):
    """Store the worksheet of a new calculated revision. Caller holds the transaction."""
    calculation = worked.calculation
    worksheet = ResultWorksheet.objects.create(
        revision=revision,
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
    try:
        outcome = calculation.run(values, revision.dilution_factor)
        limit_ok, limit_text = True, ""
        if calculation.sets_limit:
            limit_text = plain(outcome.limit)
            limit_ok = outcome.limit == revision.reporting_limit_applied
        if outcome.value is None:
            return Reproduction(True, "ND", limit_text, limit_ok and revision.is_nondetect)
        rounded, band = Rounding.from_json(worksheet.rounding).apply(outcome.value)
    except ArithmeticError, KeyError, TypeError:
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
