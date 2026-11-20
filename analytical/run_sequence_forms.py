"""Transcription and review forms for recorded instrument injection order."""

from django import forms
from django.core.exceptions import ValidationError

from .run_sequences import MAX_INJECTIONS


class RunSequenceForm(forms.Form):
    reference = forms.CharField(
        label="Run reference",
        max_length=120,
        help_text="Use the instrument run identifier. It stays the same across corrections.",
    )
    source_reference = forms.CharField(
        label="Source reference",
        max_length=240,
        help_text="Identify the source record, such as the instrument run log or export name.",
    )
    injections = forms.CharField(
        label="Injections (one per line)",
        max_length=32_000,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}),
        help_text=(
            "Enter position,kind,label on each line, without a header. For example, "
            "1,verification,SYN-CCV then 2,sample,SYN-001 on the next line. "
            f"Enter 1–{MAX_INJECTIONS} consecutive positions starting at 1. "
            "Use sample, verification, standard, blank or other. "
            "Copy the instrument label as shown, including any commas or quotes."
        ),
    )
    reason = forms.CharField(
        label="Reason for correction", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, previous=None, **kwargs):
        super().__init__(*args, **kwargs)
        # HTML measures UTF-16 code units. The server validates Unicode code points.
        for field in self.fields.values():
            field.widget.attrs["maxlength"] = 2 * field.max_length
        if previous is None:
            self.fields.pop("reason")
        else:
            self.fields["reference"].disabled = True
            self.initial.update(
                reference=previous.reference,
                source_reference=previous.source_reference,
                injections="\n".join(
                    f"{row['position']},{row['kind']},{row['label']}" for row in previous.injections
                ),
            )

    def clean_injections(self):
        value = self.cleaned_data["injections"].replace("\r\n", "\n").replace("\r", "\n")
        lines = value.removesuffix("\n").split("\n")
        if len(lines) > MAX_INJECTIONS:
            raise ValidationError(f"Enter at most {MAX_INJECTIONS} injections.")
        rows = []
        for number, line in enumerate(lines, 1):
            position, comma, fields = line.partition(",")
            kind, second_comma, label = fields.partition(",")
            if not (comma and second_comma):
                raise ValidationError(f"Line {number} needs a position, kind and label.")
            if not (position.isascii() and position.isdecimal() and len(position) <= 3):
                raise ValidationError(f"Enter a whole-number position on line {number}.")
            rows.append({"position": int(position), "kind": kind, "label": label})
        return rows


class VerificationIntervalForm(forms.Form):
    max_samples = forms.IntegerField(
        label="Maximum sample injections",
        required=False,
        min_value=1,
        help_text="Enter a positive whole number from your method or SOP to compare the counts.",
    )
