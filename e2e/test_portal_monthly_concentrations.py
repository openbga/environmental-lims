"""Trace a monthly concentration through current issues, an amendment and stored PDFs."""

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


def test_monthly_concentrations_follow_current_issues(page: Page, sign_in_as, tmp_path):
    page.set_viewport_size(NARROW)
    sign_in_as("client")
    analyst, reviewer, custodian, manager, approver = (
        User.objects.get(username=f"demo_{role}")
        for role in ("analyst", "reviewer", "custodian", "project_manager", "approver")
    )
    local_noon = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
    prior_month = (local_noon.replace(day=1) - timedelta(days=1)).replace(day=1)

    def issue(day, text):
        reference = f"E2E-MONTHLY-{day}"
        with patch("helpers.timezone") as fixture_time:
            fixture_time.now.return_value = prior_month.replace(day=day)
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

    report, version, sample, _ = issue(1, None)
    other, other_v1, _, other_revision = issue(2, "7.20")
    page.get_by_role("link", name="Reports", exact=True).first.click()
    page.get_by_role("link", name=report.number, exact=True).click()
    page.get_by_role(
        "link", name=f"Monthly concentrations for Lead on {sample.client_sample_id}", exact=True
    ).press("Enter")
    heading = page.get_by_role("heading", level=1, name="Monthly concentrations", exact=True)
    expect(heading).to_be_visible()
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
