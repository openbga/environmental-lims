"""Recorded curve provenance, authorization, immutability and workflow gates."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone

from analytical import calibration, services
from analytical.models import QCLimit, ResultRevision, RunCalibration
from catalog import services as catalog
from catalog.models import Analyte, TestDefinition
from core.models import AuditEvent
from reporting import services as reports
from reporting.content import assemble
from reporting.models import ReportFile, ReportVersion

from .conftest import make_user
from .test_workflow import entered, result_for, run_qc, start, when

pytestmark = pytest.mark.django_db


POINTS = [
    {"standard": "Synthetic blank STD-0", "concentration": "0.0000", "response": "-0.01200"},
    {"standard": "Synthetic standard STD-1", "concentration": "1.0000e-2", "response": "12.3400"},
]


def curve(config, analyst, batch, **overrides):
    return calibration.record_calibration(
        **{
            "actor": analyst,
            "batch_id": batch.pk,
            "definition_id": config["defs"]["Lead"].pk,
            "reference": "SYNTHETIC-CAL-1",
            "calibrated_at": when() - timedelta(hours=1),
            "model_text": "Instrument-reported weighted fit; synthetic evidence only",
            "response_unit": "counts",
            "source_reference": "SYNTHETIC export run 001",
            "points": deepcopy(POINTS),
            **overrides,
        }
    )


def verification(config, analyst, batch, **overrides):
    definition = config["defs"]["Lead"]
    QCLimit.objects.get_or_create(
        definition=definition,
        qc_type="calibration_verification",
        measure="recovery",
        defaults={"lower": Decimal("80"), "upper": Decimal("120")},
    )
    sample = services.add_qc_sample(
        actor=analyst,
        batch_id=batch.pk,
        qc_type="calibration_verification",
        label="SYNTHETIC CCV-1",
    )
    return services.record_qc_measurement(
        **{
            "actor": analyst,
            "qc_sample_id": sample.pk,
            "definition_id": definition.pk,
            "observed_text": "10.0000",
            "spike_value": "10.0000",
            "reporting_limit_applied": "0.0050",
            "analysis_started_at": when(),
            **overrides,
        }
    )


def correct_check(config, analyst, check):
    return services.record_qc_measurement(
        actor=analyst,
        qc_sample_id=check.qc_sample_id,
        definition_id=check.definition_id,
        observed_text="10.0001",
        spike_value="10.0000",
        reporting_limit_applied="0.0050",
        analysis_started_at=when(),
        reason="Synthetic transcription correction",
    )


def linked(config, analyst, batch, curve_record, check=None, **overrides):
    return entered(
        config,
        analyst,
        batch,
        calibration_id=curve_record.pk,
        calibration_verification_id=check.pk if check else None,
        **overrides,
    )


def test_record_retains_exact_points_frozen_identity_and_atomic_audit(config, analyst, monkeypatch):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    assert record.points == POINTS
    assert record.unit == "mg/L" and record.equipment_label == ""
    event = AuditEvent.objects.get(action="calibration.recorded")
    assert event.actor_id == analyst.pk
    assert event.after["points"] == POINTS
    assert event.after["created_by_id"] == analyst.pk
    assert event.after["method_code"] == config["method"].code

    def fail(**kwargs):
        raise RuntimeError("Synthetic failed audit")

    monkeypatch.setattr(calibration, "record_event", fail)
    with pytest.raises(RuntimeError, match="failed audit"):
        curve(config, analyst, batch, reference="MUST-ROLL-BACK")
    assert RunCalibration.objects.count() == 1


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"points": []}, "points"),
        ({"points": POINTS * 26}, "points"),
        ({"points": [{"standard": "x", "concentration": "-1", "response": "0"}]}, "points"),
        ({"points": [{"standard": "x", "concentration": "1", "response": 1.1}]}, "points"),
        ({"points": [{"standard": "x", "concentration": "NaN", "response": "0"}]}, "points"),
        (
            {"points": [{"standard": "x", "concentration": "1", "response": "0.00000000001"}]},
            "points",
        ),
        ({"points": [{"standard": "x", "concentration": "1e14", "response": "0"}]}, "points"),
        ({"points": [{"standard": "x", "concentration": "1", "response": "Infinity"}]}, "points"),
        ({"points": [{"standard": "x" * 121, "concentration": "1", "response": "0"}]}, "points"),
        ({"reference": " "}, "reference"),
        ({"reference": "x" * 121}, "reference"),
        ({"model_text": "x" * 1001}, "model_text"),
        ({"response_unit": "x" * 41}, "response_unit"),
        ({"source_reference": "x" * 241}, "source_reference"),
        ({"calibrated_at": None}, "calibrated_at"),
        ({"calibrated_at": timezone.now() + timedelta(days=1)}, "calibrated_at"),
    ],
)
def test_record_validation(config, analyst, changes, field):
    batch = start(config, analyst)
    with pytest.raises(ValidationError) as exc:
        curve(config, analyst, batch, **changes)
    assert field in exc.value.message_dict
    assert not RunCalibration.objects.exists()


def test_record_roles_and_scope(config, analyst, reviewer):
    batch = start(config, analyst)
    client = make_user("calibration-client", "analyst")
    client.client = config["samples"][0].order.project.client
    client.save()
    for actor in (reviewer, client):
        with pytest.raises(PermissionDenied):
            curve(config, actor, batch)
    prep = services.create_batch(
        actor=analyst, method=config["method"], matrix="water", kind="preparation"
    )
    with pytest.raises(ValidationError, match="analytical batch"):
        curve(config, analyst, prep)
    definition = config["defs"]["Lead"]
    for field, value in (("is_active", False), ("matrix", "soil")):
        original = getattr(definition, field)
        TestDefinition.objects.filter(pk=definition.pk).update(**{field: value})
        with pytest.raises(ValidationError, match="active test"):
            curve(config, analyst, batch)
        TestDefinition.objects.filter(pk=definition.pk).update(**{field: original})
    services.close_batch(actor=analyst, batch_id=batch.pk)
    curve(config, analyst, batch)  # Closure freezes membership, not evidence recording.
    with pytest.raises(ValidationError, match="already exists"):
        curve(config, analyst, batch)


def test_curve_and_result_evidence_are_immutable_in_database(config, analyst):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    rev = linked(config, analyst, batch, record)
    with pytest.raises(ValidationError, match="cannot be changed"):
        record.save()
    with pytest.raises(ValidationError, match="cannot be deleted"):
        record.delete()
    for mutate in (
        lambda: RunCalibration.objects.filter(pk=record.pk).update(reference="changed"),
        lambda: RunCalibration.objects.filter(pk=record.pk).delete(),
        lambda: ResultRevision.objects.filter(pk=rev.pk).update(calibration_id=None),
        lambda: ResultRevision.objects.filter(pk=rev.pk).update(calibration_evidence={}),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            mutate()


def test_link_chronology_and_identity(config, analyst):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    # A closing check can follow result analysis.
    check = verification(config, analyst, batch, analysis_started_at=when() + timedelta(minutes=20))
    rev = linked(config, analyst, batch, record, check)
    assert rev.calibration_evidence["verification"]["observed_text"] == "10.0000"
    assert rev.calibration_evidence["verification"]["true_value"] == "10.0000000000"
    assert rev.calibration_evidence["calibration"]["points"] == POINTS
    for overrides, field in (
        (
            {"calibration_id": None, "calibration_verification_id": check.pk},
            "calibration_verification_id",
        ),
        ({"calibration_id": True}, "calibration_id"),
        ({"calibration_id": "1.0"}, "calibration_id"),
        (
            {
                "calibration_id": record.pk,
                "analysis_started_at": record.calibrated_at - timedelta(seconds=1),
            },
            "calibration_id",
        ),
    ):
        args = {"calibration_id": record.pk, **overrides}
        with pytest.raises(ValidationError) as exc:
            entered(config, analyst, batch, **args)
        assert field in exc.value.message_dict
    other = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    wrong = curve(config, analyst, other)
    with pytest.raises(ValidationError, match="batch, test and unit"):
        entered(config, analyst, batch, calibration_id=wrong.pk)
    with pytest.raises(ValidationError, match="batch, test and unit"):
        entered(config, analyst, batch, "Copper", calibration_id=record.pk)
    early = verification(
        config, analyst, other, analysis_started_at=record.calibrated_at - timedelta(seconds=1)
    )
    with pytest.raises(ValidationError, match="current calibration-verification"):
        linked(config, analyst, batch, record, early)
    # Same batch and test, but the check predates the curve.
    newer_curve = curve(
        config,
        analyst,
        batch,
        reference="LATER-CAL",
        calibrated_at=check.analysis_started_at + timedelta(minutes=1),
    )
    with pytest.raises(ValidationError, match="precede"):
        linked(
            config,
            analyst,
            batch,
            newer_curve,
            check,
            analysis_started_at=when() + timedelta(minutes=30),
        )


def test_link_rejects_wrong_qc_type_unit_and_stale_observation(config, analyst):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    check = verification(config, analyst, batch)
    mb, _ = run_qc(config, analyst, batch)
    wrong_type = mb.measurements.get(definition=config["defs"]["Lead"])
    with pytest.raises(ValidationError, match="current calibration-verification"):
        linked(config, analyst, batch, record, wrong_type)
    definition = config["defs"]["Lead"]
    TestDefinition.objects.filter(pk=definition.pk).update(unit="ug/L")
    with pytest.raises(ValidationError, match="batch, test and unit"):
        linked(config, analyst, batch, record, check)
    TestDefinition.objects.filter(pk=definition.pk).update(unit="mg/L")
    correct_check(config, analyst, check)
    with pytest.raises(ValidationError, match="current calibration-verification"):
        linked(config, analyst, batch, record, check)


def test_stale_selected_verification_blocks_submission_and_approval(config, analyst, reviewer):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    check = verification(config, analyst, batch)
    run_qc(config, analyst, batch)
    revision = linked(config, analyst, batch, record, check)
    current = correct_check(config, analyst, check)
    with pytest.raises(ValidationError, match="superseded"):
        services.submit_result(actor=analyst, revision_id=revision.pk)
    corrected = linked(config, analyst, batch, record, current, expected_revision=revision.number)
    services.submit_result(actor=analyst, revision_id=corrected.pk)
    correct_check(config, analyst, current)
    with pytest.raises(ValidationError, match="superseded"):
        services.approve_result(actor=reviewer, revision_id=corrected.pk)
    corrected.refresh_from_db()
    assert corrected.status == "submitted"


@pytest.mark.parametrize("contribution", ["curve", "verification"])
def test_contributors_cannot_review_or_return(config, analyst, both, reviewer, contribution):
    batch = start(config, analyst)
    record = curve(config, both if contribution == "curve" else analyst, batch)
    check = verification(config, both if contribution == "verification" else analyst, batch)
    run_qc(config, analyst, batch)
    revision = linked(config, analyst, batch, record, check)
    services.submit_result(actor=analyst, revision_id=revision.pk)
    for action in (services.approve_result, services.return_result):
        with pytest.raises(PermissionDenied, match="calibration evidence"):
            action(
                actor=both,
                revision_id=revision.pk,
                **({"reason": "Synthetic return"} if action == services.return_result else {}),
            )
    services.approve_result(actor=reviewer, revision_id=revision.pk)


def test_corrections_preserve_links_snapshot_and_stale_edit_protection(config, analyst):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    check = verification(config, analyst, batch)
    old = linked(config, analyst, batch, record, check)
    frozen = deepcopy(old.calibration_evidence)
    check.qc_sample.label = "Edited label"
    check.qc_sample.save(update_fields=["label"])
    definition = config["defs"]["Lead"]
    definition.analyte.name = "Lead (edited synthetic name)"
    definition.analyte.save(update_fields=["name"])
    new_curve = curve(config, analyst, batch, reference="SYNTHETIC-CAL-2")
    correction = linked(config, analyst, batch, new_curve, expected_revision=old.number)
    with pytest.raises(ValidationError, match="changed after you opened"):
        linked(config, analyst, batch, record, expected_revision=old.number)
    old.refresh_from_db()
    assert old.calibration_id == record.pk and old.calibration_verification_id == check.pk
    assert old.calibration_evidence == frozen
    assert (
        correction.calibration_id == new_curve.pk and correction.calibration_verification_id is None
    )
    # Entry with no explicit choice does not infer a current curve or carry old evidence.
    plain = entered(config, analyst, batch, expected_revision=correction.number)
    assert plain.calibration_id is None and plain.calibration_evidence is None
    event = AuditEvent.objects.get(action="result.entered", object_id=str(old.pk))
    assert event.after["calibration_evidence"] == frozen


def report(config, batch, analyst, reviewer):
    # Existing batch QC policy evaluates each recorded control for every included test.
    copper = config["defs"]["Copper"]
    QCLimit.objects.create(
        definition=copper,
        qc_type="calibration_verification",
        measure="recovery",
        lower=Decimal("80"),
        upper=Decimal("120"),
    )
    control = batch.qc_samples.get(qc_type="calibration_verification")
    services.record_qc_measurement(
        actor=analyst,
        qc_sample_id=control.pk,
        definition_id=copper.pk,
        observed_text="10",
        spike_value="10",
        reporting_limit_applied="0.0100",
        analysis_started_at=when(),
    )
    for name in ("Lead", "Copper"):
        result = result_for(batch, name)
        revision = (
            result.revisions.get(number=result.latest_number)
            if result.latest_number
            else entered(config, analyst, batch, name)
        )
        services.submit_result(actor=analyst, revision_id=revision.pk)
        services.approve_result(actor=reviewer, revision_id=revision.pk)
    sample = config["samples"][0]
    return reports.create_report(
        actor=make_user("calibration-pm", "project_manager"),
        project=sample.order.project,
        title="Synthetic calibration evidence",
        samples=[sample],
    )


def release_actor(name):
    user = make_user(name)
    user.user_permissions.add(
        Permission.objects.get(codename="authorize_release", content_type__app_label="accounts")
    )
    return user


def test_full_release_freezes_evidence_and_rechecks_selected_qc(
    config, analyst, analyst2, reviewer, monkeypatch
):
    batch = start(config, analyst)
    record = curve(config, analyst2, batch)
    check = verification(config, analyst2, batch)
    run_qc(config, analyst, batch)
    revision = linked(config, analyst, batch, record, check)
    document = report(config, batch, analyst, reviewer)
    actor = release_actor("independent-calibration-releaser")
    assembled = assemble(document, actor=actor)
    assert assembled.releasable
    monkeypatch.setattr(
        reports, "render_pdf", lambda snapshot: b"%PDF-1.7\nSynthetic calibration test"
    )
    version = reports.release_report(
        actor=actor, report_id=document.pk, expected_fingerprint=assembled.fingerprint
    )
    frozen = ReportFile.objects.get(version=version).snapshot
    row = next(row for row in frozen["content"]["samples"][0]["results"] if row.get("calibration"))
    assert row["calibration"] == revision.calibration_evidence
    correct_check(config, analyst2, check)
    blocked = assemble(document, actor=actor)
    assert any("superseded" in message for message in blocked.blockers)
    with pytest.raises(ValidationError, match="superseded"):
        reports.release_report(
            actor=actor,
            report_id=document.pk,
            expected_fingerprint=blocked.fingerprint,
            reason="Synthetic amendment",
        )
    assert ReportVersion.objects.count() == 1
    assert ReportFile.objects.get(version=version).snapshot == frozen


def test_release_excludes_curve_and_verification_contributors(
    config, analyst, both, analyst2, reviewer
):
    batch = start(config, analyst)
    record = curve(config, both, batch)
    check = verification(config, analyst2, batch)
    run_qc(config, analyst, batch)
    linked(config, analyst, batch, record, check)
    document = report(config, batch, analyst, reviewer)
    for actor in (both, analyst2):
        actor.user_permissions.add(
            Permission.objects.get(codename="authorize_release", content_type__app_label="accounts")
        )
        evidence = assemble(document, actor=actor)
        assert any("calibration" in message for message in evidence.blockers)
        with pytest.raises(ValidationError, match="calibration"):
            reports.release_report(
                actor=actor, report_id=document.pk, expected_fingerprint=evidence.fingerprint
            )
    assert not ReportVersion.objects.exists()


def test_linking_calibration_does_not_replace_existing_batch_qc_gates(config, analyst, reviewer):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    check = verification(config, analyst, batch)
    revision = linked(config, analyst, batch, record, check)
    services.submit_result(actor=analyst, revision_id=revision.pk)
    with pytest.raises(ValidationError, match="Method blank|Laboratory control"):
        services.approve_result(actor=reviewer, revision_id=revision.pk)


def test_entry_audit_failure_rolls_back_new_evidence_and_supersession(config, analyst, monkeypatch):
    batch = start(config, analyst)
    original = entered(config, analyst, batch)
    record = curve(config, analyst, batch)

    def fail(**kwargs):
        raise RuntimeError("Synthetic result audit failure")

    monkeypatch.setattr(services, "record_event", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        linked(config, analyst, batch, record, expected_revision=original.number)
    original.refresh_from_db()
    original.result.refresh_from_db()
    assert original.status == "draft"
    assert original.result.latest_number == original.number
    assert original.result.revisions.count() == 1
    assert original.calibration_id is None


def test_release_rechecks_verification_corrected_while_pdf_renders(
    config, analyst, analyst2, reviewer, monkeypatch
):
    batch = start(config, analyst)
    record = curve(config, analyst2, batch)
    check = verification(config, analyst2, batch)
    run_qc(config, analyst, batch)
    linked(config, analyst, batch, record, check)
    document = report(config, batch, analyst, reviewer)
    actor = release_actor("calibration-recheck-releaser")
    preview = assemble(document, actor=actor)
    assert preview.releasable

    def render_then_correct(snapshot):
        correct_check(config, analyst2, check)
        return b"%PDF-1.7\nSynthetic calibration recheck test"

    monkeypatch.setattr(reports, "render_pdf", render_then_correct)
    with pytest.raises(ValidationError, match="superseded"):
        reports.release_report(
            actor=actor, report_id=document.pk, expected_fingerprint=preview.fingerprint
        )
    assert not ReportVersion.objects.exists()
    assert not ReportFile.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["unit", "definition_analyte", "analyte_name"])
def test_first_curve_serializes_catalog_identity_edits(
    config, analyst, admin_user, monkeypatch, change
):
    """The first evidence reference freezes an unused test before a catalog edit proceeds."""
    batch = start(config, analyst)
    analyte = Analyte.objects.create(name="Unused synthetic calibration analyte")
    definition = TestDefinition.objects.create(
        method=config["method"], analyte=analyte, matrix="water", unit="mg/L"
    )
    replacement = Analyte.objects.create(name="Replacement synthetic calibration analyte")
    writer_pid = []
    writer_started = threading.Event()
    original_points = calibration._points
    future = None

    def edit_catalog():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(cursor.fetchone()[0])
            writer_started.set()
            if change == "analyte_name":
                fresh = Analyte.objects.get(pk=analyte.pk)
                fresh.name = "Changed synthetic calibration analyte"
                save = catalog.save_analyte
            else:
                fresh = TestDefinition.objects.get(pk=definition.pk)
                if change == "unit":
                    fresh.unit = "ug/L"
                else:
                    fresh.analyte = replacement
                save = catalog.save_test_definition
            with pytest.raises(ValidationError, match="Locked"):
                save(actor=admin_user, instance=fresh, reason="Synthetic concurrent identity edit")
        finally:
            connections.close_all()

    def pause_after_identity_read(points):
        nonlocal future
        future = executor.submit(edit_catalog)
        assert writer_started.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [writer_pid[0]])
                blocked = cursor.fetchone()[0]
            if blocked:
                break
            if future.done():
                future.result()  # Surface a writer failure instead of timing out.
            time.sleep(0.01)
        assert blocked, "Catalog writer did not wait for the calibration definition lock."
        return original_points(points)

    monkeypatch.setattr(calibration, "_points", pause_after_identity_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        recorded = curve(config, analyst, batch, definition_id=definition.pk)
        future.result(timeout=10)
    definition.refresh_from_db()
    analyte.refresh_from_db()
    assert definition.unit == recorded.unit == "mg/L"
    assert definition.analyte_id == analyte.pk
    assert recorded.analyte_name == analyte.name == "Unused synthetic calibration analyte"
    assert catalog.definition_locks(definition)
    assert catalog.analyte_locks(analyte)


def test_resolve_rejects_frozen_analyte_identity_drift(config, analyst):
    batch = start(config, analyst)
    record = curve(config, analyst, batch)
    # Defensive check for out-of-service catalog changes: the same test ID is insufficient.
    analyte = config["defs"]["Lead"].analyte
    Analyte.objects.filter(pk=analyte.pk).update(name="Lead (different synthetic identity)")
    with pytest.raises(ValidationError, match="batch, test and unit"):
        linked(config, analyst, batch, record)
    assert not ResultRevision.objects.exists()
