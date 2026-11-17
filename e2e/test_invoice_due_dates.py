"""A PM records and clears a date through the shared dialog and filters the internal list."""

from datetime import date

import pytest
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import Page, expect
from test_invoice_manual import manual_draft as manual_draft

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("width,theme", [(1280, "light"), (390, "dark")])
def test_due_date_dialog_keyboard_history_and_filter(page: Page, manual_draft, width, theme):
    from core.models import AuditEvent

    demo, draft = manual_draft
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(page, "demo_project_manager", demo.password)
    page.evaluate("theme => localStorage.setItem('vr-theme', theme)", theme)
    page.goto(reverse("commercial:invoice-detail", args=[draft.pk]))
    page.get_by_role("link", name="Edit", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    due = field(dialog, "Due date")
    expect(due).to_have_value("")
    expect(
        dialog.get_by_text("Enter the date while preparing the draft.", exact=False)
    ).to_be_visible()
    due.fill("2026-02-30")
    field(dialog, "Reason for change").fill("Synthetic agreed draft date")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(due).to_have_attribute("aria-invalid", "true")
    expect(due).to_be_focused()
    expect(due).to_have_value("2026-02-30")
    expect(field(dialog, "Reason for change")).to_have_value("Synthetic agreed draft date")
    draft.refresh_from_db()
    assert draft.due_date is None and draft.revision == 1

    due.fill("2020-02-28")
    due.press("Alt+ArrowDown")
    calendar = dialog.get_by_role("dialog", name="Choose a date", exact=True)
    expect(calendar).to_be_visible()
    expect(calendar.get_by_role("button", name="February 28, 2020", exact=True)).to_be_focused()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    expect(calendar).to_be_hidden()
    expect(due).to_be_focused()
    expect(due).to_have_value("2020-02-29")
    due.press("Alt+ArrowDown")
    page.keyboard.press("Escape")
    expect(calendar).to_be_hidden()
    expect(dialog).to_be_visible()
    expect(due).to_be_focused()
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator('.vr-facts time[datetime="2020-02-29"]')).to_be_visible()
    expect(page.get_by_text("Synthetic agreed draft date", exact=True)).to_be_visible()
    draft.refresh_from_db()
    assert draft.due_date == date(2020, 2, 29)
    event = AuditEvent.objects.get(action="invoice_draft.saved", object_id=str(draft.pk))
    assert event.before["due_date"] is None and event.after["due_date"] == "2020-02-29"

    page.goto(reverse("commercial:invoice-list"))
    field(page, "Due from").fill("2020-02-29")
    field(page, "Due through").fill("2020-02-29")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page.get_by_role("link", name=draft.number, exact=True)).to_be_visible()
    expect(page.locator("table tbody").get_by_text("2020-02-29", exact=True)).to_be_visible()
    field(page, "Due from").fill("2020-03-01")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(field(page, "Due through")).to_have_attribute("aria-invalid", "true")
    expect(page.get_by_role("link", name=draft.number, exact=True)).to_have_count(0)

    page.goto(reverse("commercial:invoice-detail", args=[draft.pk]))
    page.get_by_role("link", name="Edit", exact=True).click()
    expect(due).to_have_value("2020-02-29")
    due.press("Alt+ArrowDown")
    calendar.get_by_role("button", name="Clear", exact=True).click()
    expect(due).to_have_value("")
    field(dialog, "Reason for change").fill("Synthetic date removed pending review")
    dialog.get_by_role("button", name="Save draft", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_text("Synthetic date removed pending review", exact=True)).to_be_visible()
    draft.refresh_from_db()
    assert draft.due_date is None and draft.revision == 3
    assert not page.console_errors
