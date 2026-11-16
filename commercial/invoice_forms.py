"""Forms for internal invoice worksheets; source identities are always read-only."""

from decimal import Decimal

from django import forms

from receipt.forms import StyledForm
from receipt.models import Client

from .forms import FilterForm, GroupedForm
from .invoices import BILLING_FIELDS, MAX_LINES, MAX_QUANTITY, source_state
from .models import InvoiceDraft, PriceList


class InvoiceFilter(FilterForm):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *InvoiceDraft.Status.choices]
    )


class InvoiceCreateForm(StyledForm, forms.Form):
    expected_source_state = forms.CharField(widget=forms.HiddenInput)
    price_list = forms.ModelChoiceField(
        PriceList.objects.filter(is_active=True),
        empty_label="Choose an active price list",
        help_text=(
            "Copies the current USD price for every received requested test. "
            "Every test must have a price."
        ),
    )

    def __init__(self, *args, order, tests, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_source_state"] = source_state(order, tests)


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
