"""A low-alkalinity worksheet through configuration, refused entry, correction and review.

All catalog rules, observations and acceptance limits are synthetic. The service regressions
cover every equation; this journey checks that the longest worksheet works at the bench.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from helpers import field, local_input, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

LABEL = "Low alkalinity by two-endpoint titration (version 1)"
READINGS = {
    "Initial burette reading": "0.20",
    "First endpoint burette reading": "1.50",
    "Final burette reading": "1.70",
    "Sample volume": "200.0",
    "Standardized acid normality": "0.0200",
    "First endpoint pH": "4.60",
    "Final endpoint pH": "4.30",
}


def titration_batch():
    from django.utils import timezone

    from accounts.models import User
    from analytical import services
    from catalog.models import TestDefinition
    from receipt.models import Project
    from receipt.services import receive_sample

    definition = TestDefinition.objects.get(method__code="DEMO-LOW-ALK-AQ")
    analyst = User.objects.get(username="demo_analyst")
    now = timezone.now()
    sample = receive_sample(
        actor=User.objects.get(username="demo_custodian"),
        project=Project.objects.get(name="Test Creek Outfall 001"),
        coc_reference="E2E-TITRATION",
        tests=[definition],
        container_description="Synthetic 500 mL bottle; no chemical preservative",
        container_condition="Intact",
        relinquished_by="Example Courier",
        client_sample_id="E2E-TITRATION-1",
        matrix="water",
        collection_type="grab",
        collected_at=now - timedelta(hours=3),
        collection_started_at=None,
        collector_name="Example Collector",
        collection_location="Synthetic titration station",
        received_at=now - timedelta(hours=1),
        cooler_temperature=Decimal("4.200"),
        seal_condition="Intact",
        preservation_check="Synthetic: cooled; no chemical preservation",
        discrepancy="",
        disposition="proceed",
        disposition_note="",
        current_location="Receipt bench",
    )
    batch = services.create_batch(actor=analyst, method=definition.method, matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    for kind, value, extra in (("method_blank", "ND", {}), ("lcs", "10.0", {"spike_value": "10"})):
        qc = services.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type=kind, label=kind)
        services.record_qc_measurement(
            actor=analyst,
            qc_sample_id=qc.pk,
            definition_id=definition.pk,
            observed_text=value,
            reporting_limit_applied="0.5",
            analysis_started_at=now - timedelta(minutes=20),
            **extra,
        )
    return batch, sample, definition


def test_titration_readings_correction_and_independent_review(page: Page, demo: Lab):
    from analytical.models import CalculationRule, ResultRevision

    batch, sample, definition = titration_batch()
    rule = CalculationRule.objects.get(definition=definition)
    sign_in(page, "demo_admin", demo.password)
    page.goto(f"/analytical/config/calculation-rule/{rule.pk}/")
    pick(field(page, "Calculation"), label=LABEL)
    field(page, "Digits").fill("2")
    field(page, "Reason for change").fill("Synthetic browser check of two decimal places.")
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Calculation rule saved.")).to_be_visible()
    sign_out(page, "^Demo")

    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batch.pk}/entry/")
    field(page, "Default analysis start").fill(local_input(timedelta(minutes=-15)))
    entry = page.locator("tr[id^=row-]").filter(has_text=sample.lab_id)
    expect(entry).to_contain_text(LABEL)
    for label, text in READINGS.items():
        entry.get_by_label(re.compile(f"^{label}")).fill(text)
    entry.get_by_label(re.compile("^Final endpoint pH")).fill("4.20")
    save = page.get_by_role("button", name=re.compile(r"^Save \d+ rows? on this page as entered"))
    save.click()
    expect(entry.get_by_text(re.compile("final endpoint pH must be exactly 0.3"))).to_be_visible()
    assert not ResultRevision.objects.filter(definition=definition).exists()
    for label, text in READINGS.items():
        expected = "4.20" if label == "Final endpoint pH" else text
        expect(entry.get_by_label(re.compile(f"^{label}"))).to_have_value(expected)
    entry.get_by_label(re.compile("^Final endpoint pH")).fill("4.30")
    save.click()
    expect(page.get_by_text("Saved 1 result as entered")).to_be_visible()
    original = ResultRevision.objects.get(definition=definition)
    assert original.supplied_text == "5.50"

    page.goto(f"/analytical/results/{original.result_id}/")
    expect(page.get_by_text("Same result", exact=True)).to_be_visible()
    sheet = page.get_by_role("table", name=re.compile("^Worksheet:"))
    expect(
        sheet.get_by_role("row", name=re.compile("^Standardized acid normality"))
    ).to_contain_text("0.0200")
    page.locator("#entry").get_by_role("button", name="Correct result", exact=True).click()
    correction = page.locator("#vr-form-dialog")
    for label, text in READINGS.items():
        correction.get_by_label(re.compile(f"^{label}")).fill(
            "1.72" if label == "Final burette reading" else text
        )
    field(correction, "Analysis start").fill(local_input(timedelta(minutes=-10)))
    field(correction, "Reason for correction or reanalysis").fill(
        "Synthetic: corrected final burette reading."
    )
    correction.get_by_role("button", name="Correct result", exact=True).click()
    expect(page.get_by_text("Saved as a new revision. The earlier entry is kept.")).to_be_visible()
    revisions = list(ResultRevision.objects.filter(result_id=original.result_id).order_by("number"))
    assert [r.supplied_text for r in revisions] == ["5.50", "5.40"]
    assert [r.worksheet.readings.get(key="final").supplied_text for r in revisions] == [
        "1.70",
        "1.72",
    ]
    expect(page.get_by_text("Same result", exact=True)).to_have_count(2)

    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile("Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 1 result")).to_be_visible()
    sign_out(page)
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(f"/analytical/review/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile(rf"^Select {sample.lab_id}")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill(
        "Synthetic: checked both endpoints and normality."
    )
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.")).to_be_visible()
    revisions[-1].refresh_from_db()
    assert revisions[-1].status == "approved"
    assert revisions[-1].reviewed_by.username == "demo_reviewer"
    assert not page.console_errors
