"""Staff calibration entry, scoped provenance choices and stale correction behavior."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from analytical import calibration, services
from analytical.calibration_forms import CalibrationForm
from analytical.models import Result, RunCalibration
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def setup_run(lab, analyst):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=RequestedTest.objects.filter(sample=lab["samples"][0]).values_list(
            "pk", flat=True
        ),
    )
    return batch, Result.objects.get(batch=batch, requested_test__definition=lab["defs"]["Lead"])


def curve_data(lab, **changes):
    return {
        "definition_id": lab["defs"]["Lead"].pk,
        "reference": "SYNTHETIC-CURVE-01",
        "calibrated_at": timezone.now() - timedelta(hours=3),
        "model_text": "Instrument-reported linear fit; synthetic",
        "response_unit": "counts",
        "source_reference": "SYNTHETIC instrument record 01",
        "points": [{"standard": "SYN-STD-1", "concentration": "0.0100", "response": "-0.0200"}],
        **changes,
    }


def record(lab, analyst, batch, **changes):
    return calibration.record_calibration(
        actor=analyst, batch_id=batch.pk, **curve_data(lab, **changes)
    )


def entry(**changes):
    return {
        "expected_revision": "0",
        "supplied_text": "0.0200",
        "reporting_limit_applied": "0.005",
        "dilution_factor": "1",
        "analysis_started_at": (timezone.localtime() - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
        **changes,
    }


def test_record_and_read_frozen_evidence(lab, analyst, reviewer, client):
    batch, _ = setup_run(lab, analyst)
    client.force_login(analyst)
    url = reverse("analytical:calibration-create", args=[batch.pk])
    assert client.get(url).status_code == 200
    data = curve_data(lab)
    data["calibrated_at"] = data["calibrated_at"].strftime("%Y-%m-%dT%H:%M%z")
    data["points"] = "standard,concentration,response\nSYN-STD-1,0.0100,-0.0200\n"
    response = client.post(url, data)
    assert response.status_code == 302
    curve = RunCalibration.objects.get()
    assert curve.points[0]["response"] == "-0.0200"
    client.force_login(reviewer)
    html = client.get(response.url).content.decode()
    for text in ("SYNTHETIC-CURVE-01", "0.0100", "-0.0200", "counts", "Calibration facts"):
        assert text in html
    batch_html = client.get(reverse("analytical:batch-detail", args=[batch.pk])).content.decode()
    assert curve.reference in batch_html and "Record calibration</a>" not in batch_html
    assert client.get(url).status_code == 403


def test_clients_cannot_read_or_record(lab, analyst, client):
    batch, result = setup_run(lab, analyst)
    curve = record(lab, analyst, batch)
    portal = User.objects.create_user(
        username="synthetic-portal", client=lab["samples"][0].order.project.client
    )
    client.force_login(portal)
    for name, pk in (
        ("calibration-detail", curve.pk),
        ("calibration-create", batch.pk),
        ("result-detail", result.pk),
    ):
        url = reverse(f"analytical:{name}", args=[pk])
        assert client.get(url).status_code == 403
        assert client.post(url, {}).status_code == 403


def test_csv_bounds_errors_keep_input(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    data = curve_data(lab)
    for points in (
        "standard,concentration,response\n",
        "x,y,z\na,1,2",
        "standard,concentration,response\n" + "s,1,2\n" * 51,
    ):
        form = CalibrationForm({**data, "points": points}, batch=batch)
        assert not form.is_valid() and "points" in form.errors
    client.force_login(analyst)
    data["points"] = "standard,concentration,response\nSYN,-1,2"
    response = client.post(reverse("analytical:calibration-create", args=[batch.pk]), data)
    assert response.status_code == 200 and b"SYN,-1,2" in response.content
    assert not RunCalibration.objects.exists()


def test_explicit_link_correction_and_stale_token(lab, analyst, client):
    batch, result = setup_run(lab, analyst)
    curve = record(lab, analyst, batch)
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    page = client.get(url)
    assert page.context["form"]["expected_revision"].value() == 0
    assert page.context["form"]["calibration_id"].value() is None
    assert client.post(url, entry(calibration_id=curve.pk)).status_code == 302
    first = result.revisions.get()
    assert first.calibration_id == curve.pk
    detail = client.get(url)
    assert curve.reference.encode() in detail.content
    assert detail.context["form"]["calibration_id"].value() is None
    stale = client.post(url, entry(calibration_id=curve.pk, reason="Synthetic correction"))
    assert stale.status_code == 200 and result.revisions.count() == 1
    saved = client.post(url, entry(expected_revision=1, reason="Synthetic unlinked correction"))
    assert saved.status_code == 302
    assert result.revisions.get(number=2).calibration_id is None
    assert result.revisions.get(number=1).calibration_id == curve.pk
    assert client.get(reverse("analytical:calibration-detail", args=[curve.pk])).status_code == 200


def test_unoffered_curve_and_verification_cannot_be_posted(lab, analyst, client):
    batch, result = setup_run(lab, analyst)
    copper = record(lab, analyst, batch, definition_id=lab["defs"]["Copper"].pk)
    other = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    foreign = record(lab, analyst, other)
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    choices = dict(client.get(url).context["form"].fields["calibration_id"].choices)
    assert str(copper.pk) not in choices and str(foreign.pk) not in choices
    for data in (
        entry(calibration_id=copper.pk),
        entry(calibration_id=foreign.pk),
        entry(calibration_verification_id=999999),
        entry(expected_revision=""),
    ):
        response = client.post(url, data)
        assert response.status_code == 200 and response.context["form"].errors
    assert not result.revisions.exists()


def test_verification_is_visible_and_evidence_contributors_cannot_review(
    lab, analyst, analyst2, both, reviewer, client
):
    batch, result = setup_run(lab, analyst)
    curve = record(lab, both, batch)
    sample = services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="calibration_verification", label="SYN-CCV"
    )
    measurement = services.record_qc_measurement(
        actor=analyst2,
        qc_sample_id=sample.pk,
        definition_id=lab["defs"]["Lead"].pk,
        observed_text="10.0100",
        reporting_limit_applied="0.005",
        spike_value="10",
        analysis_started_at=timezone.now() - timedelta(hours=2),
    )
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    assert (
        client.post(
            url, entry(calibration_id=curve.pk, calibration_verification_id=measurement.pk)
        ).status_code
        == 302
    )
    revision = result.revisions.get()
    assert revision.calibration_verification_id == measurement.pk
    html = client.get(url).content.decode()
    assert "SYN-CCV" in html and "10.0100" in html and "true value" in html
    # Freeze the identifying text even when the mutable QC sample label changes.
    sample.label = "SYN-RENAMED"
    sample.save(update_fields=["label"])
    assert "SYN-CCV" in client.get(url).content.decode()
    services.submit_result(actor=analyst, revision_id=revision.pk)
    from analytical.selectors import review_queue_queryset

    for contributor in (both, analyst2):
        queued = review_queue_queryset(contributor).get(pk=batch.pk)
        assert (queued.awaiting, queued.own_count, queued.reviewable) == (1, 1, 0)
    queued = review_queue_queryset(reviewer).get(pk=batch.pk)
    assert (queued.awaiting, queued.own_count, queued.reviewable) == (1, 0, 1)
    client.force_login(both)
    queue = client.get(reverse("analytical:review-list"))
    assert queue.context["page_obj"][0].reviewable == 0
    assert b"selected calibration evidence" in queue.content
    review = client.get(reverse("analytical:review-batch", args=[batch.pk]))
    assert review.status_code == 200 and b"Your work" in review.content
    assert curve.reference.encode() in review.content and b"SYN-CCV" in review.content


def test_batch_calibration_pagination_is_independent(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    for number in range(26):
        record(lab, analyst, batch, reference=f"SYNTHETIC-CURVE-{number:02}")
    client.force_login(analyst)
    url = reverse("analytical:batch-detail", args=[batch.pk])
    response = client.get(url, {"cal_page": 2})
    assert response.status_code == 200
    assert response.context["calibration_page"].number == 2
    assert response.context["page_obj"].number == 1
    assert len(response.context["calibration_page"]) == 1
    assert b"cal_page=1" in response.content
    assert b"SYNTHETIC-CURVE-00" in response.content


def test_bottle_form_does_not_offer_calibration_evidence(lab, analyst, client):
    from .test_calculations import rule_for

    batch, result = setup_run(lab, analyst)
    rule_for(lab["defs"]["Lead"], "bod_bottles")
    client.force_login(analyst)
    response = client.get(reverse("analytical:result-bottles", args=[result.pk]))
    assert response.status_code == 200
    form = response.context["form"]
    assert "calibration_id" not in form.fields
    assert "calibration_verification_id" not in form.fields
    assert form["expected_revision"].value() == result.latest_number
    assert b'name="calibration_id"' not in response.content
    assert b'name="calibration_verification_id"' not in response.content
