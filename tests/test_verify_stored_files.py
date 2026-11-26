import json
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from psycopg import sql

from analytical.models import Batch, ImportSourceFile
from commercial import invoices
from commercial.models import InvoiceDraft, IssuedInvoice, PriceList, PriceListItem
from core.models import AuditEvent, Lab, LabLogo
from laboratory import equipment, nonconformance, training
from laboratory.models import (
    Equipment,
    EquipmentEvent,
    Nonconformance,
    NonconformanceEvent,
    TrainingRecord,
)
from receipt.models import Order, SubcontractResult
from receipt.services import receive_sample
from receipt.subcontracts import dispatch_subcontract, enter_external_result, review_external_result
from reporting import services as reporting_services
from reporting.content import assemble, fingerprint
from reporting.models import EddExport, Report, ReportFile, ReportVersion
from reporting.tests.conftest import build_world, fake_pdf, make_user

PDF = b"%PDF-1.7\x00\xff synthetic private content"
SOURCE_PDF = PDF + b"\n%%EOF\n"
ZIP = b"PK\x03\x04\x00\xff synthetic zip"
PNG = b"\x89PNG\r\n\x1a\n\x00\xff synthetic image"
EVIDENCE_NAME = "synthetic private content.pdf"
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
    def make(version_digest=None, *, report_pk=None, record_release=True, frozen_content=None):
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
        if frozen_content is not None:
            content = frozen_content
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
    draft = _invoice_draft(report_sample.order, actor, "DRAFT-1", InvoiceDraft.Status.ISSUED)
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
            "issued_invoice": {
                "number": invoice.number,
                "issued_at": invoice.issued_at.isoformat(),
                "issued_by": invoice.issued_by_id,
                "sha256": invoice.sha256,
            },
            "billing_contact_name": "Synthetic private contact",
        },
    }
    return AuditEvent.objects.create(**(fields | overrides))


@pytest.fixture
def voided_invoice_record(invoice_record, staff):
    invoice_record.save()
    _record_invoice_issue(invoice_record)
    invoice_record.draft = invoices.void_invoice(
        actor=staff("restore-voiding-manager", "project_manager"),
        draft_id=invoice_record.draft_id,
        expected_revision=invoice_record.draft.revision,
        reason="Synthetic void reason",
    )
    event = AuditEvent.objects.get(
        action="invoice_draft.voided", object_id=str(invoice_record.draft_id)
    )
    return invoice_record, event


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
def subcontract_source_factory(report_sample, staff):
    sample = report_sample
    analyst = staff("restore-source-enterer", "analyst")
    dispatch = dispatch_subcontract(
        actor=sample.received_by,
        requested_test_id=sample.requested_tests.get().pk,
        expected_sample_revision=sample.revision,
        container_ids=list(sample.containers.values_list("pk", flat=True)),
        external_lab="Synthetic external laboratory",
        external_reference="SYNTHETIC-DISPATCH",
        external_sample_id="SYNTHETIC-EXTERNAL-SAMPLE",
        dispatched_at=timezone.now(),
        handed_to="Synthetic courier",
        reason="Synthetic external testing",
    )

    def enter(**overrides):
        dispatch.refresh_from_db()
        return enter_external_result(
            **(
                {
                    "actor": analyst,
                    "subcontract_id": dispatch.pk,
                    "expected_revision": dispatch.revision,
                    "observed_text": "0.7500",
                    "reporting_limit": "0.1000",
                    "dilution": "1.0",
                    "unit": "mg/L",
                    "external_method": "SYNTHETIC EXTERNAL 1",
                    "external_analyst": "Synthetic external analyst",
                    "analyzed_at": timezone.now(),
                    "qualifier_text": "",
                    "external_report_reference": "SYNTHETIC-EXTERNAL-REPORT",
                    "source_pdf": SOURCE_PDF,
                }
                | overrides
            )
        )

    return enter


def _lab_logo(uploader, data=PNG):
    return LabLogo(
        sha256=sha256(data).hexdigest(),
        image=data,
        content_type="image/png",
        width=96,
        height=32,
        size=len(data),
        uploaded_by=uploader,
    )


@pytest.fixture
def document_records(legacy_report_file, invoice_record, subcontract_source_factory):
    edd_record = _edd_export_for(legacy_report_file.version, pk=10)
    _record_invoice_issue(invoice_record)
    _record_edd_export(edd_record)
    return (
        legacy_report_file,
        invoice_record,
        subcontract_source_factory(),
        edd_record,
        _lab_logo(legacy_report_file.version.released_by),
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
def nonconformance_factory(staff):
    actor = staff("restore-nonconformance-recorder", "reviewer")

    def open_record(**overrides):
        fields = {
            "actor": actor,
            "title": "Synthetic nonconformance",
            "description": "Synthetic private nonconformance details",
            "severity": Nonconformance.Severity.MINOR,
            "discovered_on": timezone.localdate(),
        }
        return nonconformance.open_nonconformance(**(fields | overrides))

    return open_record


@pytest.fixture
def nonconformance_note_factory(nonconformance_factory):
    record = nonconformance_factory()

    def add(*, attached=True, **overrides):
        fields = {
            "actor": record.opened_by,
            "nc_id": record.pk,
            "note": "Synthetic private nonconformance note",
            "evidence": SimpleUploadedFile(EVIDENCE_NAME, PDF) if attached else None,
        }
        return nonconformance.add_note(**(fields | overrides))

    return add


@pytest.fixture
def nonconformance_lifecycle_factory(nonconformance_factory):
    def record(*attached_at):
        def attachment(name):
            return SimpleUploadedFile(EVIDENCE_NAME, PDF) if name in attached_at else None

        parent = nonconformance_factory(evidence=attachment("opened"))
        status = Nonconformance.Status
        steps = (
            ("investigating", status.INVESTIGATING, {"owner": parent.opened_by}),
            (
                "action",
                status.ACTION,
                {"root_cause": "Synthetic cause", "disposition": Nonconformance.Disposition.REPEAT},
            ),
            ("verification", status.VERIFICATION, {"corrective_action": "Synthetic action"}),
            ("return_to_action", status.ACTION, {"note": "Synthetic reason for returning"}),
            ("second_verification", status.VERIFICATION, {}),
            (
                "closed",
                status.CLOSED,
                {"verification": "Synthetic verification", "reference": "SYNTHETIC-RERUN"},
            ),
            ("reopened", status.INVESTIGATING, {"note": "Synthetic reason for reopening"}),
        )
        names = ["opened"]
        for name, to_status, fields in steps:
            parent = nonconformance.transition(
                actor=parent.opened_by,
                nc_id=parent.pk,
                expected_revision=parent.revision,
                to_status=to_status,
                evidence=attachment(name),
                **fields,
            )
            names.append(name)
        return dict(zip(names, parent.events.all(), strict=True))

    return record


@pytest.fixture
def equipment_event_factory(staff):
    recorder = staff("restore-equipment-recorder", "reviewer")
    performer = staff("restore-equipment-performer", "analyst")
    asset = equipment.save_equipment(
        actor=recorder, asset_id="SYNTH-1", name="Synthetic balance", location="Synthetic bench"
    )

    def record(*, kind=EquipmentEvent.Kind.MAINTENANCE, attached=True, **overrides):
        outcome = (
            EquipmentEvent.Outcome.ACCEPTABLE
            if kind == EquipmentEvent.Kind.CALIBRATION
            else EquipmentEvent.Outcome.COMPLETED
        )
        fields = {
            "actor": recorder,
            "equipment_id": asset.pk,
            "performed_on": timezone.localdate(),
            "performed_by": performer,
            "outcome": outcome,
            "description": "Synthetic private equipment notes",
            "evidence": SimpleUploadedFile(EVIDENCE_NAME, PDF) if attached else None,
        }
        return equipment.record_equipment_event(kind=kind, **(fields | overrides))

    return record


@pytest.fixture
def training_record_factory(staff):
    trainee = staff("restore-trainee", "analyst")
    reviewer = staff("restore-training-reviewer", "reviewer")
    today = timezone.localdate()

    def record(*, attached=True, **overrides):
        fields = {
            "actor": reviewer,
            "staff": trainee,
            "record_type": TrainingRecord.Type.TRAINING,
            "title": "Synthetic training",
            "completed_on": today,
            "effective_on": today,
            "reviewed_by": reviewer,
            "reviewed_on": today,
            "evidence": SimpleUploadedFile(EVIDENCE_NAME, PDF) if attached else None,
        }
        return training.record_training(**(fields | overrides))

    return record


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
    document_records,
    nonconformance_note_factory,
    import_source,
    equipment_event_factory,
    training_record_factory,
    output_format,
):
    import_source.save()
    report_file, invoice, _, export, logo = document_records
    for document in (report_file, invoice, export, logo):
        document.save()
    nonconformance_note_factory(attached=False)
    nonconformance_note_factory()
    equipment_event_factory()
    equipment_event_factory(
        attached=False, reference="Synthetic equipment reference without a file"
    )
    training_record_factory()
    training_record_factory(attached=False, reference="Synthetic reference without a file")

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
        version_pk = report_file.version_id
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


def _source_entry(source):
    return AuditEvent.objects.get(
        action="subcontract.result_entered",
        object_type="receipt.subcontract",
        object_id=str(source.subcontract_id),
        after__latest_number=source.number,
    )


@contextmanager
def _restore_with_history_defect(model, trigger):
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        table_and_trigger = (sql.Identifier(model._meta.db_table), sql.Identifier(trigger))
        cursor.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(*table_and_trigger))
        try:
            yield
        finally:
            cursor.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(*table_and_trigger))


def _equipment_event(record):
    return AuditEvent.objects.get(
        object_type=Equipment._meta.label_lower,
        object_id=str(record.equipment_id),
        after__event=record.pk,
    )


def _assert_equipment_verification_failed(
    *problems, checked=None, output_format=("--format", "json")
):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match=r"^Stored file verification failed\.?$"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    else:
        checked = len(problems) if checked is None else checked
        assert json.loads(stdout.getvalue())["Equipment event evidence"] == {
            "checked": checked,
            "failures": len(problems),
        }
    assert stderr.getvalue().splitlines() == list(problems)


def _equipment_history_problem(record):
    return (
        f"laboratory.EquipmentEvent pk={record.pk}: "
        "missing, ambiguous, or mismatched equipment evidence event"
    )


@pytest.mark.parametrize("defect", ["bytes", "length", "replacement", "filename"])
def test_equipment_evidence_requires_intact_file_and_original_values(
    equipment_event_factory, defect
):
    record = equipment_event_factory()
    if defect == "bytes":
        fields = {"evidence_data": PDF[:-1] + b"x"}
        problem = f"laboratory.EquipmentEvent pk={record.pk}: SHA-256 mismatch"
    elif defect == "length":
        fields = {"evidence_size": record.evidence_size + 1}
        problem = f"laboratory.EquipmentEvent pk={record.pk}: byte length mismatch"
    elif defect == "replacement":
        replacement = PDF + b" synthetic replacement"
        fields = {
            "evidence_data": replacement,
            "evidence_size": len(replacement),
            "evidence_sha256": sha256(replacement).hexdigest(),
        }
        problem = _equipment_history_problem(record)
    else:
        fields = {"evidence_name": "Synthetic private replacement filename.pdf"}
        problem = _equipment_history_problem(record)
    EquipmentEvent.objects.filter(pk=record.pk).update(**fields)

    _assert_equipment_verification_failed(problem)


@pytest.mark.parametrize(
    "defect", ["actor", "action", "kind", "equipment", "object-id", "object-type", "corrects"]
)
def test_equipment_evidence_event_identifies_the_record(equipment_event_factory, defect):
    original = equipment_event_factory(attached=False)
    record = equipment_event_factory(
        corrects_id=original.pk, correction_reason="Synthetic correction"
    )
    other_equipment = equipment.save_equipment(
        actor=record.recorded_by,
        asset_id="SYNTH-2",
        name="Synthetic pH meter",
        location="Synthetic wet laboratory",
    )
    other_original = equipment_event_factory(attached=False)
    event = _equipment_event(record)
    changes = {
        "actor": {"actor": record.performed_by},
        "action": {"action": "equipment.maintenance_recorded"},
        "kind": {"after": event.after | {"kind": EquipmentEvent.Kind.CALIBRATION}},
        "equipment": {"after": event.after | {"equipment": other_equipment.pk}},
        "object-id": {"object_id": str(other_equipment.pk)},
        "object-type": {"object_type": EquipmentEvent._meta.label_lower},
        "corrects": {"after": event.after | {"corrects": other_original.pk}},
    }
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(**changes[defect])

    _assert_equipment_verification_failed(_equipment_history_problem(record))


@pytest.mark.parametrize(
    "defect", ["absent", "duplicate", "different-recording-action", "different-asset"]
)
def test_equipment_attachment_requires_one_recording_event(equipment_event_factory, defect):
    record = equipment_event_factory(kind=EquipmentEvent.Kind.CALIBRATION)
    event = _equipment_event(record)
    if defect == "absent":
        with _restore_with_history_defect(AuditEvent, "audit_immutable"):
            AuditEvent.objects.filter(pk=event.pk).delete()
    else:
        object_id = event.object_id
        if defect == "different-asset":
            asset = equipment.save_equipment(
                actor=record.recorded_by,
                asset_id="SYNTH-2",
                name="Synthetic pH meter",
                location="Synthetic wet laboratory",
            )
            object_id = str(asset.pk)
        AuditEvent.objects.create(
            actor=event.actor,
            action=(
                event.action
                if defect != "different-recording-action"
                else "equipment.event_corrected"
            ),
            object_type=event.object_type,
            object_id=object_id,
            after=event.after,
        )

    _assert_equipment_verification_failed(_equipment_history_problem(record))


@pytest.mark.parametrize(
    "defect", ["missing-event", "string-event", "decimal-event", "large-event"]
)
def test_equipment_attachment_rejects_malformed_event_ids(equipment_event_factory, defect):
    record = equipment_event_factory()
    event = _equipment_event(record)
    if defect == "missing-event":
        del event.after["event"]
    else:
        event.after["event"] = {
            "string-event": str(record.pk),
            "decimal-event": float(record.pk),
            "large-event": 10**40,
        }[defect]
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(after=event.after)

    _assert_equipment_verification_failed(
        _equipment_history_problem(record),
        f"core.AuditEvent pk={event.pk}: missing or invalid EquipmentEvent target",
    )


def test_equipment_attachment_cannot_be_attributed_to_a_different_event(equipment_event_factory):
    record = equipment_event_factory()
    other = equipment_event_factory()
    event = _equipment_event(record)
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(after=event.after | {"event": other.pk})

    _assert_equipment_verification_failed(
        _equipment_history_problem(record), _equipment_history_problem(other)
    )


@pytest.mark.parametrize(
    "defect", ["nonobject", "missing-corrects", "decimal-equipment", "invalid-name"]
)
def test_equipment_attachment_rejects_malformed_audit_payloads(equipment_event_factory, defect):
    record = equipment_event_factory()
    event = _equipment_event(record)
    if defect == "missing-corrects":
        del event.after["corrects"]
    after = {
        "nonobject": ["Synthetic private equipment audit payload"],
        "missing-corrects": event.after,
        "decimal-equipment": event.after | {"equipment": float(record.equipment_id)},
        "invalid-name": event.after | {"evidence_name": [record.evidence_name]},
    }[defect]
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(after=after)

    _assert_equipment_verification_failed(_equipment_history_problem(record))


@pytest.mark.parametrize("loss", ["cleared", "deleted"])
@pytest.mark.parametrize("marker", ["evidence_name", "evidence_sha256"])
def test_equipment_event_exposes_lost_evidence_once(equipment_event_factory, loss, marker):
    original = equipment_event_factory(kind=EquipmentEvent.Kind.CALIBRATION)
    record = equipment_event_factory(
        kind=original.kind, corrects_id=original.pk, correction_reason="Synthetic correction"
    )
    event = _equipment_event(record)
    after = {"event": record.pk, marker: event.after[marker]}
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(after=after)
    duplicate = AuditEvent.objects.create(
        actor=event.actor,
        action=event.action,
        object_type=event.object_type,
        object_id=event.object_id,
        after=after,
    )
    AuditEvent.objects.create(
        actor=event.actor,
        action=event.action,
        object_type=event.object_type,
        object_id=event.object_id,
        after={"event": record.pk},
    )
    if loss == "cleared":
        EquipmentEvent.objects.filter(pk=record.pk).update(
            evidence_data=None,
            evidence_name="",
            evidence_content_type="",
            evidence_size=None,
            evidence_sha256="",
        )
        problem = f"laboratory.EquipmentEvent pk={record.pk}: missing recorded equipment evidence"
    else:
        record.delete()
        event_pk = min(event.pk, duplicate.pk)
        problem = f"core.AuditEvent pk={event_pk}: missing or invalid EquipmentEvent target"

    _assert_equipment_verification_failed(problem, checked=2)
    _assert_equipment_verification_failed(problem, output_format=MANIFEST_FORMAT)


def _training_event(record):
    return AuditEvent.objects.get(object_type=record._meta.label_lower, object_id=str(record.pk))


def _duplicate_training_event(event, **overrides):
    fields = {
        "action": event.action,
        "actor": event.actor,
        "object_type": event.object_type,
        "object_id": event.object_id,
        "after": event.after,
    }
    return AuditEvent.objects.create(**(fields | overrides))


def _assert_training_verification_failed(*problems, output_format=("--format", "json")):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    else:
        assert json.loads(stdout.getvalue())["Training record evidence"] == {
            "checked": len(problems),
            "failures": len(problems),
        }
    assert stderr.getvalue().splitlines() == list(problems)


def _training_history_problem(record):
    return (
        f"laboratory.TrainingRecord pk={record.pk}: "
        "missing, ambiguous, or mismatched training evidence event"
    )


def test_training_provenance_preserves_manifest_and_query_bounds(training_record_factory):
    original = training_record_factory()
    corrected_pdf = PDF + b" synthetic correction"
    corrected = training_record_factory(
        corrects_id=original.pk,
        correction_reason="Synthetic correction",
        evidence=SimpleUploadedFile("corrected training.pdf", corrected_pdf),
    )
    optional = training_record_factory(attached=False, reference="External training sheet")
    _duplicate_training_event(
        _training_event(optional), action="training.viewed", after=_training_event(original).after
    )
    optional.delete()

    with CaptureQueriesContext(connection) as captured:
        manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    rows = "".join(
        f'["laboratory.TrainingRecord","{record.pk}",{record.evidence_size},"{record.evidence_sha256}"]\n'
        for record in (original, corrected)
    )
    assert manifest["version"] == 1
    assert manifest["categories"]["Training record evidence"] == {
        "count": 2,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    file_query, cleared_query, missing_query = [
        query["sql"] for query in captured if 'FROM "laboratory_trainingrecord"' in query["sql"]
    ]
    bytes_column = '"laboratory_trainingrecord"."evidence_data"'
    assert file_query.count(bytes_column) == 3
    assert f"pg_catalog.octet_length({bytes_column})" in file_query
    assert f"pg_catalog.sha256({bytes_column})" in file_query
    assert f"{bytes_column} IS NULL" in cleared_query
    assert "evidence_data" not in missing_query
    for query in (file_query, cleared_query, missing_query):
        assert 'FROM "core_auditevent"' in query
        assert '"after"' not in query.partition(" FROM ")[0]


@pytest.mark.parametrize("change", ["content", "name", "actor", "staff", "predecessor"])
def test_training_evidence_requires_original_values(training_record_factory, staff, change):
    original = training_record_factory(attached=False)
    record = training_record_factory(
        corrects_id=original.pk, correction_reason="Synthetic correction"
    )
    if change == "content":
        replacement = PDF + b" synthetic replacement"
        fields = {
            "evidence_data": replacement,
            "evidence_size": len(replacement),
            "evidence_sha256": sha256(replacement).hexdigest(),
        }
    elif change == "name":
        fields = {"evidence_name": "replacement filename.pdf"}
    elif change == "predecessor":
        other = training_record_factory(attached=False)
        fields = {"corrects_id": other.pk}
    else:
        other = staff(f"restore-other-training-{change}", "analyst")
        field = "recorded_by" if change == "actor" else "staff"
        fields = {field: other}
    TrainingRecord.objects.filter(pk=record.pk).update(**fields)

    _assert_training_verification_failed(_training_history_problem(record))


@pytest.mark.parametrize(
    "defect",
    [
        "absent",
        "duplicate",
        "both-actions",
        "different-action",
        "wrong-training-action",
        "missing-link",
        "decimal-staff",
    ],
)
def test_training_evidence_requires_one_original_event(training_record_factory, defect):
    record = training_record_factory()
    event = _training_event(record)
    if defect == "duplicate":
        _duplicate_training_event(event)
    elif defect == "both-actions":
        _duplicate_training_event(event, action="training.corrected", after={})
    else:
        with _restore_with_history_defect(AuditEvent, "audit_immutable"):
            events = AuditEvent.objects.filter(pk=event.pk)
            if defect == "absent":
                events.delete()
            elif defect == "different-action":
                events.update(action="training.viewed")
            elif defect == "wrong-training-action":
                events.update(action="training.corrected")
            elif defect == "missing-link":
                event.after.pop("corrects")
                events.update(after=event.after)
            else:
                events.update(after=event.after | {"staff": float(record.staff_id)})

    _assert_training_verification_failed(_training_history_problem(record))


@pytest.mark.parametrize("loss", ["cleared", "deleted"])
@pytest.mark.parametrize("marker", [None, "evidence_name", "evidence_sha256"])
def test_training_event_exposes_missing_evidence_once(training_record_factory, loss, marker):
    original = training_record_factory(attached=False)
    record = training_record_factory(
        corrects_id=original.pk, correction_reason="Synthetic correction"
    )
    event = _training_event(record)
    after = event.after
    if marker:
        after = {marker: after[marker]}
        with _restore_with_history_defect(AuditEvent, "audit_immutable"):
            AuditEvent.objects.filter(pk=event.pk).update(after=after)
    duplicate = _duplicate_training_event(event, after=after)
    if loss == "cleared":
        TrainingRecord.objects.filter(pk=record.pk).update(
            evidence_data=None,
            evidence_name="",
            evidence_content_type="",
            evidence_size=None,
            evidence_sha256="",
        )
        problem = f"laboratory.TrainingRecord pk={record.pk}: missing recorded training evidence"
    else:
        record.delete()
        event_pk = min(event.pk, duplicate.pk)
        problem = f"core.AuditEvent pk={event_pk}: missing or invalid TrainingRecord target"

    _assert_training_verification_failed(problem)
    _assert_training_verification_failed(problem, output_format=MANIFEST_FORMAT)


@pytest.mark.parametrize(
    "target",
    [
        {"object_type": "laboratory.equipmentevent"},
        {"object_id": "synthetic private target"},
        {"object_id": "9" * 64},
    ],
)
def test_training_evidence_rejects_invalid_event_targets(training_record_factory, target):
    record = training_record_factory()
    event = _training_event(record)
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(**target)

    _assert_training_verification_failed(
        _training_history_problem(record),
        f"core.AuditEvent pk={event.pk}: missing or invalid TrainingRecord target",
    )


def _nonconformance_note_event(note):
    return AuditEvent.objects.get(
        action="nonconformance.note_added",
        object_type=Nonconformance._meta.label_lower,
        object_id=str(note.nonconformance_id),
        after__event=note.pk,
    )


def _assert_nonconformance_evidence_failure(
    *problems, checked=None, output_format=("--format", "json")
):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    else:
        assert json.loads(stdout.getvalue())["Nonconformance event evidence"] == {
            "checked": len(problems) if checked is None else checked,
            "failures": len(problems),
        }
    assert stderr.getvalue().splitlines() == list(problems)


def _nonconformance_note_history_problem(note):
    return (
        f"laboratory.NonconformanceEvent pk={note.pk}: "
        "missing, ambiguous, or mismatched nonconformance note event"
    )


def _nonconformance_audit_target_problem(event):
    return f"core.AuditEvent pk={event.pk}: missing or invalid NonconformanceEvent target"


def _nonconformance_lifecycle_audit(row):
    return AuditEvent.objects.get(
        object_type=Nonconformance._meta.label_lower,
        object_id=str(row.nonconformance_id),
        after__event=row.pk,
    )


def _nonconformance_lifecycle_problem(row):
    return (
        f"laboratory.NonconformanceEvent pk={row.pk}: "
        "missing, ambiguous, or mismatched nonconformance lifecycle event"
    )


def _simulate_legacy_nonconformance_audits(*rows):
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        for row in rows:
            audit = _nonconformance_lifecycle_audit(row)
            after = {
                key: value for key, value in audit.after.items() if key not in {"event", "evidence"}
            }
            AuditEvent.objects.filter(pk=audit.pk).update(after=after)


def test_nonconformance_evidence_preserves_legacy_manifest(
    nonconformance_factory, nonconformance_note_factory
):
    parent = nonconformance_factory(evidence=SimpleUploadedFile("opening.pdf", PDF))
    opened = parent.events.get(kind=NonconformanceEvent.Kind.OPENED)
    nonconformance.transition(
        actor=parent.opened_by,
        nc_id=parent.pk,
        expected_revision=parent.revision,
        to_status=Nonconformance.Status.INVESTIGATING,
        owner=parent.opened_by,
        evidence=SimpleUploadedFile("investigation.pdf", PDF),
    )
    status = parent.events.get(kind=NonconformanceEvent.Kind.STATUS)
    note = nonconformance_note_factory(nc_id=parent.pk)
    nonconformance_note_factory(nc_id=parent.pk, attached=False)
    nonconformance_note_factory(nc_id=parent.pk, attached=False).delete()

    manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    rows = "".join(
        f'["laboratory.NonconformanceEvent","{event.pk}",{len(PDF)},"{sha256(PDF).hexdigest()}"]\n'
        for event in (opened, status, note)
    )
    assert manifest["version"] == 1
    assert manifest["categories"]["Nonconformance event evidence"] == {
        "count": 3,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    _simulate_legacy_nonconformance_audits(opened, status)
    assert json.loads(_verification_output(*MANIFEST_FORMAT)) == manifest


def test_nonconformance_lifecycle_evidence_matches_historical_states(
    nonconformance_lifecycle_factory,
):
    attached_at = ("opened", "action", "verification", "return_to_action", "closed", "reopened")
    timeline = nonconformance_lifecycle_factory(*attached_at)

    digest = sha256(PDF).hexdigest()
    rows = "".join(
        f'["laboratory.NonconformanceEvent","{timeline[name].pk}",{len(PDF)},"{digest}"]\n'
        for name in attached_at
    )
    manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    assert manifest["categories"]["Nonconformance event evidence"] == {
        "count": len(attached_at),
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }


def test_nonconformance_legacy_reopening_attachment_still_checks_bytes(
    nonconformance_lifecycle_factory,
):
    reopened = nonconformance_lifecycle_factory("reopened")["reopened"]
    _simulate_legacy_nonconformance_audits(reopened)
    counts = json.loads(_verification_output("--format", "json"))
    assert counts["Nonconformance event evidence"] == {"checked": 1, "failures": 0}

    NonconformanceEvent.objects.filter(pk=reopened.pk).update(evidence_data=PDF[:-1] + b"x")
    _assert_nonconformance_evidence_failure(
        f"laboratory.NonconformanceEvent pk={reopened.pk}: SHA-256 mismatch"
    )


def test_nonconformance_lifecycle_without_attachment_adds_no_inventory_rows(
    nonconformance_lifecycle_factory,
):
    timeline = nonconformance_lifecycle_factory()
    timeline["opened"].delete()
    counts = json.loads(_verification_output("--format", "json"))
    assert counts["Nonconformance event evidence"] == {"checked": 0, "failures": 0}

    row = timeline["closed"]
    NonconformanceEvent.objects.filter(pk=row.pk).update(
        evidence_data=PDF,
        evidence_name=EVIDENCE_NAME,
        evidence_content_type="application/pdf",
        evidence_size=len(PDF),
        evidence_sha256=sha256(PDF).hexdigest(),
    )
    _assert_nonconformance_evidence_failure(_nonconformance_lifecycle_problem(row))


@pytest.mark.parametrize("name", ["opened", "action", "reopened"])
@pytest.mark.parametrize("loss", ["cleared", "deleted", "replaced"])
def test_nonconformance_lifecycle_audit_exposes_lost_evidence(
    nonconformance_lifecycle_factory, name, loss
):
    row = nonconformance_lifecycle_factory(name)[name]
    audit = _nonconformance_lifecycle_audit(row)
    rows = NonconformanceEvent.objects.filter(pk=row.pk)
    if loss == "cleared":
        rows.update(
            evidence_data=None,
            evidence_name="",
            evidence_content_type="",
            evidence_sha256="",
            evidence_size=None,
        )
        problem = (
            f"laboratory.NonconformanceEvent pk={row.pk}: "
            "missing recorded nonconformance lifecycle evidence"
        )
    elif loss == "deleted":
        rows.delete()
        problem = _nonconformance_audit_target_problem(audit)
    else:
        replacement = PDF + b" synthetic replacement"
        rows.update(
            evidence_data=replacement,
            evidence_size=len(replacement),
            evidence_sha256=sha256(replacement).hexdigest(),
        )
        problem = _nonconformance_lifecycle_problem(row)

    _assert_nonconformance_evidence_failure(problem)
    _assert_nonconformance_evidence_failure(problem, output_format=MANIFEST_FORMAT)


@pytest.mark.parametrize(
    "defect",
    [
        "parent",
        "type",
        "actor",
        "action",
        "from-status",
        "to-status",
        "checksum",
        "missing-checksum",
    ],
)
def test_nonconformance_lifecycle_evidence_requires_matching_audit(
    nonconformance_factory, nonconformance_lifecycle_factory, staff, defect
):
    row = nonconformance_lifecycle_factory("action")["action"]
    audit = _nonconformance_lifecycle_audit(row)
    changes = {
        "type": {"object_type": NonconformanceEvent._meta.label_lower},
        "action": {"action": "nonconformance.verification"},
        "from-status": {"before": audit.before | {"status": Nonconformance.Status.OPEN}},
        "to-status": {"after": audit.after | {"status": Nonconformance.Status.VERIFICATION}},
        "checksum": {"after": audit.after | {"evidence": [row.evidence_sha256]}},
        "missing-checksum": {
            "after": {key: value for key, value in audit.after.items() if key != "evidence"}
        },
    }
    if defect == "parent":
        changes["parent"] = {"object_id": str(nonconformance_factory().pk)}
    if defect == "actor":
        changes["actor"] = {"actor": staff("restore-other-lifecycle-recorder", "analyst")}
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=audit.pk).update(**changes[defect])

    _assert_nonconformance_evidence_failure(_nonconformance_lifecycle_problem(row))


def test_nonconformance_lifecycle_evidence_requires_one_audit(nonconformance_lifecycle_factory):
    row = nonconformance_lifecycle_factory("action")["action"]
    audit = _nonconformance_lifecycle_audit(row)
    AuditEvent.objects.create(
        action=audit.action,
        actor=audit.actor,
        object_type=audit.object_type,
        object_id=audit.object_id,
        before=audit.before,
        after=audit.after | {"evidence": ""},
    )

    _assert_nonconformance_evidence_failure(_nonconformance_lifecycle_problem(row))


@pytest.mark.parametrize("defect", ["missing-id", "string-id", "wrong-kind"])
def test_nonconformance_lifecycle_evidence_rejects_invalid_target(
    nonconformance_lifecycle_factory, defect
):
    reopened = nonconformance_lifecycle_factory("reopened")["reopened"]
    audit = _nonconformance_lifecycle_audit(reopened)
    if defect == "wrong-kind":
        NonconformanceEvent.objects.filter(pk=reopened.pk).update(
            kind=NonconformanceEvent.Kind.STATUS
        )
    else:
        after = audit.after.copy()
        if defect == "missing-id":
            del after["event"]
        else:
            after["event"] = str(reopened.pk)
        with _restore_with_history_defect(AuditEvent, "audit_immutable"):
            AuditEvent.objects.filter(pk=audit.pk).update(after=after)

    _assert_nonconformance_evidence_failure(_nonconformance_audit_target_problem(audit), checked=2)


def test_nonconformance_lifecycle_evidence_rejects_unknown_action(nonconformance_lifecycle_factory):
    row = nonconformance_lifecycle_factory("action")["action"]
    audit = _nonconformance_lifecycle_audit(row)
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=audit.pk).update(action="synthetic.unrecognized")

    _assert_nonconformance_evidence_failure(_nonconformance_audit_target_problem(audit), checked=2)


def test_nonconformance_note_evidence_rejects_rehashed_replacement(nonconformance_note_factory):
    note = nonconformance_note_factory()
    replacement = PDF + b" synthetic replacement"
    NonconformanceEvent.objects.filter(pk=note.pk).update(
        evidence_data=replacement,
        evidence_size=len(replacement),
        evidence_sha256=sha256(replacement).hexdigest(),
    )

    _assert_nonconformance_evidence_failure(_nonconformance_note_history_problem(note))


@pytest.mark.parametrize(
    "defect", ["missing", "duplicate", "competing-parent", "action", "parent", "type", "actor"]
)
def test_nonconformance_note_attachment_requires_one_original_event(
    nonconformance_factory, nonconformance_note_factory, staff, defect
):
    note = nonconformance_note_factory()
    event = _nonconformance_note_event(note)
    if defect in {"duplicate", "competing-parent"}:
        AuditEvent.objects.create(
            action=event.action,
            object_type=event.object_type,
            object_id=(
                str(nonconformance_factory().pk)
                if defect == "competing-parent"
                else event.object_id
            ),
            actor=event.actor,
            after=event.after,
        )
    else:
        changes = {
            "action": {"action": "nonconformance.opened"},
            "type": {"object_type": NonconformanceEvent._meta.label_lower},
        }
        if defect == "parent":
            changes[defect] = {"object_id": str(nonconformance_factory().pk)}
        if defect == "actor":
            changes[defect] = {"actor": staff("restore-other-note-recorder", "analyst")}
        with _restore_with_history_defect(AuditEvent, "audit_immutable"):
            events = AuditEvent.objects.filter(pk=event.pk)
            if defect == "missing":
                events.delete()
            else:
                events.update(**changes[defect])

    _assert_nonconformance_evidence_failure(_nonconformance_note_history_problem(note))


@pytest.mark.parametrize("defect", ["missing-id", "string-id", "decimal-id", "oversized-id"])
def test_nonconformance_note_evidence_rejects_invalid_event_ids(
    nonconformance_note_factory, defect
):
    note = nonconformance_note_factory()
    event = _nonconformance_note_event(note)
    if defect == "missing-id":
        del event.after["event"]
    else:
        event.after["event"] = {
            "string-id": str(note.pk),
            "decimal-id": float(note.pk),
            "oversized-id": 10**40,
        }[defect]
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(after=event.after)

    _assert_nonconformance_evidence_failure(
        _nonconformance_note_history_problem(note), _nonconformance_audit_target_problem(event)
    )


def test_nonconformance_note_evidence_rejects_nonstring_audit_checksum(nonconformance_note_factory):
    note = nonconformance_note_factory()
    event = _nonconformance_note_event(note)
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(
            after=event.after | {"evidence": [note.evidence_sha256]}
        )

    _assert_nonconformance_evidence_failure(_nonconformance_note_history_problem(note))


@pytest.mark.parametrize("loss", ["cleared", "deleted", "changed-kind"])
def test_nonconformance_note_event_exposes_lost_evidence(nonconformance_note_factory, loss):
    note = nonconformance_note_factory()
    event = _nonconformance_note_event(note)
    if loss == "cleared":
        NonconformanceEvent.objects.filter(pk=note.pk).update(
            evidence_data=None,
            evidence_name="",
            evidence_content_type="",
            evidence_sha256="",
            evidence_size=None,
        )
        problem = (
            f"laboratory.NonconformanceEvent pk={note.pk}: "
            "missing recorded nonconformance note evidence"
        )
    else:
        problem = _nonconformance_audit_target_problem(event)
        if loss == "deleted":
            note.delete()
        else:
            NonconformanceEvent.objects.filter(pk=note.pk).update(
                kind=NonconformanceEvent.Kind.STATUS
            )

    _assert_nonconformance_evidence_failure(problem, checked=2 if loss == "changed-kind" else 1)
    _assert_nonconformance_evidence_failure(problem, output_format=MANIFEST_FORMAT)


def _assert_subcontract_audit_failure(*failures, checked, output_format=("--format", "json")):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    elif output_format:
        assert json.loads(stdout.getvalue())["Subcontract source PDFs"] == {
            "checked": checked,
            "failures": len(failures),
        }
    else:
        assert (
            f"Subcontract source PDFs: checked={checked} failures={len(failures)}"
            in stdout.getvalue().splitlines()
        )
    assert stderr.getvalue().splitlines() == list(failures)


def _source_entry_problem(source):
    return (
        f"receipt.SubcontractResult pk={source.pk}: "
        "missing, ambiguous, or mismatched subcontract source entry"
    )


def test_subcontract_entry_provenance_preserves_manifest_and_query_bounds(
    subcontract_source_factory, staff
):
    reviewer = staff("restore-source-reviewer", "reviewer")
    original = subcontract_source_factory()
    review_external_result(
        actor=reviewer,
        result_id=original.pk,
        expected_revision=original.subcontract.revision,
        decision="accepted",
        note="Compared with the synthetic source",
    )
    corrected = subcontract_source_factory(
        actor=staff("restore-source-corrector", "project_manager"),
        source_pdf=SOURCE_PDF.replace(b"1.7", b"1.6"),
        reason="Synthetic correction",
    )
    review_external_result(
        actor=reviewer,
        result_id=corrected.pk,
        expected_revision=corrected.subcontract.revision,
        decision="accepted",
        note="Compared with the synthetic correction",
    )

    with CaptureQueriesContext(connection) as captured:
        manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    rows = "".join(
        f'["receipt.SubcontractResult","{source.pk}",{source.source_size},"{source.source_sha256}"]\n'
        for source in (original, corrected)
    )
    assert manifest["version"] == 1
    assert manifest["categories"]["Subcontract source PDFs"] == {
        "count": 2,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    source_query, missing_query = [
        query["sql"] for query in captured if 'FROM "receipt_subcontractresult"' in query["sql"]
    ]
    pdf_column = '"receipt_subcontractresult"."source_pdf"'
    assert source_query.count(pdf_column) == 2
    assert f"pg_catalog.octet_length({pdf_column})" in source_query
    assert f"pg_catalog.sha256({pdf_column})" in source_query
    assert 'FROM "core_auditevent"' in source_query
    assert pdf_column not in missing_query
    assert '"core_auditevent"."after"' not in missing_query.partition(" FROM ")[0]


def test_only_the_result_entry_event_can_verify_the_original_source(subcontract_source_factory):
    original = subcontract_source_factory()
    subcontract_source_factory(reason="Synthetic correction")
    entry = _source_entry(original)
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=entry.pk).update(action="subcontract.source_reviewed")

    _assert_subcontract_audit_failure(_source_entry_problem(original), checked=2)


@pytest.mark.parametrize("identical", [True, False], ids=["matching", "mismatched"])
def test_duplicate_subcontract_entries_fail(subcontract_source_factory, identical):
    original = subcontract_source_factory()
    corrected = subcontract_source_factory(reason="Synthetic correction")
    entry = _source_entry(corrected)
    if not identical:
        entry.after["source_result"]["source_sha256"] = original.source_sha256[::-1]
    AuditEvent.objects.create(
        actor=entry.actor,
        action=entry.action,
        object_type=entry.object_type,
        object_id=entry.object_id,
        after=entry.after,
    )

    _assert_subcontract_audit_failure(_source_entry_problem(corrected), checked=2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 0),
        ("subcontract", 0),
        ("number", 2),
        ("entered_by", 0),
        ("source_sha256", ["synthetic private content"]),
        ("source_size", float(len(SOURCE_PDF))),
        ("source_size", str(len(SOURCE_PDF))),
        ("source_size", len(SOURCE_PDF) + 1),
    ],
)
def test_subcontract_entry_requires_original_snapshot_fields(
    subcontract_source_factory, field, value
):
    source = subcontract_source_factory()
    entry = _source_entry(source)
    entry.after["source_result"][field] = value
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=entry.pk).update(after=entry.after)

    _assert_subcontract_audit_failure(_source_entry_problem(source), checked=1)


def test_subcontract_entry_actor_must_match_the_source_enterer(subcontract_source_factory, staff):
    source = subcontract_source_factory()
    entry = _source_entry(source)
    wrong_actor = staff("restore-source-other-user", "analyst")
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=entry.pk).update(actor=wrong_actor)

    _assert_subcontract_audit_failure(_source_entry_problem(source), checked=1)


def test_self_consistent_replacement_subcontract_pdf_fails(subcontract_source_factory):
    source = subcontract_source_factory()
    replacement = SOURCE_PDF.replace(b"1.7", b"1.6")
    with _restore_with_history_defect(SubcontractResult, "external_result_immutable"):
        SubcontractResult.objects.filter(pk=source.pk).update(
            source_pdf=replacement, source_sha256=sha256(replacement).hexdigest()
        )

    _assert_subcontract_audit_failure(_source_entry_problem(source), checked=1)


@pytest.mark.parametrize("output_format", OUTPUT_FORMATS, ids=OUTPUT_FORMAT_IDS)
def test_surviving_subcontract_entries_detect_lost_rows(subcontract_source_factory, output_format):
    original = subcontract_source_factory()
    subcontract_source_factory(reason="Synthetic correction")
    event = _source_entry(original)
    with _restore_with_history_defect(SubcontractResult, "external_result_immutable"):
        SubcontractResult.objects.filter(pk=original.pk).delete()

    _assert_subcontract_audit_failure(
        f"core.AuditEvent pk={event.pk}: missing or invalid SubcontractResult target",
        checked=2,
        output_format=output_format,
    )


def test_malformed_subcontract_entry_targets_are_reported_by_event_id(subcontract_source_factory):
    source = subcontract_source_factory()
    entry = _source_entry(source)
    events = [
        AuditEvent.objects.create(
            **(
                {
                    "actor": entry.actor,
                    "action": entry.action,
                    "object_type": entry.object_type,
                    "object_id": entry.object_id,
                    "after": entry.after,
                }
                | overrides
            )
        )
        for overrides in (
            {"object_id": "synthetic non-integer ID"},
            {"after": entry.after | {"latest_number": "1"}},
            {"after": entry.after | {"latest_number": 1.0}},
            {"after": ["synthetic private content"]},
            {"object_type": "receipt.subcontractresult"},
        )
    ]

    _assert_subcontract_audit_failure(
        *(
            f"core.AuditEvent pk={pk}: missing or invalid SubcontractResult target"
            for pk in sorted(event.pk for event in events)
        ),
        checked=6,
    )


@pytest.mark.parametrize("microsecond", [0, 1, 123456])
def test_issued_and_voided_invoices_preserve_the_manifest(
    report_sample, staff, monkeypatch, microsecond
):
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
    before_void = _verification_output(*MANIFEST_FORMAT)
    voider = staff("restore-voiding-manager", "project_manager")
    cancelled_at = (timezone.now() + timedelta(seconds=1)).replace(microsecond=microsecond)
    with monkeypatch.context() as clock:
        clock.setattr(invoices.timezone, "now", lambda: cancelled_at)
        invoices.void_invoice(
            actor=voider,
            draft_id=original.pk,
            expected_revision=original.revision,
            reason="Synthetic correction\nOriginal kept",
        )
    assert _verification_output(*MANIFEST_FORMAT) == before_void
    replacement = issue()

    invoice_projections = []

    def collect_invoice_columns(execute, query, params, many, context):
        result = execute(query, params, many, context)
        if 'FROM "commercial_issuedinvoice"' in query:
            invoice_projections.append(
                tuple(column.name for column in context["cursor"].description)
            )
        return result

    with (
        CaptureQueriesContext(connection) as captured,
        connection.execute_wrapper(collect_invoice_columns),
    ):
        manifest = json.loads(_verification_output(*MANIFEST_FORMAT))
    rows = "".join(
        f'["commercial.IssuedInvoice","{draft.issued_invoice.pk}",{len(PDF)},"{sha256(PDF).hexdigest()}"]\n'
        for draft in (original, replacement)
    )
    assert manifest["version"] == 1
    assert manifest["categories"]["Issued invoice PDFs"] == {
        "count": 2,
        "sha256": sha256(rows.encode("ascii")).hexdigest(),
    }
    assert invoice_projections == [
        (
            "pk",
            "actual_size",
            "actual_hash",
            "recorded_size",
            "recorded_hash",
            "evidence_metadata_present",
            "metadata_0",
            "metadata_1",
            "metadata_2",
        )
    ]
    (query,) = [q["sql"] for q in captured if 'FROM "commercial_issuedinvoice"' in q["sql"]]
    (orphan_query,) = [
        q["sql"]
        for q in captured
        if 'FROM "core_auditevent"' in q["sql"] and 'FROM "commercial_invoicedraft"' in q["sql"]
    ]
    assert 'FROM "core_auditevent"' in query
    assert "invoice_draft.voided" in query and "invoice_draft.voided" in orphan_query
    pdf_column = '"commercial_issuedinvoice"."pdf"'
    assert query.count(pdf_column) == 2
    assert f"pg_catalog.octet_length({pdf_column})" in query
    assert f"pg_catalog.sha256({pdf_column})" in query
    assert pdf_column not in orphan_query
    for field in ("before", "after", "reason"):
        assert f'"core_auditevent"."{field}"' not in orphan_query


def _assert_invoice_verification_failed(*failures, output_format=("--format", "json")):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *output_format, stdout=stdout, stderr=stderr)
    if output_format == MANIFEST_FORMAT:
        assert stdout.getvalue() == ""
    else:
        assert json.loads(stdout.getvalue())["Issued invoice PDFs"] == {
            "checked": len(failures),
            "failures": len(failures),
        }
    assert stderr.getvalue().splitlines() == list(failures)


def _invoice_issuance_problem(invoice):
    return (
        f"commercial.IssuedInvoice pk={invoice.pk}: "
        "missing, ambiguous, or mismatched invoice issuance event"
    )


def _issue_target_problem(event):
    return f"core.AuditEvent pk={event.pk}: missing or invalid issued invoice target"


def _invoice_void_problem(invoice):
    return (
        f"commercial.IssuedInvoice pk={invoice.pk}: "
        "missing, unexpected, ambiguous, or mismatched invoice void event"
    )


def _void_target_problem(event):
    return f"core.AuditEvent pk={event.pk}: missing or invalid void invoice target"


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

    _assert_invoice_verification_failed(_invoice_issuance_problem(invoice))


@pytest.mark.parametrize(
    ("record_original", "other_event"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(False, {"action": "invoice_draft.updated"}, id="unrelated-event"),
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

    _assert_invoice_verification_failed(_invoice_issuance_problem(invoice))


def test_invoice_requires_the_original_issue_actor(invoice_record, staff):
    invoice_record.save()
    different_actor = staff("restore-other-invoice-actor", "project_manager")
    _record_invoice_issue(invoice_record, actor=different_actor)

    _assert_invoice_verification_failed(_invoice_issuance_problem(invoice_record))


@pytest.mark.parametrize("corruption", ["missing", "different", "string", "decimal"])
def test_invoice_requires_the_original_nested_issuer(invoice_record, corruption):
    invoice_record.save()
    issuer_id = invoice_record.issued_by_id
    document = {"number": invoice_record.number, "sha256": invoice_record.sha256}
    if corruption != "missing":
        document["issued_by"] = {
            "different": issuer_id + 1,
            "string": str(issuer_id),
            "decimal": float(issuer_id),
        }[corruption]
    _record_invoice_issue(invoice_record, after={"issued_invoice": document})

    _assert_invoice_verification_failed(_invoice_issuance_problem(invoice_record))


@pytest.mark.parametrize(
    "target",
    [
        pytest.param({"object_type": "receipt.order"}, id="wrong-object-type"),
        pytest.param({"object_id": "0"}, id="wrong-draft"),
        pytest.param({"object_id": "synthetic private content"}, id="non-integer-id"),
        pytest.param({"object_id": "9" * 64}, id="out-of-range-id"),
    ],
)
def test_mistargeted_invoice_issue_event_fails_alongside_its_invoice(invoice_record, target):
    invoice_record.save()
    event = _record_invoice_issue(invoice_record, **target)

    _assert_invoice_verification_failed(
        _invoice_issuance_problem(invoice_record), _issue_target_problem(event)
    )


@pytest.mark.parametrize("discard_draft", [False, True], ids=["draft-with-no-invoice", "no-draft"])
def test_invoice_issue_event_survives_missing_document(invoice_record, discard_draft):
    invoice_record.save()
    event = _record_invoice_issue(invoice_record)
    with _restore_with_history_defect(IssuedInvoice, "issued_invoice_immutable"):
        IssuedInvoice.objects.filter(pk=invoice_record.pk).delete()
    draft = invoice_record.draft
    if discard_draft:
        draft.delete()
    else:
        InvoiceDraft.objects.filter(pk=draft.pk).update(
            status=InvoiceDraft.Status.DRAFT, cancelled_at=None, cancelled_by=None, cancel_reason=""
        )

    _assert_invoice_verification_failed(_issue_target_problem(event))
    _assert_invoice_verification_failed(_issue_target_problem(event), output_format=MANIFEST_FORMAT)


@pytest.mark.parametrize("status", [InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED])
def test_missing_issued_invoice_is_reported_once_for_its_draft(invoice_record, status):
    draft = invoice_record.draft
    if status == InvoiceDraft.Status.VOIDED:
        InvoiceDraft.objects.filter(pk=draft.pk).update(
            status=status,
            cancelled_at=timezone.now(),
            cancelled_by=draft.created_by,
            cancel_reason="Synthetic void reason",
        )
    _record_invoice_issue(invoice_record)
    _record_invoice_issue(invoice_record)
    _record_invoice_issue(invoice_record, action="invoice_draft.voided")

    _assert_invoice_verification_failed(
        f"commercial.InvoiceDraft pk={draft.pk}: missing IssuedInvoice"
    )


@pytest.mark.parametrize("status", [InvoiceDraft.Status.DRAFT, InvoiceDraft.Status.CANCELLED])
def test_issued_document_requires_an_issued_or_voided_draft(invoice_record, status):
    invoice_record.save()
    _record_invoice_issue(invoice_record)
    changes = (
        {
            "cancelled_at": timezone.now(),
            "cancelled_by": invoice_record.issued_by,
            "cancel_reason": "Synthetic cancellation",
        }
        if status == InvoiceDraft.Status.CANCELLED
        else {}
    )
    InvoiceDraft.objects.filter(pk=invoice_record.draft_id).update(status=status, **changes)

    _assert_invoice_verification_failed(
        f"commercial.IssuedInvoice pk={invoice_record.pk}: invalid draft status for issued invoice"
    )


def test_issued_invoice_refuses_unexpected_void_event(voided_invoice_record):
    invoice, _ = voided_invoice_record
    InvoiceDraft.objects.filter(pk=invoice.draft_id).update(
        status=InvoiceDraft.Status.ISSUED, cancelled_at=None, cancelled_by=None, cancel_reason=""
    )

    _assert_invoice_verification_failed(_invoice_void_problem(invoice))


def test_voided_invoice_still_requires_original_issuance(voided_invoice_record):
    invoice, _ = voided_invoice_record
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(
            action="invoice_draft.issued", object_id=str(invoice.draft_id)
        ).delete()

    _assert_invoice_verification_failed(_invoice_issuance_problem(invoice))


@pytest.mark.parametrize("defect", ["missing", "duplicate", "duplicate-invalid", "actor", "reason"])
def test_voided_invoice_requires_one_attributed_void(voided_invoice_record, defect):
    invoice, event = voided_invoice_record
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        events = AuditEvent.objects.filter(pk=event.pk)
        if defect == "missing":
            events.delete()
        elif defect in ("duplicate", "duplicate-invalid"):
            AuditEvent.objects.create(
                actor=event.actor,
                action=event.action,
                object_type=event.object_type,
                object_id=event.object_id,
                reason=event.reason,
                before=event.before,
                after=event.after if defect == "duplicate" else {},
            )
        elif defect == "actor":
            events.update(actor=invoice.issued_by)
        else:
            events.update(reason="Different synthetic reason")

    _assert_invoice_verification_failed(_invoice_void_problem(invoice))
    _assert_invoice_verification_failed(
        _invoice_void_problem(invoice), output_format=MANIFEST_FORMAT
    )


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("field", ["status", "number", "sha256"])
def test_voided_invoice_requires_the_transition_and_original_document(
    voided_invoice_record, side, field
):
    invoice, event = voided_invoice_record
    snapshot = getattr(event, side)
    record = snapshot if field == "status" else snapshot["issued_invoice"]
    record[field] = "Wrong synthetic value"
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(**{side: snapshot})

    _assert_invoice_verification_failed(_invoice_void_problem(invoice))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cancel_reason", "Different synthetic reason"),
        ("cancelled_by", True),
        ("cancelled_by", str),
        ("cancelled_by", float),
        ("cancelled_at", "Synthetic invalid timestamp"),
        ("cancelled_at", 123456),
    ],
)
def test_voided_invoice_requires_matching_cancellation_fields(voided_invoice_record, field, value):
    invoice, event = voided_invoice_record
    if callable(value):
        value = value(event.after[field])
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(after=event.after | {field: value})

    _assert_invoice_verification_failed(_invoice_void_problem(invoice))


@pytest.mark.parametrize(
    "target",
    [
        pytest.param({"object_type": "receipt.order"}, id="wrong-object-type"),
        pytest.param({"object_id": "0"}, id="wrong-draft"),
        pytest.param({"object_id": "synthetic private content"}, id="non-integer-id"),
        pytest.param({"object_id": "9" * 64}, id="out-of-range-id"),
    ],
)
def test_mistargeted_invoice_void_event_fails_alongside_its_invoice(voided_invoice_record, target):
    invoice, event = voided_invoice_record
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        AuditEvent.objects.filter(pk=event.pk).update(**target)

    _assert_invoice_verification_failed(_invoice_void_problem(invoice), _void_target_problem(event))


def test_void_event_targeting_a_draft_without_an_issued_document_fails(invoice_record):
    InvoiceDraft.objects.filter(pk=invoice_record.draft_id).update(status=InvoiceDraft.Status.DRAFT)
    event = _record_invoice_issue(invoice_record, action="invoice_draft.voided")

    _assert_invoice_verification_failed(_void_target_problem(event))


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

    original_logo = _lab_logo(people["pm"])
    replacement_logo = _lab_logo(people["pm"], PNG + b" replacement")
    original_logo.save()
    replacement_logo.save()
    Lab.objects.update(logo=original_logo)
    original = release()
    Lab.objects.update(logo=replacement_logo)
    report = reporting_services.update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        samples=[sample],
        title="Amended outfall report",
    )
    amended = release("Correct report title")
    assert amended.amends_id == original.pk
    assert original.file.snapshot["content"]["lab"]["logo"]["sha256"] == original_logo.sha256
    assert amended.file.snapshot["content"]["lab"]["logo"]["sha256"] == replacement_logo.sha256
    Lab.objects.update(logo=None)
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
    logo_queries = [q["sql"] for q in captured if 'FROM "core_lablogo"' in q["sql"]]
    assert len(logo_queries) == 2  # The report scan and the independent file check.
    assert query in logo_queries and '"image"' not in query

    # Simulate loss of a logo that is no longer selected by the laboratory.
    with transaction.atomic(), _restore_with_history_defect(LabLogo, "lab_logo_immutable"):
        original_logo.delete()
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", "--format", "json", stdout=stdout, stderr=stderr)
    summary = json.loads(stdout.getvalue())
    assert summary["Report PDFs"] == {"checked": 2, "failures": 1}
    assert summary["Laboratory logos"] == {"checked": 1, "failures": 0}
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={original.pk}: "
        "missing or mismatched laboratory logo referenced by report snapshot"
    ]


@pytest.mark.parametrize(
    "logo",
    [
        pytest.param("private invalid logo", id="malformed-logo"),
        pytest.param({}, id="empty-object"),
        pytest.param({"sha256": "incorrect"}, id="missing-logo"),
        pytest.param({"sha256": [sha256(PNG).hexdigest()]}, id="malformed-sha256"),
        pytest.param({"content_type": "image/jpeg"}, id="changed-content-type"),
        pytest.param({"width": 128}, id="changed-width"),
        pytest.param({"height": 96}, id="changed-height"),
        pytest.param({"width": 96.0}, id="decimal-width"),
        pytest.param({"height": "32"}, id="string-height"),
    ],
)
def test_report_snapshot_logo_must_identify_the_recorded_image(
    report_sample, legacy_report_factory, logo
):
    stored = _lab_logo(report_sample.received_by)
    stored.save()
    if isinstance(logo, dict) and logo:
        logo = {
            "sha256": stored.sha256,
            "content_type": "image/png",
            "width": 96,
            "height": 32,
        } | logo
    report_file = legacy_report_factory(frozen_content={"lab": {"logo": logo}})
    report_file.save()

    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *MANIFEST_FORMAT, stdout=stdout, stderr=stderr)
    assert stdout.getvalue() == ""
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={report_file.version_id}: "
        "missing or mismatched laboratory logo referenced by report snapshot"
    ]


@pytest.mark.parametrize("content", [{"schema": 1}, {"lab": {"logo": None}}])
def test_report_snapshot_without_logo_reference(legacy_report_factory, content):
    legacy_report_factory(frozen_content=content).save()
    summary = json.loads(_verification_output("--format", "json"))
    assert summary["Report PDFs"] == {"checked": 1, "failures": 0}
    assert summary["Laboratory logos"] == {"checked": 0, "failures": 0}


def test_malformed_lab_identity_cannot_hide_a_report_logo(legacy_report_factory):
    report_file = legacy_report_factory(frozen_content={"lab": [{"logo": "synthetic private"}]})
    report_file.save()
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Stored file verification failed"):
        call_command("verify_stored_files", *MANIFEST_FORMAT, stdout=stdout, stderr=stderr)
    assert stdout.getvalue() == ""
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={report_file.version_id}: "
        "invalid laboratory identity in report snapshot"
    ]


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


@pytest.mark.parametrize("kind", EquipmentEvent.Kind.values)
def test_manifest_hashes_stable_pk_ordered_equipment_evidence(equipment_event_factory, kind):
    original = equipment_event_factory(
        kind=kind, performed_on=timezone.localdate() - timedelta(days=1)
    )
    corrected = equipment_event_factory(
        kind=kind,
        corrects_id=original.pk,
        correction_reason="Synthetic correction",
        evidence=SimpleUploadedFile(
            "Synthetic private corrected equipment.pdf", PDF + b" corrected"
        ),
    )
    optional = equipment_event_factory(kind=kind, attached=False)
    equipment_event_factory(
        kind=kind,
        attached=False,
        corrects_id=corrected.pk,
        correction_reason="Synthetic follow-up without an attachment",
    )
    audit = _equipment_event(original)
    AuditEvent.objects.create(
        actor=audit.actor,
        action="equipment.viewed",
        object_type=audit.object_type,
        object_id=audit.object_id,
        after=audit.after | {"event": optional.pk},
    )
    optional.delete()

    expected_rows = "".join(
        f'["laboratory.EquipmentEvent","{event.pk}",{event.evidence_size},"{event.evidence_sha256}"]\n'
        for event in (original, corrected)
    ).encode("ascii")
    with CaptureQueriesContext(connection) as captured:
        manifest = _verification_output(*MANIFEST_FORMAT)
    assert manifest == _verification_output(*MANIFEST_FORMAT)
    inventory = json.loads(manifest)
    assert inventory["version"] == 1
    assert inventory["categories"]["Equipment event evidence"] == {
        "count": 2,
        "sha256": sha256(expected_rows).hexdigest(),
    }
    for record in (original, corrected):
        assert record.evidence_name not in manifest
        assert record.evidence_sha256 not in manifest
        assert record.description not in manifest

    (file_query,) = (
        query["sql"] for query in captured if 'FROM "laboratory_equipmentevent"' in query["sql"]
    )
    (audit_query,) = (
        query["sql"]
        for query in captured
        if 'FROM "core_auditevent"' in query["sql"]
        and "equipment.calibration_recorded" in query["sql"]
    )
    evidence = '"laboratory_equipmentevent"."evidence_data"'
    assert f"pg_catalog.octet_length({evidence})" in file_query
    assert f"pg_catalog.sha256({evidence})" in file_query
    assert f"{evidence}," not in file_query.partition(" FROM ")[0]
    assert '"core_auditevent"."after",' not in audit_query.partition(" FROM ")[0]


@pytest.mark.parametrize("change", ["content", "identity"])
def test_manifest_detects_changes_with_the_same_file_count(
    equipment_event_factory, change, tmp_path
):
    original = equipment_event_factory()
    event = _equipment_event(original)
    reference = _write_reference_manifest(tmp_path)

    # Compare two internally consistent inventories to exercise the manifest independently.
    with _restore_with_history_defect(AuditEvent, "audit_immutable"):
        events = AuditEvent.objects.filter(pk=event.pk)
        if change == "content":
            different_pdf = PDF[:-1] + b"x"
            digest = sha256(different_pdf).hexdigest()
            EquipmentEvent.objects.filter(pk=original.pk).update(
                evidence_data=different_pdf, evidence_sha256=digest
            )
            events.update(after=event.after | {"evidence_sha256": digest})
        else:
            events.delete()
            original.delete()
            equipment_event_factory()

    before = json.loads(reference.read_text(encoding="utf-8"))["categories"]
    after = json.loads(_verification_output(*MANIFEST_FORMAT))["categories"]
    recorded = before.pop("Equipment event evidence")
    restored = after.pop("Equipment event evidence")
    assert after == before
    assert restored["count"] == recorded["count"] == 1
    assert restored["sha256"] != recorded["sha256"]
    diagnostics = _manifest_comparison_failure(reference)
    assert "Equipment event evidence" in diagnostics
    assert EVIDENCE_NAME not in diagnostics


def _write_reference_manifest(tmp_path):
    reference = tmp_path / "source.json"
    reference.write_text(_verification_output(*MANIFEST_FORMAT), encoding="utf-8")
    return reference


def _manifest_comparison_failure(reference, *args):
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(CommandError) as error:
        call_command(
            "verify_stored_files",
            "--compare-manifest",
            str(reference),
            *args,
            stdout=stdout,
            stderr=stderr,
        )
    assert stdout.getvalue() == ""
    return f"{error.value}\n{stderr.getvalue()}"


def test_compare_manifest_matches_verified_inventory(equipment_event_factory, tmp_path):
    equipment_event_factory()
    reference = _write_reference_manifest(tmp_path)

    assert "match" in _verification_output("--compare-manifest", str(reference)).lower()


def test_compare_manifest_detects_missing_file_even_when_verification_passes(staff, tmp_path):
    logo = _lab_logo(staff("restore-logo-uploader", "admin"))
    logo.save()
    reference = _write_reference_manifest(tmp_path)

    with _restore_with_history_defect(LabLogo, "lab_logo_immutable"):
        logo.delete()

    assert "Total: checked=0 failures=0" in _verification_output()
    assert "Laboratory logos" in _manifest_comparison_failure(reference)


def test_compare_manifest_fails_for_invalid_stored_file(staff, tmp_path):
    logo = _lab_logo(staff("restore-logo-uploader", "admin"))
    logo.save()
    reference = _write_reference_manifest(tmp_path)

    with _restore_with_history_defect(LabLogo, "lab_logo_immutable"):
        LabLogo.objects.filter(pk=logo.pk).update(image=PNG[:-1] + b"x")

    diagnostics = _manifest_comparison_failure(reference)
    assert f"core.LabLogo pk={logo.pk}" in diagnostics
    assert "synthetic image" not in diagnostics


@pytest.mark.parametrize("output_format", ["json", "manifest"])
def test_compare_manifest_rejects_other_output_formats(tmp_path, output_format):
    reference = _write_reference_manifest(tmp_path)

    diagnostics = _manifest_comparison_failure(reference, "--format", output_format)
    assert "--compare-manifest" in diagnostics
    assert "--format" in diagnostics


@pytest.mark.parametrize("category_change", ["missing", "extra"])
def test_compare_manifest_requires_the_exact_category_set(tmp_path, category_change):
    reference = _write_reference_manifest(tmp_path)
    manifest = json.loads(reference.read_text(encoding="utf-8"))
    category = "Laboratory logos"
    if category_change == "missing":
        del manifest["categories"][category]
    else:
        manifest["categories"][EVIDENCE_NAME] = manifest["categories"][category]
    reference.write_text(json.dumps(manifest), encoding="utf-8")

    diagnostics = _manifest_comparison_failure(reference)
    assert EVIDENCE_NAME not in diagnostics
    if category_change == "missing":
        assert category in diagnostics


def test_compare_manifest_rejects_duplicate_keys_in_valid_inventory(tmp_path):
    reference = _write_reference_manifest(tmp_path)
    content = reference.read_text(encoding="utf-8")
    reference.write_text(
        content.replace('"version": 1', '"version": 2, "version": 1'), encoding="utf-8"
    )

    _manifest_comparison_failure(reference)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_compare_manifest_requires_integer_version_one(tmp_path, version):
    reference = _write_reference_manifest(tmp_path)
    manifest = json.loads(reference.read_text(encoding="utf-8"))
    manifest["version"] = version
    reference.write_text(json.dumps(manifest), encoding="utf-8")

    _manifest_comparison_failure(reference)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("count", True),
        ("count", 0.0),
        ("count", -1),
        ("sha256", "g" * 64),
        ("sha256", EVIDENCE_NAME),
    ],
)
def test_compare_manifest_rejects_invalid_category_entries(tmp_path, field, value):
    reference = _write_reference_manifest(tmp_path)
    manifest = json.loads(reference.read_text(encoding="utf-8"))
    manifest["categories"]["Laboratory logos"][field] = value
    reference.write_text(json.dumps(manifest), encoding="utf-8")

    assert EVIDENCE_NAME not in _manifest_comparison_failure(reference)


@pytest.mark.parametrize(
    "contents",
    [None, b"\xff", b'{"synthetic private content"', b"[]", b'{"version": 1, "categories": []}'],
)
def test_compare_manifest_rejects_unreadable_or_malformed_file(tmp_path, contents):
    reference = tmp_path / "source.json"
    if contents is not None:
        reference.write_bytes(contents)

    assert "synthetic private content" not in _manifest_comparison_failure(reference)


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
def test_compare_manifest_uses_the_exported_snapshot(staff, tmp_path):
    reference = _write_reference_manifest(tmp_path)

    with _exported_snapshot() as snapshot:
        _lab_logo(staff("snapshot-uploader", "admin")).save()
        output = _verification_output("--compare-manifest", str(reference), "--snapshot", snapshot)
        assert "match" in output.lower()

    assert "Laboratory logos" in _manifest_comparison_failure(reference)


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
    document_records,
    nonconformance_note_factory,
    import_source,
    equipment_event_factory,
    training_record_factory,
    output_format,
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
    with _restore_with_history_defect(SubcontractResult, "external_result_immutable"):
        SubcontractResult.objects.filter(pk=subcontract.pk).update(
            source_size=subcontract.source_size + 1
        )
    export.data = b""
    logo.sha256 = "1" * 64
    for document in (report_file, invoice, export, logo):
        document.save()

    equipment_record = equipment_event_factory()
    EquipmentEvent.objects.filter(pk=equipment_record.pk).update(
        evidence_data=None, evidence_name="", evidence_sha256="", evidence_size=None
    )
    training = training_record_factory()
    TrainingRecord.objects.filter(pk=training.pk).update(evidence_data=None)
    note = nonconformance_note_factory()
    NonconformanceEvent.objects.filter(pk=note.pk).update(evidence_name="")

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
        f"commercial.IssuedInvoice pk={invoice.pk}: invalid draft status for issued invoice, "
        "SHA-256 mismatch",
        f"commercial.InvoiceDraft pk={missing_issued.pk}: missing IssuedInvoice",
        f"commercial.InvoiceDraft pk={missing_voided.pk}: missing IssuedInvoice",
        f"{_source_entry_problem(subcontract)}, byte length mismatch",
        f"reporting.EddExport pk={export.pk}: empty stored file, SHA-256 mismatch, "
        "byte length mismatch",
        f"core.LabLogo pk={logo.pk}: SHA-256 mismatch",
        f"laboratory.EquipmentEvent pk={equipment_record.pk}: missing file, missing evidence "
        "filename or content type, missing recorded SHA-256, missing recorded byte length, "
        "missing, ambiguous, or mismatched equipment evidence event",
        f"laboratory.TrainingRecord pk={training.pk}: missing file",
        f"laboratory.NonconformanceEvent pk={note.pk}: missing evidence filename or content type",
        f"analytical.ImportSourceFile pk={import_source.pk}: SHA-256 mismatch",
    ]
