"""Run via manage.py shell --no-imports on the disposable populated-upgrade database."""

import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from html import escape
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.db.models import F, JSONField, TextField
from django.db.models.functions import Cast
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from psycopg import sql
from psycopg.errors import RaiseException
from weasyprint import HTML

from accounts.models import User
from analytical import services as bench
from analytical.models import (
    QCCorrectiveAction,
    QCMeasurement,
    QCSample,
    QCType,
    Result,
    ResultRevision,
    ResultWorksheet,
    WorksheetReading,
)
from analytical.qc import FAIL, PASS, BatchQC, evaluate_stored
from catalog.models import TestDefinition
from commercial import invoices
from commercial.management.commands.seed_commercial_demo import PRICE_LIST
from commercial.models import InvoiceDraft, InvoiceDraftLine, PriceList
from core import lab_profile
from core.audit import snapshot
from core.management.commands.seed_demo import DEMO_LAB_NAME
from core.models import AuditEvent, Lab, LabLogo
from laboratory import equipment, nonconformance, training
from laboratory.models import EquipmentEvent, Nonconformance, NonconformanceEvent, TrainingRecord
from receipt.corrections import correct_sample
from receipt.models import Client as LabClient
from receipt.models import Container, CustodyEvent, Order, RequestedTest, Sample
from receipt.services import receive_sample, transfer_sample
from receipt.subcontract_models import Subcontract, SubcontractResult
from receipt.subcontracts import dispatch_subcontract, enter_external_result, review_external_result
from reporting.content import assemble
from reporting.edd import services as edd
from reporting.edd.nysdec import LAB_QC_TYPES, SECTION_ORDER
from reporting.models import EddExport, Report, ReportFile, ReportVersion
from reporting.services import create_report, release_report

FEE_DESCRIPTION = "Synthetic upgrade verification fee"
QC_SAMPLE_LABEL = "Synthetic upgrade LCS"
SUBCONTRACT_REFERENCE = "SYN-UPGRADE-EXTERNAL"
ORIGINAL_CLIENT_SAMPLE_ID = "SYN-EXT-01"
CORRECTED_CLIENT_SAMPLE_ID = "SYN-EXT-001"
RECEIPT_LOCATION = "Receipt bench"
TRANSFER_LOCATION = "Synthetic dispatch staging"
EDD_REPORT_TITLE = "Outfall 001 weekly monitoring"
ORIGINAL_EDD_LOCATION = "SYNTH-OUTFALL-ORIG"
UPDATED_EDD_LOCATION = "SYNTH-OUTFALL-NEW"
EDD_EXPORTS = "electronic deliverable exports"
HISTORY_MUTATION_ERROR = "Historical records cannot be updated or deleted"
LABORATORY_ATTACHMENT_REFERENCE = "SYN-UPGRADE-LAB-EVIDENCE"
NONCONFORMANCE_NOTE_REFERENCE = "SYN-UPGRADE-LAB-NOTE"
NONCONFORMANCE_NOTE = "Synthetic follow-up evidence for the populated upgrade check."
LABORATORY_ATTACHMENTS = {
    "equipment": ("equipment event attachments", EquipmentEvent),
    "training": ("staff training attachments", TrainingRecord),
    "nonconformance": ("nonconformance event attachments", NonconformanceEvent),
}


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def prepare_qc_history():
    actor = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.select_related("method").get(
        method__code="DEMO-METALS-AQ",
        method__version="demo-1",
        matrix="water",
        analyte__name="Lead",
    )
    # Keep this rehearsal in a dedicated batch with no client results or reports.
    batch = bench.create_batch(
        actor=actor,
        method=definition.method,
        matrix=definition.matrix,
        notes="Synthetic QC reanalysis for the populated upgrade check.",
    )
    sample = bench.add_qc_sample(
        actor=actor, batch_id=batch.pk, qc_type=QCType.LCS, label=QC_SAMPLE_LABEL
    )
    now = timezone.now()
    measurement_inputs = dict(
        actor=actor,
        qc_sample_id=sample.pk,
        definition_id=definition.pk,
        reporting_limit_applied=format(definition.reporting_limit, "f"),
        spike_value="20.0",
    )
    failed = bench.record_qc_measurement(
        observed_text="26.0", analysis_started_at=now - timedelta(minutes=30), **measurement_inputs
    )
    finding = next(
        (
            issue
            for issue in BatchQC.load(batch).issues(definition)
            if issue.code == "failed" and issue.measurement_id == failed.pk
        ),
        None,
    )
    require(finding is not None, "The synthetic LCS must produce a QC failure.")
    bench.record_corrective_action(
        actor=actor,
        batch_id=batch.pk,
        target_key=finding.key,
        disposition=QCCorrectiveAction.Disposition.REANALYZE,
        note="Synthetic: repeat the laboratory control sample after the failed recovery.",
    )
    bench.record_qc_measurement(
        observed_text="20.0",
        analysis_started_at=now,
        reason="Synthetic: recorded the repeat LCS observation after reanalysis.",
        **measurement_inputs,
    )


def prepare_subcontract_history(project, manager):
    custodian = User.objects.get(username="demo_custodian")
    analyst = User.objects.get(username="demo_analyst")
    reviewer = User.objects.get(username="demo_reviewer")
    approver = User.objects.get(username="demo_approver")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ",
        method__version="demo-1",
        matrix="water",
        analyte__name="Lead",
    )
    now = timezone.now()
    sample = receive_sample(
        actor=custodian,
        project=project,
        coc_reference=SUBCONTRACT_REFERENCE,
        tests=[definition],
        container_description="Synthetic bottle",
        container_condition="Intact",
        relinquished_by="Example Courier",
        client_sample_id=ORIGINAL_CLIENT_SAMPLE_ID,
        matrix="water",
        source_type="surface_water",
        collection_type="grab",
        collected_at=now - timedelta(hours=4),
        collector_name="Example Collector",
        collection_location="Synthetic Test Creek",
        received_at=now - timedelta(hours=3),
        cooler_temperature=Decimal("4.2"),
        coolant="wet_ice",
        seal_condition="Intact",
        preservation_check="Recorded for demonstration only",
        current_location=RECEIPT_LOCATION,
        disposition="proceed",
    )
    sample = correct_sample(
        actor=custodian,
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section="identity",
        client_sample_id=CORRECTED_CLIENT_SAMPLE_ID,
        reason="Synthetic: corrected the client sample ID against the chain of custody.",
    )
    sample = transfer_sample(
        actor=custodian,
        sample_id=sample.pk,
        expected_revision=sample.revision,
        occurred_at=now - timedelta(hours=2, minutes=30),
        to_location=TRANSFER_LOCATION,
        relinquished_by="Synthetic Receiving Custodian",
        received_by="Synthetic Dispatch Custodian",
        reason="Synthetic: moved the sample to dispatch staging.",
    )
    contract = dispatch_subcontract(
        actor=custodian,
        requested_test_id=sample.requested_tests.get().pk,
        expected_sample_revision=sample.revision,
        container_ids=[sample.containers.get().pk],
        external_lab="Synthetic Partner Laboratory",
        external_reference=SUBCONTRACT_REFERENCE,
        external_sample_id="PARTNER-01",
        dispatched_at=now - timedelta(hours=2),
        handed_to="Synthetic Partner Courier",
        reason="Synthetic external source for upgrade verification.",
    )
    report = create_report(
        actor=manager, project=project, title="Synthetic subcontract upgrade", samples=[sample]
    )
    for number, observed in enumerate(("4.200", "4.100"), start=1):
        reason = "Partner laboratory corrected its synthetic result." if number == 2 else ""
        reference = f"{SUBCONTRACT_REFERENCE}-v{number}"
        pdf = HTML(
            string=f"<h1>Synthetic Partner Laboratory</h1><p>{reference}</p>"
            f"<p>PARTNER-01: Lead {observed} {escape(definition.unit)}</p>"
        ).write_pdf()
        source = enter_external_result(
            actor=analyst,
            subcontract_id=contract.pk,
            expected_revision=contract.revision,
            observed_text=observed,
            reporting_limit=str(definition.reporting_limit),
            dilution="1",
            unit=definition.unit,
            external_method="Synthetic partner lead method v1",
            external_analyst="Synthetic Partner Analyst",
            analyzed_at=now - timedelta(hours=1),
            qualifier_text="",
            external_report_reference=reference,
            source_pdf=pdf,
            reason=reason,
        )
        contract.refresh_from_db()
        review_external_result(
            actor=reviewer,
            result_id=source.pk,
            expected_revision=contract.revision,
            decision=SubcontractResult.Decision.ACCEPTED,
            note="Compared transcription with the partner laboratory's PDF.",
        )
        contract.refresh_from_db()
        report.refresh_from_db()
        release_report(
            actor=approver,
            report_id=report.pk,
            expected_fingerprint=assemble(report, actor=approver).fingerprint,
            reason=reason,
        )
        if number == 1:
            # The amendment is issued after the lab stops printing its original logo.
            lab_profile.remove_logo(actor=User.objects.get(username="demo_admin"))


def prepare_edd_export(project, manager):
    version = ReportVersion.objects.select_related("file").get(
        report__project=project, report__title=EDD_REPORT_TITLE, number=1
    )
    content = version.file.snapshot["content"]
    sample_ids = [sample["lab_id"] for sample in content["samples"]]
    # The demo seed supplied the project and test codes. Supply export-time facts for its
    # field samples and lab QC, using the seed's synthetic QC run times.
    qc_samples = {
        f"{batch['number']}-{measurement['qc_sample']}": {
            "sample_date": measurement["analysis_started_at"],
            "dilution_factor": "1",
        }
        for batch in content["batches"]
        for evidence in batch["qc"].values()
        for measurement in evidence["measurements"]
        if not measurement.get("withheld") and measurement["qc_type"] in LAB_QC_TYPES
    }
    context = dict(actor=manager, version_id=version.pk, results={}, qc_samples=qc_samples)
    edd.save_version_context(
        samples={lab_id: {"sys_loc_code": ORIGINAL_EDD_LOCATION} for lab_id in sample_ids},
        **context,
    )
    edd.generate_export(actor=manager, version_id=version.pk)
    # A download rebuilt from current mappings would differ from the stored export.
    edd.save_version_context(
        samples={lab_id: {"sys_loc_code": UPDATED_EDD_LOCATION} for lab_id in sample_ids},
        **context,
    )


def laboratory_upload(kind):
    return SimpleUploadedFile(
        f"synthetic-upgrade-{kind}.txt",
        f"Synthetic upgrade evidence for {kind}.\r\n".encode(),
    )


def prepare_laboratory_attachments():
    analyst = User.objects.get(username="demo_analyst")
    reviewer = User.objects.get(username="demo_reviewer")
    custodian = User.objects.get(username="demo_custodian")
    today = timezone.localdate()
    asset = equipment.save_equipment(
        actor=reviewer,
        asset_id="SYN-UPGRADE-BAL-01",
        name="Synthetic upgrade balance",
        equipment_type="balance",
        location="Synthetic weigh room",
    )
    equipment.record_equipment_event(
        actor=analyst,
        equipment_id=asset.pk,
        kind=EquipmentEvent.Kind.CALIBRATION,
        outcome=EquipmentEvent.Outcome.ACCEPTABLE,
        performed_on=today,
        performed_by=analyst,
        reference=LABORATORY_ATTACHMENT_REFERENCE,
        evidence=laboratory_upload("equipment"),
    )
    training.record_training(
        actor=reviewer,
        staff=analyst,
        record_type=TrainingRecord.Type.TRAINING,
        title="Synthetic upgrade balance training",
        completed_on=today,
        effective_on=today,
        reviewed_by=reviewer,
        reviewed_on=today,
        reference=LABORATORY_ATTACHMENT_REFERENCE,
        evidence=laboratory_upload("training"),
    )
    nc = nonconformance.open_nonconformance(
        actor=analyst,
        equipment=asset,
        title="Synthetic upgrade equipment nonconformance",
        description="Synthetic documentation issue for the populated upgrade check.",
        category=Nonconformance.Category.EQUIPMENT,
        severity=Nonconformance.Severity.MINOR,
        discovered_on=today,
        reference=LABORATORY_ATTACHMENT_REFERENCE,
        evidence=laboratory_upload("nonconformance"),
    )
    nonconformance.add_note(
        actor=custodian,
        nc_id=nc.pk,
        note=NONCONFORMANCE_NOTE,
        reference=NONCONFORMANCE_NOTE_REFERENCE,
        evidence=laboratory_upload("nonconformance-note"),
    )


@transaction.atomic
def prepare():
    """Create synthetic history and stored files with baseline services."""
    actor = User.objects.get(username="demo_project_manager")
    order = Order.objects.get(coc_reference="DEMO-COC-001")
    draft = invoices.create_invoice_draft(
        actor=actor,
        order_id=order.pk,
        expected_source_state=invoices.source_state(order),
        price_list_id=PriceList.objects.get(name=PRICE_LIST).pk,
    )
    invoices.add_invoice_draft_line(
        actor=actor,
        draft_id=draft.pk,
        expected_revision=draft.revision,
        kind=InvoiceDraftLine.Kind.FEE,
        description=FEE_DESCRIPTION,
        quantity=2,
        unit_price=Decimal("4.25"),
        reason="Synthetic manual fee for the populated upgrade check.",
    )
    prepare_qc_history()
    prepare_subcontract_history(order.project, actor)
    prepare_edd_export(order.project, actor)
    prepare_laboratory_attachments()
    print(
        "Prepared a synthetic invoice draft, QC reanalysis, receipt correction, "
        "custody transfer, amended subcontract report, exploratory EDD ZIP and "
        "laboratory attachments."
    )


def require_receipt_history():
    subcontract = (
        Subcontract.objects.filter(external_reference=SUBCONTRACT_REFERENCE)
        .select_related("requested_test__sample__order")
        .first()
    )
    require(subcontract is not None, "The synthetic receipt needs its linked subcontract.")
    sample = subcontract.requested_test.sample
    containers = [snapshot(container) for container in sample.containers.order_by("pk")]
    receipt_actions = ("sample.received", "sample.identity_corrected", "sample.transferred")
    audit = list(
        AuditEvent.objects.filter(
            object_type="receipt.sample", object_id=str(sample.pk), action__in=receipt_actions
        )
    )
    custody = list(sample.custody_events.all())
    require(
        tuple(event.action for event in audit) == receipt_actions
        and [event.kind for event in custody] == ["receipt", "transfer"],
        "The synthetic sample needs its original receipt, ID correction and custody transfer.",
    )
    original, correction, transfer = audit
    receipt_event, transfer_event = custody
    require(
        sample.order.coc_reference == SUBCONTRACT_REFERENCE
        and original.after.get("order_id") == str(sample.order_id)
        and original.after.get("custody_event_id") == str(receipt_event.pk)
        and original.actor_id == receipt_event.actor_id == sample.received_by_id
        and original.after.get("client_sample_id") == ORIGINAL_CLIENT_SAMPLE_ID
        and containers
        and original.after.get("containers") == containers == subcontract.selected_containers
        and receipt_event.occurred_at == sample.received_at,
        "The original receipt must retain its sample ID, order, containers and custody event.",
    )
    require(
        correction.before.get("client_sample_id") == ORIGINAL_CLIENT_SAMPLE_ID
        and correction.after.get("client_sample_id")
        == sample.client_sample_id
        == CORRECTED_CLIENT_SAMPLE_ID
        and correction.before.get("revision") == original.after.get("revision")
        and correction.actor_id == sample.received_by_id
        and correction.reason,
        "The receipt correction must link the original and corrected client sample IDs.",
    )
    require(
        transfer.before.get("location")
        == transfer_event.from_location
        == receipt_event.to_location
        == original.after.get("current_location")
        == RECEIPT_LOCATION
        and transfer.after.get("location")
        == transfer_event.to_location
        == sample.current_location
        == subcontract.from_location
        == TRANSFER_LOCATION
        and transfer.before.get("revision") == correction.after.get("revision")
        and transfer.after.get("custody_event_id") == str(transfer_event.pk)
        and transfer.actor_id == transfer_event.actor_id == sample.received_by_id
        and transfer.reason == transfer_event.reason
        and transfer.reason
        and receipt_event.occurred_at < transfer_event.occurred_at < subcontract.dispatched_at,
        "The custody transfer must link the original receipt location to dispatch staging.",
    )


def require_qc_history():
    correction = (
        QCMeasurement.objects.filter(
            qc_sample__label=QC_SAMPLE_LABEL,
            qc_sample__qc_type=QCType.LCS,
            is_current=True,
            supersedes__is_current=False,
            supersedes__qc_sample_id=F("qc_sample_id"),
            supersedes__definition_id=F("definition_id"),
            supersedes__corrective_actions__disposition=QCCorrectiveAction.Disposition.REANALYZE,
        )
        .select_related("qc_sample__batch", "supersedes__qc_sample__batch")
        .first()
    )
    require(
        correction is not None
        and evaluate_stored(correction.supersedes).status == FAIL
        and evaluate_stored(correction).status == PASS,
        "The baseline needs a failed QC observation with a linked reanalysis disposition "
        "and a passing correction.",
    )


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(values):
    """Preserve Decimal text and timestamp precision, and hash binary data before JSON encoding."""
    values = [sha256(value) if isinstance(value, bytes | memoryview) else value for value in values]
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def collect(queryset, expected=None):
    columns = {field.attname: field for field in queryset.model._meta.concrete_fields}
    fields = (
        sorted(name for name, field in columns.items() if not field.primary_key)
        if expected is None
        else expected["fields"]
    )
    if expected is not None and queryset.model is not InvoiceDraftLine:
        # A migration may add records. The lines on our existing draft must stay the same.
        queryset = queryset.filter(pk__in=expected["rows"])
    # Read PostgreSQL JSON as text so nested numerics never pass through a Python float.
    projections = [
        Cast(name, TextField()) if isinstance(columns[name], JSONField) else name for name in fields
    ]
    rows = {}
    for pk, *values in queryset.order_by("pk").values_list("pk", *projections).iterator():
        if queryset.model is ReportVersion:
            version = ReportVersion(pk=pk)
            values.extend(
                list(related.order_by("pk").values_list("pk", flat=True))
                for related in (version.result_revisions, version.external_result_revisions)
            )
        rows[str(pk)] = digest(values)
    return {"fields": fields, "rows": rows}


def collections(draft):
    return {
        "orders": Order.objects.all(),
        "samples": Sample.objects.all(),
        "containers": Container.objects.all(),
        "custody events": CustodyEvent.objects.all(),
        "audit events": AuditEvent.objects.all(),
        "requested tests": RequestedTest.objects.all(),
        "results": Result.objects.all(),
        "result revisions": ResultRevision.objects.all(),
        "result worksheets": ResultWorksheet.objects.all(),
        "worksheet readings": WorksheetReading.objects.all(),
        "QC samples": QCSample.objects.all(),
        "QC measurements": QCMeasurement.objects.all(),
        "QC corrective actions": QCCorrectiveAction.objects.all(),
        "subcontract dispatches": Subcontract.objects.all(),
        "external source results": SubcontractResult.objects.all(),
        "issued reports": Report.objects.filter(latest_version__gt=0),
        "issued report versions": ReportVersion.objects.all(),
        "issued report files": ReportFile.objects.all(),
        "laboratory logos": LabLogo.objects.with_image(),
        EDD_EXPORTS: EddExport.objects.all(),
        **{
            label: model.objects.filter(evidence_data__isnull=False)
            for label, model in LABORATORY_ATTACHMENTS.values()
        },
        "invoice draft": InvoiceDraft.objects.filter(pk=draft.pk),
        "invoice draft lines": draft.lines.all(),
    }


def verify_history_protections(manifest):
    """Probe one preserved row per table. The caller has already verified every baseline ID."""
    append_to_reason = "reason = reason || ' [upgrade rehearsal probe]'"
    verified = []
    for label, collection, model, change in (
        ("Audit events", "audit events", AuditEvent, append_to_reason),
        ("Custody events", "custody events", CustodyEvent, append_to_reason),
        (
            "Issued report versions",
            "issued report versions",
            ReportVersion,
            "released_at = released_at + interval '1 second'",
        ),
        (
            "Stored report PDFs",
            "issued report files",
            ReportFile,
            "pdf = pdf || decode('00', 'hex')",
        ),
    ):
        target = min(manifest[collection]["rows"])
        table = sql.Identifier(model._meta.db_table)
        pk = sql.Identifier(model._meta.pk.column)
        update = sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(table, sql.SQL(change), pk)
        delete = sql.SQL("DELETE FROM {} WHERE {} = %s").format(table, pk)
        for operation, statement in (("UPDATE", update), ("DELETE", delete)):
            probe = f"History protection failed for {label} row {target} ({operation})"
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(statement, [target])
                    affected = cursor.rowcount
                    # Roll back a statement that succeeds because the protection is missing.
                    transaction.set_rollback(True)
            except DatabaseError as exc:
                # The atomic block has rolled back to a usable connection. Only the
                # rejection from prevent_record_mutation() counts as a passing probe.
                cause = exc.__cause__
                if (
                    not isinstance(cause, RaiseException)
                    or cause.diag.message_primary != HISTORY_MUTATION_ERROR
                ):
                    sqlstate = getattr(cause, "sqlstate", None) or "unknown"
                    raise SystemExit(
                        f"{probe}: unexpected database error (SQLSTATE {sqlstate}). "
                        f"Expected '{HISTORY_MUTATION_ERROR}'."
                    ) from None
            else:
                raise SystemExit(
                    f"{probe}: statement completed (row count {affected}). "
                    f"Expected '{HISTORY_MUTATION_ERROR}'."
                )
        verified.append(label)
    return verified


def amendment_for_client(version_ids):
    owner = User.objects.get(username="demo_client")
    amendment = (
        ReportVersion.objects.filter(
            pk__in=version_ids,
            amends_id__in=version_ids,
            report__project__client=owner.client_id,
        )
        .select_related("report", "amends__report")
        .order_by("pk")
        .first()
    )
    require(
        amendment is not None, "The synthetic client needs an issued amendment and its original."
    )
    return owner, amendment


def require_historical_logo(original, amendment):
    facts = original.file.snapshot["content"]["lab"].get("logo")
    require(isinstance(facts, dict), "The original subcontract report must name its logo.")
    logo = LabLogo.objects.with_image().filter(sha256=facts.get("sha256")).first()
    require(
        logo is not None
        and facts["sha256"] == sha256(logo.image)
        and facts.get("content_type") == logo.content_type
        and facts.get("width") == logo.width
        and facts.get("height") == logo.height
        and len(logo.image) == logo.size,
        f"The stored logo must match the image recorded on report version {original.pk}.",
    )
    require(
        "logo" not in amendment.file.snapshot["content"]["lab"]
        and Lab.objects.filter(pk=1, logo__isnull=True).exists(),
        "The amended subcontract report and current laboratory profile must have no logo.",
    )


def subcontract_sources():
    sources = list(
        SubcontractResult.objects.filter(subcontract__external_reference=SUBCONTRACT_REFERENCE)
        .defer(None)
        .order_by("number")
    )
    require(
        [source.number for source in sources] == [1, 2]
        and sources[0].subcontract_id == sources[1].subcontract_id
        and sources[0].source_sha256 != sources[1].source_sha256,
        "The rehearsal needs the original and corrected PDFs for the synthetic subcontract.",
    )
    previous = None
    for source in sources:
        versions = list(source.report_versions.select_related("file"))
        require(
            source.review_decision == SubcontractResult.Decision.ACCEPTED
            and len(source.source_pdf) == source.source_size
            and sha256(source.source_pdf) == source.source_sha256
            and len(versions) == 1,
            f"External source {source.pk} needs an intact PDF, acceptance and one issued version.",
        )
        version = versions[0]
        rows = [
            row
            for sample in version.file.snapshot["content"]["samples"]
            for row in sample["results"]
            if row.get("external_result_id") == source.pk
        ]
        require(
            version.number == source.number
            and version.amends_id == (previous.pk if previous else None)
            and (previous is None or version.report_id == previous.report_id)
            and len(rows) == 1
            and rows[0]["external"]["source_sha256"] == source.source_sha256
            and rows[0]["supplied_text"] == source.observed_text,
            f"Issued report version {version.pk} must retain its matching external source.",
        )
        if previous is not None:
            require_historical_logo(previous, version)
        previous = version
    return sources


def edd_export_for_client(owner, export_ids, version_ids):
    export = (
        EddExport.objects.filter(
            pk__in=export_ids,
            version_id__in=version_ids,
            version__number=1,
            version__report__project__client_id=owner.client_id,
            version__report__title=EDD_REPORT_TITLE,
        )
        .select_related("version__report", "version__edd_context")
        .defer(None)
        .first()
    )
    require(export is not None, "The synthetic client needs its issued EDD export.")
    frozen_samples = export.mapping["version_context"]["samples"]
    current_samples = export.version.edd_context.samples
    require(
        frozen_samples
        and all(
            sample == {"sys_loc_code": ORIGINAL_EDD_LOCATION} for sample in frozen_samples.values()
        )
        and current_samples
        == {lab_id: {"sys_loc_code": UPDATED_EDD_LOCATION} for lab_id in frozen_samples},
        "The synthetic EDD must retain its original location after its live context changes.",
    )
    return export


def require_baseline_edd_zip(export):
    data = bytes(export.data)
    require(
        len(data) == export.size and sha256(data) == export.sha256,
        f"Stored ZIP failed the integrity check for EDD export {export.pk}.",
    )
    try:
        with ZipFile(BytesIO(data)) as archive:
            require(
                archive.namelist() == [f"{section}.txt" for section in SECTION_ORDER]
                and archive.testzip() is None
                and all(export.row_counts.get(section, 0) > 0 for section in SECTION_ORDER),
                f"EDD export {export.pk} needs intact sample, test result and batch ZIP members.",
            )
            header, _, *sample_rows = archive.read("Sample_v5.txt").decode("utf-8").splitlines()
            location_index = header.split("\t").index("sys_loc_code")
            locations = [row.split("\t")[location_index] for row in sample_rows]
            frozen_samples = export.mapping["version_context"]["samples"]
            require(
                [location for location in locations if location]
                == [ORIGINAL_EDD_LOCATION] * len(frozen_samples),
                f"EDD export {export.pk} needs the original mapped location in its sample rows.",
            )
    except BadZipFile as exc:
        raise SystemExit(f"Stored file for EDD export {export.pk} is not a valid ZIP.") from exc


def laboratory_attachments(manifest):
    for kind, (label, model) in LABORATORY_ATTACHMENTS.items():
        records = (
            model.objects.with_evidence().filter(pk__in=manifest[label]["rows"]).order_by("pk")
        )
        for record in records:
            data = bytes(record.evidence_data or b"")
            require(
                record.evidence_name
                and record.evidence_content_type
                and data
                and len(data) == record.evidence_size
                and sha256(data) == record.evidence_sha256,
                f"Stored file failed the integrity check for laboratory {kind} record {record.pk}.",
            )
            yield kind, record


def require_baseline_laboratory_attachments(manifest):
    uploads = {(kind, LABORATORY_ATTACHMENT_REFERENCE): kind for kind in LABORATORY_ATTACHMENTS}
    uploads["nonconformance", NONCONFORMANCE_NOTE_REFERENCE] = "nonconformance-note"
    synthetic_records = {}
    for kind, record in laboratory_attachments(manifest):
        upload_kind = uploads.get((kind, record.reference))
        if upload_kind is None:
            continue
        upload = laboratory_upload(upload_kind)
        require(
            upload_kind not in synthetic_records
            and record.evidence_name == upload.name
            and record.evidence_content_type == "text/plain; charset=utf-8"
            and bytes(record.evidence_data) == upload.read(),
            f"The baseline needs the original synthetic {upload_kind} attachment.",
        )
        synthetic_records[upload_kind] = record
    missing = set(uploads.values()) - synthetic_records.keys()
    require(
        not missing, f"The baseline needs synthetic attachments for {', '.join(sorted(missing))}."
    )
    opened = synthetic_records["nonconformance"]
    note = synthetic_records["nonconformance-note"]
    require(
        opened.kind == NonconformanceEvent.Kind.OPENED
        and note.kind == NonconformanceEvent.Kind.NOTE
        and opened.nonconformance_id == note.nonconformance_id
        and opened.occurred_at <= note.occurred_at
        and opened.actor.username == "demo_analyst"
        and note.actor.username == "demo_custodian"
        and note.note == NONCONFORMANCE_NOTE,
        "The synthetic note attachment must follow the opening attachment on the same "
        "nonconformance and have its original staff author.",
    )
    require(
        AuditEvent.objects.filter(
            action="nonconformance.note_added",
            object_type="laboratory.nonconformance",
            object_id=str(note.nonconformance_id),
            actor_id=note.actor_id,
            reason=note.note,
            after={"event": note.pk, "evidence": note.evidence_sha256},
        ).exists(),
        "The synthetic nonconformance note attachment must have its original audit entry.",
    )


def verify_laboratory_downloads(browser, owner, attachments):
    urls = []
    for kind, record in attachments:
        url = reverse("laboratory:evidence", args=[kind, record.pk])
        response = browser.get(url, secure=True)
        require(
            response.status_code == 200
            and response.headers.get("Content-Type") == record.evidence_content_type
            and response.headers.get("Content-Disposition")
            == content_disposition_header(True, record.evidence_name),
            f"Staff request for laboratory {kind} attachment {record.pk} returned "
            f"{response.status_code} or the wrong download headers.",
        )
        require(
            response.content == bytes(record.evidence_data),
            f"Staff download differs from the stored laboratory {kind} attachment {record.pk}.",
        )
        urls.append((kind, record.pk, url))

    browser.force_login(owner, backend="django.contrib.auth.backends.ModelBackend")
    for kind, pk, url in urls:
        response = browser.get(url, secure=True)
        require(
            response.status_code == 403,
            f"The client requested laboratory {kind} attachment {pk} and received "
            f"{response.status_code}.",
        )
    print("Staff downloaded the original laboratory attachments, and the client received 403.")


@override_settings(ALLOWED_HOSTS=["testserver"])
@transaction.atomic
def verify_downloads(owner, amendment, sources, export, attachments):
    browser = Client()
    browser.force_login(owner, backend="django.contrib.auth.backends.ModelBackend")
    for version in (amendment.amends, amendment):
        data = bytes(version.file.pdf)
        require(
            data.startswith(b"%PDF-")
            and len(data) == version.pdf_size
            and sha256(data) == version.pdf_sha256,
            f"Stored PDF failed the integrity check for report version {version.pk}.",
        )
        response = browser.get(
            reverse("portal:report-pdf", args=[version.report_id, version.number]), secure=True
        )
        require(
            response.status_code == 200
            and response.headers.get("Content-Type") == "application/pdf",
            f"Client PDF request for report version {version.pk} returned {response.status_code} "
            "or the wrong content type.",
        )
        require(
            response.content == data, f"Downloaded PDF differs for report version {version.pk}."
        )

    url = reverse("portal:edd-download", args=[export.version.report_id, export.pk])
    response = browser.get(url, secure=True)
    require(
        response.status_code == 200
        and response.headers.get("Content-Type") == "application/zip"
        and response.headers.get("Content-Disposition")
        == f'attachment; filename="{export.filename}"',
        f"Client EDD request for export {export.pk} returned {response.status_code} "
        "or the wrong download headers.",
    )
    require(
        response.content == bytes(export.data),
        f"Client download differs from the stored EDD ZIP for export {export.pk}.",
    )
    other_client = LabClient.objects.exclude(pk=owner.client_id).get(account_number="EXCSD")
    other_user = User.objects.create_user(
        username="synthetic_edd_other_client",
        email="edd-restore-client@example.invalid",
        client=other_client,
    )
    browser.force_login(other_user, backend="django.contrib.auth.backends.ModelBackend")
    response = browser.get(url, secure=True)
    require(
        response.status_code == 404,
        f"The other client requested EDD export {export.pk} and received {response.status_code}.",
    )
    print(
        "The owning client downloaded the stored exploratory EDD ZIP byte-for-byte, "
        "and the other client received 404."
    )

    browser.force_login(
        User.objects.get(username="demo_reviewer"),
        backend="django.contrib.auth.backends.ModelBackend",
    )
    for source in sources:
        response = browser.get(
            reverse("receipt:subcontract-source-download", args=[source.pk]), secure=True
        )
        require(
            response.status_code == 200
            and response.headers.get("Content-Type") == "application/pdf"
            and response.content == bytes(source.source_pdf),
            f"Staff download differs from the stored external source PDF for result {source.pk}.",
        )
    print("Staff downloaded the original and corrected external source PDFs with matching bytes.")
    verify_laboratory_downloads(browser, owner, attachments)
    # Discard test sessions, last-login updates and the temporary user.
    transaction.set_rollback(True)
    print("The original and amended PDFs downloaded for their owner match their stored bytes.")


def main():
    mode = os.environ.get("UPGRADE_EVIDENCE_MODE")
    require(
        mode in {"prepare", "capture", "verify"},
        "Set UPGRADE_EVIDENCE_MODE to prepare, capture or verify.",
    )
    require(
        Lab.objects.filter(pk=1, name=DEMO_LAB_NAME).exists(),
        "The populated-upgrade check requires the synthetic lab from seed_demo --full.",
    )
    if mode == "prepare":
        prepare()
        return

    path = Path(os.environ["UPGRADE_EVIDENCE_PATH"])
    manifest = json.loads(path.read_text(encoding="utf-8")) if mode == "verify" else None
    if manifest is None:
        draft = InvoiceDraft.objects.filter(lines__description=FEE_DESCRIPTION).first()
    else:
        draft = InvoiceDraft.objects.filter(pk__in=manifest["invoice draft"]["rows"]).first()
    require(draft is not None, "The prepared synthetic invoice draft is missing.")
    require(
        draft.status == InvoiceDraft.Status.DRAFT, "The synthetic invoice is no longer a draft."
    )
    require(getattr(draft, "due_date", None) is None, "The legacy invoice acquired a due date.")
    require(
        getattr(draft, "issued_invoice", None) is None, "The legacy invoice acquired a release."
    )
    require(draft.lines.filter(kind=InvoiceDraftLine.Kind.TEST).exists(), "No priced test lines.")
    querysets = collections(draft)

    if mode == "capture":
        require_receipt_history()
        require_qc_history()
        subcontract_sources()
        manifest = {label: collect(queryset) for label, queryset in querysets.items()}
        for label, evidence in manifest.items():
            require(evidence["rows"], f"Baseline has no {label}.")
        versions = manifest["issued report versions"]["rows"]
        require(
            versions.keys() == manifest["issued report files"]["rows"].keys(),
            "Each baseline report version needs a stored file.",
        )
        owner, _ = amendment_for_client(versions)
        require_baseline_edd_zip(
            edd_export_for_client(owner, manifest[EDD_EXPORTS]["rows"], versions)
        )
        require_baseline_laboratory_attachments(manifest)
        with path.open("x", encoding="utf-8") as out:
            json.dump(manifest, out, sort_keys=True)
        counts = ", ".join(f"{len(item['rows'])} {label}" for label, item in manifest.items())
        print(f"Captured baseline fingerprints for {counts}.")
    else:
        verified_counts = {}
        for label, queryset in querysets.items():
            expected = manifest[label]
            before = expected["rows"]
            require(before, f"Manifest has no baseline {label}.")
            after = collect(queryset, expected)["rows"]
            missing = before.keys() - after.keys()
            added = after.keys() - before.keys()
            changed = [pk for pk in before.keys() & after.keys() if before[pk] != after[pk]]
            require(
                not missing and not added and not changed,
                f"Preservation failed for {label}: {len(missing)} missing, {len(added)} added, "
                f"and {len(changed)} changed against {len(before)} baseline rows.",
            )
            verified_counts[label] = len(after)
        if history_path := os.environ.get("UPGRADE_HISTORY_CHECKS_PATH"):
            verified_history = verify_history_protections(manifest)
        versions = manifest["issued report versions"]["rows"]
        owner, amendment = amendment_for_client(versions)
        export = edd_export_for_client(owner, manifest[EDD_EXPORTS]["rows"], versions)
        verify_downloads(
            owner, amendment, subcontract_sources(), export, laboratory_attachments(manifest)
        )
        if counts_path := os.environ.get("UPGRADE_VERIFIED_COUNTS_PATH"):
            with Path(counts_path).open("x", encoding="utf-8") as out:
                json.dump(verified_counts, out, sort_keys=True)
        if history_path:
            with Path(history_path).open("x", encoding="utf-8") as out:
                json.dump(verified_history, out)
            print("PostgreSQL rejected both UPDATE and DELETE for each protected history table.")
        print("Verified the recorded baseline data and downloads.")


main()
