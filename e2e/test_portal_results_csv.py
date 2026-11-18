"""Clients can download the current and earlier issued results from a report's portal record."""

import csv
import io
import re

import pytest
from conftest import DESKTOP, NARROW
from helpers import correct_and_approve, pick, release_version, released_water_report, save_download
from playwright.sync_api import Page, expect

from accounts.models import User
from analytical.workbook_import import read_workbook

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
    for link_name, version, expected in (
        ("Download Excel workbook", 2, amended),
        ("Download Excel workbook for version 1", 1, row),
    ):
        with page.expect_download() as workbook:
            page.get_by_role("link", name=link_name, exact=True).click()
        assert workbook.value.suggested_filename == f"{report.number}-v{version}-results.xlsx"
        table = read_workbook(workbook.value.path().read_bytes(), worksheet="Results")
        assert table.rows == [list(expected), list(expected.values())]
        assert not table.numeric_cells
    assert page.console_errors == []


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "narrow"])
def test_portal_uses_csv_template_for_current_and_superseded_versions(
    page: Page, sign_in_as, viewport
):
    page.set_viewport_size(viewport)
    sign_in_as("client")
    report, _, sample, revision = released_water_report(
        "E2E-CUSTOM-CSV", title="Synthetic custom export"
    )
    reason = "Synthetic correction of the transcribed Lead result"
    correct_and_approve(revision, supplied_text="5.600", reason=reason)
    release_version(report, User.objects.get(username="demo_approver"), reason=reason)

    page.get_by_role("link", name="Reports", exact=True).first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    report_path = f"/portal/reports/{report.pk}/"
    history = page.get_by_role("region", name="Version history", exact=True)
    original_link = history.get_by_role("link", name="Use CSV template for version 1", exact=True)
    original_link.press("Enter")
    dialog = page.get_by_role("dialog", name="Use CSV template", exact=True)
    expect(dialog).to_be_visible()
    expect(page).to_have_url(re.compile(f"{report_path}$"))
    expect(dialog.get_by_text("Superseded version", exact=True)).to_be_visible()

    with page.expect_download() as example:
        dialog.get_by_role("link", name="Download example template", exact=True).click()
    assert example.value.suggested_filename == "results-template.csv"
    example_text = example.value.path().read_text(encoding="utf-8-sig")
    headings, field_keys = csv.reader(io.StringIO(example_text, newline=""))
    assert len(headings) == len(field_keys)
    assert "result" in field_keys

    upload = dialog.get_by_label(re.compile(r"^CSV template"))
    format_field = dialog.get_by_role("combobox", name="Download format", exact=True)
    expect(format_field).to_contain_text("CSV")
    submit = dialog.get_by_role("button", name="Download results", exact=True)
    upload.set_input_files(
        {"name": "invalid.csv", "mimeType": "text/csv", "buffer": b"Value\r\nunknown_field\r\n"}
    )
    submit.click()
    expect(upload).to_have_attribute("aria-invalid", "true")
    expect(page).to_have_url(re.compile(f"{report_path}v1/csv-template/$"))

    layout = {
        "name": "client-layout.csv",
        "mimeType": "text/csv",
        "buffer": (
            b'"Lead, measured",Sample,Issued version\r\nresult,client_sample_id,report_version\r\n'
        ),
    }
    for version, value in [(1, "5.2"), (2, "5.600")]:
        if version == 2:
            dialog.get_by_role("link", name="Cancel", exact=True).click()
            expect(dialog).to_be_hidden()
            expect(page).to_have_url(re.compile(f"{report_path}$"))
            page.get_by_role("link", name="Use CSV template", exact=True).click()
            expect(dialog.get_by_text("Current version", exact=True)).to_be_visible()
            expect(page).to_have_url(re.compile(f"{report_path}$"))
        upload.set_input_files(layout)
        with page.expect_download() as download:
            submit.click()
        assert download.value.suggested_filename == f"{report.number}-v{version}-custom-results.csv"
        output = download.value.path().read_text(encoding="utf-8-sig")
        expected = [
            ["Lead, measured", "Sample", "Issued version"],
            [value, sample.client_sample_id, str(version)],
        ]
        assert list(csv.reader(io.StringIO(output, newline=""))) == expected

        pick(format_field, label="Excel (.xlsx)")
        with page.expect_download() as workbook:
            submit.click()
        filename = f"{report.number}-v{version}-custom-results.xlsx"
        assert workbook.value.suggested_filename == filename
        table = read_workbook(workbook.value.path().read_bytes(), worksheet="Results")
        assert table.rows == expected
        assert not table.numeric_cells
    expect(dialog).to_be_visible()
