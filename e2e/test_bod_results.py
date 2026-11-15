"""Synthetic bottle entry, corrected mean, retained exclusions and independent review."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from helpers import field, local_input, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def bod_batch():
    from django.utils import timezone

    from accounts.models import User
    from analytical import services
    from catalog.models import TestDefinition
    from receipt.models import Project
    from receipt.services import receive_sample

    definition = TestDefinition.objects.get(method__code="DEMO-BOD-AQ", matrix="water")
    analyst = User.objects.get(username="demo_analyst")
    now = timezone.now()
    sample = receive_sample(
        actor=User.objects.get(username="demo_custodian"),
        project=Project.objects.get(name="Test Creek Outfall 001"),
        coc_reference="E2E-BOD",
        tests=[definition],
        container_description="Synthetic 500 mL bottle; no chemical preservative",
        container_condition="Intact",
        relinquished_by="Example Courier",
        client_sample_id="E2E-BOD-1",
        matrix="water",
        collection_type="grab",
        collected_at=now - timedelta(hours=3),
        collection_started_at=None,
        collector_name="Example Collector",
        collection_location="Synthetic oxygen-demand station",
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
    for kind, value, extra in (
        ("method_blank", "ND", {}),
        ("lcs", "10.0", {"spike_value": "10"}),
        ("lab_duplicate", "10.0", {"reference_value": "10"}),
    ):
        qc = services.add_qc_sample(
            actor=analyst,
            batch_id=batch.pk,
            qc_type=kind,
            label=kind,
            parent_sample=sample if kind == "lab_duplicate" else None,
        )
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


def test_bod_bottle_mean_correction_and_review(page: Page, demo: Lab):
    from analytical.models import Result, ResultRevision

    batch, sample, definition = bod_batch()
    result = Result.objects.get(requested_test__sample=sample)
    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batch.pk}/entry/")
    expect(page.get_by_role("link", name="Record BOD5 bottles")).to_be_visible()
    page.goto(f"/analytical/results/{result.pk}/bottles/")
    field(page, "Analysis start").fill(local_input(timedelta(minutes=-15)))
    field(page, "Prior dilution factor").fill("1")
    page.get_by_role("button", name="Add bottle", exact=True).click()
    page.get_by_role("button", name="Add bottle", exact=True).click()
    table = page.get_by_role("table", name="Bottle observations")
    rows = table.locator("tbody tr")
    expect(rows).to_have_count(3)
    for i, (identifier, final, volume, selection) in enumerate(
        [
            ("BOD-A", "4.50", "30.0", "Include"),
            ("BOD-B", "5.90", "15.0", "Include"),
            ("BOD-C", "8.50", "10.0", "Exclude"),
        ]
    ):
        row = rows.nth(i)
        for label, value in {
            "Bottle identifier": identifier,
            "Initial DO": "008.00",
            "Final DO": final,
            "Sample volume": volume,
            "Filled bottle volume": "300.0",
            "Entered seed correction": "0.50",
            "Seed record reference": "SYNTHETIC-SEED-1",
        }.items():
            field(row, label).fill(value)
        pick(field(row, "Selection"), label=selection)
        if selection == "Exclude":
            field(row, "Exclusion reason").fill("Synthetic oxygen gain; retained for review.")
    # Missing correction provenance is refused with all other observations retained.
    field(rows.nth(0), "Seed record reference").fill("")
    page.get_by_role("button", name="Save bottle worksheet", exact=True).click()
    expect(
        page.get_by_text("Reference the record supporting this seed correction.")
    ).to_be_visible()
    expect(field(rows.nth(1), "Final DO")).to_have_value("5.90")
    assert not ResultRevision.objects.filter(result=result).exists()
    field(rows.nth(0), "Seed record reference").fill("SYNTHETIC-SEED-1")
    page.get_by_role("button", name="Save bottle worksheet", exact=True).click()
    expect(page.get_by_text("Same result", exact=True)).to_be_visible()
    original = ResultRevision.objects.get(result=result)
    assert original.supplied_text == "31.0"
    bottles = page.get_by_role("table", name="BOD5 bottle observations for revision 1", exact=True)
    expect(bottles).to_contain_text("008.00")
    expect(bottles).to_contain_text("Excluded")
    expect(bottles).to_contain_text("-30")
    page.goto(f"/analytical/results/{result.pk}/bottles/")
    rows = page.get_by_role("table", name="Bottle observations").locator("tbody tr")
    expect(field(rows.nth(0), "Initial DO")).to_have_value("008.00")
    field(rows.nth(0), "Final DO").fill("4.30")
    field(page, "Reason for correction or reanalysis").fill("Synthetic transcription correction.")
    page.get_by_role("button", name="Save bottle worksheet", exact=True).click()
    expect(page.get_by_text("Same result", exact=True)).to_have_count(2)
    revisions = list(ResultRevision.objects.filter(result=result).order_by("number"))
    assert [r.supplied_text for r in revisions] == ["31.0", "32.0"]
    assert revisions[0].worksheet.bottle_data[0]["final_do"] == "4.50"
    assert revisions[1].worksheet.bottle_data[0]["final_do"] == "4.30"
    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile("Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 1 result")).to_be_visible()
    sign_out(page)
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(f"/analytical/results/{result.pk}/")
    expect(page.get_by_text("Same result", exact=True)).to_have_count(2)
    expect(page.get_by_role("link", name="Record BOD5 bottles")).to_have_count(0)
    page.goto(f"/analytical/review/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile(rf"^Select {sample.lab_id}")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Approve")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill(
        "Synthetic: checked selected mean and seed source."
    )
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.")).to_be_visible()
    revisions[-1].refresh_from_db()
    assert revisions[-1].reviewed_by.username == "demo_reviewer"
    assert not page.console_errors
