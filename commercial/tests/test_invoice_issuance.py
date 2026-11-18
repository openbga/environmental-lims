"""Invoice issuance freezes exact billing and bytes, with an attributed void transition."""

import hashlib
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from defusedxml import ElementTree
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.utils import timezone

from accounts.models import User
from commercial import invoices
from commercial.invoice_pdf import InvoiceRenderError, render_html
from commercial.models import InvoiceDraft, InvoiceDraftLine, IssuedInvoice
from core.models import AuditEvent, Lab
from receipt.models import Client, Order, Sample
from reporting.tests.test_release_serialization import assert_waiting, worker

from .test_invoice_manual import add
from .test_invoice_tax import tax
from .test_invoices import accepted_adjusted_quote as accepted_adjusted_quote
from .test_invoices import create, from_quote, save
from .test_invoices import received as received

pytestmark = pytest.mark.django_db
PDF = b"%PDF-synthetic test fixture"


@pytest.fixture
def issuer_lab(db):
    return Lab.objects.create(
        name="Synthetic Environmental Laboratory",
        address_line1="5 Synthetic Way",
        city="Example City",
        state="MA",
        postal_code="01001",
        phone="555-0101",
        email="invoices@example.invalid",
        certificate_note="SYN-PRIVATE-CERTIFICATE-NOTE",
    )


@pytest.fixture
def stub_pdf(monkeypatch):
    monkeypatch.setattr(invoices, "render_pdf", lambda content: PDF)


@pytest.fixture
def net_draft(lab, received, price_list):
    client = lab["a"]["client"]
    client.payment_terms = Client.PaymentTerms.NET_15
    client.save(update_fields=["payment_terms"])
    return create(lab, received, price_list)


def issue(lab, draft, **overrides):
    return invoices.issue_invoice(
        **{
            "actor": lab["pm"],
            "draft_id": draft.pk,
            "expected_revision": draft.revision,
            **overrides,
        }
    )


def void(lab, draft, **overrides):
    return invoices.void_invoice(
        **{
            "actor": lab["admin"],
            "draft_id": draft.pk,
            "expected_revision": draft.revision,
            "reason": "  Duplicate external billing entry  ",
            **overrides,
        }
    )


def test_issue_preserves_quoted_amounts_bill_to_and_issuer(
    lab, received, accepted_adjusted_quote, issuer_lab, stub_pdf
):
    draft = from_quote(lab, received, accepted_adjusted_quote)
    draft = save(
        lab,
        draft,
        due_date=date(2026, 10, 1),
        billing={
            **{name: getattr(draft, name) for name in invoices.BILLING_FIELDS},
            "billing_contact_name": "Billing example",
            "billing_address": "99 Synthetic Lane\nBuilding 9",
            "billing_email": "billto@example.invalid",
            "payment_terms": "net_30",
            "po_number": "SYN-PO-1",
        },
    )
    draft = tax(lab, draft)
    before = invoices._state(draft)
    client = lab["a"]["client"]
    client.name = "Changed after preparation"
    client.save(update_fields=["name"])
    Sample.objects.filter(pk=received.pk).update(client_sample_id="Changed after preparation")
    issued = issue(lab, draft)
    document = issued.issued_invoice
    issuer_lab.name = "Changed after issue"
    issuer_lab.save(update_fields=["name"])
    document.refresh_from_db()
    assert issued.status == InvoiceDraft.Status.ISSUED and issued.revision == draft.revision + 1
    assert document.number == f"INV-{draft.pk:06d}" and document.issued_by == lab["pm"]
    assert document.sha256 == hashlib.sha256(PDF).hexdigest()
    assert bytes(document.pdf) == PDF
    content = document.snapshot
    assert content["number"] == document.number and content["draft_number"] == draft.number
    assert (
        content["issued_at"]
        == timezone.localtime(document.issued_at, timezone.get_default_timezone()).isoformat()
    )
    assert content["currency"] == "USD" and content["total"] == "2.78"
    assert content["due_date"] == "2026-10-01"
    assert "due_date_basis" not in content
    assert content["order_reference"] == received.order.coc_reference
    assert content["lab"]["name"] == "Synthetic Environmental Laboratory"
    assert content["lab"]["address"] == ["5 Synthetic Way", "Example City, MA 01001"]
    assert content["billing"] == {
        "client_name": "Example Utilities",
        "account_code": "",
        "billing_contact_name": "Billing example",
        "billing_email": "billto@example.invalid",
        "billing_address": "99 Synthetic Lane\nBuilding 9",
        "payment_terms": "net_30",
        "payment_terms_label": "Net 30 days",
        "po_number": "SYN-PO-1",
    }
    lines = content["lines"]
    assert [line["position"] for line in lines] == list(range(1, 8))
    assert lines[0]["client_sample_id"] == "SYN-FIELD-1" and lines[0]["source_quote_line"]
    assert [line["amount"] for line in lines[2:]] == ["0.30", "-0.10", "0.00", "0.00", "1.23"]
    assert lines[-1]["description"].endswith("SYN-TAX-1")
    assert lines[-1]["display_description"] == "Tax"
    event = AuditEvent.objects.get(action="invoice_draft.issued", object_id=str(draft.pk))
    assert event.actor == lab["pm"] and event.before == before
    assert event.after["status"] == "issued" and event.after["lines"] == before["lines"]
    assert event.after["issued_invoice"]["number"] == document.number
    assert event.after["issued_invoice"]["sha256"] == document.sha256


@pytest.mark.parametrize(
    ("terms", "issue_date", "expected"),
    [
        ("net_15", date(2028, 2, 14), date(2028, 2, 29)),
        ("net_30", date(2028, 2, 14), date(2028, 3, 15)),
        ("net_45", date(2028, 2, 14), date(2028, 3, 30)),
        ("net_60", date(2028, 2, 14), date(2028, 4, 14)),
        ("net_15", date(2026, 12, 31), date(2027, 1, 15)),
        ("due_on_receipt", date(2028, 2, 14), None),
        ("prepaid", date(2028, 2, 14), None),
        ("", date(2028, 2, 14), None),
        ("unknown", date(2028, 2, 14), None),
    ],
)
def test_proposed_due_date_uses_calendar_days_only_for_net_terms(terms, issue_date, expected):
    draft = InvoiceDraft(payment_terms=terms)
    assert invoices.proposed_invoice_due_date(draft, issue_date) == expected
    draft.due_date = date(2028, 2, 20)
    assert invoices.proposed_invoice_due_date(draft, issue_date) is None


def test_issue_freezes_accepted_due_date_using_deployment_timezone(
    lab, net_draft, issuer_lab, stub_pdf, settings
):
    client = lab["a"]["client"]
    client.payment_terms = Client.PaymentTerms.NET_60
    client.save(update_fields=["payment_terms"])
    settings.TIME_ZONE = "America/New_York"
    now = datetime(2028, 3, 1, 0, 30, tzinfo=UTC)
    proposal = date(2028, 3, 15)

    with (
        timezone.override("Asia/Tokyo"),
        patch("commercial.invoices.timezone.now", return_value=now),
    ):
        issued = issue(lab, net_draft, proposed_due_date=proposal)

    issued.refresh_from_db()
    document = issued.issued_invoice
    content = document.snapshot
    assert issued.due_date == proposal and issued.revision == net_draft.revision + 1
    assert document.issued_at == now
    assert content["issued_at"] == "2028-02-29T19:30:00-05:00"
    assert content["issued_date"] == "2028-02-29"
    assert content["billing"]["payment_terms"] == "net_15"
    assert content["due_date"] == "2028-03-15"
    assert content["due_date_basis"] == {"source": "net_terms", "days": 15}
    assert "Due 2028-03-15" in render_html(content)
    event = AuditEvent.objects.get(action="invoice_draft.issued", object_id=str(issued.pk))
    assert event.actor == lab["pm"]
    assert event.before["due_date"] is None
    assert event.after["due_date"] == content["due_date"]
    assert event.after["due_date_basis"] == content["due_date_basis"]


def test_issue_requires_refresh_after_deployment_date_changes(
    lab, net_draft, issuer_lab, stub_pdf, settings
):
    settings.TIME_ZONE = "America/New_York"
    proposal = invoices.proposed_invoice_due_date(net_draft, date(2028, 2, 29))
    now = datetime(2028, 3, 1, 5, tzinfo=UTC)
    with (
        patch("commercial.invoices.timezone.now", return_value=now),
        pytest.raises(ValidationError, match="Refresh and review") as exc,
    ):
        issue(lab, net_draft, proposed_due_date=proposal)
    assert exc.value.code == "stale_due_date_proposal"
    assert not IssuedInvoice.objects.exists()
    assert not AuditEvent.objects.filter(action="invoice_draft.issued").exists()
    state = InvoiceDraft.objects.get(pk=net_draft.pk)
    assert state.status == "draft" and state.due_date is None
    assert state.revision == net_draft.revision


@pytest.mark.parametrize(
    ("terms", "manual_date"), [("net_15", date(2028, 3, 15)), ("prepaid", None)]
)
def test_issue_refuses_proposal_for_existing_due_date_or_ineligible_terms(
    lab, net_draft, issuer_lab, stub_pdf, settings, terms, manual_date
):
    draft = save(
        lab,
        net_draft,
        due_date=manual_date,
        billing={
            **{name: getattr(net_draft, name) for name in invoices.BILLING_FIELDS},
            "payment_terms": terms,
        },
    )
    settings.TIME_ZONE = "America/New_York"
    now = datetime(2028, 2, 29, 12, tzinfo=UTC)
    with (
        patch("commercial.invoices.timezone.now", return_value=now),
        pytest.raises(ValidationError, match="Refresh and review") as exc,
    ):
        issue(lab, draft, proposed_due_date=date(2028, 3, 15))
    assert exc.value.code == "stale_due_date_proposal"
    draft.refresh_from_db()
    assert draft.status == "draft" and draft.due_date == manual_date
    assert not IssuedInvoice.objects.exists()


def test_void_preserves_original_document_and_unlocks_replacement(
    lab, received, price_list, issuer_lab, stub_pdf
):
    draft = issue(lab, create(lab, received, price_list))
    document = draft.issued_invoice
    with pytest.raises(ValidationError, match="already has an issued invoice"):
        create(lab, received, price_list)
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceDraft.objects.create(
            **{
                field.attname: getattr(draft, field.attname)
                for field in InvoiceDraft._meta.fields
                if field.name not in ("id", "number", "created_at", "status")
            },
            number="SYN-DUPLICATE",
        )
    with pytest.raises(ValidationError, match="changed"):
        void(lab, draft, expected_revision=draft.revision - 1)
    with pytest.raises(ValidationError, match="Give a reason"):
        void(lab, draft, reason=" ")
    with patch(
        "commercial.invoices.render_pdf", side_effect=AssertionError("unexpected rendering")
    ):
        voided = void(lab, draft)
    assert voided.status == "voided" and voided.revision == draft.revision + 1
    assert voided.cancelled_at and voided.cancelled_by == lab["admin"]
    assert voided.cancel_reason == "Duplicate external billing entry"
    assert voided.issued_invoice.pk == document.pk
    assert voided.issued_invoice.snapshot == document.snapshot
    assert voided.issued_invoice.sha256 == document.sha256
    assert bytes(voided.issued_invoice.pdf) == bytes(document.pdf)
    event = AuditEvent.objects.get(action="invoice_draft.voided", object_id=str(draft.pk))
    assert event.actor == lab["admin"] and event.reason == voided.cancel_reason
    assert event.before["status"] == "issued" and event.after["status"] == "voided"
    assert event.before["issued_invoice"] == event.after["issued_invoice"]
    client = lab["a"]["client"]
    client.payment_terms = Client.PaymentTerms.NET_30
    client.save(update_fields=["payment_terms"])
    replacement = issue(lab, create(lab, received, price_list), actor=lab["admin"])
    assert replacement.issued_invoice.number != document.number
    assert replacement.issued_invoice.snapshot["due_date"] is None
    assert "due_date_basis" not in replacement.issued_invoice.snapshot


@pytest.mark.parametrize("role", ["a", "b", "analyst", "custodian"])
def test_issue_and_void_refuse_unqualified_users(
    lab, received, price_list, issuer_lab, stub_pdf, role
):
    draft = create(lab, received, price_list)
    actor = lab[role]["user"] if role in ("a", "b") else lab[role]
    with pytest.raises(PermissionDenied):
        issue(lab, draft, actor=actor)
    draft = issue(lab, draft)
    with pytest.raises(PermissionDenied):
        void(lab, draft, actor=actor)
    draft.refresh_from_db()
    assert draft.status == "issued"
    assert not AuditEvent.objects.filter(action="invoice_draft.voided").exists()


def test_issuance_and_void_refuse_stale_or_unavailable_states(
    lab, received, price_list, issuer_lab, stub_pdf
):
    def assert_frozen(closed):
        message = f"{closed.status} invoice cannot be changed"
        with pytest.raises(ValidationError, match=message):
            save(lab, closed)
        with pytest.raises(ValidationError, match=message):
            add(lab, closed)
        with pytest.raises(ValidationError, match=message):
            invoices.cancel_invoice_draft(
                actor=lab["pm"],
                draft_id=closed.pk,
                expected_revision=closed.revision,
                reason="Wrong",
            )

    draft = create(lab, received, price_list)
    with pytest.raises(ValidationError, match="Issue this draft"):
        void(lab, draft)
    changed = save(lab, draft)
    with pytest.raises(ValidationError, match="changed"):
        issue(lab, draft)
    args = dict(actor=lab["pm"], draft_id=draft.pk, reason="Replace")
    cancelled = invoices.cancel_invoice_draft(expected_revision=changed.revision, **args)
    with pytest.raises(ValidationError, match="cancelled draft cannot be issued"):
        issue(lab, cancelled)
    with pytest.raises(ValidationError, match="cancelled draft has no issued invoice"):
        void(lab, cancelled)
    draft = create(lab, received, price_list)
    Order.objects.filter(pk=draft.order_id).update(
        cancelled_at=timezone.now(), cancelled_by=lab["pm"], cancel_reason="Synthetic state"
    )
    with pytest.raises(ValidationError, match="chain of custody is cancelled"):
        issue(lab, draft)
    assert not IssuedInvoice.objects.exists()
    Order.objects.filter(pk=draft.order_id).update(
        cancelled_at=None, cancelled_by=None, cancel_reason=""
    )
    issued = issue(lab, draft)
    with pytest.raises(ValidationError, match="already been issued"):
        issue(lab, issued)
    assert_frozen(issued)
    voided = void(lab, issued)
    with pytest.raises(ValidationError, match="already been voided"):
        void(lab, voided)
    with pytest.raises(ValidationError, match="voided invoice cannot be issued again"):
        issue(lab, voided)
    assert_frozen(voided)


def test_render_and_audit_failures_leave_transition_retryable(
    lab, net_draft, issuer_lab, stub_pdf, monkeypatch
):
    draft = net_draft
    now = timezone.now()
    monkeypatch.setattr(invoices.timezone, "now", lambda: now)
    proposal = invoices.proposed_invoice_due_date(
        draft, timezone.localdate(now, timezone.get_default_timezone())
    )
    before = invoices._state(draft)
    with (
        patch("commercial.invoices.render_pdf", side_effect=InvoiceRenderError("PDF unavailable")),
        pytest.raises(InvoiceRenderError),
    ):
        issue(lab, draft, proposed_due_date=proposal)
    with (
        patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")),
        pytest.raises(RuntimeError),
    ):
        issue(lab, draft, proposed_due_date=proposal)
    draft.refresh_from_db()
    assert invoices._state(draft) == before
    assert not IssuedInvoice.objects.exists()
    assert not AuditEvent.objects.filter(action="invoice_draft.issued").exists()
    issued = issue(lab, draft, proposed_due_date=proposal)
    assert issued.due_date == proposal
    issued_state = invoices._state(issued)
    with (
        patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")),
        pytest.raises(RuntimeError),
    ):
        void(lab, issued)
    issued.refresh_from_db()
    assert invoices._state(issued) == issued_state
    assert bytes(issued.issued_invoice.pdf) == PDF
    assert not AuditEvent.objects.filter(action="invoice_draft.voided").exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_issue_has_one_document_and_one_audit(
    lab, received, price_list, issuer_lab, stub_pdf
):
    draft = create(lab, received, price_list)
    barrier = Barrier(2)

    def attempt(actor):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return issue(lab, draft, actor=actor).issued_invoice.number
        except ValidationError as exc:
            return exc.messages[0]
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [lab["pm"], lab["admin"]]))
    assert IssuedInvoice.objects.filter(draft=draft).count() == 1
    assert AuditEvent.objects.filter(action="invoice_draft.issued").count() == 1
    assert f"INV-{draft.pk:06d}" in results
    assert any("changed" in result for result in results)


def revoke(actor, case):
    if case == "inactive":
        User.objects.filter(pk=actor.pk).update(is_active=False)
    else:
        actor.groups.remove(actor.groups.get(name="project_manager"))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", ["inactive", "membership"])
@pytest.mark.parametrize("transition", [issue, void], ids=["issue", "void"])
def test_transition_waits_and_refuses_revoked_actor(
    lab, received, price_list, issuer_lab, stub_pdf, case, transition
):
    actor, draft = lab["pm"], create(lab, received, price_list)
    if transition is void:
        draft = issue(lab, draft, actor=lab["admin"])
    outcomes, started = [], {"event": Event()}
    thread = None
    try:
        with transaction.atomic():
            revoke(actor, case)
            thread = worker(lambda: transition(lab, draft, actor=actor), outcomes, started)
            assert started["event"].wait(10)
            assert_waiting(started["pid"])
    finally:
        if thread is not None:
            thread.join(15)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], PermissionDenied), outcomes
    assert InvoiceDraft.objects.get(pk=draft.pk).status == draft.status
    assert IssuedInvoice.objects.count() == int(transition is void)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", ["inactive", "membership"])
def test_revocation_waits_for_invoice_render_and_commit(
    lab, received, price_list, issuer_lab, monkeypatch, case
):
    actor, draft = lab["pm"], create(lab, received, price_list)
    rendering, finish = Event(), Event()

    def pause(snapshot):
        rendering.set()
        assert finish.wait(20)
        return PDF

    monkeypatch.setattr(invoices, "render_pdf", pause)
    issued, revoked, started = [], [], {"event": Event()}
    issuer = worker(lambda: issue(lab, draft, actor=actor), issued)
    revoker = None
    try:
        assert rendering.wait(10)
        revoker = worker(lambda: revoke(actor, case), revoked, started)
        assert started["event"].wait(10)
        assert_waiting(started["pid"])
        assert not revoked
    finally:
        finish.set()
        issuer.join(20)
        if revoker is not None:
            revoker.join(20)
    assert not issuer.is_alive() and not revoker.is_alive()
    assert len(issued) == 1 and isinstance(issued[0], InvoiceDraft), issued
    assert issued[0].status == "issued" and revoked == [None]
    with pytest.raises(PermissionDenied):
        void(lab, issued[0], actor=actor)


def test_real_pdf_is_letter_sized_multipage_and_displays_only_billing_facts(
    lab, net_draft, issuer_lab, tmp_path, settings
):
    try:
        import weasyprint  # noqa: F401
    except OSError:
        if os.environ.get("REQUIRE_WEASYPRINT") == "1":
            raise
        pytest.skip("WeasyPrint native libraries are unavailable.")
    settings.REPORT_SYNTHETIC_DATA_NOTICE = True
    settings.TIME_ZONE = "America/New_York"
    draft = net_draft
    draft = save(
        lab,
        draft,
        billing={
            **{name: getattr(draft, name) for name in invoices.BILLING_FIELDS},
            "billing_address": (
                '<img src="https://example.invalid/private.gif">\nBilling address line 2'
            ),
        },
    )
    draft = tax(lab, draft, description="SYN-PRIVATE-TAX-BASIS")
    draft = add(lab, draft, kind="adjustment", quantity=1, unit_price=Decimal("-0.10"))
    charges = [("fee", 1, Decimal("0.01"))] * (invoices.MAX_LINES - 8) + [
        ("fee", invoices.MAX_QUANTITY, Decimal("0.00")),
        ("fee", 100, invoices.MAX_PRICE),
        ("adjustment", 100, -invoices.MAX_PRICE),
        ("fee", 100, Decimal("9999999999.35")),
    ]
    lines = [
        InvoiceDraftLine(
            draft=draft,
            position=position,
            kind=kind,
            description=f"Synthetic charge {position:03d}",
            quantity=quantity,
            unit_price=price,
            amount=quantity * price,
        )
        for position, (kind, quantity, price) in enumerate(charges, 5)
    ]
    InvoiceDraftLine.objects.bulk_create(lines)
    total = draft.total + sum(line.amount for line in lines)
    assert total == Decimal("999999999999.30")
    InvoiceDraft.objects.filter(pk=draft.pk).update(total=total)
    with patch(
        "commercial.invoices.timezone.now", return_value=datetime(2028, 2, 29, 12, tzinfo=UTC)
    ):
        document = issue(lab, draft, proposed_due_date=date(2028, 3, 15)).issued_invoice
    pdf = bytes(document.pdf)
    assert pdf.startswith(b"%PDF-") and document.sha256 == hashlib.sha256(pdf).hexdigest()
    html = render_html(document.snapshot)
    assert "&lt;img" in html and "<img" not in html and "SYN-PRIVATE-TAX-BASIS" not in html
    assert "SYN-PRIVATE-CERTIFICATE-NOTE" not in html
    assert "Due 2028-03-15" in html
    path = tmp_path / "invoice.pdf"
    path.write_bytes(pdf)
    if shutil.which("pdftotext") and shutil.which("pdfinfo"):
        info = subprocess.check_output(["pdfinfo", str(path)], text=True)
        text = subprocess.check_output(["pdftotext", "-layout", str(path), "-"], text=True)
        pages = text.strip().split("\f")
        assert "612 x 792 pts (letter)" in info
        assert len(pages) > 1
        assert all("Synthetic demonstration data" in page for page in pages)
        assert all("Unit price (USD)" in page for page in pages)
        assert "SYN-PRIVATE-TAX-BASIS" not in text and "SYN-PRIVATE-CERTIFICATE-NOTE" not in text
        assert "Tax" in pages[0] and "-0.10" in pages[0]
        assert "Due 2028-03-15" in pages[0]
        assert "Synthetic charge 200" in pages[-1]
        assert "Total (USD)" in pages[-1] and str(total) in pages[-1]
        boxes = subprocess.check_output(["pdftotext", "-bbox", str(path), "-"])
        words = {word.text: word for word in ElementTree.fromstring(boxes).findall(".//{*}word")}
        # Check that the widest values stay inside their cells on the Letter-sized page.
        for value, left, right in [
            (str(invoices.MAX_QUANTITY), 257, 336),
            ("-9999999999.99", 335, 444),
            ("-999999999999.00", 443, 560),
            (str(total), 443, 560),
        ]:
            assert left <= Decimal(words[value].attrib["xMin"])
            assert Decimal(words[value].attrib["xMax"]) <= right


def test_nonsynthetic_invoice_html_omits_notice(
    lab, received, price_list, issuer_lab, stub_pdf, settings
):
    settings.REPORT_SYNTHETIC_DATA_NOTICE = False
    document = issue(lab, create(lab, received, price_list)).issued_invoice
    assert document.snapshot["synthetic_notice"] is False
    assert "Synthetic demonstration data" not in render_html(document.snapshot)
