"""A PM adds exact manual fees/discounts and corrects a retained line with a reason."""

from decimal import Decimal

import pytest
from conftest import Lab
from django.urls import reverse
from helpers import field, native_select, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def manual_draft(demo: Lab):
    from accounts.models import User
    from commercial import invoices, pricing
    from receipt.models import Order, RequestedTest

    actor = User.objects.get(username="demo_project_manager")
    order = (
        Order.objects.filter(samples__requested_tests__isnull=False)
        .select_related("project__client")
        .distinct()
        .first()
    )
    definitions = RequestedTest.objects.filter(sample__order=order).values_list(
        "definition_id", flat=True
    )
    prices = pricing.save_price_list(
        actor=actor,
        name="Synthetic manual adjustment prices",
        prices={pk: Decimal("1.10") for pk in definitions},
    )
    draft = invoices.create_invoice_draft(
        actor=actor,
        order_id=order.pk,
        expected_source_state=invoices.source_state(order),
        price_list_id=prices.pk,
    )
    return demo, draft


@pytest.mark.parametrize("width,theme", [(1280, "light"), (390, "dark")])
def test_add_manual_lines_error_focus_history_and_zero_correction(
    page: Page, manual_draft, width, theme
):
    from core.models import AuditEvent

    demo, draft = manual_draft
    original_total = draft.total
    original_lines = draft.lines.count()
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(page, "demo_project_manager", demo.password)
    page.evaluate("theme => localStorage.setItem('vr-theme', theme)", theme)
    page.goto(reverse("commercial:invoice-detail", args=[draft.pk]))
    page.get_by_role("link", name="Add fee or discount", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(page.locator("#vr-form-title")).to_have_text("Add fee or discount")
    dialog.get_by_role("button", name="Help", exact=True).click()
    help_panel = page.locator("#vr-documentation")
    expect(
        help_panel.get_by_role("heading", name="Add fee or discount", exact=True)
    ).to_be_visible()
    help_panel.get_by_role("button", name="Close documentation", exact=True).click()
    expect(field(dialog, "Quantity")).to_have_value("1")
    expect(dialog.locator("#id_unit_price_unit")).to_have_text("USD")
    assert field(dialog, "Unit price").get_attribute("inputmode") is None
    field(dialog, "Description").fill("Synthetic manual handling")
    field(dialog, "Quantity").fill("3")
    field(dialog, "Unit price").fill("-0.10")
    field(dialog, "Reason for change").fill("Synthetic agreed handling")
    dialog.get_by_role("button", name="Add fee or discount", exact=True).click()
    expect(field(dialog, "Unit price")).to_have_attribute("aria-invalid", "true")
    expect(field(dialog, "Unit price")).to_be_focused()
    expect(field(dialog, "Unit price")).to_have_value("-0.10")
    expect(field(dialog, "Description")).to_have_value("Synthetic manual handling")
    expect(field(dialog, "Reason for change")).to_have_value("Synthetic agreed handling")
    field(dialog, "Unit price").fill("0.10")
    dialog.get_by_role("button", name="Add fee or discount", exact=True).click()
    expect(
        page.get_by_text("Fee or discount added; prior values retained in history.", exact=True)
    ).to_be_visible()
    fee = page.get_by_role("row").filter(
        has=page.get_by_text("Synthetic manual handling", exact=True)
    )
    expect(fee.get_by_text("Manually added", exact=True)).to_be_visible()
    expect(fee.get_by_role("cell", name="$0.30", exact=True)).to_be_visible()

    page.get_by_role("link", name="Add fee or discount", exact=True).click()
    pick(field(dialog, "Kind"), label="Discount")
    field(dialog, "Description").fill("Synthetic manual discount")
    field(dialog, "Quantity").fill("2")
    field(dialog, "Unit price").fill("-999.00")
    field(dialog, "Reason for change").fill("Synthetic agreed discount")
    dialog.get_by_role("button", name="Add fee or discount", exact=True).click()
    expect(dialog.get_by_text("the total cannot be negative.", exact=False)).to_be_visible()
    expect(native_select(field(dialog, "Kind"))).to_have_value("adjustment")
    expect(field(dialog, "Unit price")).to_have_value("-999.00")
    field(dialog, "Unit price").fill("-0.05")
    dialog.get_by_role("button", name="Add fee or discount", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_text("Synthetic agreed discount", exact=True)).to_be_visible()
    draft.refresh_from_db()
    assert draft.total == original_total + Decimal("0.20")
    assert draft.lines.count() == original_lines + 2
    assert AuditEvent.objects.filter(action="invoice_draft.line_added").count() == 2
    expect(page.locator("table tfoot")).to_contain_text(f"${draft.total:,.2f}")

    page.get_by_role("link", name="Edit", exact=True).click()
    expect(dialog.get_by_text("Manually added", exact=True)).to_have_count(2)
    field(dialog, f"Unit price, line {original_lines + 1}").fill("0.00")
    field(dialog, "Reason for change").fill("Synthetic waived handling correction")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_text("Synthetic waived handling correction", exact=True)).to_be_visible()
    draft.refresh_from_db()
    assert draft.total == original_total - Decimal("0.10")
    assert draft.lines.count() == original_lines + 2
    assert draft.lines.get(position=original_lines + 1).amount == Decimal("0.00")
    expect(fee.get_by_text("Manually added", exact=True)).to_be_visible()
    assert not page.console_errors
