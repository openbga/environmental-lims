"""PM/admin-only pages for internal draft invoices."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime

from receipt.models import Order

from . import invoices
from .forms import apply_errors
from .invoice_forms import (
    InvoiceCancelForm,
    InvoiceCreateForm,
    InvoiceEditForm,
    InvoiceFilter,
    InvoiceLineFormSet,
)
from .lists import grid_response
from .models import InvoiceDraft
from .permissions import PRICING_ROLES, staff_role_required
from .selectors import history


@staff_role_required(*PRICING_ROLES)
def invoice_list(request):
    def refine(qs, data):
        return qs.filter(status=data["status"]) if data.get("status") else qs

    return grid_response(
        request,
        queryset=InvoiceDraft.objects.all(),
        columns={
            "number": "pk",
            "client": "client_name",
            "order": "order_reference",
            "status": "status",
            "total": "total",
            "created": "created_at",
        },
        default_sort="-number",
        form_class=InvoiceFilter,
        search_fields=(
            "number",
            "client_name",
            "account_code",
            "order_reference",
            "project_name",
            "po_number",
        ),
        refine=refine,
        template="commercial/invoice_list.html",
        title="Draft invoices",
    )


@staff_role_required(*PRICING_ROLES)
def invoice_create(request, order_pk):
    order = get_object_or_404(Order.objects.select_related("project__client"), pk=order_pk)
    tests = invoices.source_tests(order)
    form = InvoiceCreateForm(request.POST or None, order=order, tests=tests)
    active_draft = order.invoice_drafts.filter(status=InvoiceDraft.Status.DRAFT).first()
    blocked = ""
    if active_draft:
        blocked = "This order already has an active draft invoice."
    elif order.is_cancelled:
        blocked = "This chain of custody is cancelled."
    elif not tests:
        blocked = "Receive samples with requested tests before creating a draft invoice."
    elif len(tests) > invoices.MAX_LINES:
        blocked = f"A draft invoice may have at most {invoices.MAX_LINES} received tests."
    if request.method == "POST" and form.is_valid():
        try:
            draft = invoices.create_invoice_draft(
                actor=request.user,
                order_id=order.pk,
                expected_source_state=form.cleaned_data["expected_source_state"],
                price_list_id=(
                    form.cleaned_data["price_list"].pk if form.cleaned_data["price_list"] else None
                ),
                quote_version_id=(
                    form.cleaned_data["quote_version"].pk
                    if form.cleaned_data["quote_version"]
                    else None
                ),
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Internal draft invoice created.")
            return redirect("commercial:invoice-detail", pk=draft.pk)
    return render(
        request,
        "commercial/invoice_create.html",
        {
            "page_title": "New draft invoice",
            "order": order,
            "tests": tests,
            "form": form,
            "blocked": blocked,
            "active_draft": active_draft,
        },
    )


def _draft(pk):
    return get_object_or_404(
        InvoiceDraft.objects.select_related(
            "order", "created_by", "cancelled_by", "quote_version__quote"
        ),
        pk=pk,
    )


@staff_role_required(*PRICING_ROLES)
def invoice_detail(request, pk):
    draft = _draft(pk)
    return render(
        request,
        "commercial/invoice_detail.html",
        {
            "page_title": draft.number,
            "draft": draft,
            "quote_accepted_at": (
                parse_datetime(draft.quote_snapshot.get("accepted_at") or "")
                if draft.quote_snapshot
                else None
            ),
            "lines": draft.lines.select_related("source_quote_line"),
            "history": history(draft),
        },
    )


@staff_role_required(*PRICING_ROLES)
def invoice_edit(request, pk):
    draft = _draft(pk)
    if draft.status != InvoiceDraft.Status.DRAFT:
        messages.error(request, "A cancelled draft cannot be changed.")
        return redirect("commercial:invoice-detail", pk=pk)
    lines = list(draft.lines.select_related("source_quote_line"))
    form = InvoiceEditForm(request.POST or None, draft=draft)
    formset = InvoiceLineFormSet(
        request.POST or None,
        source_lines=lines,
        prefix="lines",
        initial=[
            {
                "id": line.pk,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
            }
            for line in lines
        ],
    )
    if request.method == "POST" and (form.is_valid() & formset.is_valid()):
        try:
            invoices.save_invoice_draft(
                actor=request.user,
                draft_id=draft.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                billing={name: form.cleaned_data[name] for name in invoices.BILLING_FIELDS},
                lines=[row.cleaned_data for row in formset],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Draft saved; prior values retained in history.")
            return redirect("commercial:invoice-detail", pk=pk)
    return render(
        request,
        "commercial/invoice_edit.html",
        {
            "page_title": f"Edit {draft.number}",
            "draft": draft,
            "form": form,
            "formset": formset,
        },
    )


@staff_role_required(*PRICING_ROLES)
def invoice_cancel(request, pk):
    draft = _draft(pk)
    if request.method == "GET" and draft.status == InvoiceDraft.Status.CANCELLED:
        messages.info(request, "This draft is already cancelled.")
        return redirect("commercial:invoice-detail", pk=pk)
    form = InvoiceCancelForm(request.POST or None, initial={"expected_revision": draft.revision})
    if request.method == "POST" and form.is_valid():
        try:
            invoices.cancel_invoice_draft(actor=request.user, draft_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Draft cancelled; its snapshot and history are retained.")
            return redirect("commercial:invoice-detail", pk=pk)
    return render(
        request,
        "commercial/invoice_cancel.html",
        {
            "page_title": "Cancel draft invoice",
            "draft": draft,
            "form": form,
        },
    )
