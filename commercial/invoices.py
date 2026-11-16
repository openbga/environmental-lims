"""Internal draft invoices from an explicit snapshot of physically received requested tests."""

import json
import uuid
from collections import Counter
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from core.audit import record_event, snapshot
from receipt.models import Client, Order, Project, RequestedTest

from .models import InvoiceDraft, InvoiceDraftLine, PriceList, Quote, QuoteLine, QuoteVersion
from .permissions import PRICING_ROLES, require_role
from .pricing import test_label

MAX_LINES = 200
MAX_QUANTITY = 2_147_483_647
MAX_PRICE = Decimal("9999999999.99")
MAX_AMOUNT = Decimal("999999999999.99")
CENTS = Decimal("0.01")
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
    lines = list(version.lines.select_for_update())
    by_test = {}
    for line in lines:
        if line.kind != QuoteLine.Kind.TEST or not line.test_id:
            raise ValidationError(
                {
                    "quote_version": "The entire quote must contain test lines only; "
                    "fees and discounts cannot price this draft."
                }
            )
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
    return version, by_test


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


def _money(price, quantity, position):
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        raise ValidationError(f"Line {position}: quantity must be a positive whole number.")
    if not isinstance(price, Decimal) or not price.is_finite() or not 0 <= price <= MAX_PRICE:
        raise ValidationError(f"Line {position}: enter a nonnegative, bounded price in USD.")
    if price != price.quantize(CENTS):
        raise ValidationError(f"Line {position}: prices must be whole cents in USD.")
    amount = price * quantity
    if amount > MAX_AMOUNT:
        raise ValidationError(f"Line {position}: this amount is too large to record.")
    return amount


def _total(lines):
    total = sum((line.amount for line in lines), Decimal("0.00"))
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
    if InvoiceDraft.objects.filter(order=order, status=InvoiceDraft.Status.DRAFT).exists():
        raise ValidationError(
            "This order already has an active draft invoice. Open that draft instead."
        )
    if not tests:
        raise ValidationError(
            "Receive samples with requested tests before creating a draft invoice."
        )
    if len(tests) > MAX_LINES:
        raise ValidationError(f"A draft invoice may have at most {MAX_LINES} received tests.")
    quote_lines = {}
    if quote_version_id is not None:
        version, quote_lines = _quote_source(order, quote_version_id, tests)
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
    draft.total = _total(lines)
    draft.full_clean()
    draft.save()
    draft.number = f"ID-{draft.pk:06d}"
    draft.save(update_fields=["number"])
    InvoiceDraftLine.objects.bulk_create(lines)
    record_event(actor=actor, action="invoice_draft.created", obj=draft, after=_state(draft))
    return draft


def _editable(draft, expected_revision):
    if draft.revision != expected_revision:
        raise ValidationError(
            "This draft changed. Reload it and review the current values before saving."
        )
    if draft.status != InvoiceDraft.Status.DRAFT:
        raise ValidationError("A cancelled draft cannot be changed.")


def _reason(reason):
    if not (reason or "").strip():
        raise ValidationError({"reason": "Give a reason for this change."})
    return reason.strip()


@transaction.atomic
def save_invoice_draft(*, actor, draft_id, expected_revision, billing, lines, reason):
    require_role(actor, *PRICING_ROLES)
    draft = InvoiceDraft.objects.select_for_update().get(pk=draft_id)
    _editable(draft, expected_revision)
    reason = _reason(reason)
    existing = list(draft.lines.select_for_update())
    if not 1 <= len(lines) <= MAX_LINES or len(lines) != len(existing):
        raise ValidationError("Keep every original source line; lines cannot be added or removed.")
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
        line.amount = _money(line.unit_price, line.quantity, line.position)
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
    draft = InvoiceDraft.objects.select_for_update().get(pk=draft_id)
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
