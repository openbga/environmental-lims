"""Download the current analytical batch inventory in standard or mapped columns."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import require_role

from .models import Batch
from .run_list_forms import RunListTemplateForm
from .run_lists import example_run_list_template, export_run_list


def _download(content, filename):
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _export_batch(request, pk):
    require_role(request.user, "analyst")
    return get_object_or_404(
        Batch.objects.select_related("method"), pk=pk, kind=Batch.Kind.ANALYTICAL
    )


@login_required
@require_GET
def batch_run_list(request, pk):
    try:
        run_list = export_run_list(actor=request.user, batch_id=pk)
    except ValidationError as exc:
        messages.error(request, "Run list not exported. " + " ".join(exc.messages))
        return redirect("analytical:batch-detail", pk=pk)
    return _download(run_list.content, run_list.filename)


@login_required
@require_http_methods(["GET", "POST"])
def batch_run_list_template(request, pk):
    batch = _export_batch(request, pk)
    if request.method == "POST":
        form = RunListTemplateForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                run_list = export_run_list(
                    actor=request.user,
                    batch_id=batch.pk,
                    columns=form.cleaned_data["template_file"],
                )
            except ValidationError as exc:
                form.add_error(None, "Run list not exported. " + " ".join(exc.messages))
            else:
                return _download(run_list.content, run_list.filename)
    else:
        form = RunListTemplateForm()
    return render(
        request,
        "analytical/run_list_template.html",
        {"page_title": "Export run list with a template", "batch": batch, "form": form},
    )


@login_required
@require_GET
def batch_run_list_template_example(request, pk):
    _export_batch(request, pk)
    return _download(example_run_list_template(), "run-list-template.csv")
