import json
import runpy
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse

from analytical.models import QCCorrectiveAction, QCMeasurement, QCSample
from core.models import AuditEvent
from receipt.models import Client as LabClient
from receipt.models import Container, Order, Sample
from receipt.subcontract_models import Subcontract, SubcontractResult
from reporting.models import EddExport


@pytest.mark.django_db
def test_upgrade_evidence_preserves_issued_files_and_laboratory_history(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-upgrade-test-password")
    call_command("seed_demo", full=True, verbosity=0)
    evidence_path = tmp_path / "evidence.json"
    counts_path = tmp_path / "verified-counts.json"
    monkeypatch.setenv("UPGRADE_EVIDENCE_PATH", str(evidence_path))
    monkeypatch.setenv("UPGRADE_VERIFIED_COUNTS_PATH", str(counts_path))
    script = Path(__file__).resolve().parents[1] / "scripts/ci/upgrade_evidence.py"

    def run(mode):
        monkeypatch.setenv("UPGRADE_EVIDENCE_MODE", mode)
        return runpy.run_path(str(script))

    prepared = run("prepare")
    action = QCCorrectiveAction.objects.get(
        qc_sample__label=prepared["QC_SAMPLE_LABEL"],
        disposition=QCCorrectiveAction.Disposition.REANALYZE,
    )
    failed = action.measurement
    correction = QCMeasurement.objects.get(supersedes=failed)
    assert not failed.is_current and correction.is_current
    assert action.qc_sample_id == failed.qc_sample_id == correction.qc_sample_id

    sample = Sample.objects.get(order__coc_reference=prepared["SUBCONTRACT_REFERENCE"])
    container = sample.containers.get()
    receipt, transfer = sample.custody_events.all()
    events = AuditEvent.objects.filter(object_type="receipt.sample", object_id=str(sample.pk))
    original_receipt = events.get(action="sample.received").after
    sample_correction = events.get(action="sample.identity_corrected")
    assert original_receipt["client_sample_id"] == sample_correction.before["client_sample_id"]
    assert original_receipt["client_sample_id"] != sample.client_sample_id
    assert sample_correction.after["client_sample_id"] == sample.client_sample_id
    assert original_receipt["containers"][0]["id"] == container.pk
    assert original_receipt["custody_event_id"] == str(receipt.pk)
    assert events.get(action="sample.transferred").after["custody_event_id"] == str(transfer.pk)
    assert transfer.from_location == receipt.to_location
    assert transfer.to_location == sample.current_location

    export = EddExport.objects.select_related("version__report__project").get()
    assert export.version.number == 1 < export.version.report.latest_version
    edd_label = "electronic deliverable exports"
    with monkeypatch.context() as missing:
        missing.setattr(EddExport.objects, "all", EddExport.objects.none)
        with pytest.raises(SystemExit, match=f"Baseline has no {edd_label}"):
            run("capture")
    assert not evidence_path.exists()

    run("capture")
    assert not counts_path.exists()
    manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
    for name, model in (
        ("orders", Order),
        ("samples", Sample),
        ("containers", Container),
        ("QC samples", QCSample),
        ("QC measurements", QCMeasurement),
        ("QC corrective actions", QCCorrectiveAction),
        ("subcontract dispatches", Subcontract),
        ("external source results", SubcontractResult),
        (edd_label, EddExport),
    ):
        assert manifest[name]["rows"].keys() == {
            str(pk) for pk in model.objects.values_list("pk", flat=True)
        }
    edd_evidence = manifest[edd_label]
    assert {"version_id", "mapping", "manifest", "row_counts", "data", "sha256", "size"} <= set(
        edd_evidence["fields"]
    )
    assert str(export.version_id) in manifest["issued report versions"]["rows"]

    edd_url = reverse("portal:edd-download", args=[export.version.report_id, export.pk])
    get = Client.get
    edd_responses = []

    def observe_edd_download(client, path, **kwargs):
        response = get(client, path, **kwargs)
        if path == edd_url:
            edd_responses.append((response.wsgi_request.user.client_id, response.status_code))
            if response.status_code == 200:
                assert response.content == bytes(export.data)
        return response

    # Candidate migrations may add rows to an existing sample. Verify the baseline IDs.
    with transaction.atomic(), monkeypatch.context() as download:
        download.setattr(Client, "get", observe_edd_download)
        LabClient.objects.create(name="Additional synthetic upgrade client")
        Container.objects.create(
            sample=sample,
            label=f"{sample.lab_id}-02",
            description="Synthetic additional bottle",
            condition="Intact",
        )
        run("verify")
        transaction.set_rollback(True)
    assert edd_responses == [
        (export.version.report.project.client_id, 200),
        (LabClient.objects.get(account_number="EXCSD").pk, 404),
    ]
    assert json.loads(counts_path.read_text(encoding="utf-8")) == {
        label: len(evidence["rows"]) for label, evidence in manifest.items()
    }

    original, corrected = SubcontractResult.objects.filter(
        subcontract__external_reference=prepared["SUBCONTRACT_REFERENCE"]
    ).order_by("number")
    assert (original.observed_text, corrected.observed_text) == ("4.200", "4.100")
    failed_counts_path = tmp_path / "failed-counts.json"
    monkeypatch.setenv("UPGRADE_VERIFIED_COUNTS_PATH", str(failed_counts_path))

    # A migration can change receiving records without touching their append-only history.
    other_sample = Sample.objects.exclude(pk=sample.pk).first()
    for name, records, changes in (
        ("orders", Order.objects.filter(pk=sample.order_id), {"instructions": "Changed"}),
        (
            "samples",
            Sample.objects.filter(pk=sample.pk),
            {"client_sample_id": original_receipt["client_sample_id"]},
        ),
        ("containers", Container.objects.filter(pk=container.pk), {"sample": other_sample}),
    ):
        with transaction.atomic():
            records.update(**changes)
            with pytest.raises(SystemExit, match=rf"{name}: 0 missing, 0 added, and 1 changed"):
                run("verify")
            transaction.set_rollback(True)

    source_url = reverse("receipt:subcontract-source-download", args=[corrected.pk])

    def corrupt_download_at(download_url):
        def corrupt_download(client, path, **kwargs):
            response = get(client, path, **kwargs)
            if path == download_url:
                response.content = b"different bytes"
            return response

        return corrupt_download

    for download_url, error in (
        (source_url, "Staff download differs from the stored external source"),
        (edd_url, "Client download differs from the stored EDD ZIP"),
    ):
        with monkeypatch.context() as download:
            download.setattr(Client, "get", corrupt_download_at(download_url))
            with pytest.raises(SystemExit, match=error):
                run("verify")
    assert not failed_counts_path.exists()

    # A substituted ZIP must alter the evidence even when its recorded SHA-256 stays the same.
    substituted_zip = EddExport.objects.filter(pk=export.pk).extra(
        select={"data": "%s::bytea"}, select_params=[b"different bytes"]
    )
    assert (
        prepared["collect"](substituted_zip)["rows"][str(export.pk)]
        != edd_evidence["rows"][str(export.pk)]
    )

    # Hash the bytes as well as the stored digest. Substitute only the historical
    # source PDF in the baseline projection, without modifying the immutable row.
    historical_pdf = SubcontractResult.objects.filter(pk=original.pk).extra(
        select={"source_pdf": "%s::bytea"}, select_params=[bytes(corrected.source_pdf)]
    )
    alternate = prepared["collect"](historical_pdf)
    original_rows = manifest["external source results"]["rows"]
    baseline_digest = original_rows[str(original.pk)]
    assert alternate["rows"][str(original.pk)] != baseline_digest
    original_rows.update(alternate["rows"])
    evidence_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        SystemExit, match=r"external source results: 0 missing, 0 added, and 1 changed"
    ):
        run("verify")
    original_rows[str(original.pk)] = baseline_digest
    evidence_path.write_text(json.dumps(manifest), encoding="utf-8")

    # The same source records and PDFs still exist when an issued version points
    # to an extra external result. Its original evidence association must be exact.
    with transaction.atomic():
        original.report_versions.get().external_result_revisions.add(corrected)
        with pytest.raises(
            SystemExit, match=r"issued report versions: 0 missing, 0 added, and 1 changed"
        ):
            run("verify")
        transaction.set_rollback(True)

    # A change to the exact supplied text must fail even when the numeric reading is identical.
    QCMeasurement.objects.filter(pk=failed.pk).update(observed_text=f"{failed.observed_text}0")
    with pytest.raises(SystemExit, match=r"QC measurements: 0 missing, 0 added, and 1 changed"):
        run("verify")
    QCMeasurement.objects.filter(pk=failed.pk).update(observed_text=failed.observed_text)

    # Losing the correction still leaves QC records and the corrective action in the database.
    correction.delete()
    with pytest.raises(SystemExit, match=r"QC measurements: 1 missing, 0 added, and 0 changed"):
        run("verify")
    assert not failed_counts_path.exists()
    with pytest.raises(SystemExit, match="The baseline needs a failed QC observation"):
        run("capture")
