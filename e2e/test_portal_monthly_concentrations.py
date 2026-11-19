"""Trace monthly concentrations and grab flows through current issues and amendments."""

import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from conftest import NARROW
from django.urls import reverse
from django.utils import timezone
from helpers import correct_and_approve, release_version, save_download, submit_fresh_batch
from playwright.sync_api import Page, expect

from accounts.models import User
from analytical.services import approve_result
from receipt.corrections import correct_sample
from reporting.services import create_report

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def monthly_issues(demo):
    analyst, reviewer, custodian, manager, approver = (
        User.objects.get(username=f"demo_{role}")
        for role in ("analyst", "reviewer", "custodian", "project_manager", "approver")
    )
    local_noon = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
    prior_month = (local_noon.replace(day=1) - timedelta(days=1)).replace(day=1)

    def issue(day, text, *, hour=12):
        reference = f"E2E-MONTHLY-{day}-{hour}"
        with patch("helpers.timezone") as fixture_time:
            fixture_time.now.return_value = prior_month.replace(day=day, hour=hour)
            _, revision, sample = submit_fresh_batch(analyst, coc_reference=reference)
        correct_sample(
            actor=custodian,
            sample_id=sample.pk,
            expected_revision=sample.revision,
            section="identity",
            collection_location="Synthetic monthly monitoring location",
            reason="Synthetic outfall identifier confirmed for the monthly worksheet",
        )
        approve_result(actor=reviewer, revision_id=revision.pk, note="Synthetic browser review")
        if text:
            revision = correct_and_approve(
                revision, supplied_text=text, reason="Synthetic result transcription correction"
            )
        report = create_report(
            actor=manager, project=sample.order.project, title=reference, samples=[sample]
        )
        return report, release_version(report, approver), sample, revision

    return prior_month, issue, approver


def open_monthly_worksheet(page, report, sample):
    page.get_by_role("link", name="Reports", exact=True).first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    page.get_by_role(
        "link", name=f"Monthly concentrations for Lead on {sample.client_sample_id}", exact=True
    ).press("Enter")
    heading = page.get_by_role("heading", level=1, name="Monthly concentrations", exact=True)
    expect(heading).to_be_visible()
    return heading


def test_monthly_concentrations_follow_current_issues(
    page: Page, sign_in_as, monthly_issues, tmp_path
):
    page.set_viewport_size(NARROW)
    sign_in_as("client")
    prior_month, issue, approver = monthly_issues
    report, version, sample, _ = issue(1, None)
    other, other_v1, _, other_revision = issue(2, "7.20")
    heading = open_monthly_worksheet(page, report, sample)
    source = reverse("portal:monthly-concentrations", args=[report.pk, version.number, 1, 1])
    assert urlsplit(page.url).path == source
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    expect(page.get_by_text(prior_month.strftime("%B %Y"), exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^0\.00620* mg/L$"))).to_be_visible()
    expect(page.get_by_text(re.compile(r"^0\.00720* mg/L$"))).to_be_visible()
    table = page.get_by_role("region", name="Issued source results").get_by_role("table")
    expect(table.locator("tbody tr")).to_have_count(2)
    other_pdf = table.get_by_role("link", name=re.compile(rf"^{other.number} v1\b"))
    assert save_download(page, other_pdf.click, tmp_path / other_v1.filename) == bytes(
        other_v1.file.pdf
    )
    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer.get_by_role("heading", name="Monthly concentrations", exact=True)).to_be_visible()
    expect(drawer).to_contain_text("Sampled days")
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()

    reason = "Synthetic check of the original non-detect result"
    correct_and_approve(other_revision, supplied_text="ND", reason=reason)
    other_v2 = release_version(other, approver, reason=reason)
    page.reload()
    expect(heading).to_be_visible()
    expect(page.get_by_text("Calculation blocked", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^0\.00620* mg/L$"))).to_have_count(0)
    expect(page.get_by_text(re.compile(r"^0\.00720* mg/L$"))).to_have_count(0)
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(other_pdf).to_have_count(0)
    current_pdf = page.get_by_role("link", name=f"{other.number} v2", exact=True)
    expect(table.get_by_role("row").filter(has=current_pdf)).to_contain_text(
        re.compile(r"non[- ]detect", re.IGNORECASE)
    )
    assert save_download(page, current_pdf.click, tmp_path / other_v2.filename) == bytes(
        other_v2.file.pdf
    )
    assert page.console_errors == []


def test_monthly_concentrations_weight_distinct_grabs(page: Page, sign_in_as, monthly_issues):
    page.set_viewport_size(NARROW)
    sign_in_as("client")
    prior_month, issue, approver = monthly_issues
    report, version, lower, _ = issue(1, "6.20")
    another_report, _, higher, higher_revision = issue(1, "12.20", hour=13)
    _, _, single, _ = issue(2, "7.20")
    open_monthly_worksheet(page, report, lower)
    source = reverse("portal:monthly-concentrations", args=[report.pk, version.number, 1, 1])
    assert urlsplit(page.url).path == source
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    flows = page.get_by_role("region", name="Grab sample flows")
    lower_flow = flows.get_by_label(re.compile(rf"\b{lower.lab_id}\b.*T09:00"))
    higher_flow = flows.get_by_label(re.compile(rf"\b{higher.lab_id}\b.*T10:00"))
    expect(flows.get_by_label(re.compile(rf"\b{single.lab_id}\b"))).to_have_count(0)
    expect(lower_flow).to_have_value("")
    expect(higher_flow).to_have_value("")
    summary = page.get_by_role("region", name="Monthly summary")
    calculated = summary.get_by_text(re.compile(r"\d+ mg/L"))
    mean = page.get_by_text("0.0087625 mg/L", exact=True)
    maximum = page.get_by_text("0.010325 mg/L", exact=True)
    expect(calculated).to_have_count(0)
    flow_link = summary.get_by_role("link", name="Enter grab flows", exact=True)
    flow_link.press("Enter")
    expect(page.locator("#grab-flow-fields")).to_be_focused()
    page.keyboard.press("Tab")
    expect(lower_flow).to_be_focused()

    calculate = flows.get_by_role("button", name="Calculate concentrations", exact=True)
    lower_flow.fill("1.25")
    calculate.press("Enter")
    expect(higher_flow).to_have_attribute("aria-invalid", "true")
    expect(higher_flow).to_have_value("")
    expect(lower_flow).to_have_value("1.25")
    expect(calculated).to_have_count(0)

    higher_flow.fill("0")
    calculate.press("Enter")
    expect(higher_flow).to_have_attribute("aria-invalid", "true")
    expect(flows.get_by_text("Enter a flow greater than zero.", exact=True)).to_be_visible()
    expect(higher_flow).to_have_value("0")
    expect(lower_flow).to_have_value("1.25")
    expect(calculated).to_have_count(0)

    higher_flow.fill("2.75")
    calculate.press("Enter")
    expect(mean).to_be_visible()
    expect(maximum).to_be_visible()
    expect(flow_link).to_have_count(0)
    # (6.20 × 1.25 + 12.20 × 2.75) / 4 = 10.325 µg/L on day 1.
    # Day 2 is 7.20 µg/L, so the monthly maximum is below the highest individual grab.
    daily_region = page.get_by_role("region", name="Daily concentrations")
    daily = daily_region.get_by_role("table")
    expect(
        daily.get_by_role("row").filter(has_text=prior_month.date().isoformat())
    ).to_contain_text("0.010325")
    expect(
        daily.get_by_role("row").filter(has_text=prior_month.replace(day=2).date().isoformat())
    ).to_contain_text("0.0072")
    calculated_url = urlsplit(page.url)
    assert calculated_url.path == source
    assert not calculated_url.query and not calculated_url.fragment
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer.get_by_role("heading", name="Monthly concentrations", exact=True)).to_be_visible()
    expect(drawer).to_contain_text("Grab sample flows")
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()

    assert page.goto(source).ok
    expect(lower_flow).to_have_value("")
    expect(higher_flow).to_have_value("")
    expect(calculated).to_have_count(0)
    expect(daily_region).to_have_count(0)
    lower_flow.fill("1.25")
    higher_flow.fill("2.75")
    reason = "Synthetic correction to one grab after the worksheet opened"
    correct_and_approve(higher_revision, supplied_text="11.20", reason=reason)
    amended = release_version(another_report, approver, reason=reason)
    calculate.press("Enter")
    expect(page.get_by_text("Issued source results changed", exact=True)).to_be_visible()
    expect(lower_flow).to_have_value("")
    expect(higher_flow).to_have_value("")
    expect(calculated).to_have_count(0)
    expect(daily_region).to_have_count(0)
    table = page.get_by_role("region", name="Issued source results").get_by_role("table")
    expect(
        table.get_by_role("row")
        .filter(has_text=higher.lab_id)
        .get_by_role("link", name=f"{another_report.number} v{amended.number}", exact=True)
    ).to_be_visible()
    assert page.console_errors == []
