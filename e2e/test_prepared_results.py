"""A soil result worked from bench readings to a dry-weight result with its reporting limit.

An administrator sets the dry-weight calculation for a synthetic mg/kg test through the shared
form dialog, which refuses it for a test in another unit. The analyst weighs a dish for percent
solids (81.6 %), then enters the readings of a prepared nitrate solution: saving without the
percent solids is refused and keeps what was typed; with it, the result is 22.4 mg/kg (three
significant figures, half to even) with a reporting limit applied of 0.609 mg/kg, worked from the
limit in the solution and rounded by the same rule. The result page shows the worksheet and works
it again; an independent reviewer approves it from the review grid.

The test, its method and its numbers are invented; nothing here states a method or a laboratory
policy.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from helpers import field, local_input, native_select, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

BASIS = "SYNTHETIC E2E: invented rule basis; not method guidance."
SOURCE = "SYNTHETIC E2E: percent solids of this sample, solids batch"
SOLIDS_LABEL = "Percent solids by weight (version 1)"
DRY_LABEL = "Prepared solid corrected to dry weight, mg/L in solution to mg/kg (version 1)"
WEIGHTS = {
    "Dish alone": "21.4035",
    "Dish with the sample as received": "31.6182",
    "Dish with the dried sample": "29.7391",
}
READINGS = {
    "Instrument reading": "1.84",
    "Reporting limit in the solution": "0.05",
    "Final volume": "50",
    "Sample weight as received": "5.03",
}


def soil_batches():
    """A synthetic soil method with a percent solids test and a nitrate test, one received soil
    sample requesting both, and a batch of each. The method has no QC requirements."""
    from django.utils import timezone

    from accounts.models import User
    from analytical import services
    from analytical.models import PreservationRule
    from catalog.models import Analyte, Method, TestDefinition
    from receipt.models import Project, RequestedTest
    from receipt.services import receive_sample

    definitions = {}
    batches = {}
    analyst = User.objects.get(username="demo_analyst")
    for name, code, unit, limit in (
        ("Percent solids (synthetic)", "E2E-SOLIDS", "%", "0.1"),
        ("Nitrate (synthetic soil)", "E2E-SOIL-NO3", "mg/kg", "0.5"),
    ):
        method = Method.objects.create(code=code, version="r1", title=f"Synthetic {name}")
        definitions[unit] = TestDefinition.objects.create(
            method=method,
            analyte=Analyte.objects.create(name=name),
            matrix="soil",
            unit=unit,
            reporting_limit=Decimal(limit),
        )
        PreservationRule.objects.create(
            definition=definitions[unit],
            temperature_mode="not_required",
            basis="SYNTHETIC E2E: no temperature requirement; not method guidance.",
        )
    now = timezone.now()
    sample = receive_sample(
        actor=User.objects.get(username="demo_custodian"),
        project=Project.objects.get(name="Test Creek Outfall 001"),
        coc_reference="E2E-DRY-1",
        tests=list(definitions.values()),
        container_description="Synthetic 250 mL jar",
        container_condition="Intact",
        relinquished_by="Example Courier",
        client_sample_id="E2E-DRY-1-S1",
        matrix="soil",
        collection_type="grab",
        collected_at=now - timedelta(hours=3),
        collection_started_at=None,
        collector_name="Example Collector",
        collection_location="Synthetic drying bed 1",
        received_at=now - timedelta(hours=1),
        cooler_temperature=Decimal("4.200"),
        seal_condition="Intact",
        preservation_check="Recorded for demonstration only",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Receipt bench",
    )
    for unit, definition in definitions.items():
        batch = services.create_batch(actor=analyst, method=definition.method, matrix="soil")
        requested = RequestedTest.objects.get(sample=sample, definition=definition)
        services.add_tests_to_batch(
            actor=analyst, batch_id=batch.pk, requested_test_ids=[requested.pk]
        )
        batches[unit] = batch
    return batches, sample, definitions


def entry_row(page: Page, sample):
    return page.locator("tr[id^=row-]").filter(has_text=sample.lab_id)


def save_grid(page: Page):
    page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered")).click()


def test_dry_weight_result_from_readings_to_approved_worksheet(page: Page, demo: Lab):
    from accounts.models import User
    from analytical.models import CalculationRule, ResultRevision
    from analytical.services import save_config

    batches, sample, definitions = soil_batches()
    solids, nitrate = definitions["%"], definitions["mg/kg"]
    admin = User.objects.get(username="demo_admin")
    save_config(
        actor=admin,
        instance=CalculationRule(
            definition=solids,
            calculation="percent_solids",
            rounding_mode="decimal_places",
            rounding_digits=1,
            rounding_method="half_even",
            basis=BASIS,
        ),
    )

    # ---- administrator: the dry-weight rule through the shared form dialog
    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="QC configuration").click()
    rules = page.get_by_role("region", name="Calculation rules")
    rules.get_by_role("link", name="Add calculation rule").first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog.locator("#vr-form-title")).to_have_text("Add calculation rule")
    pick(field(dialog, "Test"), label=str(solids))
    pick(field(dialog, "Calculation"), label=DRY_LABEL)
    pick(field(dialog, "Round to"), label="Significant figures")
    field(dialog, "Digits").fill("3")
    pick(field(dialog, "When the dropped digits are exactly 5"), value="half_even")
    field(dialog, "Basis").fill(BASIS)
    submit = dialog.get_by_role("button", name="Add calculation rule", exact=True)
    submit.click()
    # The calculation gives mg/kg: it is refused for the test in %, and nothing is saved.
    expect(dialog).to_contain_text("This calculation gives mg/kg; the test's unit is %.")
    assert (
        not CalculationRule.objects.filter(definition=solids)
        .exclude(calculation="percent_solids")
        .exists()
    )
    pick(field(dialog, "Test"), label=str(nitrate))
    submit.click()
    expect(page.get_by_text("Calculation rule saved.")).to_be_visible()
    rule = CalculationRule.objects.get(definition=nitrate)
    assert (rule.calculation, rule.rounding_digits, rule.is_active) == ("solid_dry_mg_l", 3, True)
    row = rules.get_by_role("row", name=re.compile(r"Nitrate \(synthetic soil\)"))
    expect(row).to_contain_text("Prepared solid corrected to dry weight")
    sign_out(page, "^Demo")

    # ---- analyst: percent solids from the three weights, on the wet basis
    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batches['%'].pk}/entry/")
    field(page, "Default analysis start").fill(local_input(timedelta(minutes=-20)))
    entry = entry_row(page, sample)
    expect(entry).to_contain_text(f"Calculated: {SOLIDS_LABEL}")
    expect(entry).to_contain_text("Wet weight")
    expect(native_select(field(entry, "Result basis"))).to_have_value("wet")
    for label, text in WEIGHTS.items():
        entry.get_by_label(re.compile(f"^{label}")).fill(text)
    save_grid(page)
    expect(page.get_by_text("Saved 1 result as entered")).to_be_visible()
    entry = entry_row(page, sample)
    expect(entry).to_contain_text("81.6")
    expect(entry).to_contain_text("calculated")

    # ---- analyst: the nitrate readings; the dry-weight correction needs the percent solids
    page.goto(f"/analytical/batches/{batches['mg/kg'].pk}/entry/")
    field(page, "Default analysis start").fill(local_input(timedelta(minutes=-20)))
    entry = entry_row(page, sample)
    expect(entry).to_contain_text(f"Calculated: {DRY_LABEL}")
    expect(entry).to_contain_text("Dry weight")
    expect(native_select(field(entry, "Result basis"))).to_have_value("dry")
    summary = entry.get_by_text("Percent solids for the dry-weight correction (required)")
    expect(summary).to_be_visible()
    # Required here, so its fields are open on the fresh page, not folded away as optional.
    expect(entry.get_by_label(re.compile("^Percent solids"))).to_be_visible()
    expect(entry.get_by_label("Solids source reference")).to_be_visible()
    for label, text in READINGS.items():
        entry.get_by_label(re.compile(f"^{label}")).fill(text)
    save_grid(page)
    expect(
        page.get_by_text("Percent solids is required by this calculation.").first
    ).to_be_visible()
    assert not ResultRevision.objects.filter(result__requested_test__definition=nitrate).exists()
    entry = entry_row(page, sample)
    for label, text in READINGS.items():
        expect(entry.get_by_label(re.compile(f"^{label}"))).to_have_value(text)

    entry.get_by_label(re.compile("^Percent solids")).fill("81.6")
    entry.get_by_label("Solids source reference").fill(SOURCE)
    save_grid(page)
    expect(page.get_by_text("Saved 1 result as entered")).to_be_visible()
    entry = entry_row(page, sample)
    expect(entry).to_contain_text("22.4")
    expect(entry).to_contain_text("calculated")

    # ---- result page: the worksheet, the limit before rounding, worked again
    revision = ResultRevision.objects.get(result__requested_test__definition=nitrate)
    page.goto(f"/analytical/results/{revision.result_id}/")
    expect(page.get_by_role("heading", name=f"Worksheet: {DRY_LABEL}")).to_be_visible()
    worksheet = page.get_by_role("table", name=re.compile("^Worksheet:"))
    expect(worksheet.get_by_role("row", name=re.compile("^Percent solids"))).to_contain_text("81.6")
    expect(
        worksheet.get_by_role("row", name=re.compile("^Reporting limit before rounding"))
    ).to_be_visible()
    limit = page.locator("dt").filter(has_text=re.compile("^Reporting limit applied$"))
    expect(limit.locator("xpath=following-sibling::dd[1]")).to_contain_text("0.609 mg/kg")
    expect(page.get_by_text("Same result")).to_be_visible()
    assert (revision.supplied_text, revision.source) == ("22.4", "calculated")
    assert (revision.value, revision.reporting_limit_applied) == (Decimal("22.4"), Decimal("0.609"))
    assert (revision.result_basis, revision.percent_solids) == ("dry", Decimal("81.6"))
    assert revision.solids_reference == SOURCE
    assert "percent_solids" in set(revision.worksheet.readings.values_list("key", flat=True))

    # ---- submit; an independent reviewer approves from the review grid
    page.goto(f"/analytical/batches/{batches['mg/kg'].pk}/")
    page.get_by_role("checkbox", name=re.compile(r"Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 1 result")).to_be_visible()
    sign_out(page)

    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batches["mg/kg"].number).click()
    table = page.get_by_role("table", name=re.compile("Results awaiting review"))
    expect(table).to_contain_text("22.4")
    expect(table).to_contain_text("calculated")
    page.get_by_role("checkbox", name=re.compile(rf"^Select {sample.lab_id}")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill("Worksheet checked.")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.")).to_be_visible()
    revision.refresh_from_db()
    assert revision.status == "approved" and revision.reviewed_by.username == "demo_reviewer"
    assert not page.console_errors
