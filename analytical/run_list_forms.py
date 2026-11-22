from django import forms

from core.csv_templates import MAX_TEMPLATE_BYTES

from .run_lists import parse_run_list_template


class RunListTemplateForm(forms.Form):
    template_file = forms.FileField(
        label="Run list template",
        help_text=f"Choose a two-row UTF-8 CSV file, up to {MAX_TEMPLATE_BYTES // 1024} KiB.",
        widget=forms.FileInput(attrs={"accept": ".csv,text/csv"}),
    )

    output_format = forms.TypedChoiceField(
        label="Output format",
        choices=(("csv", "CSV (comma-separated)"), ("tsv", "TSV (tab-separated)")),
        initial="csv",
        required=False,
        empty_value="csv",
    )

    def clean_template_file(self):
        return parse_run_list_template(self.cleaned_data["template_file"])
