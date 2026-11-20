"""Record and display the transcribed injection order for an instrument run."""

from django.core.exceptions import ValidationError
from django.db import models, transaction

from accounts.roles import require_role
from core.audit import record_event

from .models import Batch, RunSequence

MAX_INJECTIONS = 200


class InjectionKind(models.TextChoices):
    SAMPLE = "sample", "Sample"
    VERIFICATION = "verification", "Verification"
    STANDARD = "standard", "Standard"
    BLANK = "blank", "Blank"
    OTHER = "other", "Other"


def _check_id(value, field):
    if type(value) is not int or value <= 0:
        raise ValidationError({field: "Choose an exact integer record ID."})


def _text(value, field, limit, *, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        minimum = 1 if required else 0
        raise ValidationError({field: f"Enter {minimum} to {limit} characters."})
    if "\x00" in value:
        raise ValidationError({field: "Remove null characters."})
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError({field: "Use valid Unicode text."}) from None
    return value.strip()


def _injections(injections):
    if not isinstance(injections, list) or not 1 <= len(injections) <= MAX_INJECTIONS:
        raise ValidationError({"injections": f"Record 1 to {MAX_INJECTIONS} injections."})
    cleaned = []
    kinds = InjectionKind.values
    for position, row in enumerate(injections, 1):
        if not isinstance(row, dict) or set(row) != {"position", "kind", "label"}:
            raise ValidationError(
                {"injections": f"Injection {position} needs a position, type and instrument label."}
            )
        if type(row["position"]) is not int or row["position"] != position:
            raise ValidationError({"injections": f"Expected consecutive position {position}."})
        if row["kind"] not in kinds:
            raise ValidationError({"injections": f"Choose a valid type for injection {position}."})
        label = row["label"]
        if not isinstance(label, str) or not 1 <= len(label) <= 120 or not label.strip():
            raise ValidationError(
                {"injections": f"Enter an instrument label of 1 to 120 characters for {position}."}
            )
        if not label.isprintable():
            raise ValidationError(
                {"injections": f"Use only printable characters in label {position}."}
            )
        cleaned.append(row.copy())
    return cleaned


def _audit_reference(sequence):
    return {
        "id": sequence.pk,
        "batch_id": sequence.batch_id,
        "reference": sequence.reference,
        "version": sequence.version,
        "injection_count": len(sequence.injections),
    }


@transaction.atomic
def record_sequence(
    *, actor, batch_id, reference, source_reference, injections, previous_id=None, reason=""
):
    """Append a run's first sequence or a correction of its current version."""
    require_role(actor, "analyst")
    _check_id(batch_id, "batch_id")
    reference = _text(reference, "reference", 120)
    source_reference = _text(source_reference, "source_reference", 240)
    injections = _injections(injections)
    reason = _text(reason, "reason", 500, required=previous_id is not None)
    # All recordings for this batch use the same lock before checking the current version.
    batch = Batch.objects.select_for_update().filter(pk=batch_id).first()
    if batch is None:
        raise ValidationError({"batch_id": "Choose an existing batch."})
    if batch.kind != Batch.Kind.ANALYTICAL:
        raise ValidationError("Injection sequences belong to an analytical batch.")
    sequences = batch.run_sequences.all()
    previous = None
    if previous_id is None:
        if reason:
            raise ValidationError({"reason": "A reason is only recorded with a correction."})
        if sequences.filter(reference=reference).exists():
            raise ValidationError(
                {"reference": "This batch already has a run with this reference."}
            )
    else:
        _check_id(previous_id, "previous_id")
        previous = sequences.filter(pk=previous_id).first()
        if previous is None:
            raise ValidationError({"previous_id": "Choose a sequence version from this batch."})
        if previous.reference != reference:
            raise ValidationError({"reference": "A correction keeps the original run reference."})
        if sequences.filter(previous=previous).exists():
            raise ValidationError(
                {"previous_id": "A newer version exists. Reload and correct that version."}
            )

    sequence = RunSequence.objects.create(
        batch=batch,
        reference=reference,
        source_reference=source_reference,
        version=1 if previous is None else previous.version + 1,
        previous=previous,
        reason=reason,
        injections=injections,
        created_by=actor,
    )
    record_event(
        actor=actor,
        action="sequence.recorded" if previous is None else "sequence.corrected",
        obj=sequence,
        before=None if previous is None else _audit_reference(previous),
        after=_audit_reference(sequence),
        reason=reason,
    )
    return sequence


def sequence_rows(injections):
    """Show the nearest recorded verification on each side of each sample."""
    verifications = (row for row in injections if row["kind"] == InjectionKind.VERIFICATION)
    before, after = None, next(verifications, None)
    rows = []
    for row in injections:
        if row["kind"] == InjectionKind.VERIFICATION:
            before, after = after, next(verifications, None)
        is_sample = row["kind"] == InjectionKind.SAMPLE
        rows.append(
            {**row, "before": before if is_sample else None, "after": after if is_sample else None}
        )
    return rows
