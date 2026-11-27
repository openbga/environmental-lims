import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from itertools import groupby
from operator import attrgetter

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import Error as DjangoDatabaseError
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import (
    BinaryField,
    Case,
    CharField,
    Count,
    DateTimeField,
    DecimalField,
    Exists,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import (
    Cast,
    Coalesce,
    Concat,
    Greatest,
    JSONObject,
    Length,
    LPad,
    Replace,
    Substr,
)
from psycopg import Error as PsycopgError
from psycopg import sql
from psycopg.pq import TransactionStatus

from analytical.models import Batch, ImportSourceFile
from commercial.models import InvoiceDraft, IssuedInvoice
from core.management.commands._stored_file_manifest import (
    inventory_differences,
    load_manifest_categories,
)
from core.models import AuditEvent, LabLogo
from laboratory.models import (
    Equipment,
    EquipmentEvent,
    EvidenceMixin,
    Nonconformance,
    NonconformanceEvent,
    TrainingRecord,
)
from laboratory.nonconformance import STEP_FIELDS
from receipt.models import Subcontract, SubcontractResult
from reporting.content import fingerprint
from reporting.models import EddExport, ReportVersion

_NO_EVIDENCE = Q(
    evidence_data__isnull=True,
    evidence_name="",
    evidence_content_type="",
    evidence_sha256="",
    evidence_size__isnull=True,
)
_EVIDENCE_METADATA_PRESENT = ~(Q(evidence_name="") | Q(evidence_content_type=""))


def _report_content_problems(row):
    try:
        content = json.loads(row.content_json) if row.content_json is not None else None
        if not isinstance(content, dict) or not content:
            return ["missing or invalid report snapshot content"]
        actual_hash = fingerprint(content)
    except ValueError, RecursionError:
        return ["missing or invalid report snapshot content"]

    problems = []
    if actual_hash != row.content_sha256:
        problems.append("report content SHA-256 does not match the version's digest")
    if f'"{actual_hash}"' != row.snapshot_hash_json:
        problems.append("report content SHA-256 does not match the snapshot's digest")
    lab = content.get("lab", {})
    if not isinstance(lab, dict):
        problems.append("invalid laboratory identity in report snapshot")
    elif (logo := lab.get("logo")) is not None:
        # JSON booleans and decimals can compare equal to Python integer dimensions.
        if not (
            isinstance(logo, dict)
            and row.stored_logo is not None
            and all(
                type(logo.get(key)) is type(value) and logo[key] == value
                for key, value in row.stored_logo.items()
            )
        ):
            problems.append("missing or mismatched laboratory logo referenced by report snapshot")
    return problems


def _measure_files(files, binary, checksum, size):
    # Use PostgreSQL's bytea functions to avoid returning the file bytes.
    return files.annotate(
        actual_size=Func(binary, function="pg_catalog.octet_length", output_field=IntegerField()),
        actual_hash=Func(
            Func(binary, function="pg_catalog.sha256", output_field=BinaryField()),
            Value("hex"),
            function="pg_catalog.encode",
            output_field=CharField(),
        ),
        recorded_size=F(size) if size else Value(None, output_field=IntegerField()),
        recorded_hash=F(checksum),
    )


def _file_problems(row, *, missing="missing file", metadata_problems=(), check_size=True):
    problems = []
    if row.actual_size is None:
        problems.append(missing)
    elif row.actual_size == 0:
        problems.append("empty stored file")
    if not row.evidence_metadata_present:
        problems.append("missing evidence filename or content type")
    problems.extend(metadata_problems)
    if not row.recorded_hash:
        problems.append("missing recorded SHA-256")
    elif row.actual_hash is not None and row.actual_hash != row.recorded_hash:
        problems.append("SHA-256 mismatch")
    if check_size:
        if row.recorded_size is None:
            problems.append("missing recorded byte length")
        elif row.actual_size is not None and row.actual_size != row.recorded_size:
            problems.append("byte length mismatch")
    return problems


def _check_files(
    model,
    binary,
    checksum,
    size=None,
    *,
    evidence=False,
    missing="missing file",
    metadata=(),
    check_report_content=False,
):
    files = model._base_manager.order_by("pk")
    if evidence:
        files = files.exclude(_NO_EVIDENCE)

    metadata_fields = tuple(f"metadata_{index}" for index in range(len(metadata)))
    rows = _measure_files(files, binary, checksum, size).annotate(
        evidence_metadata_present=_EVIDENCE_METADATA_PRESENT if evidence else Value(True),
        **{field: check for field, (check, _) in zip(metadata_fields, metadata, strict=True)},
    )
    fields = (
        "pk",
        "actual_size",
        "actual_hash",
        "recorded_size",
        "recorded_hash",
        "evidence_metadata_present",
        *metadata_fields,
    )
    if check_report_content:
        # Join the frozen content and PDF measurements in the same query. Do not transfer
        # the release metadata or more than 67 characters of the quoted, 64-digit hash.
        # The logo can have changed since release. Use the indexed digest frozen in
        # each version to retrieve its historical image metadata without loading the bytes.
        stored_logo = LabLogo.objects.filter(sha256=OuterRef("snapshot_logo_sha256")).values(
            facts=JSONObject(
                sha256="sha256", content_type="content_type", width="width", height="height"
            )
        )
        rows = rows.alias(
            snapshot_logo_sha256=KeyTextTransform("sha256", "file__snapshot__content__lab__logo")
        ).annotate(
            stored_logo=Subquery(stored_logo),
            content_json=Cast(KeyTransform("content", "file__snapshot"), TextField()),
            snapshot_hash_json=Substr(
                Cast(KeyTransform("content_sha256", "file__snapshot"), TextField()), 1, 67
            ),
        )
        fields += ("content_json", "content_sha256", "snapshot_hash_json", "stored_logo")
    # Report content can be large. Process one row at a time.
    chunk_size = 1 if check_report_content else None
    for row in rows.values_list(*fields, named=True).iterator(chunk_size=chunk_size):
        problems = _file_problems(
            row,
            missing=missing,
            metadata_problems=(
                message
                for field, (_, message) in zip(metadata_fields, metadata, strict=True)
                if not getattr(row, field)
            ),
            check_size=bool(size),
        )
        if check_report_content and row.actual_size is not None:
            problems.extend(_report_content_problems(row))
        yield model._meta.label, row.pk, row.actual_size, row.actual_hash, problems


def _check_reports():
    release_events = AuditEvent.objects.filter(action__in=("report.released", "report.amended"))
    matching_action = Q(action="report.released", audit_version="1") | (
        Q(action="report.amended") & ~Q(audit_version="1")
    )
    matches_version = matching_action & Q(
        actor_id=OuterRef("released_by_id"),
        after__contains=JSONObject(
            report=_report_number(OuterRef("report_id")),
            pdf_sha256=OuterRef("pdf_sha256"),
            content_sha256=OuterRef("content_sha256"),
        ),
        audit_version=Cast(OuterRef("number"), TextField()),
        audit_predecessor=Coalesce(Cast(OuterRef("amends__number"), TextField()), Value("null")),
        audit_pdf_size=Cast(OuterRef("pdf_size"), TextField()),
    )
    version_type = ReportVersion._meta.label_lower
    events = (
        release_events.filter(object_type=version_type, object_id=Cast(OuterRef("pk"), CharField()))
        .order_by()
        .values("object_id")
        # JSONB equates 1.0 with 1. The JSON text also distinguishes null from a missing key.
        .alias(
            audit_version=Cast(KeyTransform("version", "after"), TextField()),
            audit_predecessor=Cast(KeyTransform("amends_version", "after"), TextField()),
            audit_pdf_size=Cast(KeyTransform("pdf_size", "after"), TextField()),
        )
        .annotate(event_count=Count("pk"), matching_count=Count("pk", filter=matches_version))
        .filter(event_count=1, matching_count=1)
    )
    valid_lineage = Q(number=1, amends__isnull=True) | Q(
        number__gt=1, amends__report_id=F("report_id"), amends__number=F("number") - 1
    )

    yield from _check_files(
        ReportVersion,
        "file__pdf",
        "pdf_sha256",
        "pdf_size",
        missing="missing ReportFile",
        metadata=(
            (
                valid_lineage & Exists(events),
                "missing, ambiguous, or mismatched report release history",
            ),
        ),
        check_report_content=True,
    )
    # Compare the stored ID as text so a malformed target cannot raise a cast error.
    versions = ReportVersion._base_manager.alias(
        target_id=Cast("pk", CharField()), target_type=Value(version_type)
    ).filter(target_id=OuterRef("object_id"), target_type=OuterRef("object_type"))
    missing = release_events.filter(~Exists(versions)).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield (
            AuditEvent._meta.label,
            pk,
            None,
            None,
            ["missing or invalid ReportVersion target"],
        )


def _invoice_utc_timestamp(field, pattern):
    timestamp = Func(
        Value("UTC"),
        OuterRef(field),
        function="pg_catalog.timezone",
        output_field=DateTimeField(),
    )
    return Func(timestamp, Value(pattern), function="pg_catalog.to_char", output_field=TextField())


def _invoice_cancellation_timestamp():
    # The draft snapshot uses DjangoJSONEncoder: UTC ends in Z, with a fraction
    # truncated to milliseconds when the stored timestamp has any microseconds.
    return Func(
        _invoice_utc_timestamp("draft__cancelled_at", 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        Value(r"\.000000Z$|(\.[0-9]{3})[0-9]{3}Z$"),
        Value(r"\1Z"),
        function="pg_catalog.regexp_replace",
        output_field=TextField(),
    )


def _invoice_event_issuer(side):
    # Compare JSON text so only the original integer representation matches.
    return Cast(KeyTransform("issued_by", KeyTransform("issued_invoice", side)), TextField())


def _check_invoices():
    draft_type = InvoiceDraft._meta.label_lower
    issued_statuses = (InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED)
    issue_events = AuditEvent.objects.filter(action="invoice_draft.issued")
    void_events = AuditEvent.objects.filter(action="invoice_draft.voided")
    draft_target = Q(object_type=draft_type, object_id=Cast(OuterRef("draft_id"), CharField()))
    # Invoice issue events use datetime.isoformat(), which omits an all-zero fraction.
    issue_time = Replace(
        _invoice_utc_timestamp("issued_at", 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
        Value(".000000+00:00"),
        Value("+00:00"),
    )
    document = JSONObject(
        number=OuterRef("number"), sha256=OuterRef("sha256"), issued_at=issue_time
    )
    issuer = Cast(OuterRef("issued_by_id"), TextField())
    # The original issuance is unique for each draft, including drafts later voided.
    # Count all issuance events and compare just the relevant fields within PostgreSQL.
    matches_invoice = Q(
        actor_id=OuterRef("issued_by_id"),
        audit_issuer=issuer,
        after__contains=JSONObject(issued_invoice=document),
    )
    events = (
        issue_events.filter(draft_target)
        .order_by()
        .values("object_id")
        .alias(audit_issuer=_invoice_event_issuer("after"))
        .annotate(
            event_count=Count("pk"),
            matching_count=Count("pk", filter=matches_invoice),
        )
        .filter(event_count=1, matching_count=1)
    )
    draft_voids = void_events.filter(draft_target).order_by()
    matching_voids = (
        draft_voids.values("object_id")
        .alias(
            audit_voider=Cast(KeyTransform("cancelled_by", "after"), TextField()),
            issuer_before=_invoice_event_issuer("before"),
            issuer_after=_invoice_event_issuer("after"),
        )
        .annotate(
            event_count=Count("pk"),
            matching_count=Count(
                "pk",
                filter=Q(
                    issuer_before=issuer,
                    issuer_after=issuer,
                    actor_id=OuterRef("draft__cancelled_by_id"),
                    reason=OuterRef("draft__cancel_reason"),
                    audit_voider=Cast(OuterRef("draft__cancelled_by_id"), TextField()),
                    before__contains=JSONObject(
                        status=Value(InvoiceDraft.Status.ISSUED), issued_invoice=document
                    ),
                    after__contains=JSONObject(
                        status=Value(InvoiceDraft.Status.VOIDED),
                        issued_invoice=document,
                        cancel_reason=OuterRef("draft__cancel_reason"),
                        cancelled_at=_invoice_cancellation_timestamp(),
                    ),
                ),
            ),
        )
        .filter(event_count=1, matching_count=1)
    )
    # to_char omits the era and formats infinity as NULL. The issuing application
    # can only have recorded an instant in Python's supported datetime range.
    valid_issue_time = Q(
        issued_at__range=(datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC))
    )
    valid_status = Q(draft__status__in=issued_statuses)
    valid_void_history = (
        ~valid_status
        | (Q(draft__status=InvoiceDraft.Status.ISSUED) & ~Exists(draft_voids))
        | (Q(draft__status=InvoiceDraft.Status.VOIDED) & Exists(matching_voids))
    )
    yield from _check_files(
        IssuedInvoice,
        "pdf",
        "sha256",
        metadata=(
            (
                valid_issue_time & Exists(events),
                "missing, ambiguous, or mismatched invoice issuance event",
            ),
            (valid_status, "invalid draft status for issued invoice"),
            (
                valid_void_history,
                "missing, unexpected, ambiguous, or mismatched invoice void event",
            ),
        ),
    )
    missing = InvoiceDraft.objects.filter(
        status__in=issued_statuses,
        issued_invoice__isnull=True,
    ).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield InvoiceDraft._meta.label, pk, None, None, ["missing IssuedInvoice"]

    # Match the draft PK as text so malformed IDs cannot raise a cast error. Issued and
    # voided drafts with no invoice are already reported above, once per draft.
    drafts = (
        InvoiceDraft.objects.alias(target_id=Cast("pk", CharField()), target_type=Value(draft_type))
        .filter(target_id=OuterRef("object_id"), target_type=OuterRef("object_type"))
        .filter(Q(issued_invoice__isnull=False) | Q(status__in=issued_statuses))
    )
    missing = (issue_events | void_events).filter(~Exists(drafts)).order_by("pk")
    for pk, action in missing.values_list("pk", "action").iterator():
        problem = (
            "missing or invalid void invoice target"
            if action == "invoice_draft.voided"
            else "missing or invalid issued invoice target"
        )
        yield AuditEvent._meta.label, pk, None, None, [problem]


def _check_subcontract_sources():
    subcontract_type = Subcontract._meta.label_lower
    entries = AuditEvent.objects.filter(action="subcontract.result_entered").alias(
        entered_number=Cast(KeyTransform("latest_number", "after"), TextField()),
    )
    source = KeyTransform("source_result", "after")
    # Entry events target the dispatch and record the new source number. Compare the
    # nested result fields too, using JSON text to distinguish integer IDs from other types.
    events = (
        entries.filter(
            object_type=subcontract_type,
            object_id=Cast(OuterRef("subcontract_id"), CharField()),
            entered_number=Cast(OuterRef("number"), TextField()),
        )
        .order_by()
        .values("object_id")
        .alias(
            source_id=Cast(KeyTransform("id", source), TextField()),
            source_dispatch=Cast(KeyTransform("subcontract", source), TextField()),
            source_number=Cast(KeyTransform("number", source), TextField()),
            source_actor=Cast(KeyTransform("entered_by", source), TextField()),
            source_size=Cast(KeyTransform("source_size", source), TextField()),
        )
        .annotate(
            event_count=Count("pk"),
            matching_count=Count(
                "pk",
                filter=Q(
                    actor_id=OuterRef("entered_by_id"),
                    source_id=Cast(OuterRef("pk"), TextField()),
                    source_dispatch=Cast(OuterRef("subcontract_id"), TextField()),
                    source_number=Cast(OuterRef("number"), TextField()),
                    source_actor=Cast(OuterRef("entered_by_id"), TextField()),
                    source_size=Cast(OuterRef("source_size"), TextField()),
                    after__source_result__contains=JSONObject(
                        source_sha256=OuterRef("source_sha256")
                    ),
                ),
            ),
        )
        .filter(event_count=1, matching_count=1)
    )
    yield from _check_files(
        SubcontractResult,
        "source_pdf",
        "source_sha256",
        "source_size",
        metadata=((Exists(events), "missing, ambiguous, or mismatched subcontract source entry"),),
    )

    # Check the reverse link without casting an untrusted event value to an integer.
    sources = SubcontractResult._base_manager.alias(
        dispatch_id=Cast("subcontract_id", CharField()),
        source_number=Cast("number", TextField()),
        dispatch_type=Value(subcontract_type),
    ).filter(
        dispatch_id=OuterRef("object_id"),
        source_number=OuterRef("entered_number"),
        dispatch_type=OuterRef("object_type"),
    )
    missing = entries.filter(~Exists(sources)).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield (
            AuditEvent._meta.label,
            pk,
            None,
            None,
            ["missing or invalid SubcontractResult target"],
        )


def _report_number(report_id):
    # Match Report.number's minimum width without truncating any digits or padding before a sign.
    report_id = Cast(report_id, TextField())
    padded_id = LPad(report_id, Greatest(Value(6), Length(report_id)), Value("0"))
    return Concat(
        Value("RPT-"),
        Func(
            padded_id,
            Value(r"^(0+)-"),
            Value(r"-\1"),
            function="pg_catalog.regexp_replace",
            output_field=TextField(),
        ),
        output_field=TextField(),
    )


def _check_edd_exports():
    export_events = AuditEvent.objects.filter(
        action="report.edd_exported", object_type=EddExport._meta.label_lower
    )
    matches_export = Q(
        after__contains=JSONObject(
            report=_report_number(OuterRef("version__report_id")),
            sha256=OuterRef("sha256"),
            content_sha256=OuterRef("version__content_sha256"),
        ),
        audit_version=Cast(OuterRef("version__number"), TextField()),
    )
    events = (
        export_events.filter(object_id=Cast(OuterRef("pk"), CharField()))
        .order_by()
        .values("object_id")
        # Compare the JSON representation since JSONB numeric equality accepts 1.0 as 1.
        .alias(audit_version=Cast(KeyTransform("version", "after"), TextField()))
        .annotate(event_count=Count("pk"), matching_count=Count("pk", filter=matches_export))
        .filter(event_count=1, matching_count=1)
    )
    yield from _check_files(
        EddExport,
        "data",
        "sha256",
        "size",
        metadata=((Exists(events), "missing, ambiguous, or mismatched EDD export event"),),
    )

    # Join on the recorded ID text so malformed IDs count as missing and cannot raise a cast error.
    exports = EddExport._base_manager.alias(object_id=Cast("pk", CharField())).filter(
        object_id=OuterRef("object_id")
    )
    missing = export_events.filter(~Exists(exports)).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield AuditEvent._meta.label, pk, None, None, ["missing EddExport"]


def _check_training_evidence():
    record_type = TrainingRecord._meta.label_lower
    training_events = AuditEvent.objects.filter(
        action__in=("training.recorded", "training.corrected")
    ).alias(
        audit_name=Cast(KeyTransform("evidence_name", "after"), TextField()),
        audit_hash=Cast(KeyTransform("evidence_sha256", "after"), TextField()),
    )
    record_events = training_events.filter(
        object_type=record_type, object_id=Cast(OuterRef("pk"), CharField())
    ).order_by()
    matches_action = Q(action="training.recorded", audit_corrects="null") | (
        Q(action="training.corrected") & ~Q(audit_corrects="null")
    )
    matches_record = matches_action & Q(
        actor_id=OuterRef("recorded_by_id"),
        audit_staff=Cast(OuterRef("staff_id"), TextField()),
        audit_corrects=Coalesce(Cast(OuterRef("corrects_id"), TextField()), Value("null")),
        after__contains=JSONObject(
            evidence_name=OuterRef("evidence_name"),
            evidence_sha256=OuterRef("evidence_sha256"),
        ),
    )
    matching_events = (
        record_events.values("object_id")
        # JSON text distinguishes integer IDs from numbers like 1.0, and null from a missing key.
        .alias(
            audit_staff=Cast(KeyTransform("staff", "after"), TextField()),
            audit_corrects=Cast(KeyTransform("corrects", "after"), TextField()),
        )
        .annotate(event_count=Count("pk"), matching_count=Count("pk", filter=matches_record))
        .filter(event_count=1, matching_count=1)
    )
    yield from _check_files(
        TrainingRecord,
        "evidence_data",
        "evidence_sha256",
        "evidence_size",
        evidence=True,
        metadata=(
            (Exists(matching_events), "missing, ambiguous, or mismatched training evidence event"),
        ),
    )

    # The writer stores empty strings for optional evidence. Either nonempty string can
    # still show an attachment was recorded if the other field was lost from the event.
    attachment_recorded = Q(audit_name__regex=r'^".+"$') | Q(audit_hash__regex=r'^".+"$')
    records = TrainingRecord._base_manager
    # Use a scalar lookup so PostgreSQL can probe the audit target index for each
    # cleared record rather than comparing every record against the history rows.
    event_id = Subquery(record_events.filter(attachment_recorded).values("pk")[:1])
    cleared = (
        records.filter(_NO_EVIDENCE)
        .alias(recorded_attachment=event_id)
        .filter(recorded_attachment__isnull=False)
        .order_by("pk")
    )
    for pk in cleared.values_list("pk", flat=True).iterator():
        yield TrainingRecord._meta.label, pk, None, None, ["missing recorded training evidence"]

    # Compare the target as text to tolerate malformed IDs, and count a missing target once.
    targets = records.alias(
        target_id=Cast("pk", CharField()), target_type=Value(record_type)
    ).filter(target_id=OuterRef("object_id"), target_type=OuterRef("object_type"))
    missing = (
        training_events.filter(attachment_recorded, ~Exists(targets))
        .order_by("object_type", "object_id", "pk")
        .distinct("object_type", "object_id")
    )
    for pk in missing.values_list("pk", flat=True).iterator():
        yield AuditEvent._meta.label, pk, None, None, ["missing or invalid TrainingRecord target"]


def _attachment_recordings(events, *fields, tie_break=()):
    rows = (
        events.alias(event_json=Cast(KeyTransform("event", "after"), TextField()))
        .annotate(
            # Numeric also holds a malformed 19-digit ID outside bigint range,
            # so the cast succeeds and the number cannot match a stored event.
            target_id=Case(
                When(
                    event_json__regex=r"^(0|-?[1-9][0-9]{0,18})$",
                    then=Cast("event_json", DecimalField(max_digits=19, decimal_places=0)),
                )
            ),
        )
        .order_by("target_id", F("attachment_recorded").desc(nulls_last=True), *tie_break, "pk")
        .values_list("target_id", "pk", "attachment_recorded", *fields, named=True)
    )
    for target, group in groupby(rows.iterator(), key=attrgetter("target_id")):
        if target is None:
            for audit in group:
                if audit.attachment_recorded:
                    yield target, None, audit
            continue
        # A duplicate invalidates the match even if only one audit records a file.
        first = audit = None
        for entry in group:
            audit = entry if first is None else None
            first = first or entry
        yield target, audit, first


def _equipment_recordings():
    events = (
        AuditEvent.objects.filter(
            action__in=(
                "equipment.calibration_recorded",
                "equipment.maintenance_recorded",
                "equipment.event_corrected",
            ),
        )
        .alias(
            name_json=Cast(KeyTransform("evidence_name", "after"), TextField()),
            hash_json=Cast(KeyTransform("evidence_sha256", "after"), TextField()),
        )
        .annotate(
            attachment_recorded=Q(name_json__regex=r'^".+"$') | Q(hash_json__regex=r'^".+"$'),
            # Project one character past the valid width so truncation cannot hide damage.
            asset_json=Substr(Cast(KeyTransform("equipment", "after"), TextField()), 1, 21),
            corrects_json=Substr(Cast(KeyTransform("corrects", "after"), TextField()), 1, 21),
            audit_kind=Substr(KeyTextTransform("kind", "after"), 1, 13),
            filename=Case(
                When(
                    name_json__regex=r'^".*"$',
                    then=Substr(KeyTextTransform("evidence_name", "after"), 1, 256),
                )
            ),
            checksum=Case(
                When(
                    hash_json__regex=r'^".*"$',
                    then=Substr(KeyTextTransform("evidence_sha256", "after"), 1, 65),
                )
            ),
        )
    )
    return _attachment_recordings(
        events,
        "action",
        "actor_id",
        "object_type",
        "object_id",
        "asset_json",
        "audit_kind",
        "corrects_json",
        "filename",
        "checksum",
    )


def _pair_evidence_recordings(rows, audits):
    # The ordered merge reads the audit table once and retains one group at a time.
    pending = next(audits, None)
    for row in rows.iterator():
        while pending and pending[0] is not None and pending[0] < row.pk:
            if pending[2].attachment_recorded:
                yield None, None, pending[2]
            pending = next(audits, None)
        if pending and pending[0] == row.pk:
            _, audit, claim = pending
            pending = next(audits, None)
            yield row, audit, claim
        else:
            yield row, None, None
    while pending:
        if pending[2].attachment_recorded:
            yield None, None, pending[2]
        pending = next(audits, None)


def _matches_equipment_recording(record, audit):
    asset = str(record.equipment_id)
    action = (
        "equipment.event_corrected"
        if record.corrects_id is not None
        else f"equipment.{record.kind}_recorded"
    )
    return (
        audit is not None
        and record.kind in EquipmentEvent.Kind.values
        and audit.action == action
        and audit.actor_id == record.recorded_by_id
        and audit.object_type == Equipment._meta.label_lower
        and audit.object_id == asset
        and audit.asset_json == asset
        and audit.audit_kind == record.kind
        and audit.corrects_json
        == ("null" if record.corrects_id is None else str(record.corrects_id))
        and audit.filename == record.evidence_name
        and audit.checksum == record.recorded_hash
    )


def _check_equipment_evidence():
    files = EquipmentEvent._base_manager.order_by("pk")
    rows = _measure_files(files, "evidence_data", "evidence_sha256", "evidence_size").annotate(
        optional=_NO_EVIDENCE,
        evidence_metadata_present=_EVIDENCE_METADATA_PRESENT,
    )
    fields = (
        "pk",
        "equipment_id",
        "kind",
        "corrects_id",
        "recorded_by_id",
        "optional",
        "actual_size",
        "actual_hash",
        "recorded_size",
        "recorded_hash",
        "evidence_name",
        "evidence_metadata_present",
    )
    rows = rows.values_list(*fields, named=True)
    for row, audit, claim in _pair_evidence_recordings(rows, _equipment_recordings()):
        if row is None:
            yield (
                AuditEvent._meta.label,
                claim.pk,
                None,
                None,
                ["missing or invalid EquipmentEvent target"],
            )
            continue
        if row.optional:
            if claim is not None and claim.attachment_recorded:
                yield (
                    EquipmentEvent._meta.label,
                    row.pk,
                    None,
                    None,
                    ["missing recorded equipment evidence"],
                )
            continue
        problems = _file_problems(row)
        if not _matches_equipment_recording(row, audit):
            problems.append("missing, ambiguous, or mismatched equipment evidence event")
        yield EquipmentEvent._meta.label, row.pk, row.actual_size, row.actual_hash, problems


_NOTE_ACTION = "nonconformance.note_added"
_REOPEN_STEP = (Nonconformance.Status.CLOSED, Nonconformance.Status.INVESTIGATING)
_NONCONFORMANCE_RECORDING_KINDS = {
    f"nonconformance.{target}": NonconformanceEvent.Kind.STATUS
    for current, target in STEP_FIELDS
    if (current, target) != _REOPEN_STEP
} | {
    "nonconformance.opened": NonconformanceEvent.Kind.OPENED,
    "nonconformance.reopened": NonconformanceEvent.Kind.REOPENED,
    _NOTE_ACTION: NonconformanceEvent.Kind.NOTE,
}


def _nonconformance_recordings():
    # Older lifecycle audits had neither key. A surviving key is enough to check
    # the recording, even if its companion field was lost or changed.
    events = (
        AuditEvent.objects.filter(
            Q(action=_NOTE_ACTION)
            | (
                (
                    Q(action__startswith="nonconformance.")
                    | Q(object_type=Nonconformance._meta.label_lower)
                )
                & (Q(after__has_key="event") | Q(after__has_key="evidence"))
            )
        )
        .alias(hash_json=Cast(KeyTransform("evidence", "after"), TextField()))
        .annotate(
            lifecycle=~Q(action=_NOTE_ACTION),
            attachment_recorded=Case(
                When(action=_NOTE_ACTION, then=Q(hash_json__regex=r'^".+"$')),
                When(hash_json='""', then=Value(False)),
                default=Value(True),
            ),
            checksum=Case(
                When(
                    hash_json__regex=r'^".*"$',
                    then=Substr(KeyTextTransform("evidence", "after"), 1, 65),
                )
            ),
            opening_before=Q(before={}),
            before_status=Substr(Cast(KeyTransform("status", "before"), TextField()), 1, 17),
            after_status=Substr(Cast(KeyTransform("status", "after"), TextField()), 1, 17),
        )
    )
    return _attachment_recordings(
        events,
        "action",
        "actor_id",
        "object_type",
        "object_id",
        "checksum",
        "lifecycle",
        "opening_before",
        "before_status",
        "after_status",
        tie_break=("-lifecycle",),
    )


def _matches_nonconformance_recording(row, audit):
    if not (
        audit is not None
        and audit.object_type == Nonconformance._meta.label_lower
        and audit.object_id == str(row.nonconformance_id)
        and audit.actor_id == row.actor_id
        and audit.checksum == row.recorded_hash
    ):
        return False
    if not audit.lifecycle:
        return True
    if audit.after_status != f'"{row.to_status}"':
        return False
    if row.kind == NonconformanceEvent.Kind.OPENED:
        return (
            audit.opening_before
            and not row.from_status
            and row.to_status == Nonconformance.Status.OPEN
        )
    step = (row.from_status, row.to_status)
    action = (
        "nonconformance.reopened" if step == _REOPEN_STEP else f"nonconformance.{row.to_status}"
    )
    return (
        step in STEP_FIELDS
        and audit.action == action
        and audit.before_status == f'"{row.from_status}"'
    )


def _check_nonconformance_evidence():
    files = NonconformanceEvent._base_manager.order_by("pk")
    rows = (
        _measure_files(files, "evidence_data", "evidence_sha256", "evidence_size")
        .annotate(optional=_NO_EVIDENCE, evidence_metadata_present=_EVIDENCE_METADATA_PRESENT)
        .values_list(
            "pk",
            "nonconformance_id",
            "actor_id",
            "kind",
            "from_status",
            "to_status",
            "optional",
            "actual_size",
            "actual_hash",
            "recorded_size",
            "recorded_hash",
            "evidence_metadata_present",
            named=True,
        )
    )
    label = NonconformanceEvent._meta.label
    for row, audit, claim in _pair_evidence_recordings(rows, _nonconformance_recordings()):
        is_note = row is not None and row.kind == NonconformanceEvent.Kind.NOTE
        has_file = row is not None and not row.optional
        kind_matches = (
            row is not None
            and claim is not None
            and (_NONCONFORMANCE_RECORDING_KINDS.get(claim.action) == row.kind)
        )
        # Keep an attached note's provenance failures on the note row, as before.
        if (
            claim is not None
            and (claim.attachment_recorded or (claim.lifecycle and has_file))
            and not kind_matches
            and not (is_note and has_file)
        ):
            yield (
                AuditEvent._meta.label,
                claim.pk,
                None,
                None,
                ["missing or invalid NonconformanceEvent target"],
            )
        if row is None:
            continue
        subject = "note" if is_note else "lifecycle"
        if row.optional:
            if kind_matches and claim.attachment_recorded:
                yield (
                    label,
                    row.pk,
                    None,
                    None,
                    [f"missing recorded nonconformance {subject} evidence"],
                )
            continue
        problems = _file_problems(row)
        if (is_note or kind_matches) and not (
            kind_matches and _matches_nonconformance_recording(row, audit)
        ):
            problems.append(f"missing, ambiguous, or mismatched nonconformance {subject} event")
        yield label, row.pk, row.actual_size, row.actual_hash, problems


def _check_import_sources():
    # Match only the recorded source fields, leaving the rest of the audit payload in PostgreSQL.
    matches_event = Q(
        event__action="batch.results_imported",
        event__object_type=Batch._meta.label_lower,
        event__object_id=Cast("batch_id", CharField()),
        event__after__contains=JSONObject(
            source_file_stored=Value(True),
            filename=F("filename"),
            file_sha256=F("sha256"),
            file_bytes=F("size"),
        ),
    )
    yield from _check_files(
        ImportSourceFile,
        "data",
        "sha256",
        "size",
        metadata=((matches_event, "import event does not match the stored source"),),
    )
    missing = AuditEvent.objects.filter(
        action="batch.results_imported",
        object_type=Batch._meta.label_lower,
        after__source_file_stored=True,
        import_source_file__isnull=True,
    ).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield AuditEvent._meta.label, pk, None, None, ["missing ImportSourceFile"]


def _check_migration_history():
    # Django records the individual migrations when it applies a squash. An installation
    # that already applied all the originals may not have the squash's own marker yet.
    loader = MigrationLoader(connection, load=False)
    loader.load_disk()
    known = set(loader.disk_migrations)
    squash_markers = set()
    for key, migration in loader.disk_migrations.items():
        if migration.replaces:
            known.update(migration.replaces)
            squash_markers.add(key)

    applied = MigrationRecorder(connection).applied_migrations().keys()
    unapplied = known - squash_markers - applied
    unknown = applied - known
    if unapplied or unknown:
        raise CommandError(
            f"Migration history mismatch: {len(unapplied)} unapplied, {len(unknown)} unknown. "
            "Check the database and use the application version that created the backup."
        )


class Command(BaseCommand):
    help = "Verify database-stored files, report content and associated audit history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=("text", "json", "manifest"),
            default="text",
            help="Summary format. Use manifest to compare verified files before and after restore.",
        )
        parser.add_argument(
            "--compare-manifest",
            metavar="PATH",
            help="Check the restored files against a saved version 1 inventory (text output).",
        )
        parser.add_argument(
            "--snapshot",
            metavar="ID",
            help="Use a PostgreSQL snapshot whose exporting transaction is still open.",
        )

    def handle(self, *args, **options):
        output_format = options["format"]
        snapshot = options["snapshot"]
        reference_path = options["compare_manifest"]
        saved = None
        if reference_path is not None:
            if output_format != "text":
                raise CommandError("--compare-manifest requires --format text.")
            saved = load_manifest_categories(reference_path)
        scan_format = "manifest" if saved is not None else output_format
        summary, total_checked, total_failures = self._scan_snapshot(snapshot, scan_format)

        if saved is not None:
            if total_failures:
                raise CommandError("Stored file verification failed before inventory comparison.")
            differences = list(inventory_differences(saved, summary))
            if differences:
                raise CommandError("\n".join(["Stored file inventory differs.", *differences]))
            self.stdout.write(f"Stored file inventory matches. Files checked: {total_checked}.")
        elif output_format == "manifest":
            if not total_failures:
                self.stdout.write(
                    json.dumps({"version": 1, "categories": summary}, indent=2, sort_keys=True)
                )
        elif output_format == "json":
            summary["Total"] = {"checked": total_checked, "failures": total_failures}
            self.stdout.write(json.dumps(summary))
        else:
            self.stdout.write(f"Total: checked={total_checked} failures={total_failures}")
        if total_failures:
            raise CommandError("Stored file verification failed.")

    def _scan_snapshot(self, snapshot, output_format):
        # pg_export_snapshot() returns two eight-digit hex fields and a decimal counter.
        if snapshot is not None and (
            not isinstance(snapshot, str)
            or not re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{8}-[1-9][0-9]{0,9}", snapshot)
        ):
            raise CommandError("--snapshot requires a PostgreSQL snapshot ID.")

        try:
            autocommit = connection.get_autocommit()
        except DjangoDatabaseError:
            raise CommandError(
                "Could not connect to PostgreSQL for snapshot verification."
            ) from None
        if (
            not autocommit
            or connection.connection.info.transaction_status != TransactionStatus.IDLE
        ):
            if snapshot is not None:
                raise CommandError("Snapshot verification requires a fresh PostgreSQL transaction.")
            # Programmatic callers own their isolation level and any uncommitted records.
            return self._scan(output_format)

        try:
            with transaction.atomic(durable=True):
                with connection.connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    if snapshot is not None:
                        # SET needs a SQL literal. Keep the ID out of Django's query logs.
                        try:
                            cursor.execute(
                                sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot))
                            )
                        except PsycopgError:
                            raise CommandError(
                                "Could not import the PostgreSQL snapshot. Use the same database "
                                "and keep the exporting transaction open during import."
                            ) from None
                return self._scan(output_format)
        except DjangoDatabaseError, PsycopgError:
            raise CommandError("A PostgreSQL error interrupted stored file verification.") from None

    def _scan(self, output_format):
        _check_migration_history()
        categories = [
            ("Report PDFs", _check_reports()),
            ("Issued invoice PDFs", _check_invoices()),
            ("Subcontract source PDFs", _check_subcontract_sources()),
            ("EDD export ZIPs", _check_edd_exports()),
            ("Laboratory logos", _check_files(LabLogo, "image", "sha256", "size")),
        ]
        evidence_checks = {
            EquipmentEvent: _check_equipment_evidence,
            NonconformanceEvent: _check_nonconformance_evidence,
            TrainingRecord: _check_training_evidence,
        }
        categories.extend(
            (
                f"{model._meta.verbose_name.capitalize()} evidence",
                evidence_checks[model]()
                if model in evidence_checks
                else _check_files(
                    model, "evidence_data", "evidence_sha256", "evidence_size", evidence=True
                ),
            )
            for model in apps.get_models()
            if issubclass(model, EvidenceMixin) and not model._meta.proxy
        )

        categories.append(("Import source files", _check_import_sources()))

        summary = {}
        total_checked = total_failures = 0
        for category, rows in categories:
            checked = failures = 0
            digest = sha256() if output_format == "manifest" else None
            for model_label, pk, actual_size, actual_hash, problems in rows:
                checked += 1
                if problems:
                    failures += 1
                    self.stderr.write(f"{model_label} pk={pk}: {', '.join(problems)}")
                elif digest is not None:
                    # Version 1 frames each PK-ordered row as one compact JSON array per line.
                    row = json.dumps(
                        (model_label, str(pk), actual_size, actual_hash), separators=(",", ":")
                    )
                    digest.update((row + "\n").encode("ascii"))
            if digest is not None:
                summary[category] = {"count": checked, "sha256": digest.hexdigest()}
            elif output_format == "json":
                summary[category] = {"checked": checked, "failures": failures}
            else:
                self.stdout.write(f"{category}: checked={checked} failures={failures}")
            total_checked += checked
            total_failures += failures
        return summary, total_checked, total_failures
