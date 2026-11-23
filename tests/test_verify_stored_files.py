from hashlib import sha256
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from commercial.models import InvoiceDraft, IssuedInvoice, PriceList
from core.models import LabLogo
from laboratory.models import (
    Equipment,
    EquipmentEvent,
    Nonconformance,
    NonconformanceEvent,
    TrainingRecord,
)
from receipt.models import Order, Subcontract, SubcontractResult
from receipt.services import receive_sample
from reporting.models import EddExport, Report, ReportFile, ReportVersion

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
)

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
def document_records(staff, receipt_data):
    actor = staff("restore-custodian", "custodian")
    sample = receive_sample(actor=actor, **receipt_data)
    now = timezone.now()
    report = Report.objects.create(project=sample.order.project, created_by=actor)
    version = ReportVersion.objects.create(
        report=report,
        number=1,
        content_sha256="a" * 64,
        pdf_sha256=sha256(PDF).hexdigest(),
        pdf_size=len(PDF),
        released_by=actor,
        released_at=now,
    )
    draft = _invoice_draft(sample.order, actor, "DRAFT-1", InvoiceDraft.Status.VOIDED)
    dispatch = Subcontract.objects.create(
        requested_test=sample.requested_tests.get(),
        dispatched_at=now,
        selected_containers=[],
        created_by=actor,
    )
    return (
        ReportFile(version=version, snapshot={}, pdf=PDF),
        IssuedInvoice(
            draft=draft,
            number="INV-1",
            issued_at=now,
            issued_by=actor,
            snapshot={},
            pdf=PDF,
            sha256=sha256(PDF).hexdigest(),
        ),
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
        EddExport(
            version=version,
            format_name="NYSDEC",
            format_version="5",
            reference_values="20260429",
            mapping={},
            row_counts={},
            data=ZIP,
            sha256=sha256(ZIP).hexdigest(),
            size=len(ZIP),
            created_by=actor,
        ),
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


def test_empty_database():
    stdout, stderr = StringIO(), StringIO()
    call_command("verify_stored_files", stdout=stdout, stderr=stderr)
    assert stdout.getvalue().splitlines() == [
        f"{category}: checked=0 failures=0" for category in (*CATEGORIES, "Total")
    ]
    assert stderr.getvalue() == ""


def test_intact_documents_and_optional_evidence(document_records, evidence_records):
    for document in document_records:
        document.save()
    for model, fields in evidence_records.items():
        model.objects.create(**fields)
        model.objects.create(**fields, **EVIDENCE)

    stdout, stderr = StringIO(), StringIO()
    call_command("verify_stored_files", stdout=stdout, stderr=stderr)
    assert stdout.getvalue().splitlines() == [
        *(f"{category}: checked=1 failures=0" for category in CATEGORIES),
        "Total: checked=8 failures=0",
    ]
    assert stderr.getvalue() == ""


def test_corrupt_and_missing_rows_report_ids_and_fail(document_records, evidence_records):
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
    order = Order.objects.create(project=version.report.project, created_by=invoice.issued_by)
    missing_issued = _invoice_draft(order, invoice.issued_by, "DRAFT-2", InvoiceDraft.Status.ISSUED)
    missing_voided = _invoice_draft(order, invoice.issued_by, "DRAFT-3", InvoiceDraft.Status.VOIDED)
    InvoiceDraft.objects.filter(pk=invoice.draft_id).update(
        status=InvoiceDraft.Status.DRAFT, cancelled_at=None, cancelled_by=None, cancel_reason=""
    )

    report_file.pdf = PDF[:-1] + b"x"
    invoice.sha256 = "0" * 64
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
        call_command("verify_stored_files", stdout=stdout, stderr=stderr)
    assert stdout.getvalue().splitlines() == [
        "Report PDFs: checked=2 failures=2",
        "Issued invoice PDFs: checked=3 failures=3",
        *(f"{category}: checked=1 failures=1" for category in CATEGORIES[2:]),
        "Total: checked=11 failures=11",
    ]
    assert stderr.getvalue().splitlines() == [
        f"reporting.ReportVersion pk={version.pk}: SHA-256 mismatch",
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
    ]
