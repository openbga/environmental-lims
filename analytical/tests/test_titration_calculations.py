"""EPA 310.1 and 130.2 arithmetic through the existing immutable worksheet workflow.

Examples are synthetic. No test fixture represents laboratory acceptance or current approval.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction

from analytical import calculations, worksheets
from analytical import services as svc
from analytical.calculations import CalculationError
from analytical.models import CalculationRule, ResultRevision, ResultWorksheet, WorksheetReading
from core.models import AuditEvent

from .test_calculations import errors_of, rule_for
from .test_workflow import result_for, run_qc, start, when

pytestmark = pytest.mark.django_db
D = Decimal
KEYS = ("alkalinity_titration", "alkalinity_low_titration", "hardness_edta_titration")
NORMAL = dict(initial="1.20", final="3.70", sample_volume="60", normality="0.0200", final_ph="4.50")
LOW = dict(
    initial="0.20",
    first_endpoint="1.10",
    final="1.40",
    sample_volume="100",
    normality="0.0201",
    first_ph="4.50",
    final_ph="4.20",
)
HARDNESS = dict(initial="0.15", final="5.30", sample_volume="25", normality="0.0198")
EXAMPLES = dict(zip(KEYS, (NORMAL, LOW, HARDNESS), strict=True))


def configure(config, key, **rounding):
    """Reuse the workflow's synthetic definitions, explicitly changing their result unit."""
    definition = config["defs"]["Lead"]
    definition.unit = "mg/L as CaCO3"
    definition.save(update_fields=["unit"])
    return rule_for(definition, key, **rounding)


def entry(key="alkalinity_titration", **changes):
    return {
        "supplied_text": "",
        "readings": {**EXAMPLES[key], **changes},
        "reporting_limit_applied": "1",
        "dilution_factor": "1",
        "analysis_started_at": when(),
    }


def run(key, **changes):
    return calculations.current(key).run(
        {name: D(value) for name, value in {**EXAMPLES[key], **changes}.items()}, D(1)
    )


@pytest.mark.parametrize(
    "key,expected", list(zip(KEYS, ("41.66666666666666666667", "6.03", "203.94"), strict=True))
)
def test_method_equations_use_actual_normality_and_total_titrant(key, expected):
    calculation = calculations.current(key)
    outcome = run(key)
    assert calculations.unrounded(outcome.value) == D(expected)
    assert outcome.limit is None
    assert calculation.result_unit == "mg/L as CaCO3" and calculation.liquid_only
    assert not calculation.uses_dilution and not calculation.sets_limit
    assert calculation.version == 1 and calculations.registered(key, 1) is calculation
    assert calculation.gives("mg/L as CaCO3") and not calculation.gives("mg/L")
    # No intermediate repeating quotient: 0.3 x 0.0002 x 50,000 / 3 is exactly one.
    changes = dict(initial="0.10", final="0.40", normality="0.0002", sample_volume="3")
    if key == "alkalinity_low_titration":
        changes["final"] = "0.70"
        changes["first_endpoint"] = "0.55"  # 2 x 0.45 - 0.60 = 0.30.
    assert run(key, **changes).value == D(1)


@pytest.mark.parametrize("key", KEYS)
def test_rounding_and_observation_provenance_survive_reproduction(config, analyst, key):
    configure(config, key)
    batch = start(config, analyst)
    rev = svc.enter_result(actor=analyst, result_id=result_for(batch, "Lead").pk, **entry(key))
    assert rev.supplied_text == dict(zip(KEYS, ("41.7", "6.03", "204"), strict=True))[key]
    assert rev.unit == "mg/L as CaCO3" and rev.source == "calculated"
    assert rev.reporting_limit_applied == 1 and rev.dilution_factor == 1
    sheet = rev.worksheet
    assert (sheet.calculation, sheet.version) == (key, 1)
    stored = {r.key: (r.supplied_text, r.value, r.unit) for r in sheet.readings.all()}
    assert {name: value[0] for name, value in stored.items()} == EXAMPLES[key]
    assert stored["normality"] == (EXAMPLES[key]["normality"], D(EXAMPLES[key]["normality"]), "N")
    assert stored["initial"][2] == stored["final"][2] == stored["sample_volume"][2] == "mL"
    assert stored.get("final_ph", (None, None, "pH"))[2] == "pH"
    assert "2026-11-15" in sheet.source and "current regulatory approval" in sheet.source
    assert sheet.rule_basis.startswith("SYNTHETIC TEST")
    event = AuditEvent.objects.get(action="result.entered", object_id=str(rev.pk))
    assert event.after["worksheet"]["readings"] == EXAMPLES[key]
    assert event.after["worksheet"]["calculation_version"] == 1
    assert worksheets.reproduce(sheet).matches


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize(
    "field,value,fragment",
    [
        ("initial", "-0.1", "cannot be negative"),
        ("sample_volume", "0", "greater than zero"),
        ("normality", "0", "greater than zero"),
        ("normality", "-0.02", "greater than zero"),
        ("final", "0.1", "less than the initial"),
    ],
)
def test_impossible_readings_name_the_field_in_arithmetic_and_entry(
    config, analyst, key, field, value, fragment
):
    with pytest.raises(CalculationError) as arithmetic:
        run(key, **{field: value})
    assert arithmetic.value.field == field
    configure(config, key)
    batch = start(config, analyst)
    with pytest.raises(ValidationError) as invalid:
        svc.enter_result(
            actor=analyst, result_id=result_for(batch, "Lead").pk, **entry(key, **{field: value})
        )
    assert fragment in errors_of(invalid)[f"reading_{field}"][0]
    assert not ResultRevision.objects.exists() and not ResultWorksheet.objects.exists()
    assert (
        not WorksheetReading.objects.exists()
        and not AuditEvent.objects.filter(action="result.entered").exists()
    )


@pytest.mark.parametrize(
    "key,changes,field",
    [
        ("alkalinity_titration", {"final_ph": "3.9"}, "final_ph"),
        ("alkalinity_titration", {"final_ph": "4.49"}, "final_ph"),
        ("alkalinity_low_titration", {"first_endpoint": "0.1"}, "first_endpoint"),
        ("alkalinity_low_titration", {"final": "1.10"}, "final"),
        ("alkalinity_low_titration", {"first_ph": "4.2", "final_ph": "3.9"}, "first_ph"),
        ("alkalinity_low_titration", {"first_ph": "4.8", "final_ph": "4.5"}, "first_ph"),
        ("alkalinity_low_titration", {"final_ph": "4.19"}, "final_ph"),
    ],
)
def test_the_equation_requires_its_sourced_endpoint_pair(config, analyst, key, changes, field):
    configure(config, key)
    batch = start(config, analyst)
    with pytest.raises(ValidationError) as invalid:
        svc.enter_result(
            actor=analyst, result_id=result_for(batch, "Lead").pk, **entry(key, **changes)
        )
    assert set(errors_of(invalid)) == {f"reading_{field}"}
    assert not ResultWorksheet.objects.exists()


def test_low_endpoint_boundaries_and_negative_equation_are_not_new_acceptance_policy():
    for first, final in (("4.3", "4.0"), ("4.7", "4.4")):
        assert run("alkalinity_low_titration", first_ph=first, final_ph=final).value == D("6.03")
    assert run("alkalinity_low_titration", first_endpoint="0.30", final="0.50").value == D("-1.005")
    assert run("alkalinity_titration", final="1.20").value == 0
    assert run("hardness_edta_titration", final="0.15").value == 0


def test_unit_matrix_and_dilution_are_checked_on_the_server(config, soil, analyst):
    for key in KEYS:
        with pytest.raises(ValidationError, match="Units are not converted"):
            rule_for(config["defs"]["Lead"], key)
    configure(config, "alkalinity_titration")
    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    with pytest.raises(ValidationError) as invalid:
        svc.enter_result(actor=analyst, result_id=result.pk, **{**entry(), "dilution_factor": "2"})
    assert "applies no dilution factor" in errors_of(invalid)["dilution_factor"][0]
    definition = config["defs"]["Lead"]
    definition.unit = "mg/L"
    definition.save(update_fields=["unit"])
    with pytest.raises(ValidationError, match="test's unit is now mg/L"):
        svc.enter_result(actor=analyst, result_id=result.pk, **entry())
    solid = soil["defs"]["Nitrate"]
    solid.unit = "mg/L as CaCO3"
    solid.save(update_fields=["unit"])
    with pytest.raises(ValidationError, match="takes a volume of sample"):
        rule_for(solid, "alkalinity_titration")
    # A rule written past full_clean is also refused during entry.
    CalculationRule.objects.create(
        definition=solid,
        calculation="alkalinity_titration",
        rounding_method="half_even",
        rounding_mode="significant_figures",
        rounding_digits=3,
        basis="SYNTHETIC TEST: bypass validation",
    )
    with pytest.raises(ValidationError, match="this is a soil test"):
        svc.enter_result(actor=analyst, result_id=soil["results"]["Nitrate"].pk, **entry())
    assert not ResultRevision.objects.exists()


def test_input_storage_and_result_overflow_remain_exact(config, analyst):
    configure(config, "hardness_edta_titration")
    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    for changes, field, fragment in (
        ({"normality": "0.00000000001"}, "reading_normality", "would be rounded"),
        ({"normality": 0.02}, "reading_normality", "not a float"),
        ({"initial": "0" * 61}, "reading_initial", "at most 60 characters"),
        (
            {
                "sample_volume": "0.0000000001",
                "normality": "99999999999999",
                "final": "99999999999999",
            },
            "__all__",
            "cannot be stored exactly",
        ),
    ):
        with pytest.raises(ValidationError) as invalid:
            svc.enter_result(
                actor=analyst, result_id=result.pk, **entry("hardness_edta_titration", **changes)
            )
        assert fragment in errors_of(invalid)[field][0]
    assert not ResultWorksheet.objects.exists()


def test_correction_and_independent_review_keep_both_worksheets(config, both, reviewer):
    rule = configure(config, "alkalinity_titration")
    batch = start(config, both)
    run_qc(config, both, batch)
    result = result_for(batch, "Lead")
    first = svc.enter_result(actor=both, result_id=result.pk, **entry())
    svc.submit_result(actor=both, revision_id=first.pk)
    with pytest.raises(PermissionDenied):
        svc.approve_result(actor=both, revision_id=first.pk)
    svc.return_result(actor=reviewer, revision_id=first.pk, reason="Synthetic: recheck endpoint")
    with pytest.raises(ValidationError, match="reason"):
        svc.enter_result(actor=both, result_id=result.pk, **entry(final="3.80"))
    second = svc.enter_result(
        actor=both,
        result_id=result.pk,
        **{**entry(final="3.80"), "reason": "Synthetic: corrected observation"},
    )
    svc.submit_result(actor=both, revision_id=second.pk)
    approved = svc.approve_result(actor=reviewer, revision_id=second.pk)
    assert approved.reviewed_by == reviewer and approved.supplied_text == "43.3"
    first.refresh_from_db()
    assert first.status == "returned" and first.supplied_text == "41.7"
    # Later configuration cannot rewrite either version's recorded rule or observations.
    CalculationRule.objects.filter(pk=rule.pk).update(rounding_digits=2)
    assert all(worksheets.reproduce(rev.worksheet).matches for rev in (first, second))
    reading = first.worksheet.readings.get(key="final")
    with pytest.raises(DatabaseError, match="cannot be updated or deleted"), transaction.atomic():
        WorksheetReading.objects.filter(pk=reading.pk).update(value=D("9"))
    assert AuditEvent.objects.filter(action="result.entered").count() == 1
    assert AuditEvent.objects.filter(action="result.corrected").count() == 1
    assert AuditEvent.objects.filter(action="result.approved").count() == 1


def test_audit_failure_rolls_back_the_titration_and_all_original_readings(config, analyst):
    configure(config, "alkalinity_low_titration")
    batch = start(config, analyst)
    with patch(
        "analytical.services.record_event", side_effect=RuntimeError("Synthetic audit failure")
    ):
        with pytest.raises(RuntimeError, match="Synthetic audit failure"):
            svc.enter_result(
                actor=analyst,
                result_id=result_for(batch, "Lead").pk,
                **entry("alkalinity_low_titration"),
            )
    assert not ResultRevision.objects.exists() and not ResultWorksheet.objects.exists()
    assert not WorksheetReading.objects.exists()
