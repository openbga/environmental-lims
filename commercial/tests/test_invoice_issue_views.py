"""Staff invoice pages preserve the reviewed revision and protect the original download."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from commercial import invoices
from commercial.invoice_pdf import InvoiceRenderError
from commercial.models import InvoiceDraft, IssuedInvoice
from commercial.tests.test_invoices import create, save
from commercial.tests.test_invoices import received as received
from core.models import AuditEvent, Lab
from receipt.models import Order

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft(lab, received, price_list):
    Lab.objects.create(name="Synthetic Environmental Laboratory")
    return create(lab, received, price_list)


@pytest.fixture
def rendered_pdf():
    data = b"%PDF-1.7\nSynthetic invoice view fixture\n%%EOF\n"
    with patch("commercial.invoices.render_pdf", return_value=data):
        yield data


def invoice_url(draft, page):
    return reverse(f"commercial:invoice-{page}", args=[draft.pk])


def issue(client, draft):
    return client.post(invoice_url(draft, "issue"), {"expected_revision": draft.revision})


@pytest.fixture
def net_draft(lab, draft):
    billing = {name: getattr(draft, name) for name in invoices.BILLING_FIELDS}
    return save(lab, draft, billing={**billing, "payment_terms": "net_15"})


@pytest.mark.parametrize("accept", [False, True])
def test_issue_applies_only_the_selected_proposal(
    client, lab, net_draft, rendered_pdf, settings, accept
):
    settings.TIME_ZONE = "America/New_York"
    url = invoice_url(net_draft, "issue")
    issue_time = datetime(2028, 3, 1, 2, tzinfo=UTC)
    proposed = date(2028, 3, 15)
    with timezone.override(UTC), patch("commercial.invoices.timezone.now", return_value=issue_time):
        client.force_login(lab["pm"])
        preview = client.get(url)
        assert preview.context["proposed_due_date"] == proposed
        assert b'id="invoice-proposed-due-date" datetime="2028-03-15"' in preview.content
        checkbox = preview.context["form"]["use_proposed_due_date"]
        assert b'name="use_proposed_due_date"' in preview.content
        assert 'form="issue-invoice-form"' in str(checkbox) and " checked" not in str(checkbox)
        net_draft.refresh_from_db()
        assert net_draft.due_date is None
        submission = {
            "expected_revision": net_draft.revision,
            "proposed_due_date": preview.context["form"]["proposed_due_date"].value(),
        }
        if accept:
            submission["use_proposed_due_date"] = "on"
        with patch("commercial.invoices.render_pdf", side_effect=InvoiceRenderError("Retry PDF.")):
            retry = client.post(url, submission)
        assert b"Retry PDF." in retry.content and not retry.context["review_again"]
        assert (" checked" in str(retry.context["form"]["use_proposed_due_date"])) == accept
        assert not IssuedInvoice.objects.exists()
        assert client.post(url, submission).url == invoice_url(net_draft, "detail")

    net_draft.refresh_from_db()
    assert net_draft.due_date == (proposed if accept else None)
    assert net_draft.issued_invoice.snapshot["due_date"] == (
        proposed.isoformat() if accept else None
    )


@pytest.mark.parametrize("submitted", [None, "2028-03-15", "invalid"])
def test_issue_rejects_unreviewed_proposal_and_requests_reload(
    client, lab, net_draft, rendered_pdf, settings, submitted
):
    settings.TIME_ZONE = "America/New_York"
    url = invoice_url(net_draft, "issue")
    issue_time = datetime(2028, 3, 1, 5, tzinfo=UTC)
    submission = {"expected_revision": net_draft.revision, "use_proposed_due_date": "on"}
    if submitted is not None:
        submission["proposed_due_date"] = submitted
    with patch("commercial.invoices.timezone.now", return_value=issue_time):
        client.force_login(lab["pm"])
        response = client.post(url, submission)
        assert response.status_code == 200 and response.context["review_again"]
        assert b"Reload preview" in response.content and b"2028-03-16" in response.content
        assert b'name="use_proposed_due_date"' not in response.content
        assert not IssuedInvoice.objects.exists()
        net_draft.refresh_from_db()
        assert net_draft.due_date is None and net_draft.status == InvoiceDraft.Status.DRAFT
        preview = client.get(url)
        submission["proposed_due_date"] = preview.context["form"]["proposed_due_date"].value()
        assert client.post(url, submission).status_code == 302


@pytest.mark.parametrize("terms, saved_date", [("net_15", date(2028, 3, 3)), ("prepaid", None)])
def test_issue_does_not_offer_or_accept_a_date_when_ineligible(
    client, lab, draft, rendered_pdf, terms, saved_date
):
    billing = {name: getattr(draft, name) for name in invoices.BILLING_FIELDS}
    draft = save(lab, draft, billing={**billing, "payment_terms": terms}, due_date=saved_date)
    client.force_login(lab["pm"])
    url = invoice_url(draft, "issue")
    preview = client.get(url)
    assert preview.context["proposed_due_date"] is None
    assert b'id="invoice-proposed-due-date"' not in preview.content
    assert b'name="use_proposed_due_date"' not in preview.content
    submitted = {
        "expected_revision": draft.revision,
        "use_proposed_due_date": "on",
        "proposed_due_date": "2028-03-15",
    }
    assert client.post(url, submitted).context["review_again"]
    assert not IssuedInvoice.objects.exists()
    assert issue(client, draft).status_code == 302
    draft.refresh_from_db()
    assert draft.due_date == saved_date


def test_confirm_issue_download_void_and_replace(client, lab, draft, price_list, rendered_pdf):
    draft = save(lab, draft, due_date=date(2026, 12, 31))
    draft = invoices.add_invoice_draft_line(
        actor=lab["pm"],
        draft_id=draft.pk,
        expected_revision=draft.revision,
        kind="tax",
        description="Synthetic internal tax basis",
        quantity=1,
        unit_price=Decimal("0.25"),
        reason="Synthetic assessed charge",
    )
    client.force_login(lab["pm"])
    order = Order.objects.select_related("project__client").get(pk=draft.order_id)
    order_page = reverse("receipt:order-detail", args=[order.pk])
    create_page = reverse("commercial:invoice-create", args=[order.pk])
    preview = client.get(invoice_url(draft, "issue"))
    assert preview.status_code == 200
    assert b"2026-12-31" in preview.content and b"$61.50" in preview.content
    assert b"Synthetic internal tax basis" not in preview.content
    assert b">Tax</td>" in preview.content
    assert IssuedInvoice.objects.count() == 0
    assert client.get(invoice_url(draft, "pdf")).status_code == 404

    response = issue(client, draft)
    assert response.url == invoice_url(draft, "detail")
    draft.refresh_from_db()
    invoice = draft.issued_invoice
    detail = client.get(response.url)
    assert invoice.number.encode() in detail.content and draft.number.encode() in detail.content
    assert b"Synthetic internal tax basis" in detail.content
    assert invoice_url(draft, "pdf").encode() in detail.content
    for action in ("issue", "edit", "add-line", "cancel"):
        assert invoice_url(draft, action).encode() not in detail.content
        assert client.get(invoice_url(draft, action)).url == response.url

    client.force_login(lab["admin"])
    download = client.get(invoice_url(draft, "pdf"))
    assert (
        download.content == rendered_pdf and download.headers["Content-Type"] == "application/pdf"
    )
    assert download.headers["Content-Disposition"] == f'attachment; filename="{invoice.number}.pdf"'
    assert download.headers["Cache-Control"] == "private, no-store"
    assert download.headers["X-Content-Type-Options"] == "nosniff"

    order_response = client.get(order_page)
    assert invoice.number.encode() in order_response.content
    assert f'href="{create_page}"'.encode() not in order_response.content
    create_response = client.get(create_page)
    assert b"This order already has an issued invoice." in create_response.content
    creation = {"expected_source_state": invoices.source_state(order), "price_list": price_list.pk}
    blocked = client.post(create_page, creation)
    assert blocked.context["form"].non_field_errors()
    assert InvoiceDraft.objects.filter(order=order).count() == 1

    list_url = reverse("commercial:invoice-list")
    with CaptureQueriesContext(connection) as queries:
        result = client.get(list_url, {"status": "issued", "q": invoice.number})
    assert list(result.context["page_obj"]) == [draft]
    assert not any('"commercial_issuedinvoice"."pdf"' in query["sql"] for query in queries)
    void_url = invoice_url(draft, "void")
    assert client.get(void_url, HTTP_X_VR_FORM="1").status_code == 200
    missing_reason = client.post(void_url, {"expected_revision": draft.revision, "reason": " "})
    assert missing_reason.context["form"].errors["reason"]
    voided = client.post(
        void_url, {"expected_revision": draft.revision, "reason": "Synthetic duplicate charge"}
    )
    assert voided.url == invoice_url(draft, "detail")
    draft.refresh_from_db()
    assert draft.status == InvoiceDraft.Status.VOIDED
    assert list(
        client.get(list_url, {"status": "voided", "q": draft.number}).context["page_obj"]
    ) == [draft]
    assert not list(client.get(list_url, {"status": "issued"}).context["page_obj"])
    assert b"Download original PDF" in client.get(voided.url).content
    assert client.get(invoice_url(draft, "pdf")).content == download.content
    assert f'href="{create_page}"'.encode() in client.get(order_page).content
    assert client.post(create_page, creation).status_code == 302
    assert InvoiceDraft.objects.filter(order=order, status=InvoiceDraft.Status.DRAFT).count() == 1


def test_stale_review_requires_reload_and_render_failure_can_retry(
    client, lab, draft, rendered_pdf
):
    client.force_login(lab["pm"])
    current = save(lab, draft, due_date=date(2027, 1, 31))
    response = issue(client, draft)
    assert response.status_code == 200
    assert response.context["review_again"]
    assert b"Reload preview" in response.content
    assert b"2027-01-31" in response.content
    assert IssuedInvoice.objects.count() == 0

    with patch("commercial.invoices.render_pdf", side_effect=InvoiceRenderError("Retry PDF.")):
        response = issue(client, current)
    assert response.status_code == 200 and b"Retry PDF." in response.content
    assert not response.context["review_again"]
    draft.refresh_from_db()
    assert draft.status == InvoiceDraft.Status.DRAFT and draft.revision == current.revision
    assert not AuditEvent.objects.filter(action="invoice_draft.issued").exists()
    assert issue(client, current).status_code == 302


def test_issue_needs_laboratory_profile(client, lab, draft):
    Lab.objects.all().delete()
    client.force_login(lab["pm"])
    detail = client.get(invoice_url(draft, "detail"))
    assert b"Set up the laboratory profile" in detail.content
    assert b"disabled>Review and issue" in detail.content
    assert client.get(invoice_url(draft, "issue")).context["blocked"]
    assert issue(client, draft).context["form"].non_field_errors()
    assert not IssuedInvoice.objects.exists()


@pytest.mark.parametrize("actor_role", ["analyst", "custodian", "a", "b"])
def test_issue_void_and_pdf_routes_require_pricing_role(
    client, lab, draft, rendered_pdf, actor_role
):
    actor = lab[actor_role]
    client.force_login(actor["user"] if actor_role in ("a", "b") else actor)
    for page in ("issue", "void", "pdf"):
        url = invoice_url(draft, page)
        assert client.get(url).status_code == 403
        assert client.post(url, {"expected_revision": 1, "reason": "Synthetic"}).status_code == 403
    document = invoices.issue_invoice(actor=lab["pm"], draft_id=draft.pk, expected_revision=1)
    assert client.get(invoice_url(document, "pdf")).status_code == 403
    assert document.status == InvoiceDraft.Status.ISSUED


def test_corrupt_pdf_returns_integrity_error(client, lab, draft, rendered_pdf, caplog):
    client.force_login(lab["pm"])
    issue(client, draft)
    with patch("commercial.invoice_views.hashlib") as hashing:
        hashing.sha256.return_value.hexdigest.return_value = "0" * 64
        response = client.get(invoice_url(draft, "pdf"))
    assert response.status_code == 500 and response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Content-Type"].startswith("text/plain")
    assert b"Synthetic invoice view fixture" not in response.content
    assert "Stored PDF hash mismatch" in caplog.text
