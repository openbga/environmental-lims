"""Project managers set an informational hold; staff see it and clients never do."""

import re

import pytest
from helpers import field, pdf_text, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)
NOTE = "Synthetic account review: contact the project manager before proceeding."


def test_account_hold_notice_at_receipt_and_release_stays_internal(page: Page, demo, tmp_path):
    from receipt.models import Client
    from reporting.models import Report

    owner = Client.objects.get(name="Example Utilities")
    report = Report.objects.get()
    sign_in(page, "demo_project_manager", demo.password)
    page.goto(f"/clients/{owner.pk}/")
    page.get_by_role("link", name="Edit client", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    field(dialog, "Account hold").check()
    field(dialog, "Reason for change").fill("Synthetic account review")
    dialog.get_by_role("button", name="Save changes").click()
    expect(field(dialog, "Account hold note")).to_have_attribute("aria-invalid", "true")
    expect(field(dialog, "Account hold")).to_be_checked()
    field(dialog, "Account hold note").fill(NOTE)
    dialog.get_by_role("button", name="Save changes").click()
    notice = page.get_by_role("status").filter(has_text="Informational notice")
    expect(notice).to_contain_text(NOTE)
    owner.refresh_from_db()
    assert owner.account_hold and owner.account_hold_note == NOTE
    sign_out(page)

    sign_in(page, "demo_custodian", demo.password)
    # Generic receipt refreshes from the server as the remote project selection changes.
    page.goto("/samples/new/")
    expect(page.locator("#receipt-account-hold")).to_be_empty()
    pick(field(page, "Project"), label="Example Utilities / Test Creek Outfall 001")
    expect(page.locator("#receipt-account-hold")).to_contain_text(NOTE)
    page.get_by_role("button", name="Receive sample").click()
    expect(page.locator("#receipt-account-hold")).to_contain_text(NOTE)
    order = report.project.orders.first()
    page.goto(f"/orders/{order.pk}/")
    expect(page.get_by_role("status").filter(has_text="Informational notice")).to_contain_text(NOTE)
    page.get_by_role("link", name="Receive next sample", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="Informational notice")).to_contain_text(NOTE)
    expect(page.get_by_role("button", name="Receive sample", exact=True)).to_be_enabled()
    page.locator("#vr-form-dialog").get_by_role("button", name="Close form", exact=True).click()
    sign_out(page)

    sign_in(page, "demo_approver", demo.password)
    page.goto(f"/reports/{report.pk}/preview/")
    expect(page.get_by_role("status").filter(has_text="Informational notice")).to_contain_text(NOTE)
    expect(page.get_by_role("button", name="Release version 1")).to_be_enabled()
    page.get_by_label(re.compile("I authorize release of exactly the content")).check()
    page.get_by_role("button", name="Release version 1").click()
    expect(page.get_by_text("released. Its PDF is stored", exact=False)).to_be_visible()
    report.refresh_from_db()
    assert report.latest_version == 1
    sign_out(page)

    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/reports/{report.pk}/")
    expect(page.get_by_text(NOTE)).to_have_count(0)
    with page.expect_download() as download:
        page.get_by_role("link", name=re.compile("^Download version 1")).click()
    saved = tmp_path / "report.pdf"
    download.value.save_as(saved)
    assert NOTE not in pdf_text(saved)
    sign_out(page, "^demo_client")

    sign_in(page, "demo_project_manager", demo.password)
    page.goto(f"/clients/{owner.pk}/")
    page.get_by_role("link", name="Edit client", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    field(dialog, "Account hold").uncheck()
    expect(field(dialog, "Account hold note")).to_have_value(NOTE)
    field(dialog, "Reason for change").fill("Synthetic account review complete")
    dialog.get_by_role("button", name="Save changes").click()
    expect(page.get_by_role("status").filter(has_text="Informational notice")).to_have_count(0)
    owner.refresh_from_db()
    assert not owner.account_hold and owner.account_hold_note == NOTE
    assert page.console_errors == []
