from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

from receipt.models import Order
from reporting.models import ReportFile
from reporting.packages import export_report_package
from reporting.results_csv import export_results_csv
from reporting.services import create_report

from .test_qc_isolation import A_OWN, B_SECRETS, shared_batch
from .test_release import release
from .test_subcontract_release import accept, enter, subcontracted_sample


def members(snapshot, pdf):
    with ZipFile(BytesIO(export_report_package(snapshot, pdf))) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def summary(snapshot, pdf):
    return next(
        data for name, data in members(snapshot, pdf).items() if name.endswith(".txt")
    ).decode()


def test_package_reuses_stored_pdf_and_standard_csv_for_an_older_release(
    world, people, django_assert_num_queries
):
    report = world["report_a"]
    report.title = "../Order title"
    report.save(update_fields=["title"])
    version = release(report, people["approver"])
    stored = version.file
    snapshot = stored.snapshot
    assert "include_qc_summary" not in snapshot["content"]["samples"][0]
    before = export_report_package(snapshot, stored.pdf)

    Order.objects.filter(pk=world["sample_a"].order_id).update(include_qc_summary=True)
    stored = ReportFile.objects.get(pk=stored.pk)
    with django_assert_num_queries(0):
        assert export_report_package(stored.snapshot, stored.pdf) == before
    stem = f"{report.number}-v1"
    assert members(stored.snapshot, stored.pdf) == {
        f"{stem}.pdf": bytes(stored.pdf),
        f"{stem}.csv": export_results_csv(snapshot).encode("utf-8"),
    }


def test_summary_selects_only_requested_samples_and_batch_tests(world, people):
    version = release(world["report_a"], people["approver"])
    snapshot = deepcopy(version.file.snapshot)
    content = snapshot["content"]
    selected = content["samples"][0]
    selected["include_qc_summary"] = True
    (lead,) = selected["results"]
    lead.update(qualifiers=["J"], preparation={"batch": "P-000001"})
    content["qualifiers"] = [{"code": "J", "description": "Estimated value"}]
    content["synthetic_notice"] = True
    (analysis,) = content["batches"]
    (original_qc,) = analysis["qc"].values()
    copper = {**lead, "analyte": "Copper", "test_definition_id": lead["test_definition_id"] + 1}
    zinc = {**lead, "analyte": "Zinc", "test_definition_id": lead["test_definition_id"] + 2}
    selected["results"].append(copper)
    analysis["qc"] = {
        "Lead renamed after entry": original_qc,
        "Copper": {**original_qc, "test_definition_id": copper["test_definition_id"]},
        "Zinc": {
            **original_qc,
            "test_definition_id": zinc["test_definition_id"],
            "issues": [{"message": "Unrequested Zinc QC issue"}],
        },
    }
    content["batches"].append(
        {
            "number": "P-000001",
            "kind": "preparation",
            "method": "Separate digestion",
            "qc": {
                "Lead renamed after entry": {
                    **original_qc,
                    "measurements": [
                        {
                            "qc_sample": "PREP-BLANK",
                            "qc_type": "method_blank",
                            "observed_text": "ND",
                            "unit": "mg/L",
                            "evaluation": {"status": "pass"},
                            "analysis_batch": analysis["number"],
                        }
                    ],
                },
            },
        },
    )
    content["samples"].append(
        {
            **selected,
            "lab_id": "VR-NOT-REQUESTED",
            "coc_reference": "Another order",
            "include_qc_summary": False,
            "results": [lead, zinc],
        },
    )

    text = summary(snapshot, version.file.pdf)
    assert f"Report {version.report.number} · Version 1" in text
    assert "Synthetic demonstration data." in text
    assert "Requested samples · VR-A1\n" in text
    assert f"Analytical batch {analysis['number']}" in text
    assert f"Analytical run · {analysis['number']}" in text
    assert "Preparation batch P-000001 · Separate digestion" in text
    assert text.count("QC analyte · Lead renamed after entry") == 2
    assert "QC analyte · Copper" in text
    assert "VR-A1 · Lead (synthetic) · Qualifiers J (Estimated value)" in text
    assert "PREP-BLANK · Method blank · Observed ND mg/L" in text
    assert "No QC measurements recorded for this test." in text
    assert "No QC evidence recorded for this test." in text
    assert "VR-NOT-REQUESTED" not in text
    assert "Zinc" not in text
    csv = members(snapshot, version.file.pdf)[f"{version.report.number}-v1.csv"]
    assert b"VR-NOT-REQUESTED" in csv


def test_summary_preserves_cross_client_withholding(world, people):
    report_a, report_b = shared_batch(world, people)
    Order.objects.filter(pk__in=[r.samples.get().order_id for r in (report_a, report_b)]).update(
        include_qc_summary=True
    )
    a, b = [release(r, people["approver"]).file for r in (report_a, report_b)]
    a_text, b_text = summary(a.snapshot, a.pdf), summary(b.snapshot, b.pdf)
    for secret in B_SECRETS:
        assert secret.strip('"') not in a_text
    for own in A_OWN:
        assert own.strip('"') in a_text
        assert own.strip('"') not in b_text
    assert "LCS-77" in a_text and "LCS-77" in b_text
    assert "Sample not in this report · Matrix spike · Observed value withheld" in a_text
    assert "Recorded evaluation · Fail" in a_text
    assert "Disposition · accept qualified" in a_text
    assert "Disposition note withheld" in a_text
    assert "MS-VR-B2" in b_text and "Matrix interference in VR-B2" in b_text


def test_summary_states_when_requested_evidence_is_external_only(world, people):
    sample, contract = subcontracted_sample(world, people)
    Order.objects.filter(pk=sample.order_id).update(include_qc_summary=True)
    external = enter(contract, people, "0.00021", qualifier_text="J: estimated")
    accept(contract, external, people)
    report = create_report(
        actor=people["pm"], project=world["project_a"], title="External only", samples=[sample]
    )
    stored = release(report, people["approver"]).file
    text = summary(stored.snapshot, stored.pdf)
    assert f"Requested samples · {sample.lab_id}" in text
    assert "Qualifiers J: estimated" in text
    assert "Subcontracted result. QC was not evaluated by the issuing laboratory." in text
    assert "Analytical batch" not in text
