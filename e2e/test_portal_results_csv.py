"""Clients can download the current and earlier issued results from a report's portal record."""

import csv
import io

import pytest
from helpers import correct_and_approve, release_version, released_water_report, save_download
from playwright.sync_api import Page, expect

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)


def result_row(data):
    (row,) = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    return row


def test_portal_downloads_exact_results_for_each_issued_version(page: Page, sign_in_as, tmp_path):
    sign_in_as("client")
    report, _, sample, revision = released_water_report(
        "E2E-RESULTS-CSV", title="Synthetic client results export"
    )
    page.get_by_role("link", name="Reports", exact=True).first.click()
    page.get_by_role("link", name=report.number, exact=True).click()

    current_link = page.get_by_role("link", name="Download results (CSV)", exact=True)
    original = save_download(page, current_link.click, tmp_path / "original.csv")
    row = result_row(original)
    assert row["report_number"] == report.number
    assert row["report_version"] == "1"
    assert row["lab_sample_id"] == sample.lab_id
    assert row["client_sample_id"] == sample.client_sample_id
    assert (row["analyte"], row["result"], row["unit"], row["reporting_limit"]) == (
        "Lead",
        "5.2",
        "ug/L",
        "1",
    )

    reason = "Synthetic correction of the transcribed Lead result"
    correct_and_approve(revision, supplied_text="5.600", reason=reason)
    release_version(report, User.objects.get(username="demo_approver"), reason=reason)
    assert page.reload().ok
    expect(page.get_by_text(reason, exact=False).first).to_be_visible()
    expect(page.get_by_role("link", name="Download version 2 (PDF)", exact=True)).to_be_visible()

    with page.expect_download() as download:
        current_link.press("Enter")
    assert download.value.suggested_filename == f"{report.number}-v2-results.csv"
    amended = result_row(download.value.path().read_bytes())
    assert amended["report_version"] == "2"
    assert amended["result"] == "5.600"
    assert amended["lab_sample_id"] == row["lab_sample_id"]

    history_link = page.get_by_role("link", name="Download results CSV for version 1", exact=True)
    assert save_download(page, history_link.click, tmp_path / "historical.csv") == original
    assert page.console_errors == []
