"""A calculated result, from the administrator's rule to the reviewer's worksheet.

An administrator sets the solids calculation (balance in grams) for a synthetic mg/L test under QC
configuration. The analyst's entry grid then takes the three bench readings instead of a final
result; saving works out 41.7 mg/L (3 significant figures, half to even), stores the worksheet
and marks the value as calculated. The result page shows the readings as typed and works the
result again. After submission an independent reviewer follows the "Worksheet" link from the review
grid to that page, and approves.

The test, its method and its numbers are invented; nothing here states a method or a laboratory
policy.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from helpers import field, local_input, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

BASIS = "SYNTHETIC E2E: invented rule basis; not method guidance."
READINGS = {
    "Dried residue with its filter or dish": "0.1183",
    "Filter or dish alone": "0.1058",
    "Sample volume": "300",
}


def residue_batch():
    """A synthetic mg/L residue test on its own method, one received water sample, a batch."""
    from django.utils import timezone

    from accounts.models import User
    from analytical import services
    from analytical.models import PreservationRule
    from catalog.models import Analyte, Method, TestDefinition
    from receipt.models import Project, RequestedTest
    from receipt.services import receive_sample

    method = Method.objects.create(code="E2E-RESIDUE", version="r1", title="Synthetic residue")
    definition = TestDefinition.objects.create(
        method=method,
        analyte=Analyte.objects.create(name="Residue (synthetic)"),
        matrix="water",
        unit="mg/L",
        reporting_limit=Decimal("5"),
    )
    PreservationRule.objects.create(
        definition=definition,
        temperature_mode="not_required",
        basis="SYNTHETIC E2E: no temperature requirement; not method guidance.",
    )
    now = timezone.now()
    sample = receive_sample(
        actor=User.objects.get(username="demo_custodian"),
        project=Project.objects.get(name="Test Creek Outfall 001"),
        coc_reference="E2E-CALC-1",
        tests=[definition],
        container_description="Synthetic 1 L bottle",
        container_condition="Intact",
        relinquished_by="Example Courier",
        client_sample_id="E2E-CALC-1-S1",
        matrix="water",
        collection_type="grab",
        collected_at=now - timedelta(hours=3),
        collection_started_at=None,
        collector_name="Example Collector",
        collection_location="Synthetic Test Creek outfall 001",
        received_at=now - timedelta(hours=1),
        cooler_temperature=Decimal("4.200"),
        seal_condition="Intact",
        preservation_check="Recorded for demonstration only",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Receipt bench",
    )
    analyst = User.objects.get(username="demo_analyst")
    batch = services.create_batch(actor=analyst, method=method, matrix="water")
    requested = RequestedTest.objects.get(sample=sample, definition=definition)
    services.add_tests_to_batch(actor=analyst, batch_id=batch.pk, requested_test_ids=[requested.pk])
    return batch, sample, definition


def test_calculated_result_from_rule_to_reviewed_worksheet(page: Page, demo: Lab):
    from analytical.models import CalculationRule, ResultRevision

    batch, sample, definition = residue_batch()

    # ---- administrator: the rule, through the shared form dialog
    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="QC configuration").click()
    rules = page.get_by_role("region", name="Calculation rules")
    rules.get_by_role("link", name="Add calculation rule").first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog.locator("#vr-form-title")).to_have_text("Add calculation rule")
    pick(field(dialog, "Test"), label=str(definition))
    pick(field(dialog, "Calculation"), label="Solids by weight, balance in grams (version 1)")
    pick(field(dialog, "Round to"), label="Significant figures")
    field(dialog, "Digits").fill("3")
    pick(field(dialog, "When the dropped digits are exactly 5"), value="half_even")
    field(dialog, "Basis").fill(BASIS)
    dialog.get_by_role("button", name="Add calculation rule", exact=True).click()
    expect(page.get_by_text("Calculation rule saved.")).to_be_visible()
    rule = CalculationRule.objects.get(definition=definition)
    assert (rule.calculation, rule.rounding_digits, rule.is_active) == ("residue_g", 3, True)
    row = rules.get_by_role("row", name=re.compile("Residue"))
    expect(row).to_contain_text("Solids by weight, balance in grams")
    expect(row).to_contain_text("3 significant figures, half to even")
    sign_out(page, "^Demo")

    # ---- analyst: readings instead of a result; the grid shows the worked value
    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batch.pk}/entry/")
    field(page, "Default analysis start").fill(local_input(timedelta(minutes=-20)))
    entry = page.locator("tr[id^=row-]").filter(has_text=sample.lab_id)
    expect(entry).to_contain_text("Calculated: Solids by weight, balance in grams (version 1)")
    expect(entry.get_by_label("Report as ND")).not_to_be_checked()
    for label, text in READINGS.items():
        entry.get_by_label(re.compile(f"^{label}")).fill(text)
    page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered")).click()
    expect(page.get_by_text("Saved 1 result as entered")).to_be_visible()
    entry = page.locator("tr[id^=row-]").filter(has_text=sample.lab_id)
    expect(entry).to_contain_text("41.7")
    expect(entry).to_contain_text("calculated")

    # ---- result page: the worksheet, readings as typed, worked again
    revision = ResultRevision.objects.get(result__requested_test__sample=sample)
    page.goto(f"/analytical/results/{revision.result_id}/")
    expect(
        page.get_by_role(
            "heading", name="Worksheet: Solids by weight, balance in grams (version 1)"
        )
    ).to_be_visible()
    worksheet = page.get_by_role("table", name=re.compile("^Worksheet:"))
    expect(worksheet.get_by_role("row", name=re.compile("^Dried residue"))).to_contain_text(
        "0.1183"
    )
    expect(
        worksheet.get_by_role("row", name=re.compile("^Calculated value before rounding"))
    ).to_be_visible()
    expect(page.get_by_text("Rounding applied")).to_be_visible()
    expect(page.get_by_text("Same result")).to_be_visible()
    assert (revision.supplied_text, revision.source) == ("41.7", "calculated")
    assert revision.value == Decimal("41.7")
    assert list(revision.worksheet.readings.values_list("supplied_text", flat=True)) == list(
        READINGS.values()
    )

    # ---- submit; an independent reviewer follows the worksheet link from the review grid
    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile(r"Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 1 result")).to_be_visible()
    sign_out(page)

    sign_in(page, "demo_reviewer", demo.password)
    page.get_by_role("link", name="Technical review", exact=True).click()
    page.get_by_role("link", name=batch.number).click()
    table = page.get_by_role("table", name=re.compile("Results awaiting review"))
    expect(table).to_contain_text("41.7")
    expect(table).to_contain_text("calculated")
    table.get_by_role("link", name=re.compile("^Worksheet")).click()
    expect(page).to_have_url(re.compile(rf"/analytical/results/{revision.result_id}/"))
    expect(page.get_by_text("Worksheet: Solids by weight, balance in grams")).to_be_visible()
    expect(page.get_by_text("Same result")).to_be_visible()

    # the decision is made on the same grid
    page.go_back()
    page.get_by_role("checkbox", name=re.compile(rf"^Select {sample.lab_id}")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill("Worksheet checked.")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.")).to_be_visible()
    revision.refresh_from_db()
    assert revision.status == "approved" and revision.reviewed_by.username == "demo_reviewer"
    assert not page.console_errors
