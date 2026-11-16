"""Accepted quote selection validates entire-order scope and retains quote provenance."""

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
                {
                    "kind": "test",
                    "test": definitions[pk],
                    "quantity": count + (extra_quantity if i == 1 else 0),
                    "unit_price": Decimal("0.15"),
                    "description": f"Agreed testing {i}",
                }
                for i, (pk, count) in enumerate(counts.items(), 1)
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
    assert not page.console_errors
