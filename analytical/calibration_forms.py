"""Bounded, exact-text input for instrument-reported calibration evidence."""

import csv
import io

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from catalog.models import TestDefinition

from .forms import local_datetime
from .models import QCMeasurement, QCType, RunCalibration


class CalibrationTestChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj} · {obj.unit}"


class CalibrationForm(forms.Form):
    definition_id = CalibrationTestChoice(queryset=TestDefinition.objects.none(), label="Test")
    reference = forms.CharField(label="Calibration reference", max_length=120)
    calibrated_at = local_datetime("Calibration time")
    model_text = forms.CharField(
        label="Instrument-reported model",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Record the equation, fit and weighting as supplied by the instrument.",
    )
    response_unit = forms.CharField(label="Response unit", max_length=40)
    source_reference = forms.CharField(label="Source reference", max_length=240)
    points = forms.CharField(
        label="Standard points (CSV)",
        max_length=20000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}),
        help_text="Header: standard,concentration,response. Enter 1–50 rows. Concentrations use "
        "the selected test unit; responses use the response unit. "
        "Decimal text is kept as supplied.",
    )

    def __init__(self, *args, batch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition_id"].queryset = (
            TestDefinition.objects.filter(
                method_id=batch.method_id, matrix=batch.matrix, is_active=True
            )
            .select_related("analyte", "method")
            .order_by("analyte__name", "pk")
        )
        self.fields["definition_id"].empty_label = "Choose test"

    def clean_definition_id(self):
        return self.cleaned_data["definition_id"].pk

    def clean_points(self):
        reader = csv.reader(io.StringIO(self.cleaned_data["points"]), strict=True)
        try:
            if next(reader, None) != ["standard", "concentration", "response"]:
                raise ValidationError("Use the exact header standard,concentration,response.")
            points = []
            for row in reader:
                if len(row) != 3:
                    raise ValidationError("Each standard row needs exactly three columns.")
                points.append(
                    dict(zip(("standard", "concentration", "response"), row, strict=True))
                )
                if len(points) > 50:
                    raise ValidationError("Enter at most 50 standard points.")
        except csv.Error as exc:
            raise ValidationError("Enter valid comma-separated standard points.") from exc
        if not points:
            raise ValidationError("Enter at least one standard point.")
        return points


class CalibrationFitForm(forms.Form):
    weighting = forms.ChoiceField(
        label="Weighting",
        choices=[
            ("", "Choose weighting"),
            ("equal", "Unweighted (equal weights)"),
            ("inverse", "1/x (inverse concentration)"),
            ("inverse_square", "1/x² (inverse concentration squared)"),
        ],
        error_messages={"required": "Choose a weighting to calculate the fit."},
    )


def evidence_choices(form, *, batch_id, definition):
    """Inline choices belong only to this run and test, never the global evidence catalog."""
    curves = RunCalibration.objects.filter(
        batch_id=batch_id,
        definition=definition,
        unit=definition.unit,
        matrix=definition.matrix,
        method_code=definition.method.code,
        method_version=definition.method.version,
    )
    checks = (
        QCMeasurement.objects.filter(
            qc_sample__batch_id=batch_id,
            qc_sample__qc_type=QCType.CALIBRATION_VERIFICATION,
            definition=definition,
            unit=definition.unit,
            is_current=True,
        )
        .select_related("qc_sample")
        .order_by("-analysis_started_at", "-pk")
    )
    form.fields["calibration_id"].choices = [
        ("", "Not linked"),
        *[
            (
                str(c.pk),
                f"{c.reference} · "
                f"{timezone.localtime(c.calibrated_at):%Y-%m-%d %H:%M %Z} · {c.unit}",
            )
            for c in curves
        ],
    ]
    form.fields["calibration_verification_id"].choices = [
        ("", "Not linked"),
        *[
            (str(m.pk), f"{m.qc_sample.label} · observation {m.pk} · {m.observed_text} {m.unit}")
            for m in checks
        ],
    ]
