"""Accepted quote selection validates entire-order scope and retains quote provenance."""

import re
from collections import Counter
from decimal import Decimal

import pytest
from conftest import Lab
from django.utils import timezone
from helpers import field, native_select, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def quote_billing_source(demo: Lab):
    from accounts.models import User
    from commercial import quotes
    from receipt.models import Order, RequestedTest

    actor = User.objects.get(username="demo_project_manager")
    order = (
        Order.objects.filter(samples__requested_tests__isnull=False)
        .select_related("project__client")
        .distinct()
        .first()
    )
    tests = list(RequestedTest.objects.filter(sample__order=order).select_related("definition"))
    counts = Counter(t.definition_id for t in tests)
    definitions = {t.definition_id: t.definition for t in tests}
    portal_user = User.objects.filter(client=order.project.client, is_active=True).first()

    def accepted_quote(title, extra_quantity):
        quote = quotes.create_quote(
            actor=actor,
            token=None,
            client=order.project.client,
            project=order.project,
            title=title,
        )
        quote = quotes.save_draft(
            actor=actor,
            quote_id=quote.pk,
            expected_revision=quote.revision,
            title=quote.title,
            project=quote.project,
            price_list=None,
            valid_until=timezone.localdate(),
            turnaround_days=None,
            scope_notes="",
            terms="Synthetic quote terms",
            lines=[
                *[
                    {
                        "kind": "test",
                        "test": definitions[pk],
                        "quantity": count + (extra_quantity if i == 1 else 0),
                        "unit_price": Decimal("0.15"),
                        "description": f"Agreed testing {i}",
                    }
                    for i, (pk, count) in enumerate(counts.items(), 1)
                ],
                {
                    "kind": "fee",
                    "description": "Synthetic handling fee",
                    "quantity": 3,
                    "unit_price": Decimal("0.10"),
                },
                {
                    "kind": "adjustment",
                    "description": "Synthetic agreed discount",
                    "quantity": 2,
                    "unit_price": Decimal("-0.05"),
                },
            ],
        )
        quotes.issue_quote(
            actor=actor, quote_id=quote.pk, version_number=1, expected_revision=quote.revision
        )
        quotes.accept_quote(actor=portal_user, quote_id=quote.pk, version_number=1)
        return quote

    mismatched = accepted_quote("Synthetic different quantity", 1)
    matching = accepted_quote("Synthetic invoice quote", 0)
    return demo, order, mismatched, matching


def test_remote_quote_selection_scope_validation_and_provenance(page: Page, quote_billing_source):
    from commercial.models import InvoiceDraft

    demo, order, mismatched, quote = quote_billing_source
    mismatch_label = f"{mismatched.number} · version 1 · Synthetic different quantity"
    label = f"{quote.number} · version 1 · Synthetic invoice quote"
    sign_in(page, "demo_project_manager", demo.password)
    page.goto(f"/orders/{order.pk}/")
    page.get_by_role("link", name="New draft invoice", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    dialog.get_by_role("button", name="Create draft invoice", exact=True).click()
    expect(dialog.get_by_text("Choose exactly one pricing source:", exact=False)).to_be_visible()
    pick(field(dialog, "Accepted quote version"), label=mismatch_label)
    dialog.get_by_role("button", name="Create draft invoice", exact=True).click()
    expect(dialog.get_by_text("Each quote quantity must equal", exact=False)).to_be_visible()
    expect(native_select(field(dialog, "Accepted quote version"))).to_have_value(
        str(mismatched.versions.get(number=1).pk)
    )
    pick(field(dialog, "Accepted quote version"), label=label)
    dialog.get_by_role("button", name="Create draft invoice", exact=True).click()
    expect(page.get_by_text("Internal draft invoice created.", exact=True)).to_be_visible()
    expect(page.get_by_text("Accepted quote", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=quote.number, exact=True)).to_be_visible()
    expect(page.get_by_text("Agreed testing 1", exact=True)).to_be_visible()
    expect(
        page.get_by_text(f"{quote.number} · version 1 · quote line 1", exact=True).first
    ).to_be_visible()
    fee = page.get_by_role("row").filter(has=page.get_by_text("Synthetic handling fee", exact=True))
    discount = page.get_by_role("row").filter(
        has=page.get_by_text("Synthetic agreed discount", exact=True)
    )
    expect(fee.get_by_role("rowheader", name="Fee", exact=True)).to_be_visible()
    expect(fee.get_by_role("cell", name="No sample", exact=True)).to_be_visible()
    expect(fee.get_by_role("cell", name="$0.30", exact=True)).to_be_visible()
    expect(discount.get_by_role("rowheader", name="Discount", exact=True)).to_be_visible()
    expect(discount.get_by_role("cell", name="-$0.10", exact=True)).to_be_visible()
    draft = InvoiceDraft.objects.get(order=order)
    before_total = draft.total
    provenance = list(
        draft.lines.values("id", "kind", "source_quote_line_id", "source_requested_test_id")
    )
    assert (
        draft.lines.filter(kind="fee").count() == draft.lines.filter(kind="adjustment").count() == 1
    )

    page.get_by_role("link", name="Edit", exact=True).click()
    expect(dialog.get_by_text("No sample or requested test", exact=True).first).to_be_visible()
    discount_row = dialog.get_by_role("row").filter(
        has=page.get_by_role("rowheader", name="Discount", exact=True)
    )
    price = discount_row.get_by_role("textbox", name=re.compile("Unit price"))
    expect(price).to_have_value("-0.05")
    field(dialog, "Reason for change").fill("Synthetic agreed discount correction")
    price.fill("0.05")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(
        discount_row.get_by_text("A discount price must be zero or negative.", exact=True)
    ).to_be_visible()
    expect(price).to_have_value("0.05")
    price.fill("-999.00")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(
        dialog.get_by_text(
            "Discounts exceed the draft's charges; the total cannot be negative.", exact=True
        )
    ).to_be_visible()
    price.fill("-0.10")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(
        page.get_by_text("Draft saved; prior values retained in history.", exact=True)
    ).to_be_visible()
    draft.refresh_from_db()
    assert draft.total == before_total - Decimal("0.10")
    assert (
        list(draft.lines.values("id", "kind", "source_quote_line_id", "source_requested_test_id"))
        == provenance
    )

    page.get_by_role("link", name="Cancel draft invoice", exact=True).click()
    field(dialog, "Reason for cancellation").fill("Synthetic replacement scope")
    dialog.get_by_role("button", name="Cancel draft invoice", exact=True).click()
    expect(
        page.get_by_text("Draft cancelled; its snapshot and history are retained.", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("row").filter(has=page.get_by_text("Synthetic handling fee", exact=True))
    ).to_be_visible()
    draft.refresh_from_db()
    assert draft.status == "cancelled"
    assert (
        list(draft.lines.values("id", "kind", "source_quote_line_id", "source_requested_test_id"))
        == provenance
    )
    assert not page.console_errors
