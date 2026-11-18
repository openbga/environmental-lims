"""A reviewer returns a result and an analyst reaches its correction from the dashboard."""

import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import field, local_input, pick, sign_out, submit_fresh_batch
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_returns_queue_uses_review_time_and_leads_to_correction(page: Page, sign_in_as):
    from accounts.models import User
    from analytical.services import return_result

    analyst = User.objects.get(username="demo_analyst")
    reviewer = User.objects.get(username="demo_reviewer")
    now = timezone.now()
    with patch.object(timezone, "now", return_value=now - timedelta(hours=5)):
        batch, recent_revision, earlier_sample = submit_fresh_batch(
            analyst, coc_reference="E2E-RETURN-RECENT"
        )
    with patch.object(timezone, "now", return_value=now - timedelta(hours=3)):
        _, older_revision, later_sample = submit_fresh_batch(
            analyst, coc_reference="E2E-RETURN-OLDER"
        )
    with patch.object(timezone, "now", return_value=now - timedelta(hours=2)):
        older_return = return_result(
            actor=reviewer, revision_id=older_revision.pk, reason="Check the dilution log."
        )

    reason = "Confirm the lead entry against the instrument worksheet."
    sign_in_as("reviewer")
    page.goto(reverse("analytical:review-batch", args=[batch.pk]))
    page.get_by_role("checkbox", name=re.compile(rf"Select {earlier_sample.lab_id} Lead")).check()
    pick(page.get_by_label(re.compile("^Action")), label="Return")
    page.get_by_role("textbox", name=re.compile("^Note or reason")).fill(reason)
    page.get_by_role("button", name=re.compile("^Apply decision")).click()
    expect(page.get_by_text("Returned 1 result to the analyst.")).to_be_visible()
    sign_out(page)

    sign_in_as("analyst")
    queue = page.get_by_role("link", name="Results returned for correction", exact=True)
    oldest = queue.locator("xpath=ancestor::li").locator("time").get_attribute("datetime")
    assert datetime.fromisoformat(oldest) == older_return.reviewed_at
    queue.click()
    expect(page.get_by_role("heading", level=1, name="Worklist", exact=True)).to_be_visible()
    expect(page.locator("th[aria-sort=ascending]")).to_have_text(re.compile("Returned at"))
    rows = page.locator('table[data-vr-grid="analytical.worklist"] tbody tr')
    expect(rows.get_by_role("rowheader")).to_have_text([later_sample.lab_id, earlier_sample.lab_id])
    current = rows.nth(1)
    show_reason = current.locator("summary", has_text="Return reason")
    review_note = current.get_by_text(reason, exact=True)
    expect(show_reason).to_be_visible()
    expect(review_note).to_be_hidden()
    show_reason.focus()
    expect(show_reason).to_be_focused()
    page.keyboard.press("Enter")
    expect(review_note).to_be_visible()
    page.keyboard.press("Space")
    expect(review_note).to_be_hidden()
    expect(current).to_contain_text(reviewer.username)
    result = current.get_by_role("link", name="Lead", exact=True)
    expect(result).to_have_attribute(
        "href", reverse("analytical:result-detail", args=[recent_revision.result_id])
    )
    result.click()
    revisions = page.get_by_role("region", name=re.compile("^Revisions"))
    expect(revisions.get_by_text(reason, exact=False)).to_be_visible()
    page.locator("#entry").get_by_role("button", name="Correct result", exact=True).click()
    correction = page.get_by_role("dialog", name="Correct result", exact=True)
    field(correction, "Result").fill("5.30")
    field(correction, "Reporting limit applied").fill("1.0")
    field(correction, "Analysis start").fill(local_input(timedelta(minutes=-20)))
    field(correction, "Reason for correction or reanalysis").fill("Corrected transcription.")
    correction.get_by_role("button", name="Correct result", exact=True).click()
    expect(page.get_by_text("Saved as a new revision. The earlier entry is kept.")).to_be_visible()
    expect(revisions.get_by_text(reason, exact=False)).to_be_visible()
    expect(revisions.get_by_role("heading", name="Revision 2", exact=True)).to_be_visible()

    page.goto(reverse("dashboard"))
    queue.click()
    expect(rows.get_by_role("rowheader")).to_have_text([later_sample.lab_id])

    page.goto(reverse("analytical:batch-worklist"))
    expect(page.locator("th[aria-sort=ascending]")).to_have_text(re.compile("Hold deadline"))
    assert page.console_errors == []
