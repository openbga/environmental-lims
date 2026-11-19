"""Follow one issued concentration through the daily load worksheet and its source PDF."""

import re
from urllib.parse import urlsplit

import pytest
from conftest import NARROW
from helpers import released_water_report, save_download
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_client_calculates_daily_load_from_an_issued_result(page: Page, sign_in_as, tmp_path):
    page.set_viewport_size(NARROW)
    sign_in_as("client")
    report, version, sample, _ = released_water_report(
        "E2E-DAILY-LOAD", title="Synthetic discharge monitoring"
    )
    page.get_by_role("link", name="Reports", exact=True).first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    page.get_by_role(
        "link", name=f"Daily load for Lead on {sample.client_sample_id}", exact=True
    ).press("Enter")
    expect(page.get_by_role("heading", level=1, name="Daily load")).to_be_visible()

    flow = page.get_by_role("textbox", name="Sampling-day flow", exact=True)
    flow.fill("1.25")
    page.get_by_role("button", name="Calculate load", exact=True).press("Enter")
    # The frozen 5.2 ug/L is 0.0052 mg/L before multiplying by flow and 8.34.
    expect(page.get_by_text(re.compile(r"^0\.054210* lb/day$"))).to_be_visible()
    assert not urlsplit(page.url).query
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    pdf = page.get_by_role("link", name="Download issued PDF", exact=True)
    assert save_download(page, pdf.click, tmp_path / version.filename) == bytes(version.file.pdf)
    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer.get_by_role("heading", name="Daily load", exact=True)).to_be_visible()
    expect(drawer).to_contain_text("Sampling-day flow")
    assert page.console_errors == []
