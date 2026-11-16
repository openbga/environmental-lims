"""A PM snapshots received tests, corrects billing with a reason and retains a cancelled draft."""

import re
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from conftest import Lab
from helpers import field, grid_search, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def billing_source(demo: Lab):
    from accounts.models import User
    from commercial import pricing
    from commercial.models import PriceList
    from receipt.models import Order, RequestedTest

    actor = User.objects.get(username="demo_project_manager")
    order = Order.objects.filter(samples__requested_tests__isnull=False).distinct().first()
    definitions = {
        t.definition_id: t.definition for t in RequestedTest.objects.filter(sample__order=order)
    }
    pricing.save_price_list(
        actor=actor,
        name="Synthetic invoice prices",
        prices={pk: Decimal("0.10") for pk in definitions},
    )
    PriceList.objects.create(name="Synthetic missing prices")
    return demo, order


def test_draft_invoice_create_adjust_cancel_and_client_denial(page: Page, billing_source):
    demo, order = billing_source
    sign_in(page, "demo_project_manager", demo.password)
    page.goto(f"/orders/{order.pk}/")
    page.get_by_role("link", name="New draft invoice", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(page.locator("#vr-form-title")).to_have_text("New draft invoice")
    pick(field(dialog, "Price list"), label="Synthetic missing prices")
    dialog.get_by_role("button", name="Create draft invoice", exact=True).click()
    expect(dialog.get_by_text("This price list has no price for:", exact=False)).to_be_visible()
    pick(field(dialog, "Price list"), label="Synthetic invoice prices")
    dialog.get_by_role("button", name="Create draft invoice", exact=True).click()
    expect(page.get_by_text("Internal draft invoice created.", exact=True)).to_be_visible()
    draft_path = urlparse(page.url).path
    expect(page.get_by_text("Internal draft only", exact=True)).to_be_visible()
    page.get_by_role("link", name="Edit", exact=True).click()
    expect(page.locator("#vr-form-title")).to_have_text(re.compile(r"^Edit ID-"))
    field(dialog, "Client name").fill("Synthetic invoice account")
    field(dialog, "Description, line 1").fill("Synthetic agreed testing")
    field(dialog, "Quantity, line 1").fill("3")
    field(dialog, "Unit price, line 1").fill("0.10")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(field(dialog, "Reason for change")).to_have_attribute("aria-invalid", "true")
    expect(field(dialog, "Client name")).to_have_value("Synthetic invoice account")
    field(dialog, "Reason for change").fill("Synthetic agreed quantity and billing correction")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(
        page.get_by_text("Draft saved; prior values retained in history.", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("cell", name="Synthetic agreed testing", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="$0.30", exact=True)).to_be_visible()
    page.get_by_role("link", name="Cancel draft invoice", exact=True).click()
    field(dialog, "Reason for cancellation").fill("Synthetic replacement after scope review")
    dialog.get_by_role("button", name="Cancel draft invoice", exact=True).click()
    expect(
        page.get_by_text("Draft cancelled; its snapshot and history are retained.", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("link", name="Edit", exact=True)).to_have_count(0)
    expect(page.get_by_role("cell", name="Synthetic agreed testing", exact=True)).to_be_visible()
    page.get_by_role("link", name="Draft invoices", exact=True).first.click()
    grid_search(page, "Filter draft invoices").fill("Synthetic invoice account")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(
        page.locator("table.vr-table tbody tr").filter(has_text="Synthetic invoice account")
    ).to_have_count(1)
    sign_out(page)
    sign_in(page, "demo_client", demo.password)
    assert page.goto(draft_path).status == 403
    assert page.goto(f"{draft_path}edit/").status == 403
    assert page.goto(f"{draft_path}cancel/").status == 403
