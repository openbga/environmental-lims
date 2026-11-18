"""Run via manage.py shell --no-imports on the disposable populated-upgrade database."""

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.db.models import JSONField, TextField
from django.db.models.functions import Cast
from django.test import Client, override_settings
from django.urls import reverse

from accounts.models import User
from analytical.models import Result, ResultRevision, ResultWorksheet, WorksheetReading
from commercial import invoices
from commercial.management.commands.seed_commercial_demo import PRICE_LIST
from commercial.models import InvoiceDraft, InvoiceDraftLine, PriceList
from core.management.commands.seed_demo import DEMO_LAB_NAME
from core.models import AuditEvent, Lab
from receipt.models import CustodyEvent, Order, RequestedTest
from reporting.models import Report, ReportFile, ReportVersion

FEE_DESCRIPTION = "Synthetic upgrade verification fee"


def require(condition, message):
    if not condition:
        raise SystemExit(message)


@transaction.atomic
def prepare():
    """Create a real baseline draft with its source lines and a manual fee."""
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
    print("Prepared a synthetic invoice draft with priced tests and a manual fee.")


def pdf_hash(data):
    return hashlib.sha256(data).hexdigest()


def digest(values):
    """Preserve Decimal text and timestamp precision, and hash binary data before JSON encoding."""
    values = [
        pdf_hash(value) if isinstance(value, bytes | memoryview) else value for value in values
    ]
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
        "custody events": CustodyEvent.objects.all(),
        "audit events": AuditEvent.objects.all(),
        "requested tests": RequestedTest.objects.all(),
        "results": Result.objects.all(),
        "result revisions": ResultRevision.objects.all(),
        "result worksheets": ResultWorksheet.objects.all(),
        "worksheet readings": WorksheetReading.objects.all(),
        "issued reports": Report.objects.filter(latest_version__gt=0),
        "issued report versions": ReportVersion.objects.all(),
        "issued report files": ReportFile.objects.all(),
        "invoice draft": InvoiceDraft.objects.filter(pk=draft.pk),
        "invoice draft lines": draft.lines.all(),
    }


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


@override_settings(ALLOWED_HOSTS=["testserver"])
@transaction.atomic
def verify_downloads(owner, amendment):
    browser = Client()
    browser.force_login(owner, backend="django.contrib.auth.backends.ModelBackend")
    for version in (amendment.amends, amendment):
        data = bytes(version.file.pdf)
        require(
            data.startswith(b"%PDF-")
            and len(data) == version.pdf_size
            and pdf_hash(data) == version.pdf_sha256,
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
    transaction.set_rollback(True)  # discard the test session and last-login change
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
        manifest = {label: collect(queryset) for label, queryset in querysets.items()}
        for label, evidence in manifest.items():
            require(evidence["rows"], f"Baseline has no {label}.")
        versions = manifest["issued report versions"]["rows"]
        require(
            versions.keys() == manifest["issued report files"]["rows"].keys(),
            "Each baseline report version needs a stored file.",
        )
        amendment_for_client(versions)
        with path.open("x", encoding="utf-8") as out:
            json.dump(manifest, out, sort_keys=True)
        counts = ", ".join(f"{len(item['rows'])} {label}" for label, item in manifest.items())
        print(f"Captured baseline fingerprints for {counts}.")
    else:
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
        verify_downloads(*amendment_for_client(manifest["issued report versions"]["rows"]))
        print("Preserved all recorded baseline data after migration.")


main()
