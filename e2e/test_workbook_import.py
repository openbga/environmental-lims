"""An explicit worksheet is previewed and imported through the ordinary result gates."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from helpers import field, pick, receive_water, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_excel_worksheet_preview_errors_commit_and_provenance(page, demo):
    from accounts.models import User
    from analytical import import_profiles, services
    from analytical.models import ResultRevision
    from analytical.tests.xlsx_factory import workbook
    from catalog.models import TestDefinition
    from core.models import AuditEvent
    from receipt.models import RequestedTest

    sample = receive_water(
        "E2E-XLSX", count=1, received_ago=timedelta(hours=3), collected_ago=timedelta(hours=5)
    )[0]
    analyst = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    )
    batch = services.create_batch(actor=analyst, method=definition.method, matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=[RequestedTest.objects.get(sample=sample, definition=definition).pk],
    )
    import_profiles.create_profile(
        actor=analyst,
        name="Synthetic Excel table",
        layout="long",
        delimiter="comma",
        mapping={
            "header_row": 1,
            "date_format": "iso",
            "sample": "Sample",
            "analyte": "Analyte",
            "analysis_started_at": "Started",
            "result": "Result",
            "unit": {"fixed": "ug/L"},
            "reporting_limit": {"fixed": "1.0"},
            "dilution_factor": {"fixed": "1"},
        },
    )
    when = (timezone.now() - timedelta(hours=1)).isoformat()
    header = ["Sample", "Analyte", "Started", "Result"]
    row = [sample.lab_id, "Lead", when, ("n", "5.1234567890")]
    good = workbook({"Notes": [["Synthetic workbook"]], "Results": [header, row]})
    bad = workbook({"Results": [header, row[:3] + [("formula", "1+1")]]})
    url = f"/analytical/batches/{batch.pk}/import-mapped/"
    sign_in(page, "demo_analyst", demo.password)

    def upload(data, worksheet):
        page.goto(url)
        pick(field(page, "Mapping profile"), label="Synthetic Excel table")
        field(page, "Worksheet name").fill(worksheet)
        page.get_by_label("Results file (CSV, TSV or XLSX)").set_input_files(
            {
                "name": "synthetic.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "buffer": data,
            }
        )
        page.get_by_role("button", name="Validate and preview").click()

    upload(bad, "Results")
    expect(page.get_by_role("alert")).to_contain_text("formula", ignore_case=True)
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    upload(good, "Missing")
    expect(page.get_by_role("alert")).to_contain_text("Results")
    upload(good, "Results")
    expect(page.get_by_text("passed validation. Nothing is saved yet.")).to_be_visible()
    expect(page.get_by_role("cell", name="5.1234567890", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="Results!row 2", exact=True)).to_be_visible()
    assert not ResultRevision.objects.filter(result__batch=batch).exists()
    page.locator("#vr-form-dialog [data-vr-documentation-toggle]").click()
    drawer = page.locator("#vr-documentation")
    expect(drawer).to_contain_text("Worksheet name")
    page.locator("[data-vr-documentation-close]").click()
    button = page.get_by_role("button", name="Import 1 result(s) as entered")
    button.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Imported 1 result(s) from synthetic.xlsx as entered.")).to_be_visible()
    source_file = page.get_by_role("link", name="Download synthetic.xlsx")
    with page.expect_download() as clicked:
        source_file.click()
    assert clicked.value.suggested_filename == "synthetic.xlsx"
    assert clicked.value.path().read_bytes() == good
    source_file.focus()
    with page.expect_download() as activated:
        page.keyboard.press("Enter")
    assert activated.value.path().read_bytes() == good
    revision = ResultRevision.objects.get(result__batch=batch)
    assert revision.value == Decimal("5.1234567890")
    assert revision.supplied_text == "5.1234567890"
    assert revision.source_detail == "synthetic.xlsx:Results!row 2"
    audit = AuditEvent.objects.filter(
        action="batch.results_imported", object_id=str(batch.pk)
    ).get()
    assert audit.after["worksheet"] == "Results"
    upload(good, "Results")
    expect(page.get_by_role("alert")).to_contain_text("already imported")
    assert ResultRevision.objects.filter(result__batch=batch).count() == 1
    assert page.console_errors == []
