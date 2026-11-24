import json
import re
from hashlib import sha256

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import Error as DjangoDatabaseError
from django.db import connection, transaction
from django.db.models import BinaryField, CharField, F, Func, IntegerField, Q, Value
from psycopg import Error as PsycopgError
from psycopg import sql
from psycopg.pq import TransactionStatus

from commercial.models import InvoiceDraft, IssuedInvoice
from core.models import LabLogo
from laboratory.models import EvidenceMixin
from receipt.models import SubcontractResult
from reporting.models import EddExport, ReportVersion


def _check_files(model, binary, checksum, size=None, *, evidence=False, missing="missing file"):
    files = model._base_manager.order_by("pk")
    metadata_present = Value(True)
    if evidence:
        files = files.exclude(
            evidence_data__isnull=True,
            evidence_name="",
            evidence_content_type="",
            evidence_sha256="",
            evidence_size__isnull=True,
        )
        metadata_present = ~(Q(evidence_name="") | Q(evidence_content_type=""))

    # Use PostgreSQL's built-in bytea functions and stream only IDs and comparison values.
    rows = files.annotate(
        actual_size=Func(binary, function="pg_catalog.octet_length", output_field=IntegerField()),
        actual_hash=Func(
            Func(binary, function="pg_catalog.sha256", output_field=BinaryField()),
            Value("hex"),
            function="pg_catalog.encode",
            output_field=CharField(),
        ),
        recorded_size=F(size) if size else Value(None, output_field=IntegerField()),
        metadata_present=metadata_present,
    ).values_list("pk", "actual_size", "actual_hash", "recorded_size", checksum, "metadata_present")
    for pk, actual_size, actual_hash, recorded_size, recorded_hash, has_metadata in rows.iterator():
        problems = []
        if actual_size is None:
            problems.append(missing)
        elif actual_size == 0:
            problems.append("empty stored file")
        if not has_metadata:
            problems.append("missing evidence filename or content type")
        if not recorded_hash:
            problems.append("missing recorded SHA-256")
        elif actual_hash is not None and actual_hash != recorded_hash:
            problems.append("SHA-256 mismatch")
        if size:
            if recorded_size is None:
                problems.append("missing recorded byte length")
            elif actual_size is not None and actual_size != recorded_size:
                problems.append("byte length mismatch")
        yield model._meta.label, pk, actual_size, actual_hash, problems


def _check_invoices():
    yield from _check_files(IssuedInvoice, "pdf", "sha256")
    missing = InvoiceDraft.objects.filter(
        status__in=[InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED],
        issued_invoice__isnull=True,
    ).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield InvoiceDraft._meta.label, pk, None, None, ["missing IssuedInvoice"]


class Command(BaseCommand):
    help = "Verify database-stored files against their recorded SHA-256 and byte lengths."

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
            (
                "Report PDFs",
                _check_files(
                    ReportVersion,
                    "file__pdf",
                    "pdf_sha256",
                    "pdf_size",
                    missing="missing ReportFile",
                ),
            ),
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
