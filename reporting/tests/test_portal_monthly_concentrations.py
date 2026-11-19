from datetime import UTC, datetime
from decimal import Decimal

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
    assert client.post(url).status_code == 405
    client.force_login(world["user_b"])
    assert client.get(url).status_code == 404
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
    for stale in (racing, client.get(original)):
        assert stale.status_code == 200
        assert not stale.context["current"]
        assert stale.context["summary"] is None
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
