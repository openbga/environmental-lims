"""Clients can trace exact location results to the immutable PDF that issued them."""

from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import NARROW
from django.urls import reverse
from helpers import correct_and_approve, pick, release_version, save_download, submit_fresh_batch
from playwright.sync_api import Page, expect

from accounts.models import User
from analytical.models import ResultRevision
from analytical.services import approve_result
from receipt.corrections import correct_sample
from reporting.models import Report
from reporting.services import create_report

pytestmark = pytest.mark.django_db(transaction=True)
LOCATION = "Synthetic outfall &\nØst branch"


def set_location(sample, custodian):
    correct_sample(
        actor=custodian,
        sample_id=sample.pk,
        expected_revision=sample.revision,
        section="identity",
        collection_location=LOCATION,
        reason="Synthetic receipt location recorded on two lines",
    )


def test_client_follows_location_history_to_current_issued_results(
    page: Page, sign_in_as, tmp_path
):
    page.set_viewport_size(NARROW)
    sign_in_as("client")
    analyst, reviewer, custodian, manager, approver = (
        User.objects.get(username=f"demo_{role}")
        for role in ("analyst", "reviewer", "custodian", "project_manager", "approver")
    )
    later = Report.objects.get(title="Test Creek Outfall 001 analytical report")
    later_sample = later.samples.get()
    set_location(later_sample, custodian)
    later_lead = ResultRevision.objects.get(
        result__requested_test__sample=later_sample, analyte_name="Lead"
    )
    correct_and_approve(
        later_lead, supplied_text="ND", reason="Synthetic non-detect for comparison"
    )
    release_version(later, approver)

    _, revision, sample = submit_fresh_batch(analyst, coc_reference="E2E-LOCATION-HISTORY")
    set_location(sample, custodian)
    approve_result(actor=reviewer, revision_id=revision.pk, note="Synthetic browser review")
    report = create_report(
        actor=manager, project=later.project, title="Synthetic location history", samples=[sample]
    )
    release_version(report, approver)
    reason = "Synthetic correction of the transcribed Lead result"
    corrected = correct_and_approve(revision, supplied_text="5.600", reason=reason)
    current = release_version(report, approver, reason=reason)
    correct_and_approve(
        corrected, supplied_text="6.250", reason="Synthetic correction awaiting issue"
    )

    page.get_by_role("link", name="Reports", exact=True).first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    page.get_by_role(
        "link", name=f"Location history for {sample.client_sample_id}", exact=True
    ).press("Enter")
    expect(
        page.get_by_role("heading", level=1, name="Location history", exact=True)
    ).to_be_visible()
    source = reverse("portal:location-history", args=[report.pk, current.number, 1])
    assert urlsplit(page.url).path == source
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    expect(page.get_by_text(f"Location {LOCATION}", exact=True)).to_be_visible()

    table = page.get_by_role("table", name="Issued results")
    pdf_name = f"{report.number} v2 (PDF)"
    pdf = page.get_by_role("link", name=pdf_name, exact=True)
    earlier = table.get_by_role("row").filter(has=pdf)
    expect(earlier).to_have_count(1)
    snapshot_sample = current.file.snapshot["content"]["samples"][0]
    expect(earlier.locator("time")).to_have_attribute("datetime", snapshot_sample["collected_at"])
    sample_cell = earlier.get_by_role("cell").nth(1)
    expect(sample_cell).to_contain_text(sample.client_sample_id)
    expect(sample_cell).to_contain_text(sample.lab_id)
    expect(sample_cell).to_contain_text("Water")
    for column, text in enumerate(
        ("Lead", "5.600 ug/L", "—", "1 ug/L", "DEMO-METALS-AQ vdemo-1"), 2
    ):
        expect(earlier.get_by_role("cell").nth(column)).to_have_text(text)
    expect(table.get_by_text("6.250", exact=True)).to_have_count(0)
    expect(table.get_by_role("link", name=f"{report.number} v1 (PDF)", exact=True)).to_have_count(0)
    assert save_download(page, pdf.click, tmp_path / current.filename) == bytes(current.file.pdf)

    analyte = page.get_by_role("textbox", name="Analyte", exact=True)
    apply = page.get_by_role("button", name="Apply", exact=True)
    analyte.fill("lea")
    apply.click()
    rows = table.locator("tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text(f"{later.number} v1")
    collected = table.get_by_role("columnheader", name="Collected", exact=True)
    enhanced_table = table.and_(page.locator(".dataTable"))
    sort_link = collected.get_by_role("link", name="Collected", exact=True)
    expect(enhanced_table).to_be_visible()
    expect(collected).to_have_attribute("aria-sort", "descending")
    expect(sort_link.locator("svg")).to_have_css("opacity", "1")
    sort_link.click()
    expect(enhanced_table).to_be_visible()
    expect(collected).to_have_attribute("aria-sort", "ascending")
    expect(sort_link.locator("svg")).to_have_css("opacity", "1")
    expect(rows.first).to_contain_text(f"{report.number} v2")

    pick(page.get_by_role("combobox", name="Rows per page"), label="50")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text(f"{report.number} v2")
    parsed = urlsplit(page.url)
    assert parsed.path == source
    assert parse_qs(parsed.query) == {
        "analyte": ["lea"],
        "sort": ["collected_at"],
        "per_page": ["50"],
    }

    page.get_by_role("button", name="Help", exact=True).click()
    help_drawer = page.locator("#vr-documentation")
    expect(help_drawer.get_by_role("heading", name="Location history", exact=True)).to_be_visible()
    help_drawer.get_by_role("button", name="Close documentation", exact=True).click()
    expect(help_drawer).to_be_hidden()

    analyte.fill("No synthetic analyte")
    apply.press("Enter")
    expect(page.get_by_text("No results match this analyte", exact=True)).to_be_visible()
    expect(table).to_have_count(0)
    page.get_by_role("link", name="Clear all", exact=True).click()
    expect(rows).to_have_count(4)
    expect(analyte).to_have_value("")

    earlier.get_by_role("link", name="Compare with limit", exact=False).press("Enter")
    expect(page.get_by_role("heading", level=1, name="Compare with limit")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    upper_limit = page.get_by_role("textbox", name="Upper limit", exact=True)
    upper_limit.fill("5.5999999999")
    apply.click()
    compared = page.get_by_role("row").filter(has=pdf)
    expect(compared).to_contain_text("Above limit")
    expect(compared).to_contain_text("5.600")
    non_detect = page.get_by_role("row").filter(
        has=page.get_by_role("link", name=f"{later.number} v1 (PDF)", exact=True)
    )
    expect(non_detect).to_have_count(1)
    expect(non_detect).to_contain_text("Not compared · Non-detect")
    upper_limit.fill("5.600")
    apply.press("Enter")
    expect(compared).to_contain_text("At limit")
    assert save_download(page, pdf.click, tmp_path / "comparison.pdf") == bytes(current.file.pdf)
    page.get_by_role("link", name="Clear all", exact=True).click()
    expect(upper_limit).to_have_value("")
    expect(compared).to_contain_text("Enter a limit")
    page.get_by_role("link", name="Location history", exact=True).first.click()
    expect(page.get_by_role("heading", level=1, name="Location history")).to_be_visible()
    assert page.console_errors == []
