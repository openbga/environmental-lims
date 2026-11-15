"""Titration readings use the shared entry grid, correction and reviewer worksheet pages."""

import pytest
from django.urls import reverse

from analytical import calculations, worksheets
from analytical import services as svc
from analytical.models import ResultRevision
from core.models import AuditEvent

from .test_calculation_pages import grid, stamp
from .test_titration_calculations import EXAMPLES, KEYS, configure, entry
from .test_workflow import result_for, start

pytestmark = pytest.mark.django_db


def fields(key, **changes):
    return {
        "calculation_token": f"{key}:1",
        **{f"reading_{name}": value for name, value in {**EXAMPLES[key], **changes}.items()},
        "reporting_limit_applied": "1",
        "dilution_factor": "1",
    }


@pytest.mark.parametrize("key", KEYS)
def test_titration_grid_uses_registered_units_and_preserves_inline_errors(
    config, client, analyst, key
):
    configure(config, key)
    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    response = client.get(url)
    form = next(form for form, row, _ in response.context["rows"] if row.pk == result.pk)
    calculation = calculations.current(key)
    assert form.reading_names == [f"reading_{reading.key}" for reading in calculation.inputs]
    assert not form.fields["supplied_text"].required
    assert "reporting_limit_applied" in form.fields
    html = response.content.decode()
    assert calculation.title in html and "mg/L as CaCO3" in html
    assert (
        "Standardized EDTA normality" in html
        if key == "hardness_edta_titration"
        else "Standardized acid normality" in html
    )
    # No row is saved when its endpoint cannot follow its initial burette reading.
    rejected = client.post(url, grid(batch, [(result, fields(key, final="0.10"))]))
    invalid = next(form for form, row, _ in rejected.context["rows"] if row.pk == result.pk)
    assert rejected.status_code == 200 and "reading_final" in invalid.errors
    assert invalid["reading_normality"].value() == EXAMPLES[key]["normality"]
    assert invalid["reading_final"].value() == "0.10"
    assert not ResultRevision.objects.exists()
    saved = client.post(url, grid(batch, [(result, fields(key))]))
    assert saved.status_code == 302
    revision = ResultRevision.objects.get(result=result)
    assert (
        revision.worksheet.calculation == key and worksheets.reproduce(revision.worksheet).matches
    )
    assert AuditEvent.objects.filter(action="result.entered").count() == 1


def test_low_alkalinity_correction_and_review_page_keep_both_endpoint_observations(
    config, client, analyst, reviewer
):
    key = "alkalinity_low_titration"
    configure(config, key)
    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    first = svc.enter_result(actor=analyst, result_id=result.pk, **entry(key))
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    response = client.get(url)
    html = response.content.decode()
    for text in (
        "Low alkalinity by two-endpoint titration",
        "First endpoint burette reading",
        "First endpoint pH",
        "Final endpoint pH",
        "6.03",
        "Same result",
        "0.0201",
    ):
        assert text in html, text
    form = response.context["form"]
    assert "reading_first_ph" in form.fields and "reading_final_ph" in form.fields
    correction = {
        **fields(key, first_endpoint="1.20"),
        "analysis_started_at": stamp(),
        "reason": "Synthetic: corrected first endpoint observation",
    }
    bad = client.post(url, {**correction, "reading_final_ph": "4.1"})
    assert bad.status_code == 200 and "reading_final_ph" in bad.context["form"].errors
    assert bad.context["form"]["reading_first_endpoint"].value() == "1.20"
    assert ResultRevision.objects.count() == 1
    assert client.post(url, correction).status_code == 302
    latest = ResultRevision.objects.get(result=result, number=2)
    assert latest.supplied_text == "8.04"
    assert first.worksheet.readings.get(key="first_endpoint").supplied_text == "1.10"
    assert latest.worksheet.readings.get(key="first_endpoint").supplied_text == "1.20"
    client.force_login(reviewer)
    reviewed = client.get(url)
    assert reviewed.status_code == 200
    assert "Same result" in reviewed.content.decode() and "8.04" in reviewed.content.decode()
    assert 'name="reading_first_endpoint"' not in reviewed.content.decode()
    assert client.post(url, correction).status_code == 403
    assert ResultRevision.objects.count() == 2
