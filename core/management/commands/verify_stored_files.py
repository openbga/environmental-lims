import json
import re
from contextlib import nullcontext
from hashlib import sha256

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import Error as DjangoDatabaseError
from django.db import connection, transaction
from django.db.models import (
    BinaryField,
    CharField,
    Count,
    Exists,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    TextField,
    Value,
)
from django.db.models.fields.json import KeyTransform
from django.db.models.functions import Cast, JSONObject, Substr
from psycopg import Error as PsycopgError
from psycopg import sql
from psycopg.pq import TransactionStatus

from analytical.models import Batch, ImportSourceFile
from commercial.models import InvoiceDraft, IssuedInvoice
from core.models import AuditEvent, LabLogo
from laboratory.models import EvidenceMixin
from receipt.models import SubcontractResult
from reporting.content import fingerprint
from reporting.models import EddExport, ReportVersion


def _report_content_problems(content_json, recorded_hash, snapshot_hash_json):
    try:
        content = json.loads(content_json) if content_json is not None else None
        if not isinstance(content, dict) or not content:
            return ["missing or invalid report snapshot content"]
        actual_hash = fingerprint(content)
    except ValueError, RecursionError:
        return ["missing or invalid report snapshot content"]

    problems = []
    if actual_hash != recorded_hash:
        problems.append("report content SHA-256 does not match the version's digest")
    if f'"{actual_hash}"' != snapshot_hash_json:
        problems.append("report content SHA-256 does not match the snapshot's digest")
    return problems


def _check_files(
    model,
    binary,
    checksum,
    size=None,
    *,
    evidence=False,
    missing="missing file",
    metadata=None,
    check_report_content=False,
):
    files = model._base_manager.order_by("pk")
    if evidence:
        files = files.exclude(
            evidence_data__isnull=True,
            evidence_name="",
            evidence_content_type="",
            evidence_sha256="",
            evidence_size__isnull=True,
        )
        metadata = (
            ~(Q(evidence_name="") | Q(evidence_content_type="")),
            "missing evidence filename or content type",
        )

    # Use PostgreSQL's bytea functions to avoid returning the file bytes.
    rows = files.annotate(
        actual_size=Func(binary, function="pg_catalog.octet_length", output_field=IntegerField()),
        actual_hash=Func(
            Func(binary, function="pg_catalog.sha256", output_field=BinaryField()),
            Value("hex"),
            function="pg_catalog.encode",
            output_field=CharField(),
        ),
        recorded_size=F(size) if size else Value(None, output_field=IntegerField()),
        metadata_present=metadata[0] if metadata else Value(True),
    )
    fields = ("pk", "actual_size", "actual_hash", "recorded_size", checksum, "metadata_present")
    if check_report_content:
        # Join the frozen content and PDF measurements in the same query. Do not transfer
        # the release metadata or more than 67 characters of the quoted, 64-digit hash.
        rows = rows.annotate(
            content_json=Cast(KeyTransform("content", "file__snapshot"), TextField()),
            snapshot_hash_json=Substr(
                Cast(KeyTransform("content_sha256", "file__snapshot"), TextField()), 1, 67
            ),
        )
        fields += ("content_json", "content_sha256", "snapshot_hash_json")
    # Report content can be large. Process one row at a time.
    for row in rows.values_list(*fields).iterator(chunk_size=1 if check_report_content else None):
        pk, actual_size, actual_hash, recorded_size, recorded_hash, has_metadata, *content = row
        problems = []
        if actual_size is None:
            problems.append(missing)
        elif actual_size == 0:
            problems.append("empty stored file")
        if metadata and not has_metadata:
            problems.append(metadata[1])
        if not recorded_hash:
            problems.append("missing recorded SHA-256")
        elif actual_hash is not None and actual_hash != recorded_hash:
            problems.append("SHA-256 mismatch")
        if size:
            if recorded_size is None:
                problems.append("missing recorded byte length")
            elif actual_size is not None and actual_size != recorded_size:
                problems.append("byte length mismatch")
        if check_report_content and actual_size is not None:
            problems.extend(_report_content_problems(*content))
        yield model._meta.label, pk, actual_size, actual_hash, problems


def _check_reports():
    # Keep the cursor inside a transaction until it closes, so PostgreSQL does not
    # materialize the entire result on commit. Reuse a caller's transaction if present.
    needs_transaction = (
        connection.get_autocommit()
        and connection.connection.info.transaction_status == TransactionStatus.IDLE
    )
    with transaction.atomic(savepoint=False) if needs_transaction else nullcontext():
        yield from _check_files(
            ReportVersion,
            "file__pdf",
            "pdf_sha256",
            "pdf_size",
            missing="missing ReportFile",
            check_report_content=True,
        )


def _check_invoices():
    # The original issuance is unique for each draft, including drafts later voided.
    # Count all issuance events and compare just the relevant fields within PostgreSQL.
    matches_invoice = Q(
        after__contains=JSONObject(
            issued_invoice=JSONObject(number=OuterRef("number"), sha256=OuterRef("sha256"))
        )
    )
    events = (
        AuditEvent.objects.filter(
            action="invoice_draft.issued",
            object_type=InvoiceDraft._meta.label_lower,
            object_id=Cast(OuterRef("draft_id"), CharField()),
        )
        .order_by()
        .values("object_id")
        .annotate(
            event_count=Count("pk"),
            matching_count=Count("pk", filter=matches_invoice),
        )
        .filter(event_count=1, matching_count=1)
    )
    yield from _check_files(
        IssuedInvoice,
        "pdf",
        "sha256",
        metadata=(Exists(events), "missing, ambiguous, or mismatched invoice issuance event"),
    )
    missing = InvoiceDraft.objects.filter(
        status__in=[InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED],
        issued_invoice__isnull=True,
    ).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield InvoiceDraft._meta.label, pk, None, None, ["missing IssuedInvoice"]


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
        metadata=(matches_event, "import event does not match the stored source"),
    )
    missing = AuditEvent.objects.filter(
        action="batch.results_imported",
        object_type=Batch._meta.label_lower,
        after__source_file_stored=True,
        import_source_file__isnull=True,
    ).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield AuditEvent._meta.label, pk, None, None, ["missing ImportSourceFile"]


class Command(BaseCommand):
    help = "Verify database-stored files, invoice issuance history, and frozen report content."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=("text", "json", "manifest"),
            default="text",
            help="Summary format. Use manifest to compare verified files before and after restore.",
        )
        parser.add_argument(
            "--snapshot",
            metavar="ID",
            help="Use a PostgreSQL snapshot whose exporting transaction is still open.",
        )

    def handle(self, *args, **options):
        output_format = options["format"]
        snapshot = options["snapshot"]
        if snapshot is None:
            summary, total_checked, total_failures = self._scan(output_format)
        else:
            summary, total_checked, total_failures = self._scan_snapshot(snapshot, output_format)

        if output_format == "manifest":
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
        if not isinstance(snapshot, str) or not re.fullmatch(
            r"[0-9A-F]{8}-[0-9A-F]{8}-[1-9][0-9]{0,9}", snapshot
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
            raise CommandError("Snapshot verification requires a fresh PostgreSQL transaction.")

        try:
            with transaction.atomic(durable=True):
                # SET needs a SQL literal. Keep the snapshot ID out of Django's query logs.
                with connection.connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
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
        categories = [
            ("Report PDFs", _check_reports()),
            ("Issued invoice PDFs", _check_invoices()),
            (
                "Subcontract source PDFs",
                _check_files(SubcontractResult, "source_pdf", "source_sha256", "source_size"),
            ),
            ("EDD export ZIPs", _check_files(EddExport, "data", "sha256", "size")),
            ("Laboratory logos", _check_files(LabLogo, "image", "sha256", "size")),
        ]
        categories.extend(
            (
                f"{model._meta.verbose_name.capitalize()} evidence",
                _check_files(
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
