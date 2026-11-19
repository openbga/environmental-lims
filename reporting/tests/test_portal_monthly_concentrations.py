from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from analytical import services as analytical
from catalog.models import Method, TestDefinition
from core.models import AuditEvent
from reporting import portal_views, services
from reporting.models import Report, ReportFile, ReportVersion

from .conftest import approve, make_sample
from .test_release import correct, release


def worksheet(report, version=1, sample=1, result=1):
    return reverse("portal:monthly-concentrations", args=[report.pk, version, sample, result])


def test_worksheet_uses_frozen_scope_and_links_to_issued_pdfs(world, people, client, settings):
    settings.TIME_ZONE = "America/New_York"
    sample, report, definition = world["sample_a"], world["report_a"], world["definition"]
    sample.collected_at = datetime(2026, 1, 1, 2, 15, tzinfo=UTC)
    sample.source_type = "surface_water"
    sample.save(update_fields=["collected_at", "source_type"])
    selected = release(report, people["approver"])

    method = Method.objects.create(code="DEMO-UG", version="r2", title="Synthetic micrograms")
    micrograms = TestDefinition.objects.create(
        method=method,
        analyte=definition.analyte,
        matrix="water",
        unit="µg/L",
        reporting_limit=Decimal("0.010"),
    )
    source = make_sample(world["project_a"], "VR-OTHER", [micrograms], people["custodian"])
    source.collected_at = datetime(2025, 12, 30, 2, 15, tzinfo=UTC)
    source.save(update_fields=["collected_at"])
    approve(source, people, "53.500")
    source_report = services.create_report(
        actor=people["pm"], project=world["project_a"], title="Earlier source", samples=[source]
    )
    earlier = release(source_report, people["approver"])

    sample.collection_location = "Later unissued location"
    sample.collected_at = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    sample.save(update_fields=["collection_location", "collected_at"])
    settings.TIME_ZONE = "UTC"
    client.force_login(world["user_a"])
    detail = client.get(reverse("portal:report-detail", args=[report.pk]))
    url = worksheet(report)
    assert f'href="{url}"' in detail.content.decode()
    assert "Daily load" in detail.content.decode()

    events_before = AuditEvent.objects.count()
    response = client.get(url, {"month": "2026-02", "location": "Later unissued location"})
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert response.context["period"].isoformat() == "2025-12-01"
    summary = response.context["summary"]
    assert summary["days"] == 2
    assert Decimal(summary["mean"]) == Decimal("0.05175")
    assert Decimal(summary["maximum"]) == Decimal("0.0535")
    body = response.content.decode()
    assert "Later unissued location" not in body
    for text in (
        "December 2025",
        "Test Creek outfall weir (synthetic)",
        "2025-12-31",
        "Surface water",
        "DEMO-UG vr2",
        "53.500",
        "µg/L",
    ):
        assert text in body
    for version in (selected, earlier):
        pdf_url = reverse("portal:report-pdf", args=[version.report_id, version.number])
        assert f'href="{pdf_url}"' in body
        assert client.get(pdf_url).content == bytes(version.file.pdf)
    assert AuditEvent.objects.count() == events_before


def test_worksheet_requires_owned_issued_aqueous_result_with_named_location(world, people, client):
    report = world["report_a"]
    url = worksheet(report)
    assert client.get(url).status_code == 302
    client.force_login(people["approver"])
    assert client.get(url).status_code == 403
    client.force_login(world["user_a"])
    assert client.get(url).status_code == 404
    release(report, people["approver"])
    for coordinates in ((1, 0, 1), (1, 1, 0), (2, 1, 1), (1, 2, 1), (1, 1, 2)):
        assert client.get(worksheet(report, *coordinates)).status_code == 404
    assert client.delete(url).status_code == 405
    client.force_login(world["user_b"])
    assert client.get(url).status_code == 404
    assert client.post(url).status_code == 404
    sample, report = world["sample_b"], world["report_b"]
    sample.collection_location = " "
    sample.save(update_fields=["collection_location"])
    release(report, people["approver"])
    detail = client.get(reverse("portal:report-detail", args=[report.pk]))
    assert f'href="{worksheet(report)}"'.encode() not in detail.content
    assert b"Daily load" in detail.content
    assert client.get(worksheet(report)).status_code == 404


def test_worksheet_blocks_missing_sources_and_refuses_unreadable_anchors(world, people, client):
    anchor = world["report_a"]
    release(anchor, people["approver"])
    report = Report.objects.create(
        project=world["project_a"], title="Unreadable issue", created_by=people["pm"]
    )
    version = ReportVersion.objects.create(
        report=report,
        number=1,
        released_by=people["approver"],
        released_at=timezone.now(),
        pdf_size=1,
    )
    report.latest_version = 1
    report.save(update_fields=["latest_version"])
    client.force_login(world["user_a"])

    response = client.get(worksheet(anchor))
    assert response.status_code == 200
    assert response.context["summary"]["mean"] is None
    row = next(row for row in response.context["summary"]["rows"] if row["report_id"] == report.pk)
    assert not row["pdf_available"]
    pdf_url = reverse("portal:report-pdf", args=[report.pk, 1])
    assert f'href="{pdf_url}"'.encode() not in response.content
    assert b"PDF unavailable" in response.content
    assert client.get(worksheet(report)).status_code == 404

    ReportFile.objects.create(version=version, snapshot={"content": None}, pdf=b"%")
    assert client.get(worksheet(report)).status_code == 404


def test_worksheet_rechecks_anchor_after_series_read(world, people, client, monkeypatch):
    report = world["report_a"]
    release(report, people["approver"])
    client.force_login(world["user_a"])
    original = worksheet(report)
    assert client.get(original).context["summary"]["mean"] == "0.050"

    revision = correct(world["sample_a"], people, "ND")
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    select_series = portal_views.issued_monthly_concentrations

    def amended_during_calculation(**kwargs):
        summary = select_series(**kwargs)
        release(report, people["approver2"], reason="Synthetic correction to non-detect")
        return summary

    with monkeypatch.context() as patch:
        patch.setattr(portal_views, "issued_monthly_concentrations", amended_during_calculation)
        racing = client.get(original)
    for stale in (racing, client.get(original), client.post(original, {"flow_ignored": "1"})):
        assert stale.status_code == 200
        assert not stale.context["current"]
        assert stale.context["summary"] is None
        assert stale.context["form"] is None
        assert b"Superseded version" in stale.content
        assert b"current report" in stale.content
        assert b'id="issued-source-results-heading"' not in stale.content
    amended = client.get(worksheet(report, 2))
    assert amended.status_code == 200
    assert b"Calculation blocked" in amended.content
    summary = amended.context["summary"]
    assert summary["mean"] is summary["maximum"] is None
    assert [row["reported"] for row in summary["rows"]] == ["ND"]
    assert "non-detect" in summary["rows"][0]["reason"].lower()


@pytest.fixture
def grab_series(world, people, settings):
    settings.TIME_ZONE = "America/New_York"
    report, anchor = world["report_a"], world["sample_a"]
    anchor.collected_at = datetime(2026, 6, 1, 10, 30, tzinfo=UTC)
    anchor.save(update_fields=["collected_at"])
    issued = release(report, people["approver"])
    samples = []
    for lab_id, day, hour, text in (("VR-A2", 1, 15, "0.100"), ("VR-A3", 2, 11, "0.060")):
        sample = make_sample(world["project_a"], lab_id, [world["definition"]], people["custodian"])
        sample.collected_at = datetime(2026, 6, day, hour, 45, tzinfo=UTC)
        sample.save(update_fields=["collected_at"])
        approve(sample, people, text)
        samples.append(sample)
    other = services.create_report(
        actor=people["pm"], project=world["project_a"], title="Other grabs", samples=samples
    )
    release(other, people["approver"])
    return issued, other, samples


def test_worksheet_uses_exact_posted_flows_and_keeps_them_transient(grab_series, world):
    issue, _, _ = grab_series
    url = worksheet(issue.report)
    client = Client(enforce_csrf_checks=True)
    client.force_login(world["user_a"])
    page = client.get(url)
    summary, form = page.context["summary"], page.context["form"]
    assert summary["mean"] is summary["maximum"] is None
    assert not form.is_bound
    first, second = [field.name for field in form.visible_fields()]
    for label in (b"VR-A1", b"2026-06-01T06:30:00-04:00", b"VR-A2", b"2026-06-01T11:45:00-04:00"):
        assert label in page.content
    assert b"VR-A3" not in " ".join(field.label for field in form.visible_fields()).encode()

    data = {"series_token": form["series_token"].value(), first: "1.000", second: "2"}
    events_before = AuditEvent.objects.count()
    assert client.post(url, data).status_code == 403
    response = client.post(url, data, headers={"X-CSRFToken": client.cookies["csrftoken"].value})
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    calculated = response.context["summary"]
    assert (calculated["mean"], calculated["maximum"]) == ("43/600", "1/12")
    assert [row["day"] for row in calculated["daily_values"]] == [
        date(2026, 6, 1),
        date(2026, 6, 2),
    ]
    assert b"43/600 mg/L" in response.content and b"1/12 mg/L" in response.content
    assert AuditEvent.objects.count() == events_before
    assert client.get(url, data).context["summary"]["mean"] is None
    assert client.get(reverse("portal:report-pdf", args=[issue.report_id, 1])).content == bytes(
        issue.file.pdf
    )


def test_worksheet_rejects_invalid_missing_duplicate_and_unexpected_flows(
    grab_series, world, people, client
):
    issue, _, _ = grab_series
    url = worksheet(issue.report)
    client.force_login(world["user_a"])
    form = client.get(url).context["form"]
    first, second = [field.name for field in form.visible_fields()]
    valid_data = {"series_token": form["series_token"].value(), first: "1.000", second: "2"}
    for invalid in ("", "0", "-1", "NaN", "1E0", "1,000", "1 mgd", "1" * 15, ".00000000001"):
        response = client.post(url, valid_data | {second: invalid})
        assert "no-store" in response["Cache-Control"]
        errors = response.context["form"]
        assert second in errors.errors
        assert errors[second].value() == invalid
        assert errors[first].value() == "1.000"
        assert response.context["summary"]["mean"] is None
    missing = client.post(url, {"series_token": valid_data["series_token"], first: "1"})
    assert second in missing.context["form"].errors
    repeated = client.post(url, valid_data | {first: ["5", "1.000"]})
    assert "Submit exactly one flow" in str(repeated.context["form"].errors[first])
    unexpected = client.post(url, valid_data | {"flow_unlisted": "1"})
    assert "Submit flows only for the listed grab samples" in str(
        unexpected.context["form"].non_field_errors()
    )
    for invalid_token in (
        "",
        "unrelated",
        [valid_data["series_token"], valid_data["series_token"]],
    ):
        response = client.post(url, valid_data | {"series_token": invalid_token})
        assert b"Issued source results changed" in response.content
        assert not response.context["form"].is_bound
        assert response.context["form"][first].value() is None

    client.force_login(people["approver"])
    assert client.post(url, valid_data).status_code == 403
    client.force_login(world["user_b"])
    assert client.post(url, valid_data).status_code == 404


@pytest.mark.parametrize("change", ["new-version", "singleton", "non-detect"])
def test_worksheet_discards_flows_when_another_source_report_changes(
    grab_series, world, people, client, change
):
    issue, other, (other_grab, singleton) = grab_series
    url = worksheet(issue.report)
    client.force_login(world["user_a"])
    form = client.get(url).context["form"]
    data = {"series_token": form["series_token"].value()}
    data.update({field.name: "1234.56789" for field in form.visible_fields()})

    if change == "singleton":
        services.update_report(
            actor=people["pm"],
            report_id=other.pk,
            expected_revision=other.revision,
            title=other.title,
            samples=[singleton],
        )
    else:
        revision = correct(other_grab, people, "ND" if change == "non-detect" else "0.150")
        analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
        analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    release(other, people["approver2"], reason="Synthetic source amendment")

    response = client.post(url, data)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert b"Issued source results changed" in response.content
    assert b"1234.56789" not in response.content
    summary = response.context["summary"]
    assert summary["mean"] is summary["maximum"] is None
    assert not summary["daily_values"]
    assert summary["series_token"] != data["series_token"]
    assert {row["report_version"] for row in summary["rows"] if row["report_id"] == other.pk} == {2}
    if change == "new-version":
        form = response.context["form"]
        assert not form.is_bound
        assert all(field.value() is None for field in form.visible_fields())
    else:
        assert response.context["form"] is None
        if change == "singleton":
            assert b"Reload worksheet" in response.content
            assert Decimal(client.get(url).context["summary"]["mean"]) == Decimal("0.055")
        else:
            assert b"Calculation blocked" in response.content
