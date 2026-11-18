"""PM/admin-only invoice preparation, issuance and file delivery."""

import hashlib
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_http_methods, require_safe

from core.models import Lab
from receipt.models import Order

from . import invoices
from .forms import apply_errors
from .invoice_csv import export_invoice_csv
from .invoice_forms import (
    InvoiceAddLineForm,
    InvoiceCancelForm,
    InvoiceCreateForm,
    InvoiceEditForm,
    InvoiceFilter,
    InvoiceIssueForm,
    InvoiceLineFormSet,
    InvoiceVoidForm,
)
from .invoice_pdf import InvoiceRenderError
from .lists import grid_response
from .models import InvoiceDraft, IssuedInvoice
from .permissions import PRICING_ROLES, staff_role_required
from .selectors import history

log = logging.getLogger(__name__)


def _with_issue_metadata(queryset):
    return queryset.select_related("issued_invoice").defer(
        "issued_invoice__pdf", "issued_invoice__snapshot", "issued_invoice__sha256"
    )


@staff_role_required(*PRICING_ROLES)
def invoice_list(request):
    def refine(qs, data):
        if data.get("status"):
            qs = qs.filter(status=data["status"])
        if data.get("due_from"):
            qs = qs.filter(due_date__gte=data["due_from"])
        if data.get("due_through"):
            qs = qs.filter(due_date__lte=data["due_through"])
        return qs

    return grid_response(
        request,
        queryset=_with_issue_metadata(InvoiceDraft.objects.all()),
        columns={
            "number": "pk",
            "client": "client_name",
            "order": "order_reference",
            "status": "status",
            "total": "total",
            "created": "created_at",
            "due_date": "due_date",
        },
        default_sort="-number",
        form_class=InvoiceFilter,
        search_fields=(
            "number",
            "issued_invoice__number",
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
    active_invoice = (
        order.invoice_drafts.filter(
            status__in=[InvoiceDraft.Status.DRAFT, InvoiceDraft.Status.ISSUED]
        )
        .select_related("issued_invoice")
        .only("number", "status", "issued_invoice__number")
        .first()
    )
    blocked = ""
    if active_invoice and active_invoice.status == InvoiceDraft.Status.ISSUED:
        blocked = "This order already has an issued invoice."
    elif active_invoice:
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
            "active_invoice": active_invoice,
        },
    )


def _draft(pk):
    return get_object_or_404(
        _with_issue_metadata(
            InvoiceDraft.objects.select_related(
                "order",
                "created_by",
                "cancelled_by",
                "quote_version__quote",
                "issued_invoice__issued_by",
            )
        ),
        pk=pk,
    )


def _issue_blocked(draft):
    if draft.order.is_cancelled:
        return "This chain of custody is cancelled."
    if not Lab.objects.filter(pk=1).exists():
        return "Set up the laboratory profile before issuing an invoice."
    return ""


@staff_role_required(*PRICING_ROLES)
def invoice_detail(request, pk):
    draft = _draft(pk)
    lines = list(draft.lines.select_related("source_quote_line"))
    issued = getattr(draft, "issued_invoice", None)
    return render(
        request,
        "commercial/invoice_detail.html",
        {
            "page_title": issued.number if issued else draft.number,
            "draft": draft,
            "issued": issued,
            "issue_blocked": _issue_blocked(draft)
            if draft.status == InvoiceDraft.Status.DRAFT
            else "",
            "quote_accepted_at": (
                parse_datetime(draft.quote_snapshot.get("accepted_at") or "")
                if draft.quote_snapshot
                else None
            ),
            "lines": lines,
            "line_limit_reached": len(lines) >= invoices.MAX_LINES,
            "history": history(draft),
        },
    )


@staff_role_required(*PRICING_ROLES)
def invoice_edit(request, pk):
    draft = _draft(pk)
    if draft.status != InvoiceDraft.Status.DRAFT:
        messages.error(request, "Only an active draft invoice can be changed.")
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
                due_date=form.cleaned_data["due_date"],
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
def invoice_add_line(request, pk):
    draft = _draft(pk)
    if draft.status in (InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED) or (
        request.method == "GET" and draft.status == InvoiceDraft.Status.CANCELLED
    ):
        messages.error(request, "Only an active draft invoice can be changed.")
        return redirect("commercial:invoice-detail", pk=pk)
    form = InvoiceAddLineForm(request.POST or None, initial={"expected_revision": draft.revision})
    if request.method == "POST" and form.is_valid():
        try:
            invoices.add_invoice_draft_line(actor=request.user, draft_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(
                request, "Fee, discount or tax added; prior values retained in history."
            )
            return redirect("commercial:invoice-detail", pk=pk)
    return render(
        request,
        "commercial/invoice_add_line.html",
        {"page_title": "Add fee, discount or tax", "draft": draft, "form": form},
    )


@staff_role_required(*PRICING_ROLES)
def invoice_cancel(request, pk):
    draft = _draft(pk)
    if request.method == "GET" and draft.status == InvoiceDraft.Status.CANCELLED:
        messages.info(request, "This draft is already cancelled.")
        return redirect("commercial:invoice-detail", pk=pk)
    if draft.status in (InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED):
        messages.error(request, "Only an active draft invoice can be cancelled.")
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


@staff_role_required(*PRICING_ROLES)
@require_http_methods(["GET", "POST"])
def invoice_issue(request, pk):
    draft = _draft(pk)
    if draft.status != InvoiceDraft.Status.DRAFT:
        messages.error(request, "Only an active draft invoice can be issued.")
        return redirect("commercial:invoice-detail", pk=pk)
    blocked = _issue_blocked(draft)
    form = InvoiceIssueForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_revision": draft.revision},
    )
    review_again = False
    if request.method == "POST" and form.is_valid():
        revision = form.cleaned_data["expected_revision"]
        accepted_date = (
            form.cleaned_data["proposed_due_date"]
            if form.cleaned_data["use_proposed_due_date"]
            else None
        )
        try:
            draft = invoices.issue_invoice(
                actor=request.user,
                draft_id=pk,
                expected_revision=revision,
                proposed_due_date=accepted_date,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            draft.refresh_from_db()
            if draft.status != InvoiceDraft.Status.DRAFT:
                messages.error(request, "This draft has changed. Review its current status.")
                return redirect("commercial:invoice-detail", pk=pk)
            blocked = _issue_blocked(draft)
            review_again = (
                revision != draft.revision
                or getattr(exc, "code", None) == "stale_due_date_proposal"
            )
        except InvoiceRenderError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Invoice {draft.issued_invoice.number} issued.")
            return redirect("commercial:invoice-detail", pk=pk)
    elif request.method == "POST":
        review_again = True
    proposed_due_date = invoices.proposed_invoice_due_date(
        draft, timezone.localdate(timezone=timezone.get_default_timezone())
    )
    if form.is_bound:
        # A retry must display the same proposal that its form would submit.
        review_again = (
            review_again or form.cleaned_data.get("proposed_due_date") != proposed_due_date
        )
    else:
        form.initial["proposed_due_date"] = proposed_due_date
    return render(
        request,
        "commercial/invoice_issue.html",
        {
            "page_title": "Issue invoice",
            "draft": draft,
            "lines": list(draft.lines.all()),
            "blocked": blocked,
            "form": form,
            "review_again": review_again,
            "can_issue": not blocked and not review_again,
            "proposed_due_date": proposed_due_date,
        },
    )


@staff_role_required(*PRICING_ROLES)
@require_http_methods(["GET", "POST"])
def invoice_void(request, pk):
    draft = _draft(pk)
    if draft.status != InvoiceDraft.Status.ISSUED:
        messages.error(request, "Only an issued invoice can be voided.")
        return redirect("commercial:invoice-detail", pk=pk)
    form = InvoiceVoidForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_revision": draft.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            invoices.void_invoice(actor=request.user, draft_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Invoice voided. Its original PDF and history are retained.")
            return redirect("commercial:invoice-detail", pk=pk)
    return render(
        request,
        "commercial/invoice_void.html",
        {"page_title": "Void invoice", "draft": draft, "form": form},
    )


@staff_role_required(*PRICING_ROLES)
@require_safe
def invoice_pdf(request, pk):
    invoice = get_object_or_404(
        IssuedInvoice.objects.only("number", "pdf", "sha256"),
        draft_id=pk,
        draft__status__in=[InvoiceDraft.Status.ISSUED, InvoiceDraft.Status.VOIDED],
    )
    data = bytes(invoice.pdf)
    if hashlib.sha256(data).hexdigest() != invoice.sha256:
        log.error("Stored PDF hash mismatch for invoice %s", invoice.pk)
        response = HttpResponse(
            "This invoice file failed its integrity check.",
            status=500,
            content_type="text/plain",
        )
    else:
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = content_disposition_header(True, f"{invoice.number}.pdf")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@staff_role_required(*PRICING_ROLES)
@require_safe
def invoice_accounting_csv(request, pk):
    snapshot = get_object_or_404(
        IssuedInvoice.objects.values_list("snapshot", flat=True),
        draft_id=pk,
        draft__status=InvoiceDraft.Status.ISSUED,
    )
    response = HttpResponse(export_invoice_csv(snapshot), content_type="text/csv; charset=utf-8")
    filename = f"{snapshot['number']}-accounting.csv"
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
