import json

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import BinaryField, CharField, F, Func, IntegerField, Q, Value

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
        yield model._meta.label, pk, problems


def _check_invoices():
    yield from _check_files(IssuedInvoice, "pdf", "sha256")
    missing = InvoiceDraft.objects.filter(
        status__in=[InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED],
        issued_invoice__isnull=True,
    ).order_by("pk")
    for pk in missing.values_list("pk", flat=True).iterator():
        yield InvoiceDraft._meta.label, pk, ["missing IssuedInvoice"]


class Command(BaseCommand):
    help = "Verify database-stored files against their recorded SHA-256 and byte lengths."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format", choices=("text", "json"), default="text", help="Summary output format."
        )

    def handle(self, *args, **options):
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

        json_output = options["format"] == "json"
        summary = {}
        total_checked = total_failures = 0
        for category, rows in categories:
            checked = failures = 0
            for model_label, pk, problems in rows:
                checked += 1
                if problems:
                    failures += 1
                    self.stderr.write(f"{model_label} pk={pk}: {', '.join(problems)}")
            if json_output:
                summary[category] = {"checked": checked, "failures": failures}
            else:
                self.stdout.write(f"{category}: checked={checked} failures={failures}")
            total_checked += checked
            total_failures += failures

        if json_output:
            summary["Total"] = {"checked": total_checked, "failures": total_failures}
            self.stdout.write(json.dumps(summary))
        else:
            self.stdout.write(f"Total: checked={total_checked} failures={total_failures}")
        if total_failures:
            raise CommandError("Stored file verification failed.")
