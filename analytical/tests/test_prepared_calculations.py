"""Preparation factors, dry-weight correction and percent solids: the reading is taken back to
the sample and the reporting limit goes with it, the limit is rounded by the laboratory's rule
and kept before rounding, and a dry-weight result says which percent solids it was worked with."""

from decimal import Decimal, localcontext

import pytest
from django.core.exceptions import ValidationError

from analytical import calculations, worksheets
from analytical import services as svc
from analytical.calculations import HALF_EVEN, PLACES, SIGNIFICANT, CalculationError
from analytical.models import (
    CalculationRule,
    ResultRevision,
    ResultWorksheet,
)
from core.models import AuditEvent

from .test_calculations import errors_of, rule_for
from .test_workflow import result_for, start, when

pytestmark = pytest.mark.django_db

D = Decimal


def run(key, dilution="1", **readings):
    values = {name: None if text is None else D(text) for name, text in readings.items()}
    return calculations.current(key).run(values, D(dilution))


# ------------------------------------------------------------------- arithmetic


def test_a_prepared_reading_is_multiplied_first_and_divided_once():
    # 0.3 x 100 / 3 is exactly 10. Working the factor first (33.33...) would give 9.99...
    by_volume = run(
        "prep_volume", reading="0.3", base_limit="0.05", sample_volume="3", final_volume="100"
    )
    assert by_volume.value == D("10") and str(by_volume.value) == "10.0"
    assert str(by_volume.limit) == "1." + "6" * 48 + "7"
    # EPA 200.7 section 12.3: a 100 mL aliquot brought to 50 mL is a factor of 0.5, and any
    # further dilution of the prepared solution multiplies on top of it.
    halved = run(
        "prep_volume", "4", reading="1.2", base_limit="0.02", sample_volume="100", final_volume="50"
    )
    assert (halved.value, halved.limit) == (D("2.4"), D("0.04"))
    nothing = run(
        "prep_volume", reading=None, base_limit="0.02", sample_volume="100", final_volume="50"
    )
    assert nothing.value is None and nothing.limit == D("0.01")


@pytest.mark.parametrize(
    "key,unit,expected",
    [
        ("solid_mg_l", "mg/kg", "400"),  # mg/L x mL / g = mg/kg
        ("solid_ug_l", "ug/kg", "400"),  # ug/L x mL / g = ug/kg
        ("solid_ug_l_mg_kg", "mg/kg", "0.4"),  # ug/L to mg/kg: 1,000 times smaller
    ],
)
def test_each_unit_pairing_has_its_own_calculation_and_scale(key, unit, expected):
    calculation = calculations.current(key)
    worked = run(key, "2", reading="2.5", base_limit="0.5", final_volume="100", weight="1.25")
    assert calculation.result_unit == unit and worked.value == D(expected)
    assert worked.limit == D(expected) / 5  # the limit takes the same factors as the reading
    assert [r.unit for r in calculation.inputs][:2] == [key.split("_")[1] + "/L"] * 2


def test_dry_weight_correction_is_the_method_6010d_formula():
    # SW-846 6010D section 12.2: C x V / (W x S), C in mg/L, V in L, W in kg wet, S = %/100.
    with localcontext() as context:
        context.prec = 50
        expected = (D("1.84") * D("0.05")) / (D("0.00503") * (D("81.6") / 100))
    readings = dict(reading="1.84", base_limit="0.05", final_volume="50", weight="5.03")
    dry = run("solid_dry_mg_l", percent_solids="81.6", **readings)
    wet = run("solid_mg_l", **readings)
    assert calculations.unrounded(dry.value) == calculations.unrounded(expected)
    assert str(calculations.unrounded(dry.value)) == "22.41453241336295949791"
    # The same reading as weighed is lower by exactly the solids share, and so is its limit.
    assert calculations.unrounded(wet.value) == calculations.unrounded(dry.value * D("0.816"))
    assert calculations.unrounded(dry.limit * D("0.816")) == calculations.unrounded(wet.limit)
    assert calculations.current("solid_dry_mg_l").soil_basis == "dry"
    assert calculations.current("solid_mg_l").soil_basis == ""  # as weighed: the analyst says


def test_percent_solids_and_moisture_are_shares_of_the_sample_as_received():
    weights = dict(tare="21.4035", wet="31.6182", dry="29.7391")
    solids, moisture = run("percent_solids", **weights), run("percent_moisture", **weights)
    # 8.3356 g dry of 10.2147 g as received.
    assert str(calculations.unrounded(solids.value)) == "81.60396291618941329652"
    assert calculations.unrounded(solids.value + moisture.value) == D(100)
    assert solids.limit is None and calculations.current("percent_solids").result_unit == "%"
    # Not the geotechnical water content, which divides by the dry weight (22.5 % here).
    assert str(calculations.unrounded(moisture.value)).startswith("18.396037")
    # Weights that cannot be: no sample, a dried sample lighter than its dish, or heavier than
    # it was as received. Refused for both calculations, never reported as 114 % or -14 %.
    for key in ("percent_solids", "percent_moisture"):
        for field, impossible in (
            ("wet", dict(tare="21.4035", wet="21.4035", dry="21.4035")),
            ("dry", dict(tare="21.4035", wet="31.6182", dry="20.0000")),
            ("dry", dict(tare="21.4035", wet="31.6182", dry="33.0000")),
        ):
            with pytest.raises(CalculationError) as excinfo:
                run(key, **impossible)
            assert excinfo.value.field == field, (key, impossible)
    # The ends of the range are real: a sample that is all water, and one that lost nothing.
    assert run("percent_solids", tare="21.4035", wet="31.6182", dry="21.4035").value == 0
    assert run("percent_moisture", tare="21.4035", wet="31.6182", dry="31.6182").value == 0


def test_units_match_by_name_and_only_the_spelling_of_micro_may_differ():
    micro = calculations.current("solid_ug_l")
    assert micro.gives("ug/kg") and micro.gives("µg/kg") and micro.gives("μg/kg")
    assert not micro.gives("mg/kg") and not micro.gives("ug/Kg") and not micro.gives("ug/g")
    assert calculations.current("prep_volume").gives("anything: the unit of the test")
    # What the entry service relies on: a calculation that sets the limit starts from the
    # reading named base_limit, and one that rounds its limit sets it.
    for calculation in calculations.CURRENT.values():
        keys = [r.key for r in calculation.inputs]
        assert ("base_limit" in keys) == calculation.sets_limit, calculation.key
        assert calculation.sets_limit or not calculation.rounds_limit, calculation.key
        assert len(calculation.title) <= 120 and len(calculation.key) <= 40
        assert bool(calculation.soil_basis) == bool(calculation.soil_basis_reason)
    # Registering a calculation never removes one already registered.
    assert set(calculations.CURRENT) >= {"dilution", "residue_g", "residue_mg"}


# ------------------------------------------------------------------------ entry


def prepared(reading="0.842", base_limit="0.0050", sample_volume="30", final_volume="50", **kw):
    return {
        "supplied_text": "",
        "readings": {
            "reading": reading,
            "base_limit": base_limit,
            "sample_volume": sample_volume,
            "final_volume": final_volume,
        },
        "reporting_limit_applied": "",
        "dilution_factor": "2",
        "analysis_started_at": when(),
        **kw,
    }


def test_preparation_by_volume_scales_and_rounds_the_result_and_the_limit(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], "prep_volume")
    result = result_for(batch, "Lead")
    rev = svc.enter_result(actor=analyst, result_id=result.pk, **prepared())
    # 0.842 x 50 x 2 / 30 = 2.80666...; the limit 0.005 x 50 x 2 / 30 = 0.01666...
    assert (rev.supplied_text, rev.value, rev.source) == ("2.81", D("2.81"), "calculated")
    assert rev.reporting_limit_applied == D("0.0167") and rev.dilution_factor == 2
    assert rev.method_reporting_limit == D("0.0050")  # the catalog limit, for comparison
    sheet = rev.worksheet
    assert sheet.unrounded_value == D("2.80666666666666666667")
    assert sheet.unrounded_limit == D("0.01666666666666666667")
    assert (sheet.rounding["band"], sheet.rounding["limit_band"]) == ("base", "base")
    stored = [(r.key, r.supplied_text, r.unit) for r in sheet.readings.all()]
    assert stored == [
        ("reading", "0.842", "mg/L"),
        ("base_limit", "0.0050", "mg/L"),
        ("sample_volume", "30", "mL"),
        ("final_volume", "50", "mL"),
    ]
    event = AuditEvent.objects.get(action="result.entered", object_id=str(rev.pk))
    assert event.after["reporting_limit_applied"] == "0.0167"
    assert event.after["worksheet"]["unrounded_limit"] == "0.01666666666666666667"
    again = worksheets.reproduce(sheet)
    assert again.matches and (again.value_text, again.limit_text) == ("2.81", "0.0167")
    # A reading of ND is a non-detect at the limit worked out, which is still rounded.
    nd = svc.enter_result(
        actor=analyst, result_id=result.pk, **prepared(reading="ND", reason="Rerun")
    )
    assert (nd.kind, nd.value, nd.reporting_limit_applied) == ("non_detect", None, D("0.0167"))
    assert nd.worksheet.unrounded_value is None
    assert nd.worksheet.rounding["limit_band"] == "base" and "band" not in nd.worksheet.rounding
    assert worksheets.reproduce(nd.worksheet).matches


def test_the_limit_worked_out_is_checked_like_the_value(config, analyst):
    batch = start(config, analyst)
    rule = rule_for(config["defs"]["Lead"], "prep_volume")
    result = result_for(batch, "Lead")

    def refused(**entry):
        with pytest.raises(ValidationError) as excinfo:
            svc.enter_result(actor=analyst, result_id=result.pk, **prepared(**entry))
        return errors_of(excinfo)

    typed = refused(reporting_limit_applied="0.02")["reporting_limit_applied"][0]
    assert "give the reporting limit in the solution with the readings" in typed
    assert "greater than zero" in refused(final_volume="0")["reading_final_volume"][0]
    assert "is required" in refused(base_limit="")["reading_base_limit"][0]
    # A value under the limit that was worked out needs a qualifier or an explicit ND.
    assert "below the reporting limit applied (0.0167)" in refused(reading="0.004")["qualifiers"][0]
    # A rounding that takes the limit to zero is refused: a result cannot have a zero limit.
    CalculationRule.objects.filter(pk=rule.pk).update(rounding_mode=PLACES, rounding_digits=0)
    zero = refused()["reading_base_limit"][0]
    assert "takes to zero" in zero and "whole numbers" in zero
    assert not ResultRevision.objects.exists() and not ResultWorksheet.objects.exists()


def test_the_limit_is_rounded_by_its_own_size_not_by_the_size_of_the_value(config, analyst):
    """Two decimal places below 1, three significant figures from 1 up: the value 2.80666 is in
    the upper range and the limit 0.01666 in the lower one."""
    batch = start(config, analyst)
    rule_for(
        config["defs"]["Lead"],
        "prep_volume",
        rounding_mode=PLACES,
        rounding_digits=2,
        threshold=D("1"),
        upper_rounding_mode=SIGNIFICANT,
        upper_rounding_digits=3,
    )
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **prepared())
    assert (rev.supplied_text, rev.reporting_limit_applied) == ("2.81", D("0.02"))
    sheet = rev.worksheet
    assert (sheet.rounding["band"], sheet.rounding["limit_band"]) == ("upper", "base")
    assert worksheets.reproduce(sheet).matches


def test_a_worksheet_whose_limit_no_longer_follows_is_reported(config, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], "prep_volume")
    rev = svc.enter_result(
        actor=analyst, result_id=result_for(batch, "Lead").pk, **prepared(reading="ND")
    )
    sheet = ResultWorksheet.objects.prefetch_related("readings").get(revision=rev)
    assert worksheets.reproduce(sheet).matches
    # In memory only (stored rows cannot be changed): another volume, another limit rounding.
    volume = next(r for r in sheet.readings.all() if r.key == "final_volume")
    volume.value = D("100")
    moved = worksheets.reproduce(sheet)
    assert moved.available and not moved.matches and moved.limit_text == "0.0333"
    volume.value = D("50")
    sheet.rounding = {**sheet.rounding, "digits": 2}
    assert not worksheets.reproduce(sheet).matches
    sheet.rounding = {}
    assert not worksheets.reproduce(sheet).matches  # no rounding on record: cannot be worked
    # Each part of the limit's record is checked on its own: the range it was rounded in and
    # the limit before rounding.
    sheet = ResultWorksheet.objects.prefetch_related("readings").get(revision=rev)
    recorded = dict(sheet.rounding)
    sheet.rounding = {**recorded, "limit_band": "upper"}
    assert not worksheets.reproduce(sheet).matches
    sheet.rounding = recorded
    assert worksheets.reproduce(sheet).matches
    sheet.unrounded_limit = D("0.0166")
    assert not worksheets.reproduce(sheet).matches


# ------------------------------------------------------------------------- soil


SOURCE = "VR-DEMO-S1 percent solids, synthetic batch"


def extract(reading="1.84", **kw):
    return {
        "supplied_text": "",
        "readings": {
            "reading": reading,
            "base_limit": "0.05",
            "final_volume": "50",
            "weight": "5.03",
        },
        "reporting_limit_applied": "",
        "dilution_factor": "1",
        "analysis_started_at": when(),
        "result_basis": "dry",
        "percent_solids": "81.6",
        "solids_reference": SOURCE,
        **kw,
    }


def test_a_dry_weight_result_keeps_the_percent_solids_it_was_worked_with(soil, analyst):
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    result = soil["results"]["Nitrate"]
    rev = svc.enter_result(actor=analyst, result_id=result.pk, **extract())
    # 1.84 x 50 / (5.03 x 0.816) = 22.4145... mg/kg; the limit 0.05 x 50 / 4.10448 = 0.60909...
    assert (rev.supplied_text, rev.unit, rev.result_basis) == ("22.4", "mg/kg", "dry")
    assert rev.reporting_limit_applied == D("0.609")
    assert (rev.percent_solids, rev.solids_reference) == (D("81.6"), SOURCE)
    sheet = rev.worksheet
    stored = [(r.key, r.supplied_text, r.unit) for r in sheet.readings.all()]
    assert stored[-2:] == [("weight", "5.03", "g"), ("percent_solids", "81.6", "%")]
    assert sheet.unrounded_limit == D("0.60909055471094998636")
    assert "6010D" in sheet.source and "percent solids ÷ 100" in sheet.formula
    event = AuditEvent.objects.get(action="result.entered", object_id=str(rev.pk))
    assert event.after["worksheet"]["readings"]["percent_solids"] == "81.6"
    assert event.after["percent_solids"] == "81.6" and event.after["result_basis"] == "dry"
    again = worksheets.reproduce(sheet)
    assert again.matches and (again.value_text, again.limit_text) == ("22.4", "0.609")
    # A drier sample gives a lower dry-weight result from the same reading: the correction is
    # worked from the percent solids on this entry, never from an earlier one.
    drier = svc.enter_result(
        actor=analyst,
        result_id=result.pk,
        **extract(percent_solids="92.0", reason="Percent solids redetermined"),
    )
    assert (drier.supplied_text, drier.reporting_limit_applied) == ("19.9", D("0.540"))
    assert worksheets.reproduce(sheet).matches and worksheets.reproduce(drier.worksheet).matches
    # Working it again also checks that the result still says what its worksheet says. In
    # memory only: a result relabelled wet, or carrying another percent solids than was applied.
    sheet = ResultWorksheet.objects.prefetch_related("readings").get(revision=rev)
    sheet.revision.result_basis = "wet"
    assert not worksheets.reproduce(sheet).matches
    sheet.revision.result_basis = "dry"
    sheet.revision.percent_solids = D("50")
    assert not worksheets.reproduce(sheet).matches
    sheet.revision.percent_solids = D("81.6")
    assert worksheets.reproduce(sheet).matches


@pytest.mark.parametrize(
    "change,field,fragment",
    [
        ({"result_basis": "wet"}, "result_basis", "Choose Dry weight"),
        ({"result_basis": ""}, "result_basis", "Choose Dry weight"),
        ({"percent_solids": ""}, "percent_solids", "required by this calculation"),
        ({"percent_solids": None}, "percent_solids", "required by this calculation"),
        ({"percent_solids": "0"}, "percent_solids", "greater than zero"),
        ({"percent_solids": "101"}, "percent_solids", "between 0 and 100"),
        ({"percent_solids": 81.6}, "percent_solids", "not a float"),
        ({"percent_solids": "81.6 %"}, "percent_solids", "not a plain number"),
        ({"solids_reference": ""}, "solids_reference", "source or measurement reference"),
        ({"dilution_factor": "0"}, "dilution_factor", "greater than zero"),
    ],
)
def test_a_dry_weight_result_is_refused_without_its_basis_solids_and_source(
    soil, analyst, change, field, fragment
):
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(
            actor=analyst, result_id=soil["results"]["Nitrate"].pk, **{**extract(), **change}
        )
    assert fragment in " ".join(errors_of(excinfo)[field])
    assert not ResultRevision.objects.exists() and not ResultWorksheet.objects.exists()


def test_readings_too_large_to_work_are_refused_not_a_server_error(soil, analyst):
    """Every reading here can be stored, but together they give a number of 60 digits."""
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    huge, tiny = "9" * 14, "0.0000000001"
    entry = extract(percent_solids=tiny, dilution_factor="999999999.9999")
    entry["readings"] = {
        "reading": huge,
        "base_limit": huge,
        "final_volume": huge,
        "weight": tiny,
    }
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=soil["results"]["Nitrate"].pk, **entry)
    assert "too large to round and store" in errors_of(excinfo)["__all__"][0]
    assert not ResultRevision.objects.exists() and not ResultWorksheet.objects.exists()


def test_percent_solids_is_read_from_the_entry_and_not_a_second_time(soil, analyst):
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    entry = extract()
    entry["readings"] = {**entry["readings"], "percent_solids": "50"}
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=soil["results"]["Nitrate"].pk, **entry)
    assert "do not belong" in errors_of(excinfo)["supplied_text"][0]


def test_a_result_as_weighed_takes_the_basis_the_analyst_declares(soil, analyst):
    rule_for(soil["defs"]["Zinc"], "solid_ug_l")
    result = soil["results"]["Zinc"]
    plain = {"percent_solids": None, "solids_reference": ""}
    as_received = svc.enter_result(
        actor=analyst, result_id=result.pk, **extract(result_basis="wet", **plain)
    )
    # 1.84 x 50 / 5.03 = 18.2902... ug/kg: no solids correction, whatever the entry records.
    assert (as_received.supplied_text, as_received.result_basis) == ("18.3", "wet")
    assert as_received.reporting_limit_applied == D("0.497")
    # Percent solids recorded beside it stays what it was: supplied information, not applied.
    noted = svc.enter_result(
        actor=analyst, result_id=result.pk, **extract(result_basis="wet", reason="Solids noted")
    )
    assert noted.supplied_text == "18.3" and noted.percent_solids == D("81.6")
    assert "percent_solids" not in {r.key for r in noted.worksheet.readings.all()}
    # A portion dried before weighing (EPA 200.7) is a dry-weight result with nothing to correct.
    dried = svc.enter_result(
        actor=analyst,
        result_id=result.pk,
        **extract(result_basis="dry", reason="Dried portion", **plain),
    )
    assert (dried.supplied_text, dried.result_basis) == ("18.3", "dry")
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(
            actor=analyst,
            result_id=result.pk,
            **extract(result_basis="", reason="No basis", **plain),
        )
    assert "Choose wet weight or dry weight" in errors_of(excinfo)["result_basis"][0]


def weighed(tare="21.4035", wet="31.6182", dry="29.7391", **kw):
    return {
        "supplied_text": "",
        "readings": {"tare": tare, "wet": wet, "dry": dry},
        "reporting_limit_applied": "0.1",
        "dilution_factor": "1",
        "analysis_started_at": when(),
        "result_basis": "wet",
        **kw,
    }


def test_percent_solids_and_moisture_are_worked_from_the_three_weights(soil, analyst):
    rule = rule_for(
        soil["defs"]["Solids"], "percent_solids", rounding_mode=PLACES, rounding_digits=1
    )
    result = soil["results"]["Solids"]
    rev = svc.enter_result(actor=analyst, result_id=result.pk, **weighed())
    assert (rev.supplied_text, rev.unit, rev.result_basis) == ("81.6", "%", "wet")
    assert rev.reporting_limit_applied == D("0.1") and rev.worksheet.unrounded_limit is None
    assert "limit_band" not in rev.worksheet.rounding
    assert worksheets.reproduce(rev.worksheet).matches
    for change, field, fragment in (
        ({"result_basis": "dry"}, "result_basis", "Choose Wet weight"),
        (
            {"readings": {"tare": "31.7", "wet": "31.6182", "dry": "29.7391"}},
            "reading_wet",
            "more than the dish alone",
        ),
        (
            {"readings": {"tare": "21.4035", "wet": "31.6182", "dry": ""}},
            "reading_dry",
            "is required",
        ),
        ({"dilution_factor": "2"}, "dilution_factor", "applies no dilution factor"),
        (
            {"readings": {"tare": "21.4035", "wet": "31.6182", "dry": "33.0"}},
            "reading_dry",
            "cannot weigh more than with the sample as received",
        ),
        (
            {"readings": {"tare": "21.4035", "wet": "31.6182", "dry": "20.0"}},
            "reading_dry",
            "cannot weigh less than the dish alone",
        ),
    ):
        with pytest.raises(ValidationError) as excinfo:
            svc.enter_result(
                actor=analyst, result_id=result.pk, **{**weighed(reason="Retry"), **change}
            )
        assert fragment in " ".join(errors_of(excinfo)[field])
    CalculationRule.objects.filter(pk=rule.pk).update(calculation="percent_moisture")
    moisture = svc.enter_result(
        actor=analyst, result_id=result.pk, **weighed(reason="Reported as moisture")
    )
    assert moisture.supplied_text == "18.4" and worksheets.reproduce(moisture.worksheet).matches


def test_a_rule_is_refused_for_a_test_its_calculation_cannot_serve(soil, config, admin_user):
    def refused(definition, calculation):
        candidate = CalculationRule(
            definition=definition,
            calculation=calculation,
            rounding_method=HALF_EVEN,
            rounding_mode=SIGNIFICANT,
            rounding_digits=3,
            basis="SYNTHETIC TEST: invented rule.",
        )
        with pytest.raises(ValidationError) as excinfo:
            svc.save_config(actor=admin_user, instance=candidate)
        return excinfo.value.message_dict["calculation"][0]

    nitrate, zinc, lead = soil["defs"]["Nitrate"], soil["defs"]["Zinc"], config["defs"]["Lead"]
    assert "Units are not converted" in refused(zinc, "solid_dry_mg_l")  # ug/kg, not mg/kg
    assert "Units are not converted" in refused(nitrate, "percent_solids")
    # A water test in mg/kg (a sludge, say) has no percent solids on its entries to correct with.
    lead.unit = "mg/kg"
    lead.save(update_fields=["unit"])
    assert "the test's matrix is water" in refused(lead, "solid_dry_mg_l")
    for definition, calculation in ((lead, "solid_mg_l"), (zinc, "solid_dry_ug_l")):
        rule = rule_for(definition, calculation)
        assert CalculationRule.objects.filter(pk=rule.pk, is_active=True).exists()
    # A preparation by volume takes a volume of sample: not for a soil test, whatever its unit.
    assert "this is a soil test" in refused(nitrate, "prep_volume")
    # The same unit spelled with the micro sign is the same unit.
    zinc.unit = "µg/kg"
    zinc.save(update_fields=["unit"])
    CalculationRule.objects.get(definition=zinc).full_clean()


def test_a_dry_weight_rule_left_on_a_test_that_is_not_soil_stops_entry(config, analyst):
    """Written past the form: entry says what is wrong instead of working a wrong value."""
    batch = start(config, analyst)
    lead = config["defs"]["Lead"]
    lead.unit = "mg/kg"
    lead.save(update_fields=["unit"])
    rule = rule_for(lead, "solid_mg_l")
    CalculationRule.objects.filter(pk=rule.pk).update(calculation="solid_dry_mg_l")
    entry = {**extract(), "result_basis": "", "percent_solids": None, "solids_reference": ""}
    with pytest.raises(ValidationError) as excinfo:
        svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry)
    assert "not a soil test" in errors_of(excinfo)["__all__"][0]
