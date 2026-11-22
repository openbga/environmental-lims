"""Download standard and mapped instrument setup inventories from analytical batches."""

import csv
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from helpers import receive_water, sign_in, sign_out
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def batch_inventory(demo):
    from accounts.models import User
    from analytical import services
    from catalog.models import TestDefinition
    from receipt.models import RequestedTest

    samples = receive_water(
        "E2E-RUN-LIST",
        count=2,
        received_ago=timedelta(hours=3),
        collected_ago=timedelta(hours=5),
    )
    analyst = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    )
    batch = services.create_batch(actor=analyst, method=definition.method, matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(
            RequestedTest.objects.filter(sample__in=samples).values_list("pk", flat=True)
        ),
    )
    services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="RUN-LIST-BLANK"
    )
    return batch, samples, definition, analyst


def test_run_list_download_ignores_filters_and_explains_its_limits(page, demo, batch_inventory):
    from analytical import services

    batch, samples, definition, analyst = batch_inventory
    sign_in(page, "demo_analyst", demo.password)
    path = f"/analytical/batches/{batch.pk}/"
    page.goto(path + "?q=" + samples[0].lab_id)
    table = page.locator('[data-vr-grid="analytical.batch_results"]')
    expect(table).to_contain_text(samples[0].lab_id)
    expect(table).not_to_contain_text(samples[1].lab_id)
    export = page.get_by_role("link", name="Export run list", exact=True)
    export.focus()
    expect(export).to_be_focused()
    with page.expect_download() as event:
        page.keyboard.press("Enter")
    download = event.value
    assert download.suggested_filename == f"{batch.number}-run-list.csv"
    with open(download.path(), encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    tests = [row for row in rows if row["row_type"] == "sample_test"]
    assert {row["sample_id"] for row in tests} == {sample.lab_id for sample in samples}
    lead = next(row for row in tests if row["analyte"] == "Lead")
    assert Decimal(lead["configured_reporting_limit"]) == definition.reporting_limit
    assert lead["recorded_prepared_aliquot_id"] == ""
    qc = [row for row in rows if row["row_type"] == "qc_sample"]
    assert len(qc) == 1 and qc[0]["qc_label"] == "RUN-LIST-BLANK"
    assert qc[0]["analyte"] == qc[0]["configured_reporting_limit"] == ""

    page.locator("[data-vr-documentation-toggle]").first.click()
    drawer = page.locator("#vr-documentation")
    expect(drawer).to_be_visible()
    drawer.get_by_text("Run-list CSV", exact=True).click()
    expect(
        drawer.get_by_text(
            "recorded_prepared_aliquot_id, recorded_preparation_batch, recorded_aliquot_label",
            exact=True,
        )
    ).to_be_visible()
    expect(drawer).to_contain_text("not a result-import template")
    page.locator("[data-vr-documentation-close]").click()

    # A valid stored label can still be unsafe as a spreadsheet cell. Refuse the whole file.
    services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="lcs", label="=SYNTHETIC-FORMULA"
    )
    export.click()
    expect(page.get_by_text("Run list not exported.", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name="Export run list", exact=True)).to_be_visible()
    assert "text/csv" not in page.request.get(path + "run-list.csv").headers.get("content-type", "")

    sign_out(page)
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(path)
    expect(page.get_by_role("link", name="Export run list", exact=True)).to_have_count(0)
    expect(
        page.get_by_role("link", name="Export run list with a template", exact=True)
    ).to_have_count(0)
    assert page.request.get(path + "run-list.csv").status == 403
    assert page.console_errors == []


def test_run_list_template_download_and_invalid_file_recovery(page, demo, batch_inventory):
    batch, samples, _, _ = batch_inventory
    sign_in(page, "demo_analyst", demo.password)
    path = f"/analytical/batches/{batch.pk}/"
    page.goto(path + "?q=" + samples[0].lab_id)
    title = "Export run list with a template"
    page.get_by_role("link", name=title, exact=True).press("Enter")
    dialog = page.get_by_role("dialog", name=title, exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(batch.number)
    expect(page).to_have_url(re.compile(re.escape(path) + r"\?q="))

    with page.expect_download() as event:
        dialog.get_by_role("link", name="Download example template", exact=True).click()
    assert event.value.suggested_filename == "run-list-template.csv"
    with event.value.path().open(encoding="utf-8-sig", newline="") as stream:
        headings, keys = csv.reader(stream)
    assert len(headings) == len(keys)
    assert {"row_type", "sample_id", "qc_label", "configured_reporting_limit"} <= set(keys)

    upload = dialog.get_by_label(re.compile(r"^Run list template"))
    submit = dialog.get_by_role("button", name="Download run list", exact=True)
    upload.set_input_files(
        {"name": "invalid.csv", "mimeType": "text/csv", "buffer": b"Sample\r\nunknown_key\r\n"}
    )
    submit.click()
    expect(upload).to_have_attribute("aria-invalid", "true")
    expect(dialog).to_contain_text("Enter a run list field key from the example template")
    expect(page).to_have_url(re.compile(f"{path}run-list/template/$"))

    upload.set_input_files(
        {
            "name": "instrument.csv",
            "mimeType": "text/csv",
            "buffer": b'Type,"Lab, identifier",QC label\r\nrow_type,sample_id,qc_label\r\n',
        }
    )
    with page.expect_download() as event:
        submit.click()
    assert event.value.suggested_filename == f"{batch.number}-run-list.csv"
    with event.value.path().open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == ["Type", "Lab, identifier", "QC label"]
    assert {row[1] for row in rows[1:] if row[0] == "sample_test"} == {
        sample.lab_id for sample in samples
    }
    assert rows[-1] == ["qc_sample", "", "RUN-LIST-BLANK"]
    expect(dialog).to_be_visible()
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(page).to_have_url(re.compile(f"{path}$"))
    assert page.console_errors == []
