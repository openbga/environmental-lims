import csv
from codecs import BOM_UTF8
from datetime import timedelta
from io import StringIO

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from analytical import services as analytical
from analytical.models import Qualifier, Result
from reporting.results_csv import export_results_csv
from reporting.services import update_report

from .test_release import release
from .test_soil_basis import DRY, soil_report
from .test_subcontract_release import accept, enter, subcontracted_sample


def url(report, number=1, extension="csv"):
    return reverse(f"portal:report-results-{extension}", args=[report.pk, number])


def rows(response):
    assert response.status_code == 200
    assert response.content.startswith(BOM_UTF8)
    return list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"), newline="")))


def test_csv_reads_issued_text_without_fetching_pdf(world, people, client, settings):
    settings.REPORT_SYNTHETIC_DATA_NOTICE = True
    report, sample = world["report_a"], world["sample_a"]
    sample.client_sample_id = " ＝sample"
    sample.collection_location = 'Outfall "A", upstream\nØst branch'
    sample.save(update_fields=["client_sample_id", "collection_location"])
    version = release(report, people["approver"])
    frozen_sample = version.file.snapshot["content"]["samples"][0]
    settings.REPORT_SYNTHETIC_DATA_NOTICE = False
    client.force_login(world["user_a"])

    with CaptureQueriesContext(connection) as queries:
        response = client.get(url(report))

    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == (
        f'attachment; filename="{report.number}-v1-results.csv"'
    )
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    (file_query,) = [q["sql"] for q in queries if '"reporting_reportfile"' in q["sql"]]
    assert '"reporting_reportfile"."snapshot"' in file_query
    assert '"reporting_reportfile"."pdf"' not in file_query
    assert rows(response) == [
        {
            "report_number": report.number,
            "report_version": "1",
            "synthetic_data": "true",
            "lab_sample_id": "VR-A1",
            "client_sample_id": "' ＝sample",
            "matrix": "Water",
            "collected_at": frozen_sample["collected_at"],
            "collection_location": sample.collection_location,
            "analyte": "Lead (synthetic)",
            "result": "0.050",
            "unit": "mg/L",
            "qualifiers": "",
            "reporting_limit": "0.01",
            "result_basis": "",
            "percent_solids": "",
            "solids_reference": "",
            "method_code": "DEMO-ICP",
            "method_version": "r1",
            "analysis_started_at": frozen_sample["results"][0]["analysis_started_at"],
            "subcontract_lab": "",
            "subcontract_report_reference": "",
            "subcontract_sample_id": "",
        }
    ]


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_only_active_owning_client_can_download_issued_versions(world, people, client, extension):
    report, draft = world["report_a"], world["report_b"]
    release(report, people["approver"])
    assert client.get(url(report, extension=extension)).status_code == 302
    client.force_login(people["approver"])
    assert client.get(url(report, extension=extension)).status_code == 403
    client.force_login(world["user_b"])
    assert client.get(url(report, extension=extension)).status_code == 404
    assert client.get(url(draft, extension=extension)).status_code == 404
    client.force_login(world["user_a"])
    assert client.get(url(report, extension=extension)).status_code == 200
    assert client.get(url(report, 2, extension)).status_code == 404
    assert client.post(url(report, extension=extension)).status_code == 405
    User.objects.filter(pk=world["user_a"].pk).update(is_active=False)
    assert client.get(url(report, extension=extension)).status_code == 302


def test_csv_keeps_original_version_after_correction(world, people, client):
    report, sample = world["report_a"], world["sample_a"]
    release(report, people["approver"])
    client.force_login(world["user_a"])
    original = client.get(url(report))
    assert rows(original)[0]["result"] == "0.050"

    qualifier = Qualifier.objects.create(code="J", description="Synthetic estimated value")
    result = Result.objects.get(requested_test__sample=sample)
    revision = analytical.enter_result(
        actor=people["analyst"],
        result_id=result.pk,
        supplied_text="-4.800e-2",
        reporting_limit_applied="0.010",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(minutes=5),
        qualifier_codes=[qualifier.code],
        reason="Corrected lead transcription",
    )
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    release(report, people["approver2"], reason="Corrected lead transcription")

    (amended,) = rows(client.get(url(report, 2)))
    assert amended["result"] == "-4.800e-2"
    assert amended["qualifiers"] == "J"
    assert amended["report_version"] == "2"
    assert client.get(url(report)).content == original.content


def test_csv_preserves_frozen_soil_basis(world, people, client):
    report, _ = soil_report(world, people, **DRY)
    release(report, people["approver"])
    client.force_login(world["user_a"])

    (row,) = rows(client.get(url(report)))
    assert (row["result_basis"], row["percent_solids"], row["solids_reference"]) == (
        "Dry weight",
        "82.125",
        DRY["solids_reference"],
    )


def test_csv_keeps_external_nondetect_qualifiers_and_source(world, people, client):
    sample, contract = subcontracted_sample(world, people)
    source = enter(contract, people, "ND", qualifier_text="J: estimated, as reported")
    accept(contract, source, people)
    report = world["report_a"]
    update_report(
        actor=people["pm"],
        report_id=report.pk,
        expected_revision=report.revision,
        title=report.title,
        samples=[world["sample_a"], sample],
    )
    release(report, people["approver"])
    client.force_login(world["user_a"])

    internal, external = rows(client.get(url(report)))
    assert internal["result"] == "0.050" and internal["subcontract_lab"] == ""
    assert external["lab_sample_id"] == sample.lab_id
    assert external["result"] == "ND" and external["reporting_limit"] == "0.0002"
    assert external["qualifiers"] == "J: estimated, as reported"
    assert external["method_code"] == "EPA 245.1 Rev 3.0"
    assert external["method_version"] == ""
    assert external["subcontract_lab"] == "Synthetic Partner Lab"
    assert external["subcontract_report_reference"] == "SPL-REPORT-0001"
    assert external["subcontract_sample_id"] == "SPL-0001"


def test_legacy_csv_keeps_missing_fields_blank_and_escapes_formula_cells():
    result = {
        "analyte": "\u200b@analyte",
        "reported": "0.1234567890",
        "unit": "µg/kg",
        "qualifiers": ["=qualifier", "B"],
        "reporting_limit": "0.010",
        "method_code": "\t-method",
    }
    snapshot = {
        "release": {"version": 1},
        "content": {
            "report": {"number": "RPT-000001"},
            "samples": [
                {
                    "lab_id": "VR-001",
                    "client_sample_id": "S-01",
                    "matrix": "Soil",
                    "results": [result],
                }
            ],
        },
    }

    raw = export_results_csv(snapshot).removeprefix("\ufeff")
    (row,) = csv.DictReader(StringIO(raw, newline=""))
    assert row["result_basis"] == "Basis not recorded"
    assert row["result"] == "0.1234567890" and row["reporting_limit"] == "0.010"
    assert row["unit"] == "µg/kg"
    assert row["analyte"] == "'\u200b@analyte"
    assert row["qualifiers"] == "'=qualifier, B"
    assert row["method_code"] == "'\t-method"
    assert row["synthetic_data"] == row["percent_solids"] == row["solids_reference"] == ""
    assert row["method_version"] == row["analysis_started_at"] == ""
    assert result["analyte"] == "\u200b@analyte"
