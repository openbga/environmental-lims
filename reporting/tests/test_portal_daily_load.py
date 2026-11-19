from datetime import timedelta

from django.test import Client
from django.urls import reverse

from analytical import services as analytical
from core.models import AuditEvent

from .test_release import correct, release


def worksheet(report, version=1, sample=1, result=1):
    return reverse("portal:daily-load", args=[report.pk, version, sample, result])


def test_daily_load_uses_issued_concentration_and_keeps_the_flow_transient(world, people, client):
    report, sample = world["report_a"], world["sample_a"]
    sample.source_type = "surface_water"
    sample.collection_type = "composite"
    sample.collection_started_at = sample.collected_at - timedelta(days=1)
    sample.save(update_fields=["source_type", "collection_type", "collection_started_at"])
    issue = release(report, people["approver"])
    sample.source_type = "groundwater"
    sample.collection_location = "Later unissued location"
    sample.save(update_fields=["source_type", "collection_location"])
    client.force_login(world["user_a"])
    url = worksheet(report)
    detail = client.get(reverse("portal:report-detail", args=[report.pk]))
    assert f'href="{url}"' in detail.content.decode()
    page = client.get(url, {"flow_mgd": "1.000"})
    assert page.context["concentration"] == "0.050"
    assert page.context["load"] is None
    assert b"Test Creek outfall weir (synthetic)" in page.content
    assert b"Later unissued location" not in page.content
    assert b"Surface water" in page.content and b"Groundwater" not in page.content
    assert b"Composite start" in page.content and b"Composite end" in page.content
    source = issue.file.snapshot["content"]["samples"][0]
    start_day, end_day = source["collection_started_at"][:10], source["collected_at"][:10]
    assert start_day != end_day
    assert start_day.encode() in page.content and end_day.encode() in page.content

    events = AuditEvent.objects.count()
    calculated = client.post(url, {"flow_mgd": "1.0000000001", "concentration": "99999"})
    assert b"0.417000000041700 lb/day" in calculated.content
    assert b"0.050 mg/L" in calculated.content
    assert b"1.0000000001 MGD" in calculated.content
    assert b"99999" not in calculated.content
    assert "no-store" in calculated["Cache-Control"]
    assert AuditEvent.objects.count() == events
    assert client.get(url).context["load"] is None
    assert client.get(reverse("portal:report-pdf", args=[report.pk, 1])).content == bytes(
        issue.file.pdf
    )

    for invalid in ("", "-1", "NaN", "1e100000000", "1_000", "1" * 15, ".00000000001"):
        response = client.post(url, {"flow_mgd": invalid})
        assert response.context["load"] is None
        assert "flow_mgd" in response.context["form"].errors
        assert response.context["form"]["flow_mgd"].value() == invalid
    zero = client.post(url, {"flow_mgd": "0"})
    assert zero.context["load"] is not None and b"0.00000 lb/day" in zero.content


def test_daily_load_requires_client_ownership_valid_positions_and_csrf(world, people, client):
    report = world["report_a"]
    url = worksheet(report)
    assert client.get(url).status_code == 302
    client.force_login(people["approver"])
    assert client.get(url).status_code == 403
    assert client.post(url, {"flow_mgd": "1"}).status_code == 403
    client.force_login(world["user_a"])
    assert client.get(url).status_code == 404  # Still a draft
    release(report, people["approver"])
    for coords in ((1, 0, 1), (1, 1, 0), (2, 1, 1), (1, 2, 1), (1, 1, 2)):
        assert client.get(worksheet(report, *coords)).status_code == 404
    assert client.delete(url).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(world["user_a"])
    assert strict.post(url, {"flow_mgd": "1"}).status_code == 403
    client.force_login(world["user_b"])
    assert client.get(url).status_code == 404
    assert client.post(url, {"flow_mgd": "1"}).status_code == 404


def test_amended_issue_requires_reselection_and_non_detect_is_withheld(world, people, client):
    report = world["report_a"]
    release(report, people["approver"])
    client.force_login(world["user_a"])
    opened = worksheet(report)
    assert client.get(opened).context["form"] is not None

    revision = correct(world["sample_a"], people, "ND")
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    release(report, people["approver2"], reason="Synthetic correction to non-detect")

    for response in (client.get(opened), client.post(opened, {"flow_mgd": "1"})):
        assert b"current report" in response.content
        assert response.context["form"] is response.context["load"] is None
        assert b"Superseded version" in response.content
        assert response.context["result"]["reported"] == "0.050"

    current = worksheet(report, 2)
    detail = client.get(reverse("portal:report-detail", args=[report.pk]))
    assert f'href="{current}"' in detail.content.decode()
    withheld = client.post(current, {"flow_mgd": "1"})
    assert b"Daily load unavailable" in withheld.content
    assert "non-detect" in withheld.context["reason"].lower()
    assert withheld.context["form"] is withheld.context["load"] is None
    assert withheld.context["result"]["reported"] == "ND"
