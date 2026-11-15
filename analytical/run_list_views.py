"""Download the current analytical batch inventory without recording a state change."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET

from .run_lists import export_run_list


@login_required
@require_GET
def batch_run_list(request, pk):
    try:
        run_list = export_run_list(actor=request.user, batch_id=pk)
    except ValidationError as exc:
        messages.error(request, "Run list not exported. " + " ".join(exc.messages))
        return redirect("analytical:batch-detail", pk=pk)
    response = HttpResponse(run_list.content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{run_list.filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
