"""Prepare, issue and void invoices for physically received requested tests."""

import hashlib
import json
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from core.audit import record_event, snapshot
from core.models import Lab
from receipt.models import Client, Order, Project, RequestedTest

from .invoice_pdf import render_pdf
from .models import (
    InvoiceDraft,
    InvoiceDraftLine,
    IssuedInvoice,
    PriceList,
    Quote,
    QuoteLine,
    QuoteVersion,
)
from .permissions import PRICING_ROLES, require_role
from .pricing import test_label

MAX_LINES = 200
MAX_QUANTITY = 2_147_483_647
MAX_PRICE = Decimal("9999999999.99")
MAX_AMOUNT = Decimal("999999999999.99")
CENTS = Decimal("0.01")
_DUE_DATE_UNSET = object()
BILLING_FIELDS = (
    "client_name",
    "account_code",
    "billing_contact_name",
    "billing_email",
    "billing_address",
    "payment_terms",
    "po_number",
)


def eligible_quote_versions(order):
    """Accepted issued sources for this order; complete scope matching occurs on creation."""
    return (
        QuoteVersion.objects.filter(
            quote__client_id=order.project.client_id,
            quote__status=Quote.Status.ACCEPTED,
            quote__decision_version=F("number"),
            issued_at__isnull=False,
        )
        .filter(Q(quote__project_id=order.project_id) | Q(quote__project__isnull=True))
        .select_related("quote")
    )


def quote_version_label(version):
    return f"{version.quote.number} · version {version.number} · {version.presented_title}"


def _quote_source(order, quote_version_id, tests):
    error = {
        "quote_version": "Choose an accepted issued quote version for this client and project."
    }
    try:
        # Quote services lock the parent before checking/changing its decision or versions.
        quote_id = QuoteVersion.objects.values_list("quote_id", flat=True).get(pk=quote_version_id)
        quote = (
            Quote.objects.select_for_update(of=("self",))
            .select_related("decided_by")
            .get(pk=quote_id)
        )
        version = (
            eligible_quote_versions(order).select_for_update(of=("self",)).get(pk=quote_version_id)
        )
    except Quote.DoesNotExist, QuoteVersion.DoesNotExist, ValueError, TypeError, ValidationError:
        raise ValidationError(error) from None
    version.quote = quote
    lines = list(version.lines.select_for_update()[: MAX_LINES + 1])
    if len(lines) > MAX_LINES:
        raise ValidationError(
            {"quote_version": f"A draft invoice may have at most {MAX_LINES} lines."}
        )
    by_test, adjustments = {}, []
    for line in lines:
        if (
            line.kind not in QuoteLine.Kind.values
            or (line.kind == QuoteLine.Kind.TEST) != bool(line.test_id)
            or not line.description.strip()
        ):
            raise ValidationError(
                {
                    "quote_version": "The quote contains an invalid source line; "
                    "correct its scope first."
                }
            )
        try:
            amount = _money(line.unit_price, line.quantity, line.position, kind=line.kind)
        except ValidationError as exc:
            raise ValidationError({"quote_version": exc.messages}) from exc
        if line.amount != amount:
            raise ValidationError(
                {"quote_version": "A quote line amount does not match its price and quantity."}
            )
        if line.kind != QuoteLine.Kind.TEST:
            adjustments.append(line)
            continue
        if line.test_id in by_test:
            raise ValidationError(
                {
                    "quote_version": "The quote has duplicate test lines. "
                    "Choose a quote with one line per test definition."
                }
            )
        by_test[line.test_id] = line
    counts = Counter(test.definition_id for test in tests)
    if not lines or set(by_test) != set(counts):
        raise ValidationError(
            {
                "quote_version": "The entire quote must match the received requested tests, "
                "with no missing or extra tests."
            }
        )
    if any(line.quantity != counts[test_id] for test_id, line in by_test.items()):
        raise ValidationError(
            {
                "quote_version": "Each quote quantity must equal the number of current received "
                "requested tests of that definition."
            }
        )
    if len(tests) + len(adjustments) > MAX_LINES:
        raise ValidationError(
            {
                "quote_version": f"A draft invoice may have at most {MAX_LINES} lines, "
                "including fees and discounts."
            }
        )
    return version, by_test, adjustments


def source_tests(order, *, lock=False):
    qs = (
        RequestedTest.objects.filter(sample__order=order)
        .select_related("sample", "definition__analyte", "definition__method")
        .order_by("sample__lab_id", "pk")
    )
    if lock:
        qs = qs.select_for_update(of=("self",))
    return list(qs[: MAX_LINES + 1])


def source_state(order, tests=None):
    """Opaque browser token: includes billing source and every received sample revision."""
    tests = source_tests(order) if tests is None else tests
    payload = {
        "order": snapshot(order),
        "project": snapshot(order.project),
        # Deliberately select only billing data, never internal notes or account-hold notes.
        "client": billing_snapshot(order.project.client),
        "samples": list(order.samples.order_by("pk").values_list("pk", "revision")),
        "tests": [[t.pk, t.sample_id, t.definition_id, test_label(t.definition)] for t in tests],
    }
    return salted_hmac(
        "invoice.source", json.dumps(payload, sort_keys=True, default=str), algorithm="sha256"
    ).hexdigest()


def billing_snapshot(client):
    return {
        "client_name": client.name,
        "account_code": client.account_number,
        "billing_contact_name": client.billing_contact_name or client.contact_name,
        "billing_email": client.billing_email or client.email,
        "billing_address": client.billing_address or "\n".join(client.address_lines),
        "payment_terms": client.payment_terms,
    }


def _state(draft):
    return {**snapshot(draft), "lines": [snapshot(line) for line in draft.lines.all()]}


def _money(price, quantity, position, *, kind=InvoiceDraftLine.Kind.TEST):
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        raise ValidationError(f"Line {position}: quantity must be a positive whole number.")
    if kind == InvoiceDraftLine.Kind.TAX and quantity != 1:
        raise ValidationError(f"Line {position}: tax quantity must be exactly 1.")
    if not isinstance(price, Decimal) or not price.is_finite() or abs(price) > MAX_PRICE:
        raise ValidationError(f"Line {position}: enter a bounded price in USD.")
    if kind == InvoiceDraftLine.Kind.ADJUSTMENT:
        if price > 0:
            raise ValidationError(f"Line {position}: a discount price must be zero or negative.")
    elif kind in (InvoiceDraftLine.Kind.TEST, InvoiceDraftLine.Kind.FEE, InvoiceDraftLine.Kind.TAX):
        if price < 0:
            raise ValidationError(f"Line {position}: a test, fee or tax price must be nonnegative.")
    else:
        raise ValidationError(f"Line {position}: the source line kind is invalid.")
    if price != price.quantize(CENTS):
        raise ValidationError(f"Line {position}: prices must be whole cents in USD.")
    amount = price * quantity
    if abs(amount) > MAX_AMOUNT:
        raise ValidationError(f"Line {position}: this amount is too large to record.")
    return amount


def _total(lines):
    total = sum((line.amount for line in lines), Decimal("0.00"))
    if total < 0:
        raise ValidationError("Discounts exceed the draft's charges; the total cannot be negative.")
    if total > MAX_AMOUNT:
        raise ValidationError("The draft total is too large to record; split the source order.")
    return total


@transaction.atomic
def create_invoice_draft(
    *, actor, order_id, expected_source_state, price_list_id=None, quote_version_id=None
):
    require_role(actor, *PRICING_ROLES)
    if (price_list_id is None) == (quote_version_id is None):
        raise ValidationError(
            "Choose exactly one pricing source: an active price list or an accepted quote version."
        )
    # Match receipt/release order: client, project, order, then samples. Pricing edits lock their
    # parent list; holding it keeps its item prices stable throughout this snapshot.
    original = Order.objects.select_related("project").get(pk=order_id)
    client = Client.objects.select_for_update().get(pk=original.project.client_id)
    project = Project.objects.select_for_update().get(pk=original.project_id)
    order = Order.objects.select_for_update().get(pk=order_id)
    order.project = project
    project.client = client
    source_tests(order, lock=True)
    list(order.samples.select_for_update().order_by("pk"))
    # Corrections lock requested tests before samples; re-read labels/disposition only after
    # both locks are held, since the initial joined sample values may have preceded a change.
    tests = source_tests(order, lock=True)
    if not expected_source_state or not constant_time_compare(
        source_state(order, tests), expected_source_state
    ):
        raise ValidationError(
            "This order or its samples changed. "
            "Reload and review the current tests before creating a draft."
        )
    if order.is_cancelled:
        raise ValidationError("This chain of custody is cancelled; a draft cannot be created.")
    active_status = (
        InvoiceDraft.objects.filter(
            order=order, status__in=(InvoiceDraft.Status.DRAFT, InvoiceDraft.Status.ISSUED)
        )
        .values_list("status", flat=True)
        .first()
    )
    if active_status == InvoiceDraft.Status.DRAFT:
        raise ValidationError(
            "This order already has an active draft invoice. Open that draft instead."
        )
    if active_status == InvoiceDraft.Status.ISSUED:
        raise ValidationError(
            "This order already has an issued invoice. Open that invoice instead."
        )
    if not tests:
        raise ValidationError(
            "Receive samples with requested tests before creating a draft invoice."
        )
    if len(tests) > MAX_LINES:
        raise ValidationError(f"A draft invoice may have at most {MAX_LINES} received tests.")
    quote_lines, adjustments = {}, []
    if quote_version_id is not None:
        version, quote_lines, adjustments = _quote_source(order, quote_version_id, tests)
        quote = version.quote
        pricing_source = {
            "price_list": None,
            "price_list_name": "",
            "price_list_revision": None,
            "quote_version": version,
            "quote_snapshot": {
                "number": quote.number,
                "version": version.number,
                "title": version.presented_title,
                "accepted_at": quote.decided_at.isoformat() if quote.decided_at else None,
                "accepted_by_id": quote.decided_by_id,
                "accepted_by": (
                    quote.decided_by.get_full_name() or quote.decided_by.get_username()
                    if quote.decided_by_id
                    else ""
                ),
            },
        }
        prices = {test_id: line.unit_price for test_id, line in quote_lines.items()}
    else:
        try:
            price_list = PriceList.objects.select_for_update().get(pk=price_list_id)
        except PriceList.DoesNotExist, ValueError, TypeError, ValidationError:
            raise ValidationError({"price_list": "Choose an active price list."}) from None
        if not price_list.is_active:
            raise ValidationError({"price_list": "Choose an active price list."})
        prices = dict(price_list.items.values_list("test_id", "unit_price"))
        missing = [t for t in tests if t.definition_id not in prices]
        if missing:
            raise ValidationError(
                {
                    "price_list": "This price list has no price for: "
                    + "; ".join(f"{t.sample.lab_id}: {test_label(t.definition)}" for t in missing)
                    + ". Choose another active list or record those prices first."
                }
            )
        pricing_source = {
            "price_list": price_list,
            "price_list_name": price_list.name,
            "price_list_revision": price_list.revision,
        }
    draft = InvoiceDraft(
        number=f"ID-{uuid.uuid4().hex[:16]}",
        order=order,
        created_by=actor,
        **pricing_source,
        order_reference=order.coc_reference,
        project_name=project.name,
        po_number=order.po_number or project.po_number,
        **billing_snapshot(client),
    )
    lines = []
    for position, requested in enumerate(tests, 1):
        sample, definition = requested.sample, requested.definition
        label = test_label(definition)
        price = prices[definition.pk]
        quote_line = quote_lines.get(definition.pk)
        lines.append(
            InvoiceDraftLine(
                draft=draft,
                position=position,
                kind=InvoiceDraftLine.Kind.TEST,
                source_requested_test_id=requested.pk,
                source_quote_line=quote_line,
                sample=sample,
                definition=definition,
                lab_id=sample.lab_id,
                client_sample_id=sample.client_sample_id,
                sample_disposition=sample.disposition,
                test_description=label,
                method_code=definition.method.code,
                method_version=definition.method.version,
                matrix=definition.matrix,
                unit=definition.unit,
                description=quote_line.description if quote_line else label,
                quantity=1,
                unit_price=price,
                amount=_money(price, 1, position),
            )
        )
    for position, source in enumerate(adjustments, len(lines) + 1):
        lines.append(
            InvoiceDraftLine(
                draft=draft,
                position=position,
                kind=source.kind,
                source_quote_line=source,
                description=source.description,
                quantity=source.quantity,
                unit_price=source.unit_price,
                amount=_money(source.unit_price, source.quantity, position, kind=source.kind),
            )
        )
    draft.total = _total(lines)
    draft.full_clean()
    draft.save()
    draft.number = f"ID-{draft.pk:06d}"
    draft.save(update_fields=["number"])
    InvoiceDraftLine.objects.bulk_create(lines)
    record_event(actor=actor, action="invoice_draft.created", obj=draft, after=_state(draft))
    return draft


def _check_revision(draft, expected_revision):
    if draft.revision != expected_revision:
        raise ValidationError(
            "This invoice draft changed. Reload it and review the current values."
        )


def _editable(draft, expected_revision):
    _check_revision(draft, expected_revision)
    if draft.status == InvoiceDraft.Status.CANCELLED:
        raise ValidationError("A cancelled draft cannot be changed.")
    if draft.status == InvoiceDraft.Status.ISSUED:
        raise ValidationError("An issued invoice cannot be changed.")
    if draft.status == InvoiceDraft.Status.VOIDED:
        raise ValidationError("A voided invoice cannot be changed.")


def _reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError({"reason": "Give a reason for this change."})
    return reason.strip()


def _lock_order_and_draft(draft_id):
    # Creation and changes to the active invoice state share the order lock.
    order_id = InvoiceDraft.objects.values_list("order_id", flat=True).get(pk=draft_id)
    order = Order.objects.select_for_update().get(pk=order_id)
    draft = InvoiceDraft.objects.select_for_update().get(pk=draft_id, order=order)
    draft.order = order
    return draft


@transaction.atomic
def add_invoice_draft_line(
    *, actor, draft_id, expected_revision, kind, description, quantity, unit_price, reason
):
    """Append a reasoned fee, discount or tax without accepting any source identities."""
    require_role(actor, *PRICING_ROLES)
    draft = InvoiceDraft.objects.select_for_update().get(pk=draft_id)
    _editable(draft, expected_revision)
    reason = _reason(reason)
    existing = list(draft.lines.select_for_update())
    if len(existing) >= MAX_LINES:
        raise ValidationError(f"A draft invoice may have at most {MAX_LINES} lines.")
    if kind not in (
        InvoiceDraftLine.Kind.FEE,
        InvoiceDraftLine.Kind.ADJUSTMENT,
        InvoiceDraftLine.Kind.TAX,
    ):
        raise ValidationError({"kind": "Choose Fee, Discount or Tax."})
    if not isinstance(description, str) or not description.strip():
        raise ValidationError({"description": "Enter a description."})
    position = max((line.position for line in existing), default=0) + 1
    line = InvoiceDraftLine(
        draft=draft,
        position=position,
        kind=kind,
        description=description.strip(),
        quantity=quantity,
        unit_price=unit_price,
        amount=_money(unit_price, quantity, position, kind=kind),
    )
    line.full_clean()
    total = _total([*existing, line])
    before = _state(draft)
    line.save()
    draft.total = total
    draft.revision += 1
    draft.full_clean()
    draft.save()
    record_event(
        actor=actor,
        action="invoice_draft.line_added",
        obj=draft,
        before=before,
        after=_state(draft),
        reason=reason,
    )
    return draft


@transaction.atomic
def save_invoice_draft(
    *, actor, draft_id, expected_revision, billing, lines, reason, due_date=_DUE_DATE_UNSET
):
    require_role(actor, *PRICING_ROLES)
    draft = InvoiceDraft.objects.select_for_update().get(pk=draft_id)
    _editable(draft, expected_revision)
    reason = _reason(reason)
    existing = list(draft.lines.select_for_update())
    if not 1 <= len(lines) <= MAX_LINES or len(lines) != len(existing):
        raise ValidationError(
            "Keep every original source line and manually added line. "
            "Use Add fee, discount or tax to add a line; lines cannot be removed."
        )
    by_id = {line.pk: line for line in existing}
    submitted_ids = [line.get("id") for line in lines]
    if len(set(submitted_ids)) != len(existing) or set(submitted_ids) != set(by_id):
        raise ValidationError("The draft lines changed. Reload and review them before saving.")
    before = _state(draft)
    if set(billing) != set(BILLING_FIELDS):
        raise ValidationError("The billing snapshot is incomplete.")
    for name in BILLING_FIELDS:
        value = billing[name]
        if not isinstance(value, str):
            raise ValidationError("Billing values must be text.")
        setattr(draft, name, value.strip())
    if due_date is not _DUE_DATE_UNSET:
        if due_date is not None and type(due_date) is not date:
            raise ValidationError({"due_date": "Enter a calendar date or leave it blank."})
        draft.due_date = due_date
    for data in lines:
        line = by_id[data["id"]]
        description = data.get("description", "").strip()
        if not description:
            raise ValidationError(f"Line {line.position}: enter a description.")
        line.description, line.quantity, line.unit_price = (
            description,
            data.get("quantity"),
            data.get("unit_price"),
        )
        line.amount = _money(line.unit_price, line.quantity, line.position, kind=line.kind)
        line.full_clean()
    draft.total = _total(existing)
    draft.revision += 1
    draft.full_clean()
    draft.save()
    InvoiceDraftLine.objects.bulk_update(
        existing, ["description", "quantity", "unit_price", "amount"]
    )
    record_event(
        actor=actor,
        action="invoice_draft.saved",
        obj=draft,
        before=before,
        after=_state(draft),
        reason=reason,
    )
    return draft


@transaction.atomic
def cancel_invoice_draft(*, actor, draft_id, expected_revision, reason):
    require_role(actor, *PRICING_ROLES)
    draft = _lock_order_and_draft(draft_id)
    _editable(draft, expected_revision)
    reason = _reason(reason)
    before = _state(draft)
    draft.status = InvoiceDraft.Status.CANCELLED
    draft.cancelled_at, draft.cancelled_by, draft.cancel_reason = timezone.now(), actor, reason
    draft.revision += 1
    draft.save()
    record_event(
        actor=actor,
        action="invoice_draft.cancelled",
        obj=draft,
        before=before,
        after=_state(draft),
        reason=reason,
    )
    return draft


def _issued_snapshot(draft, lines, *, number, issued_at, lab):
    local_issued_at = timezone.localtime(issued_at, timezone.get_default_timezone())
    return {
        "number": number,
        "draft_number": draft.number,
        "issued_at": local_issued_at.isoformat(),
        "issued_date": local_issued_at.date().isoformat(),
        "lab": {
            "name": lab.name,
            "address": lab.address_lines,
            "phone": lab.phone,
            "email": lab.email,
            "website": lab.website,
        },
        "billing": {
            **{name: getattr(draft, name) for name in BILLING_FIELDS},
            "payment_terms_label": draft.get_payment_terms_display(),
        },
        "order_reference": draft.order_reference,
        "project_name": draft.project_name,
        "due_date": draft.due_date.isoformat() if draft.due_date else None,
        "currency": "USD",
        "total": str(draft.total),
        "lines": [
            {
                **snapshot(line),
                "kind_label": line.get_kind_display(),
                # Tax descriptions identify the assessment's source or basis for staff.
                "display_description": "Tax"
                if line.kind == InvoiceDraftLine.Kind.TAX
                else line.description,
            }
            for line in lines
        ],
        "synthetic_notice": settings.REPORT_SYNTHETIC_DATA_NOTICE,
    }


def _document_state(document):
    return {
        "number": document.number,
        "issued_at": document.issued_at.isoformat(),
        "issued_by": document.issued_by_id,
        "sha256": document.sha256,
    }


def _lock_invoice_actor(actor):
    user_model = get_user_model()
    actor = user_model.objects.select_for_update(no_key=True).filter(pk=actor.pk).first()
    if actor is None:
        raise PermissionDenied("Your role does not permit this action.")
    require_role(actor, *PRICING_ROLES)
    # An account lock alone does not protect group membership from deletion. The built-in
    # role group names are fixed by account administration; hold a qualifying membership.
    membership = (
        user_model.groups.through.objects.select_for_update(of=("self",))
        .filter(user_id=actor.pk, group__name__in=PRICING_ROLES)
        .order_by("pk")
        .first()
    )
    if membership is None:
        raise PermissionDenied("Your role does not permit this action.")
    return actor


@transaction.atomic
def issue_invoice(*, actor, draft_id, expected_revision):
    """Freeze one draft revision and its rendered PDF in the attributed transition."""
    actor = _lock_invoice_actor(actor)
    draft = _lock_order_and_draft(draft_id)
    _check_revision(draft, expected_revision)
    if draft.status == InvoiceDraft.Status.CANCELLED:
        raise ValidationError("A cancelled draft cannot be issued.")
    if draft.status == InvoiceDraft.Status.ISSUED:
        raise ValidationError("This invoice has already been issued.")
    if draft.status == InvoiceDraft.Status.VOIDED:
        raise ValidationError("A voided invoice cannot be issued again.")
    if draft.order.is_cancelled:
        raise ValidationError("This chain of custody is cancelled; the draft cannot be issued.")
    lines = list(draft.lines.select_for_update()[: MAX_LINES + 1])
    if not 1 <= len(lines) <= MAX_LINES:
        raise ValidationError(f"An invoice needs between 1 and {MAX_LINES} recorded lines.")
    if _total(lines) != draft.total:
        raise ValidationError(
            "The draft total does not match its lines. Review and save the draft."
        )
    lab = Lab.objects.filter(pk=1).first()
    if lab is None:
        raise ValidationError("Set up the laboratory before issuing an invoice.")
    issued_at = timezone.now()
    number = f"INV-{draft.pk:06d}"
    content = _issued_snapshot(draft, lines, number=number, issued_at=issued_at, lab=lab)
    pdf = render_pdf(content)
    before = _state(draft)
    document = IssuedInvoice.objects.create(
        draft=draft,
        number=number,
        issued_at=issued_at,
        issued_by=actor,
        snapshot=content,
        pdf=pdf,
        sha256=hashlib.sha256(pdf).hexdigest(),
    )
    draft.status = InvoiceDraft.Status.ISSUED
    draft.revision += 1
    draft.save(update_fields=["status", "revision"])
    record_event(
        actor=actor,
        action="invoice_draft.issued",
        obj=draft,
        before=before,
        after={**_state(draft), "issued_invoice": _document_state(document)},
    )
    return draft


@transaction.atomic
def void_invoice(*, actor, draft_id, expected_revision, reason):
    """Void an invoice with attribution while preserving the original document."""
    actor = _lock_invoice_actor(actor)
    draft = _lock_order_and_draft(draft_id)
    _check_revision(draft, expected_revision)
    if draft.status == InvoiceDraft.Status.DRAFT:
        raise ValidationError("Issue this draft before voiding the invoice.")
    if draft.status == InvoiceDraft.Status.CANCELLED:
        raise ValidationError("A cancelled draft has no issued invoice to void.")
    if draft.status == InvoiceDraft.Status.VOIDED:
        raise ValidationError("This invoice has already been voided.")
    reason = _reason(reason)
    document = IssuedInvoice.objects.only("number", "issued_at", "issued_by", "sha256").get(
        draft=draft
    )
    document_state = {"issued_invoice": _document_state(document)}
    before = {**_state(draft), **document_state}
    draft.status = InvoiceDraft.Status.VOIDED
    draft.cancelled_at, draft.cancelled_by, draft.cancel_reason = timezone.now(), actor, reason
    draft.revision += 1
    draft.save(
        update_fields=["status", "revision", "cancelled_at", "cancelled_by", "cancel_reason"]
    )
    record_event(
        actor=actor,
        action="invoice_draft.voided",
        obj=draft,
        before=before,
        after={**_state(draft), **document_state},
        reason=reason,
    )
    return draft
