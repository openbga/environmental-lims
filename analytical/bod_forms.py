"""Bounded bottle entry controls; domain validation is shared with service callers."""

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from . import bod
from .forms import CorrectionForm, raw_text


class BottleForm(forms.Form):
    identifier = forms.CharField(label="Bottle identifier", max_length=60, strip=False)
    initial_do = raw_text("Initial DO", required=True, unit="mg/L")
    final_do = raw_text("Final DO", required=True, unit="mg/L")
    sample_volume = raw_text("Sample volume", required=True, unit="mL")
    bottle_volume = raw_text("Filled bottle volume", required=True, unit="mL")
    seed_correction = raw_text("Entered seed correction", required=True, unit="mg/L")
    seed_reference = forms.CharField(
        label="Seed record reference", max_length=240, required=False, strip=False
    )
    selected = forms.ChoiceField(
        label="Selection", choices=[("", "Choose"), ("include", "Include"), ("exclude", "Exclude")]
    )
    reason = forms.CharField(label="Exclusion reason", max_length=240, required=False, strip=False)


class BaseBottleFormSet(forms.BaseFormSet):
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        form.empty_permitted = False
        return form

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [
            {**form.cleaned_data, "selected": form.cleaned_data["selected"] == "include"}
            for form in self.forms
        ]
        try:
            self.bottle_data, _ = bod.parse_bottles(rows)
        except ValidationError as exc:
            general = []
            for key, errors in exc.message_dict.items():
                if key == NON_FIELD_ERRORS:
                    general.extend(errors)
                else:
                    _, index, field = key.split("_", 2)
                    self.forms[int(index)].add_error(field, errors)
            if general:
                raise ValidationError(general) from None


BottleFormSet = forms.formset_factory(
    BottleForm,
    formset=BaseBottleFormSet,
    extra=0,
    min_num=1,
    max_num=bod.MAX_BOTTLES,
    validate_min=True,
    validate_max=True,
    absolute_max=bod.MAX_BOTTLES,
)


class BottleEntryForm(CorrectionForm):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def use_calculation(self, calculation, definition, **kwargs):
        super().use_calculation(calculation, definition, **kwargs)
        self.fields["calculation_token"].required = True
        self.fields["dilution_factor"].label = "Prior dilution factor"
        self.fields["dilution_factor"].help_text = (
            "Preliminary dilution before the sample aliquot entered the bottle. Enter 1 for "
            "none. The bottle's sample fraction is already applied; do not include it again."
        )
