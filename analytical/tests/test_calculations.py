"""Calculated results: exact arithmetic, the laboratory's rounding, kept readings, and a
reported value that can be worked out again from its worksheet."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction

from analytical import calculations, worksheets
from analytical import services as svc
from analytical.calculations import HALF_EVEN, HALF_UP, PLACES, SIGNIFICANT, Rounding, round_to
from analytical.models import (
    CalculationRule,
    ResultRevision,
    ResultWorksheet,
    WorksheetReading,
)
from core.models import AuditEvent

from .test_workflow import result_for, start, when

pytestmark = pytest.mark.django_db


def text(value, mode, digits, method=HALF_EVEN):
    return calculations.value_text(
        round_to(Decimal(value), mode=mode, digits=digits, method=method)
    )


@pytest.mark.parametrize(
    "value,mode,digits,method,expected",
    [
        # NIST SP 811 B.7.1: a lone final 5 goes to the even digit; anything after it rounds up.
        ("6.9749515", SIGNIFICANT, 3, HALF_EVEN, "6.97"),
        ("6.9749515", SIGNIFICANT, 2, HALF_EVEN, "7.0"),
        ("6.9749515", SIGNIFICANT, 7, HALF_EVEN, "6.974952"),
        ("6.9749505", SIGNIFICANT, 7, HALF_EVEN, "6.974950"),
        # NIST GLP 9 (2019) option A examples, two significant figures.
        ("3.450", SIGNIFICANT, 2, HALF_EVEN, "3.4"),
        ("3.550", SIGNIFICANT, 2, HALF_EVEN, "3.6"),
        ("2.4501", SIGNIFICANT, 2, HALF_EVEN, "2.5"),
        # The same ties under half up.
        ("3.450", SIGNIFICANT, 2, HALF_UP, "3.5"),
        ("-3.450", SIGNIFICANT, 2, HALF_UP, "-3.5"),
        # Digits the rule asks for are kept, and a carry does not add one.
        ("42", SIGNIFICANT, 3, HALF_EVEN, "42.0"),
        ("9.96", SIGNIFICANT, 2, HALF_EVEN, "10"),
        ("99.96", SIGNIFICANT, 3, HALF_EVEN, "100"),
        ("12345", SIGNIFICANT, 3, HALF_EVEN, "12300"),
        ("0.00012345", SIGNIFICANT, 2, HALF_EVEN, "0.00012"),
        ("0", SIGNIFICANT, 3, HALF_EVEN, "0"),
        ("41.666666", PLACES, 0, HALF_EVEN, "42"),
        ("2.5", PLACES, 0, HALF_EVEN, "2"),
        ("2.5", PLACES, 0, HALF_UP, "3"),
        ("-0.04", PLACES, 1, HALF_EVEN, "0.0"),  # never a negative zero
        ("1.005", PLACES, 2, HALF_UP, "1.01"),  # exact decimals: no binary 1.00499999
    ],
)
def test_rounding_is_exact_and_follows_the_named_method(value, mode, digits, method, expected):
    assert text(value, mode, digits, method) == expected


def test_a_rule_the_database_would_hold_with_an_unknown_rounding_method_is_refused(config):
    """work() looks the method up; a row written past the form must not be able to break entry."""
    with pytest.raises(DatabaseError), transaction.atomic():
        CalculationRule.objects.create(
            definition=config["defs"]["Lead"],
            calculation="residue_g",
            rounding_method="up",
            rounding_mode=SIGNIFICANT,
            rounding_digits=3,
            basis="SYNTHETIC TEST: invented rule.",
        )


def test_the_band_is_chosen_before_rounding_so_a_value_can_round_up_to_the_threshold():
    # Two decimal places below 10, two significant figures from 10 up: 9.996 is in the lower
    # band and reads 10.00; 10.0 is in the upper band and reads 10. Documented, not hidden.
    rule = Rounding(HALF_EVEN, PLACES, 2, Decimal("10"), SIGNIFICANT, 2)
    assert calculations.value_text(rule.apply(Decimal("9.996"))[0]) == "10.00"
    assert calculations.value_text(rule.apply(Decimal("10.0"))[0]) == "10"
    assert rule.band(Decimal("-10.5"))[0] == "upper"  # by size, whatever the sign


def test_a_threshold_switches_the_rounding_by_the_size_of_the_unrounded_value():
    # USGS I-3765-85 reports whole numbers below 1,000 mg/L and three figures from there up.
    rule = Rounding(HALF_EVEN, PLACES, 0, Decimal("1000"), SIGNIFICANT, 3)
    low, low_band = rule.apply(Decimal("999.6"))
    high, high_band = rule.apply(Decimal("1234.5"))
    assert (calculations.value_text(low), low_band) == ("1000", "base")
    assert (calculations.value_text(high), high_band) == ("1230", "upper")
    assert Rounding.from_json(rule.as_json("upper")) == rule
    assert rule.describe() == (
        "whole numbers below 1000; 3 significant figures at or above 1000; half to even"
    )


def test_calculations_carry_full_precision_and_never_use_floats():
    residue = calculations.current("residue_g")
    outcome = residue.run(
        {"gross": Decimal("0.1183"), "tare": Decimal("0.1058"), "volume": Decimal("300")},
        Decimal(1),
    )
    # (0.1183 - 0.1058) * 1,000,000 / 300 = 41.666..., carried to 50 significant digits.
    assert str(outcome.value) == "41." + "6" * 47 + "7" and outcome.limit is None
    milligrams = calculations.current("residue_mg").run(
        {"gross": Decimal("118.3"), "tare": Decimal("105.8"), "volume": Decimal("300")},
        Decimal(1),
    )
    assert milligrams.value == outcome.value
    diluted = calculations.current("dilution").run(
        {"reading": Decimal("0.1"), "base_limit": Decimal("0.05")}, Decimal("3")
    )
    assert (diluted.value, diluted.limit) == (Decimal("0.3"), Decimal("0.15"))  # not 0.30000000004
    nothing = calculations.current("dilution").run(
        {"reading": None, "base_limit": Decimal("0.05")}, Decimal("10")
    )
    assert nothing.value is None and nothing.limit == Decimal("0.50")
    # A worksheet names an exact version; an unknown one is reported as missing, not guessed.
    assert calculations.registered("residue_g", 1) is residue
    assert calculations.registered("residue_g", 99) is None


# ------------------------------------------------------------------------ entry


def rule_for(definition, calculation="residue_g", **kw):
    values = {
        "rounding_method": HALF_EVEN,
        "rounding_mode": SIGNIFICANT,
        "rounding_digits": 3,
        "basis": "SYNTHETIC TEST: invented calculation rule; not method guidance.",
        **kw,
    }
    rule = CalculationRule(definition=definition, calculation=calculation, **values)
    rule.full_clean()
    rule.save()
    return rule


def weights(gross="0.1183", tare="0.1058", volume="300", **kw):
    return {
        "supplied_text": "",
        "readings": {"gross": gross, "tare": tare, "volume": volume},
        "reporting_limit_applied": "5",
        "dilution_factor": "1",
        "analysis_started_at": when(),
        **kw,
    }


def diluted(reading="0.0042", base_limit="0.0050", dilution="10", **kw):
    return {
        "supplied_text": "",
        "readings": {"reading": reading, "base_limit": base_limit},
        "reporting_limit_applied": "",
        "dilution_factor": dilution,
        "analysis_started_at": when(),
        **kw,
    }


def errors_of(excinfo):
    return excinfo.value.message_dict


def test_solids_worksheet_keeps_each_reading_and_reports_the_rounded_value(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    result = result_for(batch, "Lead")
    rev = svc.enter_result(actor=analyst, result_id=result.pk, **weights())
    assert (rev.supplied_text, rev.value, rev.kind) == ("41.7", Decimal("41.7"), "detected")
    assert rev.reporting_limit_applied == Decimal("5") and rev.source == "calculated"
    sheet = rev.worksheet
    assert (sheet.calculation, sheet.version) == ("residue_g", 1)
    assert sheet.unrounded_value == Decimal("41.66666666666666666667")
    assert sheet.rounding == {
        "method": "half_even",
        "mode": "significant_figures",
        "digits": 3,
        "threshold": None,
        "upper_mode": "",
        "upper_digits": None,
        "band": "base",
    }
    readings = [(r.key, r.supplied_text, r.value, r.unit) for r in sheet.readings.all()]
    assert readings == [
        ("gross", "0.1183", Decimal("0.1183"), "g"),
        ("tare", "0.1058", Decimal("0.1058"), "g"),
        ("volume", "300", Decimal("300"), "mL"),
    ]
    event = AuditEvent.objects.get(action="result.entered", object_id=str(rev.pk))
    assert event.after["supplied_text"] == "41.7"
    assert event.after["worksheet"]["readings"] == {
        "gross": "0.1183",
        "tare": "0.1058",
        "volume": "300",
    }
    assert event.after["worksheet"]["calculation_version"] == 1
    again = worksheets.reproduce(sheet)
    assert again.available and again.matches and again.value_text == "41.7"


def test_dilution_scales_the_result_and_the_reporting_limit(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], "dilution", rounding_mode=PLACES, rounding_digits=4)
    result = result_for(batch, "Lead")
    with pytest.raises(ValidationError) as below:
        svc.enter_result(actor=analyst, result_id=result.pk, **diluted())
    assert "below the reporting limit applied (0.05)" in errors_of(below)["qualifiers"][0]
    rev = svc.enter_result(actor=analyst, result_id=result.pk, **diluted(reading="0.0125"))
    assert (rev.supplied_text, rev.value) == ("0.1250", Decimal("0.125"))
    assert rev.reporting_limit_applied == Decimal("0.05") and rev.dilution_factor == 10
    assert rev.method_reporting_limit == Decimal("0.0050")
    # A reading of ND is a non-detect at the diluted limit: no value, never zero.
    nd = svc.enter_result(
        actor=analyst, result_id=result.pk, **diluted(reading="nd", reason="Rerun at dilution")
    )
    assert (nd.kind, nd.value, nd.supplied_text) == ("non_detect", None, "ND")
    assert nd.reporting_limit_applied == Decimal("0.05")
    assert nd.worksheet.unrounded_value is None and nd.worksheet.rounding == {}
    assert worksheets.reproduce(nd.worksheet).matches
    assert worksheets.reproduce(rev.worksheet).matches  # the superseded entry keeps its own


def test_a_value_below_the_limit_needs_a_qualifier_or_an_explicit_nd(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    result = result_for(batch, "Lead")
    low = weights(gross="0.1061")  # 0.0003 g over 300 mL: 1.00 mg/L, below the limit of 5
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=result.pk, **low)
    assert "Report as ND" in errors_of(excinfo)["qualifiers"][0]
    qualified = svc.enter_result(
        actor=analyst, result_id=result.pk, **{**low, "qualifier_codes": ["J"]}
    )
    assert (qualified.supplied_text, qualified.value) == ("1.00", Decimal("1"))
    nd = svc.enter_result(
        actor=analyst, result_id=result.pk, **{**low, "report_nd": True, "reason": "Report as ND"}
    )
    assert (nd.kind, nd.value) == ("non_detect", None) and nd.worksheet.reported_as_nd
    assert nd.worksheet.unrounded_value == Decimal("1")  # what was calculated is still on record
    assert worksheets.reproduce(nd.worksheet).matches
    # ND is refused for a value at or above the limit: it would hide a detection.
    with pytest.raises(ValidationError) as hidden:
        svc.enter_result(
            actor=analyst, result_id=result.pk, **weights(report_nd=True, reason="Wrong")
        )
    assert "cannot be reported as ND" in errors_of(hidden)["report_nd"][0]


@pytest.mark.parametrize(
    "entry,field,fragment",
    [
        (weights(supplied_text="41.7"), "supplied_text", "calculated from readings"),
        (weights(volume="0"), "reading_volume", "greater than zero"),
        (weights(gross=""), "reading_gross", "is required"),
        (weights(tare="-0.1"), "reading_tare", "cannot be negative"),
        (weights(gross="0.1 g"), "reading_gross", "not a plain number"),
        (weights(gross="ND"), "reading_gross", "not a plain number"),  # only where ND is allowed
        (weights(dilution_factor="2"), "dilution_factor", "applies no dilution factor"),
        (weights(reporting_limit_applied=""), "reporting_limit_applied", "is required"),
        (weights(analysis_started_at=None), "analysis_started_at", "required"),
        (
            {**weights(), "readings": {"reading": "1", "base_limit": "1"}},
            "supplied_text",
            "do not belong",
        ),
    ],
)
def test_reading_errors_name_the_field_and_save_nothing(config, analyst, entry, field, fragment):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry)
    assert fragment in " ".join(errors_of(excinfo)[field])
    assert not ResultWorksheet.objects.exists() and not WorksheetReading.objects.exists()


def test_several_problems_are_reported_together(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], "dilution")
    bad = diluted(
        reading="", dilution="0", reporting_limit_applied="0.05", analysis_started_at=None
    )
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **bad)
    found = errors_of(excinfo)
    assert set(found) == {
        "reading_reading",
        "dilution_factor",
        "reporting_limit_applied",
        "analysis_started_at",
    }
    assert "sets the reporting limit applied" in found["reporting_limit_applied"][0]
    assert len(found["dilution_factor"]) == 1  # one wording, not the same fault twice


def test_a_value_that_cannot_be_stored_exactly_is_refused_not_rounded_again(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], rounding_digits=15)
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **weights())
    assert "cannot be stored exactly" in " ".join(excinfo.value.messages)


def test_rule_applies_to_manual_entry_only_and_readings_need_a_rule(config, analyst):
    batch = start(config, analyst)
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    # No rule: readings are refused rather than silently dropped.
    with pytest.raises(ValidationError, match="no active calculation"):
        svc.enter_result(actor=analyst, result_id=lead.pk, **weights())
    rule = rule_for(config["defs"]["Lead"])
    # A file import supplies a final value and is kept as supplied, with no worksheet.
    imported = svc.enter_result(
        actor=analyst,
        result_id=lead.pk,
        supplied_text="41.66",
        reporting_limit_applied="5",
        dilution_factor="1",
        analysis_started_at=when(),
        source="import",
        source_detail="synthetic.csv:row 2",
    )
    assert imported.supplied_text == "41.66" and not hasattr(imported, "worksheet")
    # The other analyte of the method has no rule and still takes a typed result.
    typed = svc.enter_result(
        actor=analyst,
        result_id=copper.pk,
        supplied_text="0.0300",
        reporting_limit_applied="0.0100",
        dilution_factor="1",
        analysis_started_at=when(),
    )
    assert typed.value == Decimal("0.03")
    # An inactive rule no longer applies.
    CalculationRule.objects.filter(pk=rule.pk).update(is_active=False)
    plain = svc.enter_result(
        actor=analyst,
        result_id=lead.pk,
        supplied_text="12",
        reporting_limit_applied="5",
        dilution_factor="1",
        analysis_started_at=when(),
        reason="Typed after the rule was withdrawn",
    )
    assert plain.supplied_text == "12" and not hasattr(plain, "worksheet")


def test_a_later_rule_change_does_not_touch_a_recorded_worksheet(config, analyst, admin_user):
    batch = start(config, analyst)
    rule = rule_for(config["defs"]["Lead"])
    result = result_for(batch, "Lead")
    first = svc.enter_result(actor=analyst, result_id=result.pk, **weights())
    rule.rounding_mode, rule.rounding_digits, rule.rounding_method = PLACES, 0, HALF_UP
    svc.save_config(actor=admin_user, instance=rule, reason="Synthetic: SOP revision")
    second = svc.enter_result(
        actor=analyst, result_id=result.pk, **weights(reason="Reweighed after redrying")
    )
    first.refresh_from_db()
    assert (first.supplied_text, second.supplied_text) == ("41.7", "42")
    assert first.worksheet.rounding["digits"] == 3 and second.worksheet.rounding["digits"] == 0
    assert worksheets.reproduce(first.worksheet).matches
    assert worksheets.reproduce(second.worksheet).matches
    event = AuditEvent.objects.get(action="config.calculationrule.updated")
    assert (event.before["rounding_digits"], event.after["rounding_digits"]) == (3, 0)


def test_worksheets_and_readings_cannot_be_changed_or_deleted(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **weights())
    sheet = rev.worksheet
    reading = sheet.readings.get(key="gross")
    reading.supplied_text = "9"
    with pytest.raises(ValidationError):
        reading.save()
    with pytest.raises(ValidationError):
        sheet.delete()
    # The database refuses the same writes when the model hooks are bypassed.
    for rows, change in (
        (WorksheetReading.objects.filter(pk=reading.pk), {"value": Decimal("9")}),
        (ResultWorksheet.objects.filter(pk=sheet.pk), {"unrounded_value": Decimal("9")}),
    ):
        with pytest.raises(DatabaseError, match="cannot be updated or deleted"):
            with transaction.atomic():
                rows.update(**change)
        with pytest.raises(DatabaseError, match="cannot be updated or deleted"):
            with transaction.atomic():
                rows._raw_delete(rows.db)  # straight DELETE, past Django's PROTECT collection
    assert worksheets.reproduce(ResultWorksheet.objects.get(pk=sheet.pk)).matches


def test_only_an_administrator_sets_a_rule_and_only_a_sound_one(config, analyst, admin_user):
    lead = config["defs"]["Lead"]
    rule = CalculationRule(
        definition=lead,
        calculation="residue_g",
        rounding_method=HALF_EVEN,
        rounding_mode=SIGNIFICANT,
        rounding_digits=3,
        basis="SYNTHETIC TEST: invented rule.",
    )
    with pytest.raises(PermissionDenied):
        svc.save_config(actor=analyst, instance=rule)
    saved = svc.save_config(actor=admin_user, instance=rule)
    assert AuditEvent.objects.filter(action="config.calculationrule.created").count() == 1
    with pytest.raises(ValidationError, match="reason"):
        svc.save_config(actor=admin_user, instance=saved)  # a change needs a reason

    def refused(**changes):
        candidate = CalculationRule(
            definition=config["defs"]["Copper"],
            calculation="residue_g",
            rounding_method=HALF_EVEN,
            rounding_mode=SIGNIFICANT,
            rounding_digits=3,
            basis="SYNTHETIC TEST: invented rule.",
        )
        for name, value in changes.items():
            setattr(candidate, name, value)
        with pytest.raises(ValidationError) as excinfo:
            svc.save_config(actor=admin_user, instance=candidate)
        return excinfo.value.message_dict

    assert "calculation" in refused(calculation="unknown")
    assert "rounding_digits" in refused(rounding_digits=0)
    assert "rounding_digits" in refused(rounding_mode=PLACES, rounding_digits=11)
    assert "upper_rounding_mode" in refused(threshold=Decimal("1000"))
    assert "threshold" in refused(upper_rounding_mode=SIGNIFICANT, upper_rounding_digits=3)
    assert "basis" in refused(basis=" ")
    # The solids calculation gives mg/L; a test in another unit cannot use it.
    copper = config["defs"]["Copper"]
    copper.unit = "ug/L"
    copper.save(update_fields=["unit"])
    assert "Units are not converted" in refused()["calculation"][0]


def test_a_worksheet_that_no_longer_reproduces_is_reported_not_hidden(config, analyst):
    """reproduce is the check a reviewer reads: it must say so when a record does not add up."""
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **weights())
    sheet = ResultWorksheet.objects.prefetch_related("readings").get(revision=rev)
    assert worksheets.reproduce(sheet).matches
    # In memory only (the stored rows cannot be changed): a different reading, a different
    # rounding, a version this release lacks, and a record that cannot be worked at all.
    reading = next(r for r in sheet.readings.all() if r.key == "gross")
    reading.value = Decimal("0.1283")
    changed = worksheets.reproduce(sheet)
    assert changed.available and not changed.matches and changed.value_text == "75.0"
    reading.value = Decimal("0.1183")
    sheet.rounding = {**sheet.rounding, "digits": 2}
    assert not worksheets.reproduce(sheet).matches
    sheet.rounding = {}
    broken = worksheets.reproduce(sheet)
    assert broken.available and not broken.matches and broken.value_text == ""
    sheet.version = 99
    assert not worksheets.reproduce(sheet).available
    # The same for a non-detect reading whose limit no longer follows from its readings.
    rule_for(config["defs"]["Copper"], "dilution", rounding_mode=PLACES, rounding_digits=4)
    nd = svc.enter_result(
        actor=analyst,
        result_id=result_for(batch, "Copper").pk,
        **diluted(reading="ND", base_limit="0.0100"),
    )
    nd_sheet = ResultWorksheet.objects.prefetch_related("readings").get(revision=nd)
    again = worksheets.reproduce(nd_sheet)
    assert again.matches and (again.value_text, again.limit_text) == ("ND", "0.1")
    next(r for r in nd_sheet.readings.all() if r.key == "base_limit").value = Decimal("0.02")
    moved = worksheets.reproduce(nd_sheet)
    assert not moved.matches and moved.limit_text == "0.2"
    # A reading too long to store is refused by the service, whatever the caller.
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(
            actor=analyst,
            result_id=result_for(batch, "Lead").pk,
            **weights(gross="0" * 70 + "1", reason="Synthetic"),
        )
    assert "at most 60 characters" in errors_of(excinfo)["reading_gross"][0]


def test_readings_for_another_calculation_and_unusable_rules_are_refused(config, analyst):
    batch = start(config, analyst)
    rule = rule_for(config["defs"]["Lead"])
    result = result_for(batch, "Lead")
    # A page names the calculation it asked readings for. Grams typed, milligrams now set: the
    # reading names match, the units do not, so nothing is worked.
    CalculationRule.objects.filter(pk=rule.pk).update(calculation="residue_mg")
    with pytest.raises(ValidationError, match="it is now Solids by weight, balance in milligrams"):
        svc.enter_result(
            actor=analyst, result_id=result.pk, **weights(calculation_token="residue_g:1")
        )
    assert not ResultRevision.objects.exists()
    current = svc.enter_result(
        actor=analyst,
        result_id=result.pk,
        **weights(gross="118.3", tare="105.8", calculation_token="residue_mg:1"),
    )
    assert current.supplied_text == "41.7" and current.worksheet.calculation == "residue_mg"
    # A rule naming a calculation this release lacks stops entry with one clear message.
    CalculationRule.objects.filter(pk=rule.pk).update(calculation="withdrawn")
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(
            actor=analyst, result_id=result.pk, **weights(supplied_text="41.7", reason="Retry")
        )
    assert list(errors_of(excinfo)) == ["__all__"]
    assert "not part of this release" in errors_of(excinfo)["__all__"][0]
    # A test whose unit no longer fits its calculation is refused too, not worked in the wrong unit.
    CalculationRule.objects.filter(pk=rule.pk).update(calculation="residue_g")
    lead = config["defs"]["Lead"]
    lead.unit = "ug/L"
    lead.save(update_fields=["unit"])
    with pytest.raises(ValidationError, match="the test's unit is now ug/L"):
        svc.enter_result(actor=analyst, result_id=result.pk, **weights(reason="Retry"))
