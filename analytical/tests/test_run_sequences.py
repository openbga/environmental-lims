import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction

from analytical import run_sequences, services
from analytical.models import Batch, QCSample, ResultRevision, RunSequence
from analytical.run_sequences import (
    MAX_INJECTIONS,
    record_sequence,
    sequence_rows,
    verification_intervals,
)
from core.models import AuditEvent
from laboratory.tests.test_concurrency import Hold, race

from .test_workflow import start


def injection(position=1, kind="sample", label="SYNTHETIC-1"):
    return {"position": position, "kind": kind, "label": label}


def record(actor, batch, **changes):
    data = {
        "reference": "Demo run 101",
        "source_reference": "Synthetic instrument log page 1",
        "injections": [injection()],
    }
    return record_sequence(actor=actor, batch_id=batch.pk, **(data | changes))


def test_display_uses_nearest_verifications_in_recorded_order():
    injections = [
        injection(1),
        injection(2, "verification", "Check A"),
        injection(3, "verification", "Check B"),
        injection(4, "standard"),
        injection(5),
        injection(6, "blank"),
        injection(7),
        injection(8, "verification", "Check A"),
        injection(9, "other"),
        injection(10),
    ]
    rows = sequence_rows(injections)
    assert rows == [
        {**row, "before": before, "after": after}
        for row, (before, after) in zip(
            injections,
            [
                (None, injections[1]),
                (None, None),
                (None, None),
                (None, None),
                (injections[2], injections[7]),
                (None, None),
                (injections[2], injections[7]),
                (None, None),
                (None, None),
                (injections[7], None),
            ],
            strict=True,
        )
    ]
    no_verification = injection()
    assert sequence_rows([no_verification]) == [{**no_verification, "before": None, "after": None}]


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (["sample", "blank", "sample"], [(None, None, 2)]),
        (["standard"], [(None, None, 0)]),
        (["verification"], []),
        (["verification", "verification"], [(1, 2, 0)]),
        (
            ["blank", "verification", "sample", "other", "sample", "verification", "standard"],
            [(None, 2, 0), (2, 6, 2), (6, None, 0)],
        ),
    ],
)
def test_verification_intervals_count_only_sample_positions(kinds, expected):
    # Every label is the same. Only the recorded position and type determine the intervals.
    injections = [injection(position, kind) for position, kind in enumerate(kinds, 1)]
    intervals = verification_intervals(injections)
    assert intervals == [
        {
            "earlier": injections[earlier - 1] if earlier else None,
            "later": injections[later - 1] if later else None,
            "sample_count": count,
        }
        for earlier, later, count in expected
    ]


@pytest.mark.parametrize(
    "injections",
    [
        [],
        (injection(),),
        [injection(position) for position in range(1, MAX_INJECTIONS + 2)],
        [injection(2), injection(1)],
        [injection(), injection(1)],
        [injection(), injection(3)],
        [injection(True)],
        [injection("1")],
        [injection(1.0)],
        [{"position": 1, "label": "SYNTHETIC"}],
        [dict(injection(), sample_id=1)],
        [injection(kind="lcs")],
        [injection(kind=[])],
        [injection(label="   ")],
        [injection(label="S" * 121)],
        [injection(label="A\tB")],
        [injection(label="A\x00B")],
        [injection(label="A\ud800B")],
    ],
)
def test_injection_transcription_requires_bounded_explicit_rows(injections):
    with pytest.raises(ValidationError) as error:
        run_sequences._injections(injections)
    assert set(error.value.message_dict) == {"injections"}


@pytest.mark.django_db
def test_records_and_corrects_closed_batch_evidence_with_audit(lab, analyst, analyst2):
    batch = start(lab, analyst)
    services.close_batch(actor=analyst, batch_id=batch.pk)
    label = lab["samples"][0].lab_id
    injections = [injection(1, "sample", label), injection(2, "sample", label)]
    first = record(analyst, batch, injections=injections)
    changed = [injection(1, "verification", "  Vérification A  "), injection(2, "sample", label)]
    second = record(
        analyst2,
        batch,
        previous_id=first.pk,
        injections=changed,
        source_reference="Synthetic instrument log page 2",
        reason="Transposed injection labels",
    )
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.version, first.previous, first.injections, first.successor) == (
        1,
        None,
        injections,
        second,
    )
    assert (second.version, second.previous, second.injections, second.created_by) == (
        2,
        first,
        changed,
        analyst2,
    )
    assert first.reference == second.reference == "Demo run 101"
    assert first.source_reference == "Synthetic instrument log page 1"
    assert second.source_reference == "Synthetic instrument log page 2"
    assert second.reason == "Transposed injection labels"
    assert batch.run_sequences.count() == 2
    assert not ResultRevision.objects.exists()
    assert not QCSample.objects.exists()

    opened = AuditEvent.objects.get(action="sequence.recorded", object_id=str(first.pk))
    corrected = AuditEvent.objects.get(action="sequence.corrected", object_id=str(second.pk))
    assert (opened.actor, opened.object_type, opened.before) == (
        analyst,
        "analytical.runsequence",
        {},
    )
    assert opened.after == {
        "id": first.pk,
        "batch_id": batch.pk,
        "reference": "Demo run 101",
        "version": 1,
        "injection_count": 2,
    }
    assert (corrected.actor, corrected.reason, corrected.before) == (
        analyst2,
        "Transposed injection labels",
        opened.after,
    )
    assert corrected.after == {
        "id": second.pk,
        "batch_id": batch.pk,
        "reference": "Demo run 101",
        "version": 2,
        "injection_count": 2,
    }


@pytest.mark.django_db
def test_allows_repeated_labels_and_runs_without_samples(lab, analyst):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    other_batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    injections = [
        injection(position, "standard", "Repeated label")
        for position in range(1, MAX_INJECTIONS + 1)
    ]
    first = record(analyst, batch, injections=injections)
    second = record(analyst, other_batch, injections=injections)
    first.refresh_from_db()
    assert first.injections == injections
    assert second.reference == first.reference


@pytest.mark.django_db
def test_write_requires_analyst_and_analytical_batch(lab, analyst, reviewer):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    preparation = services.create_batch(
        actor=analyst, method=lab["method"], matrix="water", kind=Batch.Kind.PREPARATION
    )
    for actor in (reviewer, AnonymousUser()):
        with pytest.raises(PermissionDenied):
            record(actor, batch)
    with pytest.raises(ValidationError, match="analytical batch"):
        record(analyst, preparation)
    assert not RunSequence.objects.exists()


@pytest.mark.django_db
def test_rejects_duplicate_reference_and_invalid_correction_targets(lab, analyst):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    other_batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    first = record(analyst, batch)
    wrong_batch = record(analyst, other_batch)

    with pytest.raises(ValidationError, match="already has a run"):
        record(analyst, batch, reference=" Demo run 101 ")
    with pytest.raises(ValidationError, match="original run reference"):
        record(analyst, batch, previous_id=first.pk, reference="Different run", reason="Correction")
    with pytest.raises(ValidationError, match="from this batch"):
        record(analyst, batch, previous_id=wrong_batch.pk, reason="Correction")
    with pytest.raises(ValidationError, match="exact integer"):
        record(analyst, batch, previous_id=float(first.pk), reason="Correction")
    with pytest.raises(ValidationError, match="reason"):
        record(analyst, batch, previous_id=first.pk, reason="   ")
    with pytest.raises(ValidationError, match="source_reference"):
        record(analyst, batch, reference="Other run", source_reference=" ")
    assert RunSequence.objects.count() == 2


@pytest.mark.django_db
def test_audit_failure_leaves_current_sequence_intact(lab, analyst, monkeypatch):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    first = record(analyst, batch)

    def fail(**kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(run_sequences, "record_event", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        record(analyst, batch, previous_id=first.pk, reason="Correction")
    assert list(batch.run_sequences.all()) == [first]
    assert not AuditEvent.objects.filter(action="sequence.corrected").exists()


@pytest.mark.django_db
def test_database_rejects_bulk_updates_and_deletes(lab, analyst):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    first = record(analyst, batch)
    records = RunSequence.objects.filter(pk=first.pk)
    with pytest.raises(DatabaseError, match="Historical records"), transaction.atomic():
        records.update(source_reference="Replacement source")
    with pytest.raises(DatabaseError, match="Historical records"), transaction.atomic():
        records.delete()
    first.refresh_from_db()
    assert first.source_reference == "Synthetic instrument log page 1"


@pytest.mark.django_db(transaction=True)
def test_competing_corrections_recheck_the_current_version(lab, analyst, analyst2, monkeypatch):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    original = record(analyst, batch)
    first, second = race(
        Hold(monkeypatch, run_sequences),
        lambda: record(analyst, batch, previous_id=original.pk, reason="First correction"),
        lambda: record(analyst2, batch, previous_id=original.pk, reason="Second correction"),
    )
    assert isinstance(first, RunSequence) and first.previous_id == original.pk
    assert isinstance(second, ValidationError) and "newer version" in str(second), second
    assert AuditEvent.objects.filter(action="sequence.corrected").count() == 1
    third = record(analyst2, batch, previous_id=first.pk, reason="Next correction")
    assert (third.version, third.previous) == (3, first)


@pytest.mark.django_db(transaction=True)
def test_competing_initial_records_check_reference_under_lock(lab, analyst, analyst2, monkeypatch):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    first, second = race(
        Hold(monkeypatch, run_sequences),
        lambda: record(analyst, batch),
        lambda: record(analyst2, batch),
    )
    assert isinstance(first, RunSequence) and first.version == 1
    assert isinstance(second, ValidationError) and "already has a run" in str(second), second
    assert AuditEvent.objects.filter(action="sequence.recorded").count() == 1
