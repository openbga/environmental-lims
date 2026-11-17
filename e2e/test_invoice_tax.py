"""A PM records, reviews, corrects and cancels explicit manual tax assessments by keyboard."""

from decimal import Decimal

import pytest
from django.urls import reverse
from helpers import field, native_select, sign_in
from playwright.sync_api import Page, expect
from test_invoice_manual import manual_draft as manual_draft

pytestmark = pytest.mark.django_db(transaction=True)


def activate(control):
    control.focus()
    control.press("Enter")


@pytest.mark.parametrize("width,theme", [(1280, "light"), (390, "dark")])
def test_tax_keyboard_errors_retained_input_edit_and_cancel(
    page: Page, manual_draft, width, theme, tmp_path
):
    from core.models import AuditEvent

    demo, draft = manual_draft
    total = draft.total
    line_count = draft.lines.count()
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(page, "demo_project_manager", demo.password)
    page.evaluate("theme => localStorage.setItem('vr-theme', theme)", theme)
    page.goto(reverse("commercial:invoice-detail", args=[draft.pk]))
    expect(page.get_by_text("Review tax separately", exact=True)).to_be_visible()
    activate(page.get_by_role("link", name="Add fee, discount or tax", exact=True))
    dialog = page.locator("#vr-form-dialog")
    expect(page.locator("#vr-form-title")).to_have_text("Add fee, discount or tax")
    activate(dialog.get_by_role("button", name="Help", exact=True))
    help_panel = page.locator("#vr-documentation")
    expect(
        help_panel.get_by_role("heading", name="Add fee, discount or tax", exact=True)
    ).to_be_visible()
    expect(
        help_panel.get_by_text("Tax never recalculates when charges change.", exact=False)
    ).to_be_visible()
    activate(help_panel.get_by_role("button", name="Close documentation", exact=True))
    kind = field(dialog, "Kind")
    kind.focus()
    page.keyboard.type("tax")
    expect(native_select(kind)).to_have_value("tax")
    field(dialog, "Description").fill("Synthetic assessment from worksheet SYN-TAX-1")
    field(dialog, "Quantity").fill("2")
    field(dialog, "Unit price").fill("1.23")
    field(dialog, "Reason for change").fill("Synthetic supplied assessment")
    activate(dialog.get_by_role("button", name="Add fee, discount or tax", exact=True))
    expect(field(dialog, "Quantity")).to_have_attribute("aria-invalid", "true")
    expect(field(dialog, "Quantity")).to_be_focused()
    expect(field(dialog, "Quantity")).to_have_value("2")
    expect(field(dialog, "Description")).to_have_value(
        "Synthetic assessment from worksheet SYN-TAX-1"
    )
    expect(native_select(field(dialog, "Kind"))).to_have_value("tax")
    field(dialog, "Quantity").fill("1")
    field(dialog, "Unit price").fill("-0.01")
    activate(dialog.get_by_role("button", name="Add fee, discount or tax", exact=True))
    expect(field(dialog, "Unit price")).to_have_attribute("aria-invalid", "true")
    expect(field(dialog, "Unit price")).to_be_focused()
    expect(field(dialog, "Reason for change")).to_have_value("Synthetic supplied assessment")
    field(dialog, "Unit price").fill("1.23")
    page.screenshot(path=str(tmp_path / f"tax-add-{width}-{theme}.png"), full_page=True)
    activate(dialog.get_by_role("button", name="Add fee, discount or tax", exact=True))
    expect(dialog).not_to_be_visible()
    tax_row = page.get_by_role("row").filter(
        has=page.get_by_text("Synthetic assessment from worksheet SYN-TAX-1", exact=True)
    )
    expect(tax_row.get_by_role("rowheader", name="Tax", exact=True)).to_be_visible()
    expect(tax_row.get_by_text("No sample", exact=True)).to_be_visible()
    expect(tax_row.get_by_text("No requested test", exact=True)).to_be_visible()
    expect(tax_row.get_by_text("Manually added", exact=True)).to_be_visible()
    expect(tax_row.get_by_role("link")).to_have_count(0)
    draft.refresh_from_db()
    assert draft.total == total + Decimal("1.23")
    expect(page.locator("table tfoot")).to_contain_text(f"${draft.total:,.2f}")
    page.screenshot(path=str(tmp_path / f"tax-detail-{width}-{theme}.png"), full_page=True)

    activate(page.get_by_role("link", name="Edit", exact=True))
    expect(dialog.get_by_text("Review tax separately", exact=True)).to_be_visible()
    field(dialog, "Unit price, line 1").fill("2.10")  # original price was 1.10
    field(dialog, "Reason for change").fill("Synthetic charge correction; assessment reviewed")
    activate(dialog.get_by_role("button", name="Save draft", exact=True))
    expect(dialog).not_to_be_visible()
    draft.refresh_from_db()
    assert draft.total == total + Decimal("2.23")
    assert draft.lines.get(kind="tax").amount == Decimal("1.23")
    activate(page.get_by_role("link", name="Edit", exact=True))
    field(dialog, f"Unit price, line {line_count + 1}").fill("0.00")
    field(dialog, "Reason for change").fill("Synthetic explicit zero assessment correction")
    activate(dialog.get_by_role("button", name="Save draft", exact=True))
    expect(dialog).not_to_be_visible()
    draft.refresh_from_db()
    assert draft.total == total + Decimal("1.00")
    assert draft.lines.get(kind="tax").amount == Decimal("0.00")
    assert draft.lines.count() == line_count + 1

    activate(page.get_by_role("link", name="Cancel draft invoice", exact=True))
    field(dialog, "Reason for cancellation").fill("Synthetic worksheet replaced")
    activate(dialog.get_by_role("button", name="Cancel draft invoice", exact=True))
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="Add fee, discount or tax", exact=True)).to_be_disabled()
    expect(tax_row.get_by_role("rowheader", name="Tax", exact=True)).to_be_visible()
    draft.refresh_from_db()
    assert draft.status == "cancelled" and draft.lines.get(kind="tax").amount == Decimal("0.00")
    assert (
        AuditEvent.objects.get(action="invoice_draft.cancelled").after["lines"][-1]["kind"] == "tax"
    )
    assert not page.console_errors
