"""What's new: the top bar's unread indicator, the list, and the indicator clearing once opened."""

import re

import pytest
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

from core import changelog

pytestmark = pytest.mark.django_db(transaction=True)


def test_open_whats_new_see_entries_and_the_indicator_clears(page: Page):
    from core.models import UserPreference
    from core.preferences import WHATS_NEW_KEY

    entries = changelog.read_entries()
    assert entries, "changelog.d/ should carry entries"
    newest = entries[0]
    # A returning user who saw everything except the newest change.
    user = make_user("e2e_whats_new", "e2e-password-4821!", "analyst")
    UserPreference.objects.create(
        user=user, key=WHATS_NEW_KEY, value=changelog.seen_marker(entries[1:])
    )
    sign_in(page, "e2e_whats_new", "e2e-password-4821!")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()

    link = page.locator(".vr-topbar").get_by_role("link", name=re.compile("^What's new"))
    expect(link).to_have_accessible_name("What's new: 1 new change")
    expect(link.locator(".vr-topbar-count")).to_have_text("1")
    link.hover()
    expect(page.get_by_role("tooltip")).to_have_text("What's new: 1 new change")

    link.click()
    expect(page.get_by_role("heading", level=1, name="What's new")).to_be_visible()
    month = page.get_by_role("region", name=newest.date.strftime("%B %Y"))
    expect(month).to_be_visible()
    first_section = changelog.group_entries(entries)[0].sections[0][1]  # e.g. "High impact"
    expect(month.get_by_role("heading", level=3).first).to_have_text(first_section)
    marked = page.locator(".vr-change[data-new]")
    expect(marked).to_have_count(1)
    expect(marked.get_by_text(newest.title, exact=True)).to_be_visible()
    # Opening the list clears the indicator, here and on the next page.
    expect(link).to_have_accessible_name("What's new")
    expect(link.locator(".vr-topbar-count")).to_have_count(0)
    expect(link).to_have_attribute("aria-current", "page")
    notes = page.locator(".vr-change-notes").first
    if notes.count():
        notes.locator("summary").click()
        expect(notes.locator(".vr-change-notes-body")).to_be_visible()
    page.goto("/")
    expect(page.locator(".vr-topbar").get_by_role("link", name="What's new")).to_be_visible()
    expect(page.locator(".vr-topbar-count")).to_have_count(0)
    assert page.console_errors == []


def test_a_new_account_sees_no_indicator_but_every_entry(page: Page):
    entries = changelog.read_entries()
    make_user("e2e_first_visit", "e2e-password-4821!", "custodian")
    sign_in(page, "e2e_first_visit", "e2e-password-4821!")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    link = page.locator(".vr-topbar").get_by_role("link", name="What's new", exact=True)
    expect(link).to_be_visible()
    expect(page.locator(".vr-topbar-count")).to_have_count(0)
    link.click()
    expect(page.locator(".vr-change")).to_have_count(len(entries))
    expect(page.locator(".vr-change[data-new]")).to_have_count(0)
