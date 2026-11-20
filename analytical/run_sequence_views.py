"""Staff access to recorded instrument injection sequences and their corrections."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import require_role

from . import run_sequences, services
from .forms import add_service_errors
from .models import Batch, RunSequence
from .run_sequence_forms import RunSequenceForm
from .views import _render, _staff


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def run_sequence_create(request, pk):
    _staff(request)
    require_role(request.user, services.ANALYST)
    batch = get_object_or_404(Batch, pk=pk, kind=Batch.Kind.ANALYTICAL)
    return _record_sequence(request, batch)


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def run_sequence_correct(request, pk):
    _staff(request)
    require_role(request.user, services.ANALYST)
    previous = get_object_or_404(RunSequence.objects.select_related("batch"), pk=pk)
    if previous.batch.kind != Batch.Kind.ANALYTICAL:
        raise Http404
    return _record_sequence(request, previous.batch, previous=previous)


def _record_sequence(request, batch, *, previous=None):
    batch_url = reverse("analytical:batch-detail", args=[batch.pk])
    crumbs = [("Batches & QC", reverse("analytical:batch-list")), (batch.number, batch_url)]
    cancel_url = batch_url
    title = "Record injection sequence"
    note = "Transcribe the order the instrument ran from the source record."
    if previous is not None:
        title = "Correct injection sequence"
        note = "Record a complete replacement sequence. The prior version stays in history."
        cancel_url = reverse("analytical:run-sequence-detail", args=[previous.pk])
        crumbs.append((previous.reference, cancel_url))
    if (
        request.method != "POST"
        and previous is not None
        and RunSequence.objects.filter(previous=previous).exists()
    ):
        messages.warning(request, "Open the current version before correcting the sequence.")
        return redirect(cancel_url)

    form = RunSequenceForm(request.POST if request.method == "POST" else None, previous=previous)
    if request.method == "POST" and form.is_valid():
        try:
            sequence = run_sequences.record_sequence(
                actor=request.user,
                batch_id=batch.pk,
                previous_id=previous.pk if previous is not None else None,
                **form.cleaned_data,
            )
        except ValidationError as exc:
            add_service_errors(form, exc)
        else:
            messages.success(
                request,
                "Recorded injection sequence."
                if previous is None
                else "Recorded sequence correction.",
            )
            return redirect("analytical:run-sequence-detail", pk=sequence.pk)
    return _render(
        request,
        "form.html",
        {
            "page_title": title,
            "form": form,
            "crumbs": [*crumbs, (title, None)],
            "cancel_url": cancel_url,
            "submit_label": title,
            "config_note": note,
        },
    )


@login_required
@require_safe
def run_sequence_detail(request, pk):
    _staff(request)
    sequence = get_object_or_404(
        RunSequence.objects.select_related("batch", "created_by"),
        pk=pk,
        batch__kind=Batch.Kind.ANALYTICAL,
    )
    versions = list(
        sequence.batch.run_sequences.filter(reference=sequence.reference)
        .select_related("created_by")
        .defer("injections", "source_reference")
        .order_by("version", "pk")
    )
    current = versions[-1]
    kinds = dict(run_sequences.InjectionKind.choices)
    rows = run_sequences.sequence_rows(sequence.injections)
    for row in rows:
        row["kind_label"] = kinds[row["kind"]]
    return _render(
        request,
        "run_sequence_detail.html",
        {
            "page_title": sequence.reference,
            "sequence": sequence,
            "current": current,
            "is_current": sequence.pk == current.pk,
            "versions": versions,
            "rows": rows,
        },
    )
