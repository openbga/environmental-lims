"""Password and two-factor settings show their state as a page; each change opens in the form
dialog over it and keeps allauth's checks (core/form_surfaces.py)."""

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    format_hotp_value,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.contrib.auth.models import Group
from helpers import field, sign_in, sign_out
from playwright.sync_api import Page, expect

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "Synthetic-account-dialog-2026!"
NEW_PASSWORD = "Synthetic-account-changed-2026!"


def current_code(secret: str) -> str:
    counters = list(yield_hotp_counters_from_time())
    return format_hotp_value(hotp_value(secret, counters[len(counters) // 2]))


@pytest.fixture
def person(transactional_db):
    user = User.objects.create_user(
        "e2e_account_pm", password=PASSWORD, first_name="E2E", last_name="Account"
    )
    user.groups.add(Group.objects.get_or_create(name="project_manager")[0])
    return user


def open_from_menu(page: Page, item: str):
    page.get_by_role("button", name="E2E Account").click()
    page.get_by_role("link", name=item, exact=True).click()


def test_password_and_two_factor_change_in_the_dialog(page: Page, person):
    sign_in(page, person.username, PASSWORD)
    dialog = page.locator("#vr-form-dialog")
    title = page.locator("#vr-form-title")

    open_from_menu(page, "Password")
    expect(page.get_by_role("heading", level=1, name="Password")).to_be_visible()
    password_page = page.url
    page.get_by_role("link", name="Change password", exact=True).click()
    expect(dialog).to_be_visible()
    expect(title).to_have_text("Change Password")
    expect(dialog.get_by_role("heading", level=1)).to_have_count(0)
    expect(field(dialog, "Current Password")).to_be_focused()
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(dialog).to_be_hidden()
    assert page.url == password_page

    page.get_by_role("link", name="Change password", exact=True).click()
    field(dialog, "Current Password").fill("not-the-password")
    field(dialog, "New Password").fill(NEW_PASSWORD)
    field(dialog, "New Password (again)").fill(NEW_PASSWORD)
    dialog.get_by_role("button", name="Change Password", exact=True).click()
    expect(field(dialog, "Current Password")).to_have_attribute("aria-invalid", "true")
    field(dialog, "Current Password").fill(PASSWORD)
    field(dialog, "New Password").fill(NEW_PASSWORD)
    field(dialog, "New Password (again)").fill(NEW_PASSWORD)
    dialog.get_by_role("button", name="Change Password", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Password successfully changed.")).to_be_visible()
    person.refresh_from_db()
    assert person.check_password(NEW_PASSWORD)

    open_from_menu(page, "Two-factor authentication")
    expect(page.get_by_role("heading", level=1, name="Two-Factor Authentication")).to_be_visible()
    page.get_by_role("link", name="Activate", exact=True).click()
    # allauth asks to confirm access on its own page first (a sign-in step), then the setup
    # opens by its address, in the dialog over Two-factor authentication.
    expect(page.get_by_role("heading", level=1, name="Confirm Access")).to_be_visible()
    field(page, "Password").fill(NEW_PASSWORD)
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(dialog).to_be_visible()
    expect(title).to_have_text("Activate Authenticator App")
    # allauth's own field markup (label text padded with whitespace): read it by its id.
    secret = dialog.locator("#authenticator_secret").input_value()
    field(dialog, "Authenticator code").fill("000000")
    dialog.get_by_role("button", name="Activate", exact=True).click()
    expect(field(dialog, "Authenticator code")).to_have_attribute("aria-invalid", "true")
    field(dialog, "Authenticator code").fill(current_code(secret))
    dialog.get_by_role("button", name="Activate", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("heading", level=1, name="Recovery Codes")).to_be_visible()
    assert Authenticator.objects.filter(user=person, type=Authenticator.Type.TOTP).exists()

    open_from_menu(page, "Two-factor authentication")
    page.get_by_role("link", name="Generate", exact=True).click()
    expect(title).to_have_text("Recovery Codes")
    expect(dialog.get_by_role("button", name="Generate", exact=True)).to_be_visible()
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(dialog).to_be_hidden()
    page.get_by_role("link", name="Deactivate", exact=True).click()
    expect(title).to_have_text("Deactivate Authenticator App")
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(dialog).to_be_hidden()
    assert Authenticator.objects.filter(user=person, type=Authenticator.Type.TOTP).exists()
    page.get_by_role("link", name="Deactivate", exact=True).click()
    dialog.get_by_role("button", name="Deactivate", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("An authenticator app is not active.")).to_be_visible()
    assert not Authenticator.objects.filter(user=person, type=Authenticator.Type.TOTP).exists()

    sign_out(page)
    sign_in(page, person.username, NEW_PASSWORD)
    expect(page.get_by_role("button", name="E2E Account")).to_be_visible()
    assert page.console_errors == []


def test_change_opened_by_its_address_opens_over_its_state_page(page: Page, person):
    sign_in(page, person.username, PASSWORD)
    page.goto("/accounts/password/change/")
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", level=1, name="Change Password")).to_be_visible()
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Password")).to_be_visible()
    expect(dialog).to_be_hidden()
    assert page.console_errors == []


def test_email_addresses_page_opens_add_email_in_the_dialog(page: Page, person):
    sign_in(page, person.username, PASSWORD)
    dialog = page.locator("#vr-form-dialog")
    page.goto("/accounts/email/")
    expect(page.get_by_role("heading", level=1, name="Email Addresses")).to_be_visible()
    expect(dialog).to_be_hidden()
    page.get_by_role("button", name="Add Email", exact=True).click()
    expect(page.locator("#vr-form-title")).to_have_text("Add Email Address")
    field(dialog, "Email").fill("not-an-address")
    dialog.get_by_role("button", name="Add Email", exact=True).click()
    expect(dialog).to_be_visible()
    expect(field(dialog, "Email")).to_have_attribute("aria-invalid", "true")
    field(dialog, "Email").fill("e2e.account@example.invalid")
    dialog.get_by_role("button", name="Add Email", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("e2e.account@example.invalid", exact=False).first).to_be_visible()
    assert person.emailaddress_set.filter(email="e2e.account@example.invalid").exists()
    assert page.console_errors == []
