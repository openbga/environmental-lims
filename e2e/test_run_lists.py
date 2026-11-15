"""Download a complete instrument setup inventory from a filtered analytical batch."""

import csv
from datetime import timedelta
from decimal import Decimal

import pytest
from helpers import receive_water, sign_in, sign_out
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_run_list_download_ignores_filters_and_explains_its_limits(page, demo):
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
    assert page.request.get(path + "run-list.csv").status == 403
    assert page.console_errors == []
