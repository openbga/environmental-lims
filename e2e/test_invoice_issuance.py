"""Staff review, issue, download and void an invoice in the normal workflow."""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from helpers import field, grid_search, pick, sign_in
from playwright.sync_api import Page, expect
from test_invoice_manual import manual_draft as manual_draft

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("width,theme", [(1280, "light"), (390, "dark")])
def test_review_issue_download_void_and_find_invoice(page: Page, manual_draft, width, theme):
    from commercial import invoices

    demo, draft = manual_draft
    draft = invoices.save_invoice_draft(
        actor=draft.created_by,
        draft_id=draft.pk,
        expected_revision=draft.revision,
        billing={name: getattr(draft, name) for name in invoices.BILLING_FIELDS},
        lines=list(draft.lines.values("id", "description", "quantity", "unit_price")),
        reason="Synthetic due date",
        due_date=date(2027, 1, 31),
    )
    draft = invoices.add_invoice_draft_line(
        actor=draft.created_by,
        draft_id=draft.pk,
        expected_revision=draft.revision,
        kind="tax",
        description="Synthetic internal assessment source",
        quantity=1,
        unit_price=Decimal("0.13"),
        reason="Synthetic assessed amount",
    )
    total = f"${draft.total:,.2f}"
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(page, "demo_project_manager", demo.password)
    page.evaluate("theme => localStorage.setItem('vr-theme', theme)", theme)
    detail_url = reverse("commercial:invoice-detail", args=[draft.pk])
    page.goto(detail_url)
    page.get_by_role("link", name="Review and issue", exact=True).press("Enter")
    expect(page.get_by_role("heading", name="Issue invoice", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Billing details", exact=True)).to_be_visible()
    expect(page.locator('time[datetime="2027-01-31"]')).to_be_visible()
    expect(page.locator("table tfoot")).to_contain_text(total)
    expect(page.get_by_role("cell", name="Tax", exact=True)).to_be_visible()
    expect(page.get_by_text("Synthetic internal assessment source", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Help", exact=True).click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel.get_by_role("heading", name="Issue invoice", exact=True)).to_be_visible()
    help_panel.get_by_role("button", name="Close documentation", exact=True).click()

    page.get_by_role("button", name="Issue invoice", exact=True).press("Enter")
    page.wait_for_url(f"**{detail_url}")
    draft.refresh_from_db()
    invoice = draft.issued_invoice
    expect(page.get_by_role("heading", name=invoice.number, exact=True)).to_be_visible()
    expect(page.get_by_text("Synthetic internal assessment source", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Edit", exact=True)).to_have_count(0)
    with page.expect_download() as first:
        page.get_by_role("link", name="Download invoice PDF", exact=True).click()
    original = first.value
    assert original.suggested_filename == f"{invoice.number}.pdf"
    assert original.path().read_bytes() == bytes(invoice.pdf)

    page.get_by_role("link", name="Void invoice", exact=True).press("Enter")
    dialog = page.locator("#vr-form-dialog")
    expect(page.locator("#vr-form-title")).to_have_text("Void invoice")
    dialog.get_by_role("button", name="Help", exact=True).click()
    expect(help_panel.get_by_role("heading", name="Void invoice", exact=True)).to_be_visible()
    help_panel.get_by_role("button", name="Close documentation", exact=True).click()
    dialog.get_by_role("button", name="Void invoice", exact=True).click()
    reason = field(dialog, "Reason for voiding")
    expect(reason).to_have_attribute("aria-invalid", "true")
    expect(reason).to_be_focused()
    reason.fill("Synthetic replacement requested")
    dialog.get_by_role("button", name="Void invoice", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(
        page.get_by_text("Invoice voided. Its original PDF and history are retained.")
    ).to_be_visible()
    with page.expect_download() as second:
        page.get_by_role("link", name="Download original PDF", exact=True).click()
    assert second.value.suggested_filename == original.suggested_filename
    assert second.value.path().read_bytes() == original.path().read_bytes()

    page.goto(reverse("commercial:invoice-list"))
    grid_search(page, "Filter draft invoices").fill(invoice.number)
    pick(field(page, "Status"), label="Voided")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page.get_by_role("link", name=invoice.number, exact=True)).to_be_visible()
    page.goto(reverse("receipt:order-detail", args=[draft.order_id]))
    expect(page.get_by_role("link", name="New draft invoice", exact=True)).to_be_visible()
    assert not page.console_errors

    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    assert page.goto(detail_url).status == 403
    assert page.request.get(reverse("commercial:invoice-pdf", args=[draft.pk])).status == 403
