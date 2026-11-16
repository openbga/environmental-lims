"""An instrument curve is recorded, selected on a revision and independently reviewed."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from helpers import field, local_input, pick, sign_in, sign_out, submit_fresh_batch
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_record_curve_link_verification_and_review(page, demo):
    from django.utils import timezone

    from accounts.models import User
    from analytical import services
    from analytical.models import QCLimit, ResultRevision, RunCalibration

    analyst = User.objects.get(username="demo_analyst")
    reviewer = User.objects.get(username="demo_reviewer")
    batch, original, sample = submit_fresh_batch(analyst, coc_reference="E2E-CALIBRATION")
    services.return_result(
        actor=reviewer, revision_id=original.pk, reason="Synthetic evidence check"
    )
    # Invented fixture criteria exercise the configured gate, not a method's requirements.
    QCLimit.objects.get_or_create(
        definition=original.definition,
        qc_type="calibration_verification",
        measure="recovery",
        defaults={"lower": Decimal("90"), "upper": Decimal("110")},
    )
    qc = services.add_qc_sample(
        actor=analyst,
        batch_id=batch.pk,
        qc_type="calibration_verification",
        label="SYNTHETIC-CHECK-LOT-1",
    )
    check = services.record_qc_measurement(
        actor=analyst,
        qc_sample_id=qc.pk,
        definition_id=original.definition_id,
        observed_text="10.0000",
        spike_value="10.0000",
        reporting_limit_applied="1.0",
        analysis_started_at=timezone.now() - timedelta(minutes=5),
    )
    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("link", name="Record calibration", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    pick(field(dialog, "Test"), value=str(original.definition_id))
    field(dialog, "Calibration reference").fill("SYNTHETIC-CURVE-1")
    field(dialog, "Calibration time").fill(local_input(timedelta(hours=-1)))
    field(dialog, "Instrument-reported model").fill(
        "Synthetic instrument fit: y = 2.0000 x - 0.0100"
    )
    field(dialog, "Response unit").fill("counts")
    field(dialog, "Source reference").fill("SYNTHETIC instrument output 001")
    field(dialog, "Standard points (CSV)").fill(
        "standard,concentration,response\nSYN-BLANK,0.0000,-0.0100\nSYN-LOT-1,10.0000,19.9900\n"
    )
    dialog.locator("[data-vr-documentation-toggle]").click()
    expect(page.locator("#vr-documentation")).to_contain_text("standard,concentration,response")
    page.locator("[data-vr-documentation-close]").click()
    dialog.get_by_role("button", name="Record calibration", exact=True).click()
    expect(page.get_by_role("heading", name="SYNTHETIC-CURVE-1", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Standard points")).to_contain_text("-0.0100")
    # Record layout panels must stack; multiple panels in its lead slot overlap at desktop.
    previous_bottom = 0
    for title in (
        "Calibration facts",
        "Standard points",
        "Result revisions using this calibration",
        "History",
    ):
        box = page.get_by_role("region", name=title, exact=True).bounding_box()
        assert box["y"] >= previous_bottom - 1
        previous_bottom = box["y"] + box["height"]
    curve = RunCalibration.objects.get(batch=batch)
    assert curve.points[1]["concentration"] == "10.0000"

    page.goto(f"/analytical/results/{original.result_id}/")
    page.locator("#entry").get_by_role("button", name="Correct result", exact=True).click()
    pick(field(page, "Calibration"), value=str(curve.pk))
    pick(field(page, "Calibration verification"), value=str(check.pk))
    field(page, "Result").fill("5.2000")
    field(page, "Reporting limit applied").fill("1.0")
    field(page, "Dilution factor").fill("1.00")
    field(page, "Analysis start").fill(local_input(timedelta(minutes=-20)))
    field(page, "Reason for correction or reanalysis").fill(
        "Synthetic: explicitly linked run evidence"
    )
    page.locator("#vr-form-dialog").get_by_role("button", name="Correct result", exact=True).click()
    expect(page.get_by_text("Saved as a new revision.", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name="SYNTHETIC-CURVE-1", exact=True)).to_be_visible()
    expect(page.get_by_text("SYNTHETIC-CHECK-LOT-1", exact=False).first).to_be_visible()
    revision = ResultRevision.objects.get(result_id=original.result_id, number=2)
    assert revision.calibration_evidence["verification"]["observed_text"] == "10.0000"
    original.refresh_from_db()
    assert original.calibration_id is None

    page.goto(f"/analytical/batches/{batch.pk}/")
    page.get_by_role("checkbox", name=re.compile("Select all entered")).check()
    page.get_by_role("button", name="Submit selected for review").click()
    expect(page.get_by_text("Submitted 1 result", exact=False)).to_be_visible()
    sign_out(page)
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(f"/analytical/review/batches/{batch.pk}/")
    page.get_by_role("link", name="SYNTHETIC-CURVE-1", exact=True).click()
    expect(page.get_by_role("table", name="Standard points")).to_contain_text("19.9900")
    page.go_back()
    page.get_by_role("checkbox", name=re.compile(rf"^Select {sample.lab_id}")).check()
    pick(field(page, "Action"), label="Approve")
    field(page, "Note or reason").fill("Synthetic curve and selected verification checked")
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Approved 1 result.", exact=True)).to_be_visible()
    revision.refresh_from_db()
    assert revision.status == "approved" and revision.reviewed_by_id == reviewer.pk
    assert not page.console_errors
