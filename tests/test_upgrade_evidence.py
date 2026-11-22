import json
import runpy
from pathlib import Path

import pytest
from django.core.management import call_command

from analytical.models import QCCorrectiveAction, QCMeasurement, QCSample


@pytest.mark.django_db
def test_upgrade_evidence_preserves_qc_history(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-upgrade-test-password")
    call_command("seed_demo", full=True, verbosity=0)
    evidence_path = tmp_path / "evidence.json"
    monkeypatch.setenv("UPGRADE_EVIDENCE_PATH", str(evidence_path))
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

    run("capture")
    manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
    for name, model in (
        ("QC samples", QCSample),
        ("QC measurements", QCMeasurement),
        ("QC corrective actions", QCCorrectiveAction),
    ):
        assert manifest[name]["rows"].keys() == {
            str(pk) for pk in model.objects.values_list("pk", flat=True)
        }
    run("verify")

    # A change to the exact supplied text must fail even when the numeric reading is identical.
    QCMeasurement.objects.filter(pk=failed.pk).update(observed_text=f"{failed.observed_text}0")
    with pytest.raises(SystemExit, match=r"QC measurements: 0 missing, 0 added, and 1 changed"):
        run("verify")
    QCMeasurement.objects.filter(pk=failed.pk).update(observed_text=failed.observed_text)

    # Losing the correction still leaves QC records and the corrective action in the database.
    correction.delete()
    with pytest.raises(SystemExit, match=r"QC measurements: 1 missing, 0 added, and 0 changed"):
        run("verify")
    with pytest.raises(SystemExit, match="The baseline needs a failed QC observation"):
        run("capture")
