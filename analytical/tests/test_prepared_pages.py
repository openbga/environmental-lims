"""Preparation and dry-weight calculations on the pages: the readings in the entry grid and the
correction form, the weight basis the row offers, and the limit on the worksheet."""

from decimal import Decimal

import pytest
from django.urls import reverse

from analytical import services as svc
from analytical.models import CalculationRule, ResultRevision

from .test_calculation_pages import grid, stamp
from .test_calculations import rule_for
from .test_prepared_calculations import SOURCE, extract
from .test_workflow import when

pytestmark = pytest.mark.django_db

EXTRACT = {
    "calculation_token": "solid_dry_mg_l:1",
    "reading_reading": "1.84",
    "reading_base_limit": "0.05",
    "reading_final_volume": "50",
    "reading_weight": "5.03",
    "dilution_factor": "1",
    "result_basis": "dry",
    "percent_solids": "81.6",
    "solids_reference": SOURCE,
}
UNTOUCHED = {"dilution_factor": "1"}


def test_grid_offers_the_dry_basis_and_asks_for_the_percent_solids(soil, client, analyst):
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    rule_for(soil["defs"]["Solids"], "percent_solids")
    nitrate, zinc, solids = (soil["results"][name] for name in ("Nitrate", "Zinc", "Solids"))
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[soil["batch"].pk])
    page = client.get(url).content.decode()
    assert "Calculated: Prepared solid corrected to dry weight, mg/L in solution to mg/kg" in page
    assert "Percent solids for the dry-weight correction (required)" in page
    assert "Supplied percent solids (optional)" in page  # the typed zinc row keeps its wording
    assert "limit in the solution × the same factors, rounded like the result" in page
    forms = {f.initial["result_id"]: f for f in client.get(url).context["formset"]}
    offered = {r: forms[r.pk]["result_basis"].value() for r in (nitrate, zinc, solids)}
    assert offered == {nitrate: "dry", zinc: None, solids: "wet"}
    # A row left as the page drew it, with the basis it offered, is not an entry.
    rows = [
        (nitrate, {**UNTOUCHED, "calculation_token": "solid_dry_mg_l:1", "result_basis": "dry"}),
        (solids, {**UNTOUCHED, "calculation_token": "percent_solids:1", "result_basis": "wet"}),
        (zinc, {**UNTOUCHED, "reporting_limit_applied": "5"}),
    ]
    nothing = client.post(url, grid(soil["batch"], rows))
    assert "Enter at least one result." in nothing.content.decode()
    assert not ResultRevision.objects.exists()
    # Changing the basis the row offered makes it an entry, and the service says why it is wrong.
    rows[0] = (nitrate, {**EXTRACT, "result_basis": "wet"})
    wrong = client.post(url, grid(soil["batch"], rows)).content.decode()
    assert "Choose Dry weight" in wrong and "1.84" in wrong  # refused, what was typed is kept
    assert not ResultRevision.objects.exists()
    rows[0] = (nitrate, EXTRACT)
    assert client.post(url, grid(soil["batch"], rows)).status_code == 302
    revision = ResultRevision.objects.get()
    assert (revision.supplied_text, revision.result_basis) == ("22.4", "dry")
    assert revision.reporting_limit_applied == Decimal("0.609")
    assert revision.percent_solids == Decimal("81.6")


def test_result_page_shows_the_limit_before_rounding_and_corrects_with_new_solids(
    soil, client, analyst
):
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    result = soil["results"]["Nitrate"]
    svc.enter_result(actor=analyst, result_id=result.pk, **extract())
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    response = client.get(url)
    page = response.content.decode()
    for text in (
        "Worksheet: Prepared solid corrected to dry weight, mg/L in solution to mg/kg",
        "Reporting limit before rounding",
        "0.60909055471094998636",
        "applied to the value and to the reporting limit",
        "Same result",
        "with a reporting limit of 0.609, as recorded",
    ):
        assert text in page, text
    form = response.context["form"]
    assert form["result_basis"].value() == "dry"  # offered, as in the grid
    assert form.fields["percent_solids"].required and form.fields["solids_reference"].required
    assert "reporting_limit_applied" not in form.fields and "supplied_text" not in form.fields
    correction = {
        "calculation_token": "solid_dry_mg_l:1",
        "reading_reading": "ND",
        "reading_base_limit": "0.05",
        "reading_final_volume": "50",
        "reading_weight": "5.03",
        "dilution_factor": "1",
        "result_basis": "dry",
        "analysis_started_at": stamp(),
        "reason": "Synthetic: re-read after recalibration",
    }
    missing = client.post(url, correction)
    assert missing.status_code == 200 and missing.context["form"].errors.keys() >= {
        "percent_solids",
        "solids_reference",
    }
    saved = client.post(url, {**correction, "percent_solids": "81.6", "solids_reference": SOURCE})
    assert saved.status_code == 302
    latest = ResultRevision.objects.get(result=result, number=2)
    assert (latest.kind, latest.reporting_limit_applied) == ("non_detect", Decimal("0.609"))
    page = client.get(url).content.decode()
    assert "applied to the reporting limit. A reading was ND" in page
    assert "The stored reading is ND at a reporting limit of 0.609" in page


def test_an_untouched_row_with_an_offered_basis_never_blocks_the_page(soil, client, analyst):
    """The page posts back the weight basis it offered. A row the analyst did not touch must
    stay skipped when its rule was withdrawn or replaced after the page was drawn, or when the
    row was entered and submitted from elsewhere in the meantime."""
    rule_for(soil["defs"]["Nitrate"], "solid_dry_mg_l")
    solids_rule = rule_for(soil["defs"]["Solids"], "percent_solids")
    nitrate, zinc, solids = (soil["results"][name] for name in ("Nitrate", "Zinc", "Solids"))
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[soil["batch"].pk])
    # As the page drew the percent solids row: its token, the limit and the Wet weight it offered.
    untouched = {
        **UNTOUCHED,
        "calculation_token": "percent_solids:1",
        "reporting_limit_applied": "0.1",
        "result_basis": "wet",
    }
    typed_zinc = {
        "supplied_text": "12",
        "reporting_limit_applied": "5",
        "dilution_factor": "1",
        "result_basis": "wet",
    }

    def post(*rows):
        return client.post(url, grid(soil["batch"], list(rows)))

    # The rule is withdrawn after the page was drawn: the row now takes a typed result.
    CalculationRule.objects.filter(pk=solids_rule.pk).update(is_active=False)
    assert post((solids, untouched), (zinc, typed_zinc)).status_code == 302
    assert list(ResultRevision.objects.values_list("result_id", flat=True)) == [zinc.pk]
    # The rule is replaced by one that means another basis (Wet offered then, Dry meant now).
    soil["defs"]["Solids"].unit = "mg/kg"
    soil["defs"]["Solids"].save(update_fields=["unit"])
    CalculationRule.objects.filter(pk=solids_rule.pk).update(
        is_active=True, calculation="solid_dry_mg_l"
    )
    assert post((solids, untouched), (nitrate, EXTRACT)).status_code == 302
    assert not ResultRevision.objects.filter(result=solids).exists()
    # A basis the analyst chose is still an entry: here it is reported, not dropped.
    chosen = post((solids, {**untouched, "result_basis": "dry"}))
    assert chosen.status_code == 200 and not ResultRevision.objects.filter(result=solids).exists()
    assert b"changed after this page was opened" in chosen.content
    # Someone else enters and submits the row: it is no longer current, and still blocks nothing.
    CalculationRule.objects.filter(pk=solids_rule.pk).update(is_active=False)
    revision = svc.enter_result(
        actor=analyst,
        result_id=solids.pk,
        supplied_text="81.6",
        reporting_limit_applied="0.1",
        dilution_factor="1",
        analysis_started_at=when(),
        result_basis="wet",
    )
    svc.submit_result(actor=analyst, revision_id=revision.pk)
    retyped = {**typed_zinc, "supplied_text": "13", "reason": "Synthetic: reread"}
    assert post((solids, untouched), (zinc, retyped)).status_code == 302
    assert ResultRevision.objects.filter(result=zinc).count() == 2
    assert ResultRevision.objects.filter(result=solids).count() == 1
    # What the analyst typed into the row that is gone is still reported.
    lost = post((solids, {**untouched, "supplied_text": "80"}))
    assert lost.status_code == 200 and b"can no longer be entered here" in lost.content
