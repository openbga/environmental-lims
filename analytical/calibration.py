"""Immutable instrument-reported calibration evidence and explicit result provenance.

These records do not fit curves or establish method acceptance. Callers resolving result
links hold the batch policy lock used by QC corrections and result entry.
"""

from datetime import datetime
from decimal import DecimalException

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import require_role
from catalog.models import TestDefinition
from core.audit import record_event

from .models import QCMeasurement, QCType, RunCalibration
from .parsing import MAX_TEXT_LENGTH, parse_decimal


def _identifier(value, field, *, optional=False):
    if optional and value in (None, ""):
        return None
    try:
        pk = int(value)
    except TypeError, ValueError, OverflowError:
        raise ValidationError({field: "Choose an exact integer record ID."}) from None
    if pk <= 0 or str(pk) != str(value).strip():
        raise ValidationError({field: "Choose an exact integer record ID."})
    return pk


def _text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError({field: f"Enter 1 to {limit} characters."})
    return value.strip()


def _points(points):
    if not isinstance(points, list) or not 1 <= len(points) <= 50:
        raise ValidationError({"points": "Record between 1 and 50 standard points."})
    cleaned = []
    for index, point in enumerate(points, 1):
        if not isinstance(point, dict) or set(point) != {"standard", "concentration", "response"}:
            raise ValidationError(
                {"points": f"Point {index} needs standard, concentration and response."}
            )
        try:
            standard = _text(point["standard"], "points", 120)
            for field in ("concentration", "response"):
                raw = point[field]
                if not isinstance(raw, str) or len(raw) > MAX_TEXT_LENGTH:
                    raise ValidationError("Supply bounded exact decimal text for each value.")
                value = parse_decimal(raw, field.capitalize())
                if field == "concentration" and value < 0:
                    raise ValidationError("Standard concentration cannot be negative.")
        except (ValidationError, DecimalException) as exc:
            message = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "Value cannot be stored exactly."
            )
            raise ValidationError({"points": f"Point {index}: {message}"}) from None
        # Preserve supplied scientific text, including significant trailing zeros.
        cleaned.append(
            {
                "standard": standard,
                "concentration": point["concentration"],
                "response": point["response"],
            }
        )
    return cleaned


def _curve_snapshot(curve):
    return {
        "id": curve.pk,
        "batch_id": curve.batch_id,
        "definition_id": curve.definition_id,
        "reference": curve.reference,
        "calibrated_at": curve.calibrated_at.isoformat(),
        "model_text": curve.model_text,
        "response_unit": curve.response_unit,
        "source_reference": curve.source_reference,
        "points": curve.points,
        "method_code": curve.method_code,
        "method_version": curve.method_version,
        "analyte_name": curve.analyte_name,
        "matrix": curve.matrix,
        "unit": curve.unit,
        "equipment_label": curve.equipment_label,
        "created_by_id": curve.created_by_id,
        "created_at": curve.created_at.isoformat(),
    }


@transaction.atomic
def record_calibration(
    *,
    actor,
    batch_id,
    definition_id,
    reference,
    calibrated_at,
    model_text,
    response_unit,
    source_reference,
    points,
):
    """Record supplied calibration evidence under the same locks as analytical work."""
    from .services import ANALYST, CLOCK_SKEW, lock_batch

    require_role(actor, ANALYST)
    batch_id = _identifier(batch_id, "batch_id")
    definition_id = _identifier(definition_id, "definition")
    batch = lock_batch(batch_id, definition_ids=[definition_id])
    # Catalog writers lock this row before checking whether its identity is in use.
    # Lock only the definition: save_analyte takes Analyte before TestDefinition.
    definition = (
        TestDefinition.objects.select_for_update(of=("self",)).filter(pk=definition_id).first()
    )
    if definition is not None:
        # Read joined identity after obtaining the lock, with a fresh statement snapshot.
        definition = TestDefinition.objects.select_related("method", "analyte").get(
            pk=definition.pk
        )
    if batch.kind != "analytical":
        raise ValidationError("Calibration evidence belongs to an analytical batch.")
    if (
        definition is None
        or definition.method_id != batch.method_id
        or definition.matrix != batch.matrix
        or not definition.is_active
        or not definition.method.is_active
    ):
        raise ValidationError(
            {"definition": "Choose an active test for this batch's method and matrix."}
        )
    if (
        not isinstance(calibrated_at, datetime)
        or timezone.is_naive(calibrated_at)
        or calibrated_at > timezone.now() + CLOCK_SKEW
    ):
        raise ValidationError(
            {"calibrated_at": "Record a calibration time with a time zone, not in the future."}
        )
    curve = RunCalibration(
        batch=batch,
        definition=definition,
        reference=_text(reference, "reference", 120),
        calibrated_at=calibrated_at,
        model_text=_text(model_text, "model_text", 1000),
        response_unit=_text(response_unit, "response_unit", 40),
        source_reference=_text(source_reference, "source_reference", 240),
        points=_points(points),
        method_code=definition.method.code,
        method_version=definition.method.version,
        analyte_name=definition.analyte.name,
        matrix=definition.matrix,
        unit=definition.unit,
        equipment_label=str(batch.equipment) if batch.equipment_id else "",
        created_by=actor,
    )
    if RunCalibration.objects.filter(batch=batch, reference=curve.reference).exists():
        raise ValidationError(
            {"reference": "A calibration with this reference already exists in this batch."}
        )
    curve.full_clean()
    curve.save()
    record_event(
        actor=actor, action="calibration.recorded", obj=curve, after=_curve_snapshot(curve)
    )
    return curve


def resolve_evidence(
    *,
    batch,
    definition,
    analysis_started_at,
    calibration_id=None,
    calibration_verification_id=None,
):
    """Resolve optional explicit links while the caller holds this analytical batch's lock."""
    curve_id = _identifier(calibration_id, "calibration_id", optional=True)
    check_id = _identifier(
        calibration_verification_id, "calibration_verification_id", optional=True
    )
    if curve_id is None:
        if check_id is not None:
            raise ValidationError(
                {"calibration_verification_id": "Choose the calibration for this verification."}
            )
        return {
            "calibration_id": None,
            "calibration_verification_id": None,
            "calibration_evidence": None,
        }
    curve = RunCalibration.objects.filter(pk=curve_id).first()
    if (
        curve is None
        or batch.kind != "analytical"
        or curve.batch_id != batch.pk
        or curve.definition_id != definition.pk
        or curve.unit != definition.unit
        or curve.matrix != definition.matrix
        or curve.method_code != definition.method.code
        or curve.method_version != definition.method.version
        or curve.analyte_name != definition.analyte.name
        or batch.matrix != definition.matrix
        or batch.method_id != definition.method_id
    ):
        raise ValidationError(
            {"calibration_id": "Choose calibration evidence for this batch, test and unit."}
        )
    if analysis_started_at is None or curve.calibrated_at > analysis_started_at:
        raise ValidationError(
            {
                "calibration_id": (
                    "Calibration must be recorded at or before the result's analysis start."
                )
            }
        )
    check_snapshot = None
    if check_id is not None:
        check = QCMeasurement.objects.select_related("qc_sample").filter(pk=check_id).first()
        if (
            check is None
            or not check.is_current
            or check.qc_sample.qc_type != QCType.CALIBRATION_VERIFICATION
            or check.qc_sample.batch_id != batch.pk
            or check.definition_id != definition.pk
            or check.unit != definition.unit
            or check.unit != curve.unit
            or check.analysis_batch_id not in (None, batch.pk)
        ):
            raise ValidationError(
                {
                    "calibration_verification_id": (
                        "Choose a current calibration-verification observation for this "
                        "batch, test and unit."
                    )
                }
            )
        if check.analysis_started_at < curve.calibrated_at:
            raise ValidationError(
                {
                    "calibration_verification_id": (
                        "Verification cannot precede the selected calibration."
                    )
                }
            )
        check_snapshot = {
            "id": check.pk,
            "qc_sample_id": check.qc_sample_id,
            "label": check.qc_sample.label,
            "qc_type": check.qc_sample.qc_type,
            "definition_id": check.definition_id,
            "analyte_name": check.analyte_name,
            "observed_text": check.observed_text,
            "kind": check.kind,
            "observed_value": None
            if check.observed_value is None
            else format(check.observed_value, "f"),
            "true_value": None if check.spike_value is None else format(check.spike_value, "f"),
            "unit": check.unit,
            "analysis_started_at": check.analysis_started_at.isoformat(),
            "entered_by_id": check.entered_by_id,
        }
    return {
        "calibration_id": curve.pk,
        "calibration_verification_id": check_id,
        "calibration_evidence": {
            "calibration": _curve_snapshot(curve),
            "verification": check_snapshot,
        },
    }


def calibration_blockers(revision):
    """A correction to a relied-on verification requires a new result revision."""
    if revision.calibration_verification_id and not revision.calibration_verification.is_current:
        return [
            "The selected calibration verification was superseded. "
            "Enter a new result revision with current evidence."
        ]
    return []


def calibration_contributors(revision):
    """Frozen evidence contributors cannot independently review or release this result."""
    evidence = revision.calibration_evidence or {}
    contributors = set()
    for section, field in (("calibration", "created_by_id"), ("verification", "entered_by_id")):
        if actor_id := (evidence.get(section) or {}).get(field):
            contributors.add(actor_id)
    return contributors
