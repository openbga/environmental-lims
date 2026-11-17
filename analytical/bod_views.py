"""The bottle bench uses the ordinary result entry transaction and provenance machinery."""

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect

from accounts.roles import require_role, require_staff

from . import calculations, services, worksheets
from .bod_forms import BottleEntryForm, BottleFormSet
from .forms import add_service_errors
from .models import Result, ResultWorksheet
from .preparation import aliquot_choices
from .qc import trim
from .views import _local_value, _render


@login_required
def result_bottles(request, pk):
    require_staff(request.user)
    require_role(request.user, services.ANALYST)
    result = get_object_or_404(
        Result.objects.select_related(
            "requested_test__sample",
            "requested_test__definition__analyte",
            "requested_test__definition__method",
            "batch",
        ),
        pk=pk,
    )
    definition = result.requested_test.definition
    rule = worksheets.active_rules([definition.pk]).get(definition.pk)
    current = calculations.current(rule.calculation) if rule else None
    calculation = current
    if request.method == "POST" and not (current and current.supports_bottles):
        calculation = worksheets.shown(request.POST.get("calculation_token"))
    if not calculation or not calculation.supports_bottles:
        raise Http404("This test does not use a BOD5 bottle worksheet.")
    latest = services.latest_revision(result)
    can_edit = bool(result.batch_id) and (not latest or latest.status != "submitted")
    sheet = ResultWorksheet.objects.filter(revision=latest).first() if latest else None
    rows = sheet.bottle_data if sheet and sheet.bottle_data else [{}]
    initial_rows = [
        {**row, "selected": "include" if row.get("selected") else "exclude"} if row else {}
        for row in rows
    ]
    initial = {
        "expected_revision": result.latest_number,
        "prepared_aliquot_id": latest.prepared_aliquot_id if latest else None,
        "dilution_factor": trim(latest.dilution_factor) if latest else "1",
        "reporting_limit_applied": trim(latest.reporting_limit_applied)
        if latest
        else ("" if definition.reporting_limit is None else trim(definition.reporting_limit)),
        "analysis_started_at": _local_value(latest.analysis_started_at) if latest else "",
        "comments": latest.comments if latest else "",
        "qualifiers": " ".join(q["code"] for q in latest.qualifiers) if latest else "",
    }
    data = request.POST.copy() if request.method == "POST" else None
    form = BottleEntryForm(data, initial=initial)
    form.fields["prepared_aliquot_id"].widget.choices = aliquot_choices([result.requested_test_id])[
        result.requested_test_id
    ]
    for name in ("result_basis", "percent_solids", "solids_reference"):
        del form.fields[name]
    form.use_calculation(calculation, definition)
    formset = BottleFormSet(data, initial=initial_rows, prefix="bottles")
    action = request.POST.get("action", "") if request.method == "POST" else ""
    changing_rows = action == "add" or action.startswith("remove-")
    if changing_rows and can_edit and formset.management_form.is_valid():
        total = formset.total_form_count()
        removed = re.fullmatch(r"remove-([0-9]{1,2})", action)
        if removed and 1 < total <= 12 and int(removed[1]) < total:
            index = int(removed[1])
            updated = data.copy()
            for key in list(updated):
                if re.fullmatch(r"bottles-[0-9]{1,2}-.+", key):
                    del updated[key]
            for key in data:
                match = re.fullmatch(r"bottles-([0-9]{1,2})-(.+)", key)
                if match and (old := int(match[1])) < total and old != index:
                    new = old - (old > index)
                    updated.setlist(f"bottles-{new}-{match[2]}", data.getlist(key))
            updated["bottles-TOTAL_FORMS"] = str(total - 1)
            updated["bottles-INITIAL_FORMS"] = str(
                max(0, formset.initial_form_count() - (index < formset.initial_form_count()))
            )
            data = updated
            form = BottleEntryForm(data, initial=initial)
            form.fields["prepared_aliquot_id"].widget.choices = aliquot_choices(
                [result.requested_test_id]
            )[result.requested_test_id]
            for name in ("result_basis", "percent_solids", "solids_reference"):
                del form.fields[name]
            form.use_calculation(calculation, definition)
            formset = BottleFormSet(data, initial=initial_rows, prefix="bottles")
        elif action == "add" and 1 <= total < 12:
            data["bottles-TOTAL_FORMS"] = str(total + 1)
            formset = BottleFormSet(data, initial=initial_rows, prefix="bottles")
        else:
            form.add_error(None, "Keep from 1 to 12 bottle rows. No row was changed.")
    elif request.method == "POST":
        valid = form.is_valid() & formset.is_valid()
        if not can_edit:
            form.add_error(
                None, "This result cannot be edited now. Submitted results must be returned first."
            )
        elif valid:
            values = form.cleaned_data
            try:
                services.enter_result(
                    actor=request.user,
                    result_id=result.pk,
                    supplied_text="",
                    reporting_limit_applied=values["reporting_limit_applied"],
                    dilution_factor=values["dilution_factor"],
                    analysis_started_at=values["analysis_started_at"],
                    qualifier_codes=form.qualifier_codes(),
                    comments=values["comments"],
                    reason=values["reason"],
                    prepared_aliquot_id=values.get("prepared_aliquot_id"),
                    report_nd=values.get("report_nd", False),
                    readings={},
                    calculation_token=values["calculation_token"],
                    expected_revision=values["expected_revision"],
                    bottle_data=formset.bottle_data,
                )
            except ValidationError as exc:
                add_service_errors(form, exc)
            else:
                messages.success(request, "BOD5 bottle worksheet saved as a new result revision.")
                return redirect("analytical:result-detail", pk=result.pk)
    return _render(
        request,
        "result_bottles.html",
        {
            "page_title": "Record BOD5 bottles",
            "result": result,
            "batch": result.batch,
            "latest": latest,
            "form": form,
            "formset": formset,
            "can_edit": can_edit,
            "calculation": calculation,
        },
    )
