"""Forms for internal invoice worksheets; source identities are always read-only."""

from decimal import Decimal
from urllib.parse import urlencode

from django import forms
from django.core.exceptions import ValidationError
from django.db import connection

from core.lookups import RemoteSelect
from receipt.forms import StyledForm
from receipt.models import Client

from .forms import FilterForm, GroupedForm
from .invoices import (
    BILLING_FIELDS,
    MAX_LINES,
    MAX_QUANTITY,
    eligible_quote_versions,
    quote_version_label,
    source_state,
)
from .models import InvoiceDraft, PriceList, QuoteVersion


class InvoiceFilter(FilterForm):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *InvoiceDraft.Status.choices]
    )


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
            "The entire quote must match this order's received tests and quantities, with one line "
            "per test definition and no fees or discounts. "
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
        help_text="Recorded terms only; no due date or payment is calculated.",
    )
    po_number = forms.CharField(max_length=60, required=False, label="Purchase order")
    reason = forms.CharField(
        max_length=2000, label="Reason for change", widget=forms.Textarea(attrs={"rows": 2})
    )
    groups = (
        ("Billing snapshot", list(BILLING_FIELDS)),
        ("Change record", ["reason"]),
    )

    def __init__(self, *args, draft, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update({name: getattr(draft, name) for name in BILLING_FIELDS})
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
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
        label="Unit price",
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "unit": "USD", "class": "form-control text-end"}
        ),
    )


InvoiceLineFormSet = forms.formset_factory(
    InvoiceLineForm,
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
