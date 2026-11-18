"""Forms for internal invoice worksheets; source identities are always read-only."""

from urllib.parse import urlencode

from django import forms
from django.core.exceptions import ValidationError
from django.db import connection

from core.lookups import RemoteSelect
from receipt.forms import StyledForm
from receipt.models import Client

from .forms import DATE, FilterForm, GroupedForm
from .invoices import (
    BILLING_FIELDS,
    MAX_LINES,
    MAX_PRICE,
    MAX_QUANTITY,
    _money,
    eligible_quote_versions,
    quote_version_label,
    source_state,
)
from .models import InvoiceDraft, InvoiceDraftLine, PriceList, QuoteVersion


class InvoiceFilter(FilterForm):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *InvoiceDraft.Status.choices]
    )

    due_from = forms.DateField(required=False, label="Due from", widget=DATE)
    due_through = forms.DateField(required=False, label="Due through", widget=DATE)

    def clean(self):
        data = super().clean()
        start, end = data.get("due_from"), data.get("due_through")
        if start and end and start > end:
            self.add_error("due_through", "Due through must be on or after Due from.")
        return data


class InvoiceQuoteSelect(RemoteSelect):
    def __init__(self, order, attrs=None):
        super().__init__("invoice-quotes", attrs)
        self.order_id = order.pk

    def build_attrs(self, base_attrs, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-vr-lookup"] += "?" + urlencode({"order": self.order_id})
        return attrs

    def optgroups(self, name, value, attrs=None):
        # Invalid bound input must still render its field error, without reaching the
        # remote widget's selected-option query as an invalid or overflowing primary key.
        pk_field = self.choices.field.queryset.model._meta.pk
        lower, upper = connection.ops.integer_field_range(pk_field.get_internal_type())
        selected = []
        for raw in value:
            try:
                pk = pk_field.to_python(raw)
            except ValueError, TypeError, OverflowError, ValidationError:
                continue
            if pk is not None and lower <= pk <= upper:
                selected.append(str(pk))
        return super().optgroups(name, selected, attrs)


class QuoteVersionChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return quote_version_label(obj)


class InvoiceCreateForm(StyledForm, forms.Form):
    expected_source_state = forms.CharField(widget=forms.HiddenInput)
    price_list = forms.ModelChoiceField(
        PriceList.objects.filter(is_active=True),
        required=False,
        empty_label="Choose an active price list",
        help_text=(
            "Copies the current USD price for every received requested test. "
            "Every test must have a price."
        ),
    )

    quote_version = QuoteVersionChoiceField(
        QuoteVersion.objects.none(),
        required=False,
        label="Accepted quote version",
        empty_label="Choose an accepted quote version",
        help_text=(
            "Choose exactly one source: a price list or an accepted issued quote. "
            "The quote's tests must match this order's received tests and quantities, "
            "with one line per test definition. Each quoted fee and discount is copied once, "
            "up to 200 draft lines "
            "in total. The final total cannot be negative. "
            "This does not track cumulative quote usage. "
            "Billing and purchase order come from the client and order; quote terms are not copied."
        ),
    )

    def __init__(self, *args, order, tests, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_source_state"] = source_state(order, tests)
        self.fields["quote_version"].queryset = eligible_quote_versions(order)
        self.fields["quote_version"].widget = InvoiceQuoteSelect(
            order, attrs=self.fields["quote_version"].widget.attrs
        )
        self.fields["quote_version"].widget.choices = self.fields["quote_version"].choices

    def clean(self):
        data = super().clean()
        if not self.errors and bool(data.get("price_list")) == bool(data.get("quote_version")):
            raise forms.ValidationError(
                "Choose exactly one pricing source: "
                "an active price list or an accepted quote version."
            )
        return data


class InvoiceEditForm(GroupedForm, StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    client_name = forms.CharField(max_length=160, label="Client name")
    account_code = forms.CharField(max_length=40, required=False, label="Account code")
    billing_contact_name = forms.CharField(
        max_length=160, required=False, label="Invoice-to contact"
    )
    billing_email = forms.EmailField(required=False, label="Invoice email")
    billing_address = forms.CharField(
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Billing address",
    )
    payment_terms = forms.ChoiceField(
        required=False,
        choices=[("", "Not recorded"), *Client.PaymentTerms.choices],
        help_text="Net terms can suggest a due date when you issue the invoice.",
    )
    due_date = forms.DateField(
        required=False,
        label="Due date",
        widget=DATE,
        help_text=(
            "Enter the agreed date to use on the issued invoice. "
            "Leave blank if you haven’t agreed on a date."
        ),
    )
    po_number = forms.CharField(max_length=60, required=False, label="Purchase order")
    reason = forms.CharField(
        max_length=2000, label="Reason for change", widget=forms.Textarea(attrs={"rows": 2})
    )
    groups = (
        ("Billing snapshot", [*BILLING_FIELDS, "due_date"]),
        ("Change record", ["reason"]),
    )

    def __init__(self, *args, draft, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update({name: getattr(draft, name) for name in BILLING_FIELDS})
        self.initial["due_date"] = draft.due_date
        self.initial["expected_revision"] = draft.revision


class InvoiceLineForm(StyledForm, forms.Form):
    id = forms.IntegerField(widget=forms.HiddenInput)
    description = forms.CharField(max_length=400)
    quantity = forms.IntegerField(
        min_value=1,
        max_value=MAX_QUANTITY,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "class": "form-control text-end"}),
    )
    unit_price = forms.DecimalField(
        min_value=-MAX_PRICE,
        max_value=MAX_PRICE,
        max_digits=12,
        decimal_places=2,
        label="Unit price",
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "unit": "USD", "class": "form-control text-end"}
        ),
    )

    def __init__(self, *args, source_line=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_line = source_line

    def clean_id(self):
        value = self.cleaned_data["id"]
        if self.source_line is None or value != self.source_line.pk:
            raise forms.ValidationError(
                "The draft lines changed. Reload and review them before saving."
            )
        return value

    def clean_quantity(self):
        value = self.cleaned_data["quantity"]
        if self.source_line is not None and self.source_line.kind == InvoiceDraftLine.Kind.TAX:
            if value != 1:
                raise forms.ValidationError("Tax quantity must be exactly 1.")
        return value

    def clean_unit_price(self):
        value = self.cleaned_data["unit_price"]
        if self.source_line is not None:
            if self.source_line.kind == InvoiceDraftLine.Kind.ADJUSTMENT:
                if value > 0:
                    raise forms.ValidationError("A discount price must be zero or negative.")
            elif value < 0:
                raise forms.ValidationError("A test, fee or tax price must be nonnegative.")
        return value


class InvoiceAddLineForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    kind = forms.ChoiceField(
        choices=[
            (InvoiceDraftLine.Kind.FEE, "Fee"),
            (InvoiceDraftLine.Kind.ADJUSTMENT, "Discount"),
            (InvoiceDraftLine.Kind.TAX, "Tax"),
        ]
    )
    description = forms.CharField(
        max_length=400,
        help_text="For tax, identify the basis or source of the entered assessment.",
    )
    quantity = forms.IntegerField(
        min_value=1,
        max_value=MAX_QUANTITY,
        initial=1,
        help_text="Tax quantity must be exactly 1.",
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )
    unit_price = forms.DecimalField(
        min_value=-MAX_PRICE,
        max_value=MAX_PRICE,
        max_digits=12,
        decimal_places=2,
        label="Unit price",
        help_text=(
            "Whole cents in USD. Fees and tax must be nonnegative; "
            "discounts must be zero or negative."
        ),
        widget=forms.NumberInput(attrs={"unit": "USD"}),
    )
    reason = forms.CharField(
        max_length=2000, label="Reason for change", widget=forms.Textarea(attrs={"rows": 2})
    )

    def clean(self):
        data = super().clean()
        if data.get("kind") == InvoiceDraftLine.Kind.TAX and data.get("quantity", 1) != 1:
            self.add_error("quantity", "Tax quantity must be exactly 1.")
        if all(name in data for name in ("kind", "quantity", "unit_price")):
            try:
                _money(data["unit_price"], data["quantity"], 1, kind=data["kind"])
            except ValidationError as exc:
                self.add_error("unit_price", exc)
        return data


class BaseInvoiceLineFormSet(forms.BaseFormSet):
    def __init__(self, *args, source_lines, **kwargs):
        self.source_lines = {str(line.pk): line for line in source_lines}
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        if index is None:
            return kwargs
        if self.is_bound:
            source_id = self.data.get(f"{self.prefix}-{index}-id")
        else:
            source_id = str(self.initial[index]["id"]) if index < len(self.initial) else None
        kwargs["source_line"] = self.source_lines.get(source_id)
        return kwargs


InvoiceLineFormSet = forms.formset_factory(
    InvoiceLineForm,
    formset=BaseInvoiceLineFormSet,
    extra=0,
    min_num=1,
    max_num=MAX_LINES,
    validate_min=True,
    validate_max=True,
    absolute_max=MAX_LINES,
)


class InvoiceCancelForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        max_length=2000, label="Reason for cancellation", widget=forms.Textarea(attrs={"rows": 3})
    )


class InvoiceIssueForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(
        widget=forms.HiddenInput, min_value=1, label="Draft revision"
    )
    proposed_due_date = forms.DateField(
        required=False, input_formats=["%Y-%m-%d"], widget=forms.HiddenInput
    )
    use_proposed_due_date = forms.BooleanField(
        required=False,
        label="Use proposed due date",
        widget=forms.CheckboxInput(
            attrs={
                "form": "issue-invoice-form",
                "aria-describedby": "invoice-proposed-due-date id_use_proposed_due_date_helptext",
            }
        ),
        help_text=(
            "The proposal adds the term’s calendar days to the invoice date. "
            "Leave unchecked to issue without a due date."
        ),
    )

    def clean(self):
        data = super().clean()
        if data.get("use_proposed_due_date") and not data.get("proposed_due_date"):
            raise ValidationError("Reload the preview and review the proposed due date.")
        return data


class InvoiceVoidForm(StyledForm, forms.Form):
    expected_revision = forms.IntegerField(
        widget=forms.HiddenInput, min_value=1, label="Invoice revision"
    )
    reason = forms.CharField(
        max_length=2000, label="Reason for voiding", widget=forms.Textarea(attrs={"rows": 3})
    )
