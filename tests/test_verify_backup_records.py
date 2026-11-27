import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.utils import timezone

from analytical import services as analytical
from analytical.models import Result
from commercial.models import InvoiceDraft, IssuedInvoice, PriceList
from core.models import AuditEvent
from receipt.models import CustodyEvent
from receipt.services import receive_sample
from reporting.models import Report, ReportFile, ReportVersion

GROUPS = (
    "audit",
    "custody",
    "result_revisions",
    "report_versions",
    "report_files",
    "issued_invoices",
)


def _capture(*args):
    stdout = StringIO()
    call_command("verify_backup_records", *args, stdout=stdout)
    return stdout.getvalue()


def _empty_summary():
    return {"version": 1, "records": {key: {"count": 0, "max_id": None} for key in GROUPS}}


def test_empty_summary_matches_saved_copy(db, tmp_path):
    captured = _capture()
    assert json.loads(captured) == _empty_summary()
    reference = tmp_path / "summary.json"
    reference.write_text(captured, encoding="utf-8")
    assert "All six recovery record groups match" in _capture("--compare", str(reference))


def test_records_match_the_six_recovery_tables(staff, receipt_data, django_assert_num_queries):
    custodian = staff("backup-custodian", "custodian")
    analyst = staff("backup-analyst", "analyst")
    sample = receive_sample(actor=custodian, **receipt_data)
    requested = sample.requested_tests.get()
    batch = analytical.create_batch(
        actor=analyst, method=requested.definition.method, matrix="water"
    )
    analytical.add_tests_to_batch(
        actor=analyst, batch_id=batch.pk, requested_test_ids=[requested.pk]
    )
    result = Result.objects.get(requested_test=requested)
    revision = analytical.enter_result(
        actor=analyst,
        result_id=result.pk,
        supplied_text="1.000",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now(),
    )
    # Cross a decimal digit boundary. Comparing the integer keys as strings would pick 9.
    for pk in (9, 10):
        report = Report.objects.create(project=sample.order.project, created_by=custodian)
        version = ReportVersion.objects.create(
            pk=pk,
            report=report,
            number=1,
            released_by=custodian,
            released_at=timezone.now(),
            pdf_size=1,
            pdf_sha256="a" * 64,
            content_sha256="b" * 64,
        )
    ReportFile.objects.create(version=version, snapshot={}, pdf=b"x")
    draft = InvoiceDraft.objects.create(
        order=sample.order,
        number="DRAFT-SYN",
        status=InvoiceDraft.Status.ISSUED,
        price_list=PriceList.objects.create(name="Synthetic schedule"),
        price_list_revision=1,
        total=0,
        created_by=custodian,
    )
    invoice = IssuedInvoice.objects.create(
        draft=draft,
        number="INV-SYN",
        issued_by=custodian,
        issued_at=timezone.now(),
        snapshot={},
        pdf=b"x",
        sha256="c" * 64,
    )

    with django_assert_num_queries(1):
        summary = json.loads(_capture())
    for key, model in [("audit", AuditEvent), ("custody", CustodyEvent)]:
        ids = [str(pk) for pk in model.objects.values_list("pk", flat=True)]
        assert summary["records"][key] == {"count": len(ids), "max_id": max(ids)}
    assert summary["records"]["result_revisions"] == {"count": 1, "max_id": str(revision.pk)}
    assert summary["records"]["report_versions"] == {"count": 2, "max_id": "10"}
    assert summary["records"]["report_files"] == {"count": 1, "max_id": None}
    assert summary["records"]["issued_invoices"] == {"count": 1, "max_id": str(invoice.pk)}


def test_compare_reports_count_and_same_count_id_differences(staff, tmp_path):
    actor = staff("summary-check", "admin")
    AuditEvent.objects.create(
        id="11111111-1111-4111-8111-111111111110", actor=actor, action="synthetic"
    )
    saved = json.loads(_capture())
    other_id = "11111111-1111-4111-8111-111111111111"
    saved["records"]["audit"]["max_id"] = other_id
    saved["records"]["report_files"]["count"] = 1
    target = tmp_path / "source-records.json"
    target.write_text(json.dumps(saved), encoding="utf-8")

    stdout = StringIO()
    with pytest.raises(CommandError) as error:
        call_command("verify_backup_records", "--compare", str(target), stdout=stdout)
    assert "audit" in str(error.value)
    assert "report_files" in str(error.value)
    assert stdout.getvalue() == ""


@pytest.mark.parametrize(
    "group,fields",
    [
        ("custody", {"count": 0, "max_id": "11111111-1111-4111-8111-111111111111"}),
        ("custody", {"count": 1, "max_id": "1"}),
        ("audit", {"count": True, "max_id": None}),
        ("report_files", {"count": 1, "max_id": "1"}),
        ("result_revisions", {"count": 1, "max_id": None}),
        ("report_versions", {"count": 1, "max_id": "1.3"}),
        ("issued_invoices", {"count": 1, "max_id": 1}),
        ("issued_invoices", {"count": -1, "max_id": "1"}),
        ("issued_invoices", {"count": 1, "max_id": "9223372036854775808"}),
        ("issued_invoices", {"count": 1, "max_id": "1", "extra": None}),
    ],
)
def test_invalid_saved_fields_fail_before_querying(group, fields, tmp_path):
    invalid = _empty_summary()
    invalid["records"][group] = fields
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(CommandError, match=f"Invalid count or maximum identifier for {group}"):
        _capture("--compare", str(target))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version": 1, "version": 1, "records": {}}',
        b'{"version": 2, "records": {}}',
        b'{"version": true, "records": {}}',
        b'{"version": 1, "records": {}}',
        b'{"version": 1, "records": {"private-extra-category": []}}',
        b"\xff",
        b"x" * 65537,
    ],
)
def test_invalid_saved_document_fails_before_querying(payload, tmp_path):
    target = tmp_path / "invalid.json"
    target.write_bytes(payload)
    with pytest.raises(CommandError) as error:
        _capture("--compare", str(target))
    assert "private-extra-category" not in str(error.value)


def test_unreadable_saved_file_fails_before_querying(tmp_path):
    with pytest.raises(CommandError, match="Could not read the saved record summary"):
        _capture("--compare", str(tmp_path / "absent.json"))


def test_database_error_does_not_emit_a_partial_summary_or_connection_details(monkeypatch):
    def unavailable():
        raise OperationalError("private diagnostic")

    monkeypatch.setattr(connection, "cursor", unavailable)
    stdout = StringIO()
    with pytest.raises(CommandError, match="Could not read the recovery records") as error:
        call_command("verify_backup_records", stdout=stdout)
    assert "private diagnostic" not in str(error.value)
    assert stdout.getvalue() == ""
