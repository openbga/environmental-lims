"""Staff pages for immutable instrument-reported calibration records."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from core.models import AuditEvent

from . import calibration, services
from .calibration_forms import CalibrationForm
from .forms import add_service_errors
from .models import Batch, RunCalibration
from .views import _grid, _paginate, _render, _staff


@login_required
def calibration_create(request, pk):
    _staff(request)
    services.require_role(request.user, services.ANALYST)
    batch = get_object_or_404(Batch.objects.select_related("method"), pk=pk)
    if batch.kind != "analytical":
        raise Http404
    form = CalibrationForm(request.POST if request.method == "POST" else None, batch=batch)
    if request.method == "POST" and form.is_valid():
        try:
            curve = calibration.record_calibration(
                actor=request.user, batch_id=batch.pk, **form.cleaned_data
            )
        except ValidationError as exc:
            if hasattr(exc, "error_dict") and "definition" in exc.error_dict:
                errors = dict(exc.error_dict)
                errors["definition_id"] = errors.pop("definition")
                exc = ValidationError(errors)
            add_service_errors(form, exc)
        else:
            messages.success(
                request, "Recorded calibration evidence. The record is kept unchanged."
            )
            return redirect("analytical:calibration-detail", pk=curve.pk)
    batch_url = reverse("analytical:batch-detail", args=[batch.pk])
    return _render(
        request,
        "form.html",
        {
            "page_title": "Record calibration",
            "form": form,
            "crumbs": [
                ("Batches & QC", reverse("analytical:batch-list")),
                (batch.number, batch_url),
                ("Record calibration", None),
            ],
            "cancel_url": batch_url,
            "submit_label": "Record calibration",
            "config_note": "Record instrument-reported evidence. The application does not fit the "
            "curve or evaluate method acceptance. To correct it, "
            "record a new calibration reference.",
        },
    )


@login_required
def calibration_detail(request, pk):
    _staff(request)
    curve = get_object_or_404(RunCalibration.objects.select_related("batch", "created_by"), pk=pk)
    revisions = curve.revisions.select_related("result__requested_test__sample").order_by(
        "-entered_at", "-pk"
    )
    page = _paginate(_grid(request), revisions)
    events = AuditEvent.objects.filter(
        object_type="analytical.runcalibration", object_id=str(curve.pk)
    ).select_related("actor")
    return _render(
        request,
        "calibration_detail.html",
        {
            "page_title": curve.reference,
            "curve": curve,
            "page_obj": page,
            "events": events,
        },
    )
