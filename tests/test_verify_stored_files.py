import json
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from analytical.models import Batch, ImportSourceFile
from commercial import invoices
from commercial.models import InvoiceDraft, IssuedInvoice, PriceList, PriceListItem
from core.models import AuditEvent, Lab, LabLogo
from laboratory.models import (
    Equipment,
    EquipmentEvent,
    Nonconformance,
    NonconformanceEvent,
    TrainingRecord,
)
from receipt.models import Order, Subcontract, SubcontractResult
from receipt.services import receive_sample
from reporting import services as reporting_services
from reporting.content import assemble, fingerprint
from reporting.models import EddExport, Report, ReportFile, ReportVersion
from reporting.tests.conftest import build_world, fake_pdf, make_user

PDF = b"%PDF-1.7\x00\xff synthetic private content"
ZIP = b"PK\x03\x04\x00\xff synthetic zip"
PNG = b"\x89PNG\r\n\x1a\n\x00\xff synthetic image"
EVIDENCE = {
    "evidence_name": "synthetic private content.pdf",
    "evidence_content_type": "application/pdf",
    "evidence_data": PDF,
    "evidence_sha256": sha256(PDF).hexdigest(),
    "evidence_size": len(PDF),
}
CATEGORIES = (
    "Report PDFs",
    "Issued invoice PDFs",
    "Subcontract source PDFs",
    "EDD export ZIPs",
    "Laboratory logos",
    "Equipment event evidence",
    "Training record evidence",
    "Nonconformance event evidence",
    "Import source files",
)
MANIFEST_FORMAT = ("--format", "manifest")
OUTPUT_FORMATS = [(), ("--format", "json"), MANIFEST_FORMAT]
OUTPUT_FORMAT_IDS = ["text", "json", "manifest"]

pytestmark = pytest.mark.django_db


def _invoice_draft(order, actor, number, status):
    price_list, _ = PriceList.objects.get_or_create(name="Synthetic rates")
    attribution = (
        {"cancelled_at": timezone.now(), "cancelled_by": actor, "cancel_reason": "Synthetic void"}
        if status == InvoiceDraft.Status.VOIDED
        else {}
    )
    return InvoiceDraft.objects.create(
        order=order,
        number=number,
        status=status,
        price_list=price_list,
        price_list_revision=price_list.revision,
        total=0,
        created_by=actor,
        **attribution,
    )


@pytest.fixture
def report_sample(staff, receipt_data):
    actor = staff("restore-custodian", "custodian")
    return receive_sample(actor=actor, **receipt_data)


@pytest.fixture
def legacy_report_factory(report_sample):
    def make(version_digest=None, *, report_pk=None, record_release=True):
        sample = report_sample
        actor = sample.received_by
        now = timezone.now()
        report = Report.objects.create(
            pk=report_pk,
            project=sample.order.project,
            title="Synthetic report",
            created_by=actor,
            latest_version=1,
        )
        report.samples.add(sample)
        # Legacy release shape, before laboratory identity details and result test references.
        content = {
            "schema": 1,
            "synthetic_notice": True,
            "lab": {"name": "Example Environmental Lab", "timezone": "UTC"},
            "report": {"number": report.number, "title": report.title, "narrative": "4 °C"},
            "client": {"name": "Example Client", "contact_name": "", "email": ""},
            "project": {"name": "Example Project", "program": ""},
            "samples": [
                {
                    "lab_id": sample.lab_id,
                    "client_sample_id": sample.client_sample_id,
                    "results": [{"reported": "0.0500", "unit": "mg/L"}],
                }
            ],
            "batches": [],
            "methods": [],
            "qualifiers": [],
        }
        digest = fingerprint(content)
        snapshot = {
            "content": content,
            "content_sha256": digest,
            "release": {
                "version": 1,
                "released_by": actor.username,
                "released_at": now.isoformat(),
                "amends": None,
                "reason": "",
                "changes": [],
            },
        }
        version = ReportVersion.objects.create(
            report=report,
            number=1,
            content_sha256=digest if version_digest is None else version_digest,
            pdf_sha256=sha256(PDF).hexdigest(),
            pdf_size=len(PDF),
            released_by=actor,
            released_at=now,
        )
        if record_release:
            _record_report_release(version)
        return ReportFile(version=version, snapshot=snapshot, pdf=PDF)

    return make


@pytest.fixture
def legacy_report_file(legacy_report_factory):
    return legacy_report_factory()


def _record_report_release(version, *, after_overrides=None, **overrides):
    fields = {
        "actor": version.released_by,
        "action": "report.amended" if version.amends_id else "report.released",
        "object_type": "reporting.reportversion",
        "object_id": str(version.pk),
        "after": {
            "report": version.report.number,
            "version": version.number,
            "amends_version": version.amends.number if version.amends_id else None,
            "content_sha256": version.content_sha256,
            "pdf_sha256": version.pdf_sha256,
            "pdf_size": version.pdf_size,
        }
        | (after_overrides or {}),
    }
    return AuditEvent.objects.create(**(fields | overrides))


@pytest.fixture
def invoice_record(report_sample):
    actor = report_sample.received_by
    draft = _invoice_draft(report_sample.order, actor, "DRAFT-1", InvoiceDraft.Status.VOIDED)
    return IssuedInvoice(
        draft=draft,
        number="INV-1",
        issued_at=timezone.now(),
        issued_by=actor,
        snapshot={},
        pdf=PDF,
        sha256=sha256(PDF).hexdigest(),
    )


def _record_invoice_issue(invoice, **overrides):
    fields = {
        "actor": invoice.issued_by,
        "action": "invoice_draft.issued",
        "object_type": "commercial.invoicedraft",
        "object_id": str(invoice.draft_id),
        "after": {
            "issued_invoice": {"number": invoice.number, "sha256": invoice.sha256},
            "billing_contact_name": "Synthetic private contact",
        },
    }
    return AuditEvent.objects.create(**(fields | overrides))


def _edd_export_for(version, *, pk):
    return EddExport(
        pk=pk,
        version=version,
        format_name="NYSDEC",
        format_version="5",
        reference_values="20260429",
        mapping={},
        row_counts={},
        data=ZIP,
        sha256=sha256(ZIP).hexdigest(),
        size=len(ZIP),
        created_by=version.released_by,
    )


@pytest.fixture
def edd_record(legacy_report_file):
    legacy_report_file.save()
    return _edd_export_for(legacy_report_file.version, pk=10)


def _record_edd_export(export, *, after_overrides=None, **overrides):
    fields = {
        "actor": export.created_by,
        "action": "report.edd_exported",
        "object_type": "reporting.eddexport",
        "object_id": str(export.pk),
        "after": {
            "report": export.version.report.number,
            "version": export.version.number,
            "sha256": export.sha256,
            "content_sha256": export.version.content_sha256,
            "format": "NYSDEC 5",
            "row_counts": export.row_counts,
        }
        | (after_overrides or {}),
    }
    return AuditEvent.objects.create(**(fields | overrides))


@pytest.fixture
def document_records(report_sample, legacy_report_file, invoice_record):
    sample = report_sample
    actor = sample.received_by
    version = legacy_report_file.version
    now = version.released_at
    edd_record = _edd_export_for(version, pk=10)
    _record_invoice_issue(invoice_record)
    _record_edd_export(edd_record)
    dispatch = Subcontract.objects.create(
        requested_test=sample.requested_tests.get(),
        dispatched_at=now,
        selected_containers=[],
        created_by=actor,
    )
    return (
        legacy_report_file,
        invoice_record,
        SubcontractResult(
            subcontract=dispatch,
            number=1,
            kind="non_detect",
            reporting_limit="0.1",
            dilution="1",
            analyzed_at=now,
            source_pdf=PDF,
            source_sha256=sha256(PDF).hexdigest(),
            source_size=len(PDF),
            source_snapshot={},
            entered_by=actor,
        ),
        edd_record,
        LabLogo(
            sha256=sha256(PNG).hexdigest(),
            image=PNG,
            content_type="image/png",
            width=8,
            height=8,
            size=len(PNG),
            uploaded_by=actor,
        ),
    )


@pytest.fixture
def import_source(staff, receipt_data):
    actor = staff("restore-analyst", "analyst")
    batch = Batch.objects.create(
        method=receipt_data["tests"][0].method, matrix="water", created_by=actor
    )
    data = b"sample,result\r\nSYNTH-1,0.7500\r\n"
    filename = "synthetic results.csv"
    digest = sha256(data).hexdigest()
    event = AuditEvent.objects.create(
        actor=actor,
        action="batch.results_imported",
        object_type="analytical.batch",
        object_id=str(batch.pk),
        after={
            "source_file_stored": True,
            "filename": filename,
            "file_sha256": digest,
            "file_bytes": len(data),
        },
    )
    return ImportSourceFile(
        batch=batch, event=event, data=data, filename=filename, sha256=digest, size=len(data)
    )


@pytest.fixture
def evidence_records(staff):
    actor = staff("restore-recorder", "analyst")
    reviewer = staff("restore-reviewer", "reviewer")
    today = timezone.localdate()
    equipment = Equipment.objects.create(asset_id="SYNTH-1", name="Balance", created_by=actor)
    nonconformance = Nonconformance.objects.create(discovered_on=today, opened_by=actor)
    return {
        EquipmentEvent: dict(
            equipment=equipment,
            kind=EquipmentEvent.Kind.MAINTENANCE,
            performed_on=today,
            performed_by=actor,
            outcome=EquipmentEvent.Outcome.COMPLETED,
            recorded_by=actor,
        ),
        TrainingRecord: dict(
            staff=actor,
            record_type=TrainingRecord.Type.TRAINING,
            completed_on=today,
            effective_on=today,
            reviewed_by=reviewer,
            reviewed_on=today,
            recorded_by=actor,
        ),
        NonconformanceEvent: dict(
            nonconformance=nonconformance, kind=NonconformanceEvent.Kind.NOTE, actor=actor
        ),
    }


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_empty_database(output_format):
    stdout, stderr = StringIO(), StringIO()
    call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        expected = {
            "version": 1,
            "categories": {
                category: {"count": 0, "sha256": sha256(b"").hexdigest()} for category in CATEGORIES
            },
        }
        assert stdout.getvalue() == json.dumps(expected, indent=2, sort_keys=True) + "\n"
    elif output_format:
        assert json.loads(stdout.getvalue()) == {
            category: {"checked": 0, "failures": 0} for category in (*CATEGORIES, "Total")
        }
    else:
        assert stdout.getvalue().splitlines() == [
            f"{category}: checked=0 failures=0" for category in (*CATEGORIES, "Total")
        ]
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_intact_documents_and_optional_evidence(
    document_records, evidence_records, import_source, output_format
):
    import_source.save()
    for document in document_records:
        document.save()
    for model, fields in evidence_records.items():
        model.objects.create(**fields)
        model.objects.create(**fields, **EVIDENCE)

    stdout, stderr = StringIO(), StringIO()
    call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        manifest = json.loads(stdout.getvalue())
        assert set(manifest) == {"version", "categories"}
        assert manifest["version"] == 1
        assert set(manifest["categories"]) == set(CATEGORIES)
        for entry in manifest["categories"].values():
            assert set(entry) == {"count", "sha256"}
            assert entry["count"] == 1
            assert len(bytes.fromhex(entry["sha256"])) == 32
        version_pk = document_records[0].version_id
        row = f'["reporting.ReportVersion","{version_pk}",{len(PDF)},"{sha256(PDF).hexdigest()}"]\n'
        assert manifest["categories"]["Report PDFs"]["sha256"] == sha256(row.encode()).hexdigest()
    elif output_format:
        assert json.loads(stdout.getvalue()) == {
            **{category: {"checked": 1, "failures": 0} for category in CATEGORIES},
            "Total": {"checked": 9, "failures": 0},
        }
    else:
        assert stdout.getvalue().splitlines() == [
            *(f"{category}: checked=1 failures=0" for category in CATEGORIES),
            "Total: checked=9 failures=0",
        ]
    assert stderr.getvalue() == ""


def _verification_output(*args):
    stdout, stderr = StringIO(), StringIO()
    call_command("verify_stored_files", *args, stdout=stdout, stderr=stderr)
    assert stderr.getvalue() == ""
    return stdout.getvalue()


def test_issued_and_voided_invoices_share_one_issuance_check(report_sample, staff, monkeypatch):
    order = report_sample.order
    actor = staff("restore-manager", "project_manager")
    Lab.objects.create(name="Synthetic Environmental Laboratory")
    price_list = PriceList.objects.create(name="Synthetic rates")
    PriceListItem.objects.create(
        price_list=price_list,
        test=report_sample.requested_tests.get().definition,
        unit_price=Decimal("1.25"),
    )
    monkeypatch.setattr(invoices, "render_pdf", lambda content: PDF)

    def issue():
        draft = invoices.create_invoice_draft(
            actor=actor,
            order_id=order.pk,
            price_list_id=price_list.pk,
            expected_source_state=invoices.source_state(order, invoices.source_tests(order)),
        )
        return invoices.issue_invoice(
            actor=actor, draft_id=draft.pk, expected_revision=draft.revision
        )

    original = issue()
    invoices.void_invoice(
        actor=actor,
        draft_id=original.pk,
        expected_revision=original.revision,
        reason="Synthetic correction",
    )
    replacement = issue()

    with CaptureQueriesContext(connection) as captured:
        manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    rows = "".join(
        f'["commercial.IssuedInvoice","{draft.issued_invoice.pk}",{len(PDF)},"{sha256(PDF).hexdigest()}"]\n'
        for draft in (original, replacement)
    )
    assert manifest["categories"]["Issued invoice PDFs"] == {
        "count": 2,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    invoice_queries = [q["sql"] for q in captured if 'FROM "commercial_issuedinvoice"' in q["sql"]]
    (query,) = invoice_queries
    assert 'FROM "core_auditevent"' in query
    pdf_column = '"commercial_issuedinvoice"."pdf"'
    assert query.count(pdf_column) == 2
    assert f"pg_catalog.octet_length({pdf_column})" in query
    assert f"pg_catalog.sha256({pdf_column})" in query


def _assert_invoice_audit_failure(invoice):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", "--format", "json", stdout=stdout, stderr=stderr)
    assert json.loads(stdout.getvalue())["Issued invoice PDFs"] == {"checked": 1, "failures": 1}
    assert stderr.getvalue().splitlines() == [
        f"commercial.IssuedInvoice pk={invoice.pk}: "
        "missing, ambiguous, or mismatched invoice issuance event"
    ]


@pytest.mark.parametrize("change", ["pdf-and-digest", "number"])
def test_stored_invoice_must_match_the_original_issuance(invoice_record, change):
    invoice = invoice_record
    _record_invoice_issue(invoice)
    if change == "pdf-and-digest":
        invoice.pdf += b" synthetic replacement"
        invoice.sha256 = sha256(invoice.pdf).hexdigest()
    else:
        invoice.number = "INV-REPLACEMENT"
    invoice.save()

    _assert_invoice_audit_failure(invoice)


@pytest.mark.parametrize(
    ("record_original", "other_event"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(False, {"action": "invoice_draft.voided"}, id="void-event"),
        pytest.param(False, {"object_type": "receipt.order"}, id="wrong-object-type"),
        pytest.param(False, {"object_id": "0"}, id="wrong-draft"),
        pytest.param(False, {"after": {"issued_invoice": []}}, id="malformed-document"),
        pytest.param(True, {}, id="duplicate-matching"),
        pytest.param(True, {"after": {}}, id="duplicate-mismatched"),
    ],
)
def test_invoice_requires_one_original_issuance(invoice_record, record_original, other_event):
    invoice = invoice_record
    invoice.save()
    if record_original:
        _record_invoice_issue(invoice)
    if other_event is not None:
        _record_invoice_issue(invoice, **other_event)

    _assert_invoice_audit_failure(invoice)


def test_edd_export_provenance_preserves_the_manifest_and_query_bounds(edd_record):
    # Two exports can have the same ZIP and released version, each with its own original event.
    second = _edd_export_for(edd_record.version, pk=2)
    for export in (edd_record, second):
        export.save()
        _record_edd_export(export)
    _record_edd_export(second, action="edd.version_context_saved")
    _record_edd_export(second, object_type="reporting.reportversion")

    with CaptureQueriesContext(connection) as captured:
        manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    rows = "".join(
        f'["reporting.EddExport","{pk}",{len(ZIP)},"{edd_record.sha256}"]\n'
        for pk in (second.pk, edd_record.pk)
    )
    assert manifest["version"] == 1
    assert manifest["categories"]["EDD export ZIPs"] == {
        "count": 2,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    export_query, missing_query = [
        q["sql"] for q in captured if 'FROM "reporting_eddexport"' in q["sql"]
    ]
    zip_column = '"reporting_eddexport"."data"'
    assert export_query.count(zip_column) == 2
    assert f"pg_catalog.octet_length({zip_column})" in export_query
    assert f"pg_catalog.sha256({zip_column})" in export_query
    assert 'FROM "core_auditevent"' in export_query
    assert '"core_auditevent"."after"' not in missing_query
    assert zip_column not in missing_query


@pytest.mark.parametrize("report_pk", [1_234_567, -42])
def test_edd_export_uses_the_linked_reports_number(legacy_report_factory, report_pk):
    report_file = legacy_report_factory(report_pk=report_pk)
    report_file.save()
    export = _edd_export_for(report_file.version, pk=10)
    export.save()
    _record_edd_export(export)

    summary = json.loads(_verification_output("--format", "json"))
    assert summary["EDD export ZIPs"] == {"checked": 1, "failures": 0}


def _assert_edd_audit_failure(export):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", "--format", "json", stdout=stdout, stderr=stderr)
    assert json.loads(stdout.getvalue())["EDD export ZIPs"] == {"checked": 1, "failures": 1}
    assert stderr.getvalue().splitlines() == [
        f"reporting.EddExport pk={export.pk}: missing, ambiguous, or mismatched EDD export event"
    ]


def test_self_consistent_replacement_zip_must_match_the_original_export(edd_record):
    export = edd_record
    _record_edd_export(export)
    export.data += b" synthetic replacement"
    export.sha256 = sha256(export.data).hexdigest()
    export.size = len(export.data)
    export.save()

    _assert_edd_audit_failure(export)


@pytest.mark.parametrize(
    ("record_original", "other_event"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(False, {"action": "edd.version_context_saved"}, id="wrong-action"),
        pytest.param(False, {"object_type": "reporting.reportversion"}, id="wrong-type"),
        pytest.param(False, {"after": []}, id="malformed-payload"),
        pytest.param(False, {"after_overrides": {"report": "RPT-OTHER"}}, id="wrong-report"),
        pytest.param(False, {"after_overrides": {"version": 2}}, id="wrong-version"),
        pytest.param(False, {"after_overrides": {"version": "1"}}, id="string-version"),
        pytest.param(False, {"after_overrides": {"version": True}}, id="boolean-version"),
        pytest.param(False, {"after_overrides": {"version": 1.0}}, id="fractional-version"),
        pytest.param(
            False, {"after_overrides": {"content_sha256": "a" * 64}}, id="wrong-report-content"
        ),
        pytest.param(True, {}, id="duplicate-matching"),
        pytest.param(True, {"after": {}}, id="duplicate-mismatched"),
    ],
)
def test_edd_requires_one_original_export_event(edd_record, record_original, other_event):
    edd_record.save()
    if record_original:
        _record_edd_export(edd_record)
    if other_event is not None:
        _record_edd_export(edd_record, **other_event)

    _assert_edd_audit_failure(edd_record)


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_orphan_edd_export_events_are_verification_failures(edd_record, output_format):
    missing = _record_edd_export(edd_record)
    malformed = _record_edd_export(edd_record, object_id="synthetic malformed target")
    _record_edd_export(edd_record, action="edd.version_context_saved")
    _record_edd_export(edd_record, object_type="reporting.reportversion")

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    elif output_format:
        assert json.loads(stdout.getvalue())["EDD export ZIPs"] == {"checked": 2, "failures": 2}
    else:
        assert "EDD export ZIPs: checked=2 failures=2" in stdout.getvalue().splitlines()
    assert stderr.getvalue().splitlines() == [
        f"core.AuditEvent pk={pk}: missing EddExport" for pk in sorted([missing.pk, malformed.pk])
    ]


@pytest.mark.django_db(transaction=True)
def test_original_and_amended_releases_preserve_the_manifest_and_stream_report_rows(monkeypatch):
    people = {
        "analyst": make_user("restore-analyst", "analyst"),
        "reviewer": make_user("restore-reviewer", "reviewer"),
        "approver": make_user("restore-approver", "approver", release=True),
        "pm": make_user("restore-manager", "project_manager"),
        "custodian": make_user("restore-custodian", "custodian"),
    }
    world = build_world(people)
    report, sample = world["report_a"], world["sample_a"]
    monkeypatch.setattr(reporting_services, "render_pdf", fake_pdf)

    def release(reason=""):
        actor = people["approver"]
        return reporting_services.release_report(
            actor=actor,
            report_id=report.pk,
            expected_fingerprint=assemble(report, actor=actor).fingerprint,
            reason=reason,
        )

    original = release()
    report = reporting_services.update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        samples=[sample],
        title="Amended outfall report",
    )
    amended = release("Correct report title")
    assert amended.amends_id == original.pk
    assert amended.content_sha256 != original.content_sha256
    Report.objects.filter(pk=report.pk).update(narrative="Unissued draft notes")

    def check_cursor(execute, query, params, many, context):
        if 'FROM "reporting_reportversion"' in query:
            assert not connection.get_autocommit()
            assert not context["cursor"].cursor.withhold
        return execute(query, params, many, context)

    assert connection.get_autocommit()
    with CaptureQueriesContext(connection) as captured, connection.execute_wrapper(check_cursor):
        manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    assert connection.get_autocommit()
    rows = "".join(
        json.dumps(
            ("reporting.ReportVersion", str(version.pk), version.pdf_size, version.pdf_sha256),
            separators=(",", ":"),
        )
        + "\n"
        for version in (original, amended)
    )
    assert manifest["categories"]["Report PDFs"] == {
        "count": 2,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    report_queries = [q["sql"] for q in captured if 'FROM "reporting_reportversion"' in q["sql"]]
    query, orphan_query = report_queries
    pdf_column = '"reporting_reportfile"."pdf"'
    assert query.count(pdf_column) == 2
    assert f"pg_catalog.octet_length({pdf_column})" in query
    assert f"pg_catalog.sha256({pdf_column})" in query
    assert 'FROM "core_auditevent"' in query
    assert '"core_auditevent"."after"' not in orphan_query
    assert pdf_column not in orphan_query


def _assert_report_audit_failure(version, *, checked=1):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", "--format", "json", stdout=stdout, stderr=stderr)
    assert json.loads(stdout.getvalue())["Report PDFs"] == {"checked": checked, "failures": 1}
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={version.pk}: "
        "missing, ambiguous, or mismatched report release history"
    ]


@pytest.mark.parametrize(
    ("record_original", "other_event"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(False, {"action": "report.amended"}, id="wrong-action"),
        pytest.param(False, {"after": {}}, id="malformed-after"),
        pytest.param(False, {"after": "synthetic string"}, id="string-after"),
        pytest.param(False, {"after_overrides": {"report": "RPT-000000"}}, id="wrong-number"),
        pytest.param(False, {"after_overrides": {"version": 2}}, id="wrong-version"),
        pytest.param(False, {"after_overrides": {"version": 1.0}}, id="decimal-version"),
        pytest.param(False, {"after_overrides": {"version": "1"}}, id="string-version"),
        pytest.param(False, {"after_overrides": {"amends_version": "null"}}, id="string-null"),
        pytest.param(False, {"after_overrides": {"pdf_size": len(PDF) + 1}}, id="wrong-pdf-size"),
        pytest.param(False, {"after_overrides": {"pdf_size": float(len(PDF))}}, id="decimal-size"),
        pytest.param(False, {"after_overrides": {"pdf_sha256": "a" * 64}}, id="wrong-pdf-digest"),
        pytest.param(False, {"after_overrides": {"content_sha256": "b" * 64}}, id="wrong-content"),
        pytest.param(True, {}, id="duplicate-matching"),
        pytest.param(True, {"after": {}}, id="duplicate-mismatched"),
        pytest.param(True, {"action": "report.amended"}, id="both-actions"),
    ],
)
def test_report_requires_one_matching_release_event(
    legacy_report_factory, record_original, other_event
):
    report_file = legacy_report_factory(record_release=record_original)
    report_file.save()
    if other_event is not None:
        _record_report_release(report_file.version, **other_event)

    _assert_report_audit_failure(report_file.version)


def test_release_event_must_have_the_version_actor(legacy_report_factory, staff):
    report_file = legacy_report_factory(record_release=False)
    report_file.save()
    _record_report_release(report_file.version, actor=staff("different-release-actor", "reviewer"))

    _assert_report_audit_failure(report_file.version)


@pytest.mark.parametrize("field", ["pdf_sha256", "content_sha256"])
def test_release_event_checksum_fields_must_be_strings(legacy_report_factory, field):
    report_file = legacy_report_factory(record_release=False)
    report_file.save()
    version = report_file.version
    _record_report_release(version, after_overrides={field: [getattr(version, field)]})

    _assert_report_audit_failure(version)


@pytest.mark.parametrize("replacement", ["pdf", "content"])
def test_report_replacement_with_matching_digests_fails_against_the_release_event(
    legacy_report_file, replacement
):
    report_file = legacy_report_file
    if replacement == "pdf":
        report_file.pdf += b" replacement"
        fields = {
            "pdf_sha256": sha256(report_file.pdf).hexdigest(),
            "pdf_size": len(report_file.pdf),
        }
    else:
        report_file.snapshot["content"]["report"]["title"] = "Replaced report title"
        content_hash = fingerprint(report_file.snapshot["content"])
        report_file.snapshot["content_sha256"] = content_hash
        fields = {"content_sha256": content_hash}
    report_file.save()
    # Simulate a restored copy with self-consistent file data and different release metadata.
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(
            "ALTER TABLE reporting_reportversion DISABLE TRIGGER issued_record_immutable"
        )
        ReportVersion.objects.filter(pk=report_file.version_id).update(**fields)
        cursor.execute("ALTER TABLE reporting_reportversion ENABLE TRIGGER issued_record_immutable")

    _assert_report_audit_failure(report_file.version)


@pytest.mark.parametrize(
    "defect", ["cross-report", "skipped-version", "wrong-predecessor", "decimal-predecessor"]
)
def test_amendment_event_must_match_the_linked_predecessor(legacy_report_factory, defect):
    original_file = legacy_report_factory()
    original_file.save()
    predecessor = original_file.version
    checked = 2
    if defect == "cross-report":
        unrelated_file = legacy_report_factory()
        unrelated_file.save()
        predecessor = unrelated_file.version
        checked += 1
    version = ReportVersion.objects.create(
        report=original_file.version.report,
        number=3 if defect == "skipped-version" else 2,
        amends=predecessor,
        amendment_reason="Synthetic amendment",
        content_sha256=original_file.version.content_sha256,
        pdf_sha256=predecessor.pdf_sha256,
        pdf_size=predecessor.pdf_size,
        released_by=predecessor.released_by,
        released_at=predecessor.released_at,
    )
    ReportFile.objects.create(version=version, snapshot=original_file.snapshot, pdf=PDF)
    invalid_values = {"wrong-predecessor": 2, "decimal-predecessor": 1.0}
    _record_report_release(
        version, after_overrides={"amends_version": invalid_values.get(defect, predecessor.number)}
    )

    _assert_report_audit_failure(version, checked=checked)


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_orphan_report_release_events_are_verification_failures(legacy_report_file, output_format):
    legacy_report_file.save()
    version = legacy_report_file.version
    events = (
        _record_report_release(version, object_id="0"),
        _record_report_release(version, action="report.amended", object_id="invalid ID"),
        _record_report_release(version, object_type="reporting.report"),
    )
    _record_report_release(version, action="report.updated", object_type="reporting.report")

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    elif output_format:
        assert json.loads(stdout.getvalue())["Report PDFs"] == {"checked": 4, "failures": 3}
    else:
        assert "Report PDFs: checked=4 failures=3" in stdout.getvalue().splitlines()
    assert stderr.getvalue().splitlines() == [
        f"core.AuditEvent pk={pk}: missing or invalid ReportVersion target"
        for pk in sorted(event.pk for event in events)
    ]


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_changed_snapshot_content_fails_with_intact_pdf(legacy_report_file, output_format):
    report_file = legacy_report_file
    report_file.snapshot["content"]["samples"][0]["results"][0]["reported"] = "0.05"
    report_file.save()

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    elif output_format:
        assert json.loads(stdout.getvalue())["Report PDFs"] == {"checked": 1, "failures": 1}
    else:
        assert stdout.getvalue().splitlines()[0] == "Report PDFs: checked=1 failures=1"
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={report_file.version_id}: report content SHA-256 does not "
        "match the version's digest, report content SHA-256 does not match the snapshot's digest"
    ]


@pytest.mark.parametrize(
    "snapshot",
    [[], "synthetic private content", {}, {"content": None}, {"content": {}}, {"content": []}],
)
def test_missing_or_malformed_snapshot_content(legacy_report_file, snapshot):
    report_file = legacy_report_file
    report_file.snapshot = snapshot
    report_file.save()

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *MANIFEST_FORMAT, stdout=stdout, stderr=stderr)
    assert stdout.getvalue() == ""
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={report_file.version_id}: "
        "missing or invalid report snapshot content"
    ]


@pytest.mark.parametrize(
    ("digest_location", "digest"),
    [
        pytest.param("version", "", id="missing-version"),
        pytest.param("version", "synthetic private content", id="invalid-version"),
        pytest.param("version", "b" * 64, id="mismatched-version"),
        pytest.param("snapshot", None, id="missing-snapshot"),
        pytest.param("snapshot", 5, id="non-string-snapshot"),
        pytest.param("snapshot", "b" * 65, id="overlong-snapshot"),
        pytest.param("snapshot", "b" * 64, id="mismatched-snapshot"),
    ],
)
def test_rejects_missing_malformed_or_changed_digest(
    legacy_report_factory, digest_location, digest
):
    report_file = legacy_report_factory(digest if digest_location == "version" else None)
    if digest_location == "snapshot":
        if digest is None:
            del report_file.snapshot["content_sha256"]
        else:
            report_file.snapshot["content_sha256"] = digest
    report_file.save()

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *MANIFEST_FORMAT, stdout=stdout, stderr=stderr)
    assert stdout.getvalue() == ""
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={report_file.version_id}: report content SHA-256 does not "
        f"match the {digest_location}'s digest"
    ]


def test_marked_import_event_requires_its_file_but_legacy_events_do_not(import_source):
    event = import_source.event
    old_metadata = event.after.copy()
    del old_metadata["source_file_stored"]
    AuditEvent.objects.create(
        actor=event.actor,
        action=event.action,
        object_type=event.object_type,
        object_id=event.object_id,
        after=old_metadata,
    )
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *MANIFEST_FORMAT, stdout=stdout, stderr=stderr)
    assert stdout.getvalue() == ""
    assert stderr.getvalue().splitlines() == [
        f"core.AuditEvent pk={event.pk}: missing ImportSourceFile"
    ]


@pytest.mark.parametrize("change", ["batch", "content"])
def test_verified_bytes_must_match_the_recorded_import(import_source, change):
    if change == "batch":
        import_source.batch = Batch.objects.create(
            method=import_source.batch.method, matrix="water", created_by=import_source.event.actor
        )
    else:
        import_source.data = import_source.data.replace(b"0.7500", b"0.7501")
        import_source.sha256 = sha256(import_source.data).hexdigest()
    import_source.save()
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", "--format", "json", stdout=stdout, stderr=stderr)
    assert json.loads(stdout.getvalue())["Import source files"] == {"checked": 1, "failures": 1}
    assert stderr.getvalue().splitlines() == [
        f"analytical.ImportSourceFile pk={import_source.pk}: "
        "import event does not match the stored source"
    ]


def test_manifest_hashes_stable_pk_ordered_rows(evidence_records):
    fields = evidence_records[EquipmentEvent] | EVIDENCE
    EquipmentEvent.objects.create(pk=42, **fields)
    EquipmentEvent.objects.create(pk=10, **fields)

    expected_rows = (
        f'["laboratory.EquipmentEvent","10",{len(PDF)},"{sha256(PDF).hexdigest()}"]\n'
        f'["laboratory.EquipmentEvent","42",{len(PDF)},"{sha256(PDF).hexdigest()}"]\n'
    ).encode("ascii")
    manifest = _verification_output(*MANIFEST_FORMAT)
    assert manifest == _verification_output(*MANIFEST_FORMAT)
    assert json.loads(manifest)["categories"]["Equipment event evidence"] == {
        "count": 2,
        "sha256": sha256(expected_rows).hexdigest(),
    }


@pytest.mark.parametrize("change", ["content", "identity"])
def test_manifest_detects_changes_with_the_same_file_count(evidence_records, change):
    fields = evidence_records[EquipmentEvent] | EVIDENCE
    original = EquipmentEvent.objects.create(**fields)
    before = json.loads(_verification_output(*MANIFEST_FORMAT))["categories"]

    if change == "content":
        different_pdf = PDF[:-1] + b"x"
        EquipmentEvent.objects.filter(pk=original.pk).update(
            evidence_data=different_pdf, evidence_sha256=sha256(different_pdf).hexdigest()
        )
    else:
        original.delete()
        EquipmentEvent.objects.create(**fields)

    after = json.loads(_verification_output(*MANIFEST_FORMAT))["categories"]
    recorded = before.pop("Equipment event evidence")
    restored = after.pop("Equipment event evidence")
    assert after == before
    assert restored["count"] == recorded["count"] == 1
    assert restored["sha256"] != recorded["sha256"]


@contextmanager
def _exported_snapshot():
    exporter = connection.copy()
    try:
        exporter.set_autocommit(False)
        with exporter.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("SELECT pg_catalog.pg_export_snapshot()")
            (snapshot,) = cursor.fetchone()
        yield snapshot
    finally:
        exporter.close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_snapshot_verifies_committed_files_at_export_time(staff, output_format):
    uploader = staff("snapshot-uploader", "admin")
    logo_fields = {"uploaded_by": uploader, "content_type": "image/png", "width": 8, "height": 8}
    LabLogo.objects.create(image=PNG, sha256=sha256(PNG).hexdigest(), size=len(PNG), **logo_fields)
    before = _verification_output(*output_format)

    def check_transaction(execute, query, params, many, context):
        with connection.connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            assert cursor.fetchone() == ("repeatable read",)
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone() == ("on",)
        return execute(query, params, many, context)

    with _exported_snapshot() as snapshot:
        later = PNG + b" committed after export"
        LabLogo.objects.create(
            image=later, sha256=sha256(later).hexdigest(), size=len(later), **logo_fields
        )
        with connection.execute_wrapper(check_transaction):
            assert _verification_output(*output_format, "--snapshot", snapshot) == before
        assert connection.get_autocommit()
        assert _verification_output(*output_format) != before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_expired_snapshot_fails_without_an_inventory(output_format):
    with _exported_snapshot() as snapshot:
        pass

    stdout, stderr = StringIO(), StringIO()
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(CommandError, match="Could not import the PostgreSQL snapshot") as error:
            call_command(
                "verify_stored_files",
                *output_format,
                "--snapshot",
                snapshot,
                stdout=stdout,
                stderr=stderr,
            )

    assert snapshot not in str(error.value)
    assert all(snapshot not in query["sql"] for query in captured)
    assert stdout.getvalue() == stderr.getvalue() == ""
    assert connection.get_autocommit()
    assert _verification_output(*output_format)


@pytest.mark.parametrize("snapshot", ["", "synthetic invalid snapshot"])
def test_malformed_snapshot_fails_without_an_inventory(snapshot):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match=r"^--snapshot requires a PostgreSQL snapshot ID\.$"):
        call_command("verify_stored_files", snapshot=snapshot, stdout=stdout, stderr=stderr)
    assert stdout.getvalue() == stderr.getvalue() == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("caller_transaction", ["django", "psycopg"])
def test_snapshot_does_not_reuse_caller_transaction(caller_transaction):
    connection.ensure_connection()
    begin = (
        transaction.atomic if caller_transaction == "django" else connection.connection.transaction
    )
    with _exported_snapshot() as snapshot, begin():
        stdout, stderr = StringIO(), StringIO()
        with pytest.raises(CommandError, match="requires a fresh PostgreSQL transaction"):
            call_command("verify_stored_files", snapshot=snapshot, stdout=stdout, stderr=stderr)
        assert stdout.getvalue() == stderr.getvalue() == ""
        assert _verification_output()


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_corrupt_and_missing_rows_report_ids_and_fail(
    document_records, evidence_records, import_source, output_format
):
    import_source.data = import_source.data.replace(b"0.7500", b"0.7501")
    import_source.save()
    report_file, invoice, subcontract, export, logo = document_records
    version = report_file.version
    missing_report = ReportVersion.objects.create(
        report=version.report,
        number=2,
        amends=version,
        amendment_reason="Synthetic amendment",
        content_sha256=version.content_sha256,
        pdf_sha256=version.pdf_sha256,
        pdf_size=version.pdf_size,
        released_by=version.released_by,
        released_at=version.released_at,
    )
    _record_report_release(missing_report)
    order = Order.objects.create(project=version.report.project, created_by=invoice.issued_by)
    missing_issued = _invoice_draft(order, invoice.issued_by, "DRAFT-2", InvoiceDraft.Status.ISSUED)
    missing_voided = _invoice_draft(order, invoice.issued_by, "DRAFT-3", InvoiceDraft.Status.VOIDED)
    InvoiceDraft.objects.filter(pk=invoice.draft_id).update(
        status=InvoiceDraft.Status.DRAFT, cancelled_at=None, cancelled_by=None, cancel_reason=""
    )

    report_file.pdf = PDF[:-1] + b"x"
    report_file.snapshot["content"]["report"]["title"] = "Damaged synthetic snapshot"
    invoice.pdf = PDF[:-1] + b"x"
    subcontract.source_size += 1
    export.data = b""
    logo.sha256 = "1" * 64
    for document in document_records:
        document.save()

    equipment = EquipmentEvent.objects.create(
        **evidence_records[EquipmentEvent], evidence_content_type="application/pdf"
    )
    training = TrainingRecord.objects.create(
        **evidence_records[TrainingRecord], **(EVIDENCE | {"evidence_data": None})
    )
    nonconformance = NonconformanceEvent.objects.create(
        **evidence_records[NonconformanceEvent], **(EVIDENCE | {"evidence_name": ""})
    )

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    elif output_format:
        assert json.loads(stdout.getvalue()) == {
            "Report PDFs": {"checked": 2, "failures": 2},
            "Issued invoice PDFs": {"checked": 3, "failures": 3},
            **{category: {"checked": 1, "failures": 1} for category in CATEGORIES[2:]},
            "Total": {"checked": 12, "failures": 12},
        }
    else:
        assert stdout.getvalue().splitlines() == [
            "Report PDFs: checked=2 failures=2",
            "Issued invoice PDFs: checked=3 failures=3",
            *(f"{category}: checked=1 failures=1" for category in CATEGORIES[2:]),
            "Total: checked=12 failures=12",
        ]
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={version.pk}: SHA-256 mismatch, report content SHA-256 does "
        "not match the version's digest, report content SHA-256 does not match the snapshot's "
        "digest",
        f"reporting.ReportVersion pk={missing_report.pk}: missing ReportFile",
        f"commercial.IssuedInvoice pk={invoice.pk}: SHA-256 mismatch",
        f"commercial.InvoiceDraft pk={missing_issued.pk}: missing IssuedInvoice",
        f"commercial.InvoiceDraft pk={missing_voided.pk}: missing IssuedInvoice",
        f"receipt.SubcontractResult pk={subcontract.pk}: byte length mismatch",
        f"reporting.EddExport pk={export.pk}: empty stored file, SHA-256 mismatch, "
        "byte length mismatch",
        f"core.LabLogo pk={logo.pk}: SHA-256 mismatch",
        f"laboratory.EquipmentEvent pk={equipment.pk}: missing file, missing evidence filename or "
        "content type, missing recorded SHA-256, missing recorded byte length",
        f"laboratory.TrainingRecord pk={training.pk}: missing file",
        f"laboratory.NonconformanceEvent pk={nonconformance.pk}: "
        "missing evidence filename or content type",
        f"analytical.ImportSourceFile pk={import_source.pk}: SHA-256 mismatch",
    ]
