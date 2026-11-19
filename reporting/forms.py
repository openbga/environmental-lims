from decimal import Decimal

from django import forms
from django.core.validators import DecimalValidator
from django.utils import timezone

from receipt.models import Project, Sample

from .daily_load import FLOW_DECIMAL_PLACES, FLOW_MAX_DIGITS
from .models import MAX_NARRATIVE
from .results_csv import parse_csv_template


class FlowInputField(forms.RegexField):
    def __init__(self, *, positive=False, **kwargs):
        self.positive = positive
        amount = "a positive decimal" if positive else "zero or a positive decimal"
        super().__init__(
            regex=r"\A\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z",
            max_length=60,
            widget=forms.NumberInput(attrs={"unit": "MGD", "inputmode": "decimal"}),
            error_messages={"invalid": f"Enter {amount} without units or an exponent."},
            **kwargs,
        )

    def clean(self, value):
        flow = Decimal(super().clean(value))
        DecimalValidator(FLOW_MAX_DIGITS, FLOW_DECIMAL_PLACES)(flow)
        if self.positive and flow == 0:
            raise forms.ValidationError("Enter a flow greater than zero.")
        return flow


class DailyLoadForm(forms.Form):
    flow_mgd = FlowInputField(
        label="Sampling-day flow",
        help_text="Enter the corresponding daily flow in million US gallons per day.",
    )


class GrabFlowForm(forms.Form):
    series_token = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, flow_keys, series_token, *, data=None):
        super().__init__(data=data, initial={"series_token": series_token})
        for name, label in flow_keys.items():
            self.fields[name] = FlowInputField(label=label, positive=True)

    def clean(self):
        cleaned = super().clean()
        if self.data.keys() - self.fields.keys() - {"csrfmiddlewaretoken"}:
            self.add_error(None, "Submit flows only for the listed grab samples.")
        for field in self.visible_fields():
            if len(self.data.getlist(field.name)) > 1:
                self.add_error(field.name, "Submit exactly one flow for this grab sample.")
        return cleaned


class ResultCSVTemplateForm(forms.Form):
    template_file = forms.FileField(
        label="CSV template",
        help_text="Choose a two-row UTF-8 CSV file, up to 16 KiB.",
        widget=forms.FileInput(attrs={"accept": ".csv,text/csv"}),
    )

    output_format = forms.TypedChoiceField(
        label="Download format",
        choices=[("csv", "CSV"), ("xlsx", "Excel (.xlsx)")],
        initial="csv",
        required=False,
        empty_value="csv",
        help_text="The Excel workbook keeps reported values and identifiers as text.",
    )

    def clean_template_file(self):
        return parse_csv_template(self.cleaned_data["template_file"])


class SampleChoiceField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def label_from_instance(self, obj):
        collected = timezone.localtime(obj.collected_at).strftime("%Y-%m-%d %H:%M")
        return (
            f"{obj.lab_id} · {obj.client_sample_id} · {obj.get_matrix_display()} · "
            f"collected {collected} · {obj.get_disposition_display()}"
        )


class ProjectChoiceForm(forms.Form):
    project = forms.ModelChoiceField(
        Project.objects.select_related("client").order_by("client__name", "name"),
        empty_label="Choose a project",
        help_text="A report covers samples of one client project.",
    )


class ReportForm(forms.Form):
    title = forms.CharField(max_length=160)
    samples = SampleChoiceField(
        Sample.objects.none(), help_text="Samples of this project to include."
    )
    narrative = forms.CharField(
        required=False,
        max_length=MAX_NARRATIVE,
        widget=forms.Textarea(attrs={"rows": 4}),
        label="Case narrative",
        help_text="Printed on the report. Describe anything the client needs to interpret "
        "the results.",
    )

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["samples"].queryset = Sample.objects.filter(order__project=project).order_by(
            "-received_at", "lab_id"
        )


class ReleaseForm(forms.Form):
    fingerprint = forms.CharField(widget=forms.HiddenInput, max_length=64)
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Amendment reason",
        help_text="Required for an amendment. It is printed on the new version.",
    )
    confirm = forms.BooleanField(
        label="I authorize release of exactly the content previewed above."
    )

    def __init__(self, *args, amendment, **kwargs):
        super().__init__(*args, **kwargs)
        self.amendment = amendment
        if amendment:
            self.fields["reason"].required = True
        else:
            del self.fields["reason"]


class PickerForm(forms.Form):
    """The report scope's sample-picker filters, posted with the scope form and read by the view
    from the raw data (reporting.queries.picker_samples); this form only renders them."""

    pshow = forms.ChoiceField(
        label="Show",
        choices=[
            ("all", "All samples (selected first)"),
            ("selected", "Selected only"),
            ("approved", "All tests approved"),
            ("unreported", "Not in any issued report"),
        ],
        widget=forms.Select(attrs={"id": "picker-show"}),
    )
    pper = forms.TypedChoiceField(
        label="Rows", coerce=int, widget=forms.Select(attrs={"id": "picker-per-page"})
    )

    def __init__(self, show, per_page, per_page_options, **kwargs):
        super().__init__(initial={"pshow": show, "pper": per_page}, **kwargs)
        self.fields["pper"].choices = [(n, str(n)) for n in per_page_options]
