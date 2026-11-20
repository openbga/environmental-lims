"""An analyst transcribes instrument order and a reviewer traces a correction."""

import pytest
from django.urls import reverse
from helpers import field, sign_in, sign_out
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)

REFERENCE = "SYNTHETIC-INJECTION-RUN-1"
SOURCE = "Synthetic instrument sequence file 01"
CORRECTION = "The closing verification was missing from the transcription."
OPENING_CHECK = "Opening  verification"
REPEAT_VIAL = 'Vial "A",  repeat'
INJECTIONS = "\n".join(
    (
        "1,sample,First sample",
        f"2,verification,{OPENING_CHECK}",
        "3,blank,Instrument blank",
        f"4,sample,{REPEAT_VIAL}",
        "5,sample,Effluent replicate",
        "6,verification,Mid-run verification",
        "7,standard,Calibration standard",
        "8,sample,Last sample",
    )
)


def test_record_and_correct_injection_sequence_for_review(page, demo):
    from accounts.models import User
    from analytical.services import create_batch
    from catalog.models import TestDefinition

    analyst = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    )
    batch = create_batch(actor=analyst, method=definition.method, matrix=definition.matrix)
    batch_path = reverse("analytical:batch-detail", args=[batch.pk])
    sign_in(page, "demo_analyst", demo.password)
    assert page.goto(batch_path).ok
    sequences = page.get_by_role("region", name="Injection sequences", exact=True)
    launch = sequences.get_by_role("link", name="Record injection sequence", exact=True)
    create_path = launch.get_attribute("href")
    launch.click()
    field(page, "Run reference").fill(REFERENCE)
    field(page, "Source reference").fill(SOURCE)
    injections = field(page, "Injections (one per line)")
    record = page.get_by_role("button", name="Record injection sequence", exact=True)
    gapped = INJECTIONS.replace("8,sample", "9,sample")
    injections.fill(gapped)
    record.click()
    expect(page.get_by_text("Expected consecutive position 8.", exact=True)).to_be_visible()
    expect(injections).to_have_value(gapped)
    expect(field(page, "Run reference")).to_have_value(REFERENCE)
    expect(field(page, "Source reference")).to_have_value(SOURCE)
    injections.fill(INJECTIONS)
    record.click()
    expect(page.get_by_role("heading", level=1, name=REFERENCE, exact=True)).to_be_visible()
    original_url = page.url
    expect(page.get_by_text(SOURCE, exact=True)).to_be_visible()
    table = page.get_by_role("table", name="Injections", exact=True)
    for sample, preceding, following in (
        ("First sample", "None recorded", f"#2 {OPENING_CHECK}"),
        ('Vial "A",', f"#2 {OPENING_CHECK}", "#6 Mid-run verification"),
        ("Last sample", "#6 Mid-run verification", "None recorded"),
    ):
        cells = table.get_by_role("row").filter(has_text=sample).get_by_role("cell")
        expect(cells.nth(-2)).to_have_text(preceding)
        expect(cells.nth(-1)).to_have_text(following)
    repeat_cells = table.get_by_role("row").filter(has_text='Vial "A",').get_by_role("cell")
    for cell, label in ((repeat_cells.nth(1), REPEAT_VIAL), (repeat_cells.nth(-2), OPENING_CHECK)):
        literal = cell.locator(".vr-preserve-space")
        assert literal.text_content() == label
        expect(literal).to_have_css("white-space", "break-spaces")

    launch = page.get_by_role("link", name="Correct injection sequence", exact=True)
    correct_path = launch.get_attribute("href")
    launch.click()
    injections = field(page, "Injections (one per line)")
    expect(injections).to_have_value(INJECTIONS)
    injections.fill(INJECTIONS + "\n9,verification,Closing verification")
    field(page, "Reason for correction").fill(CORRECTION)
    page.get_by_role("button", name="Correct injection sequence", exact=True).click()
    details = page.get_by_role("region", name="Sequence details", exact=True)
    expect(details.get_by_text(CORRECTION, exact=True)).to_be_visible()
    corrected_url = page.url
    assert corrected_url != original_url

    sign_out(page)
    sign_in(page, "demo_reviewer", demo.password)
    assert page.goto(batch_path).ok
    expect(sequences.get_by_role("link", name="Record injection sequence")).to_have_count(0)
    sequences.get_by_role("link", name=REFERENCE, exact=True).click()
    expect(page).to_have_url(corrected_url)
    expect(page.get_by_text("Current version", exact=True).first).to_be_visible()
    expect(page.get_by_role("link", name="Correct injection sequence")).to_have_count(0)
    expect(details.get_by_text(CORRECTION, exact=True)).to_be_visible()
    last_sample = table.get_by_role("row").filter(has_text="Last sample")
    expect(last_sample.get_by_role("cell").last).to_have_text("#9 Closing verification")

    page.set_viewport_size({"width": 390, "height": 844})
    intervals = page.get_by_role("table", name="Verification intervals", exact=True)
    maximum = field(page, "Maximum sample injections")
    expect(maximum).to_be_empty()
    expect(intervals.get_by_role("columnheader", name="Count comparison")).to_have_count(0)
    maximum.fill("1")
    page.get_by_role("button", name="Compare counts", exact=True).press("Enter")
    leading = intervals.get_by_role("row").filter(has_text="No earlier verification")
    expect(leading).to_contain_text("At or below maximum")
    middle = intervals.get_by_role("row").filter(has_text="Opening").filter(has_text="Mid-run")
    expect(middle).to_contain_text("Over maximum")
    expect(middle.get_by_role("cell", name="2", exact=True)).to_be_visible()
    expect(intervals.get_by_text("No later verification", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Clear comparison", exact=True).press("Enter")
    expect(maximum).to_be_empty()
    expect(intervals.get_by_role("columnheader", name="Count comparison")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    scroll_area = table.locator("xpath=..")
    assert scroll_area.evaluate("el => el.scrollWidth > el.clientWidth")
    scroll_area.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    assert last_sample.get_by_role("cell").last.evaluate(
        "el => el.getBoundingClientRect().right <= innerWidth"
    )
    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Verification before")
    expect(drawer).to_contain_text("Verification after")
    expect(drawer).to_contain_text("Maximum sample injections")
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()

    history = page.get_by_role("region", name="Version history", exact=True)
    history.get_by_role("link", name="Version 1", exact=True).click()
    expect(page).to_have_url(original_url)
    expect(page.get_by_text("Superseded", exact=True).first).to_be_visible()
    expect(last_sample.get_by_role("cell").last).to_have_text("None recorded")
    expect(table.get_by_text("Closing verification", exact=True)).to_have_count(0)
    trailing = intervals.get_by_role("row").filter(has_text="No later verification")
    expect(trailing).to_contain_text("Mid-run verification")
    expect(trailing.get_by_role("cell", name="1", exact=True)).to_be_visible()
    history.get_by_role("link", name="Version 2", exact=True).click()
    expect(page).to_have_url(corrected_url)
    assert not page.console_errors

    assert page.request.get(create_path).status == 403
    assert page.request.get(correct_path).status == 403
    page.set_viewport_size({"width": 1366, "height": 860})
    sign_out(page)
    sign_in(page, "demo_client", demo.password)
    for path in (create_path, original_url, corrected_url, correct_path):
        assert page.request.get(path).status == 403
