"""Calculated results on the pages: readings in the entry grid and the correction form, the
worksheet on the result page, the reviewer's link to it and the rule form."""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from analytical import services as svc
from analytical.models import CalculationRule, ResultRevision
from core.models import AuditEvent

from .conftest import make_user
from .test_calculations import diluted, rule_for, weights
from .test_workflow import result_for, run_qc, start

pytestmark = pytest.mark.django_db


def stamp(hours=1):
    return (timezone.localtime() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def grid(batch, rows, **extra):
    """POST data for the entry grid: rows is [(result, {field: value})] in page order."""
    data = {
        "form-TOTAL_FORMS": len(rows),
        "form-INITIAL_FORMS": len(rows),
        "form-MIN_NUM_FORMS": 0,
        "form-MAX_NUM_FORMS": 1000,
        "default_analysis_started_at": stamp(),
        **extra,
    }
    for i, (result, fields) in enumerate(rows):
        data[f"form-{i}-result_id"] = result.pk
        data |= {f"form-{i}-{name}": value for name, value in fields.items()}
    return data


WEIGHTS = {
    "calculation_token": "residue_g:1",
    "reading_gross": "0.1183",
    "reading_tare": "0.1058",
    "reading_volume": "300",
    "reporting_limit_applied": "5",
    "dilution_factor": "1",
}
TYPED = {"supplied_text": "0.0300", "reporting_limit_applied": "0.0100", "dilution_factor": "1"}


def test_grid_takes_readings_for_a_calculated_test_and_a_result_for_the_other(
    config, client, analyst
):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    page = client.get(url)
    html = page.content.decode()
    assert "Result or readings" in html and "Solids by weight, balance in grams" in html
    by_result = {form["result_id"].value(): form for form, _, _ in page.context["rows"]}
    assert by_result[lead.pk].reading_names == ["reading_gross", "reading_tare", "reading_volume"]
    assert by_result[copper.pk].calculation is None
    assert 'name="form-0-supplied_text"' not in html or 'name="form-1-supplied_text"' not in html

    # A reading that is not a number: inline error on that reading, input kept, nothing saved.
    bad = client.post(
        url, grid(batch, [(lead, {**WEIGHTS, "reading_volume": "300 mL"}), (copper, TYPED)])
    )
    assert bad.status_code == 200 and not ResultRevision.objects.exists()
    form = next(f for f, r, _ in bad.context["rows"] if r.pk == lead.pk)
    assert "not a plain number" in str(form.errors["reading_volume"])
    assert form["reading_gross"].value() == "0.1183"

    saved = client.post(url, grid(batch, [(lead, WEIGHTS), (copper, TYPED)]))
    assert saved.status_code == 302
    worked = ResultRevision.objects.get(result=lead)
    assert (worked.supplied_text, worked.source) == ("41.7", "calculated")
    assert worked.worksheet.readings.count() == 3
    typed = ResultRevision.objects.get(result=copper)
    assert (typed.supplied_text, typed.source) == ("0.0300", "manual")
    again = client.get(url).content.decode()  # entered rows stay on the grid until submitted
    assert "41.7" in again and "calculated" in again


def test_untouched_rows_are_skipped_even_with_the_limit_the_page_filled_in(config, client, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    # What the page offers: the catalog limits (0.005 and 0.01), dilution 1, nothing else.
    offered_lead = {"reporting_limit_applied": "0.005", "dilution_factor": "1"}
    offered_copper = {"reporting_limit_applied": "0.01", "dilution_factor": "1"}
    # Only the copper row is entered: the untouched calculated row is skipped, not an error.
    one = client.post(
        url, grid(batch, [(lead, offered_lead), (copper, {**offered_copper, **TYPED})])
    )
    assert one.status_code == 302
    assert list(ResultRevision.objects.values_list("result_id", flat=True)) == [copper.pk]
    # And the reverse, in a second batch: only the calculated row is entered, and the typed row
    # is left as offered.
    other = start(config, analyst, samples=config["samples"][1:])
    lead2, copper2 = result_for(other, "Lead"), result_for(other, "Copper")
    two = client.post(
        reverse("analytical:batch-entry", args=[other.pk]),
        grid(other, [(lead2, WEIGHTS), (copper2, offered_copper)]),
    )
    assert two.status_code == 302
    assert ResultRevision.objects.filter(result=lead2).exists()
    assert not ResultRevision.objects.filter(result=copper2).exists()
    # A limit the analyst changed, with no result, is an unfinished entry and says so plainly.
    changed = client.post(
        reverse("analytical:batch-entry", args=[other.pk]),
        grid(other, [(copper2, {"reporting_limit_applied": "0.02", "dilution_factor": "1"})]),
    )
    assert changed.status_code == 200 and b"Result is required." in changed.content
    # A row the analyst started (a qualifier, a dilution) but gave no result is not skipped.
    for started in ({"qualifiers": "J"}, {"dilution_factor": "10"}, {"reason": "Rerun"}):
        half = client.post(
            reverse("analytical:batch-entry", args=[other.pk]),
            grid(other, [(copper2, {**offered_copper, **started})]),
        )
        assert half.status_code == 200 and b"Result is required." in half.content, started
    assert not ResultRevision.objects.filter(result=copper2).exists()
    # Nothing typed anywhere on the page is reported, not saved silently.
    nothing = client.post(url, grid(batch, [(lead, offered_lead)]))
    assert nothing.status_code == 200 and b"Enter at least one result." in nothing.content


def test_grid_dilution_row_sets_the_limit_and_offers_the_catalog_limit(config, client, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], "dilution", rounding_mode="decimal_places", rounding_digits=4)
    lead = result_for(batch, "Lead")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk]) + "?q=Lead"
    page = client.get(url)
    form = page.context["rows"][0][0]
    assert form["reading_base_limit"].value() == "0.005"  # offered from the catalog
    assert b"limit before dilution" in page.content
    post = grid(
        batch,
        [
            (
                lead,
                {
                    "reading_reading": "0.0125",
                    "reading_base_limit": "0.005",
                    "dilution_factor": "10",
                },
            )
        ],
    )
    assert client.post(url, post).status_code == 302
    rev = ResultRevision.objects.get(result=lead)
    assert (rev.supplied_text, str(rev.reporting_limit_applied)) == ("0.1250", "0.0500000000")


def test_result_page_shows_the_worksheet_and_corrects_from_new_readings(
    config, client, analyst, reviewer
):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    result = result_for(batch, "Lead")
    svc.enter_result(actor=analyst, result_id=result.pk, **weights())
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    page = client.get(url)
    html = page.content.decode()
    for text in (
        "Result as calculated",
        "Worksheet: Solids by weight, balance in grams (version 1)",
        "0.1183",
        "41.66666666666666666667",
        "3 significant figures, half to even",
        "Same result",
        "Calculated from readings",
    ):
        assert text in html, text
    form = page.context["form"]
    assert "supplied_text" not in form.fields and "reading_gross" in form.fields
    # A correction is worked from new readings and keeps the first worksheet.
    data = {
        "reading_gross": "0.1190",
        "reading_tare": "0.1058",
        "reading_volume": "300",
        "reporting_limit_applied": "5",
        "dilution_factor": "1",
        "analysis_started_at": stamp(),
        "reason": "Synthetic: reweighed after a second drying cycle",
    }
    missing = client.post(url, {**data, "reading_tare": ""})
    assert missing.status_code == 200
    assert "is required" in str(missing.context["form"].errors["reading_tare"])
    assert client.post(url, data).status_code == 302
    first, second = ResultRevision.objects.filter(result=result).order_by("number")
    assert (first.supplied_text, second.supplied_text) == ("41.7", "44.0")
    assert first.worksheet.readings.get(key="gross").supplied_text == "0.1183"
    assert AuditEvent.objects.filter(action="result.corrected").count() == 1
    # A reviewer reads the same worksheet and is pointed to it from the review grid.
    svc.submit_result(actor=analyst, revision_id=second.pk)
    client.force_login(reviewer)
    review = client.get(reverse("analytical:review-batch", args=[batch.pk])).content.decode()
    assert f'{url}#worksheet-2"' in review and "calculated" in review
    assert "Same result" in client.get(url).content.decode()


def test_result_page_queries_do_not_grow_with_the_number_of_worksheets(config, client, analyst):
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"])
    result = result_for(batch, "Lead")
    svc.enter_result(actor=analyst, result_id=result.pk, **weights())
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    client.get(url)
    with CaptureQueriesContext(connection) as one:
        client.get(url)
    for n in range(4):
        svc.enter_result(
            actor=analyst,
            result_id=result.pk,
            **weights(gross=f"0.12{n}3", reason=f"Synthetic reweigh {n}"),
        )
    with CaptureQueriesContext(connection) as five:
        client.get(url)
    assert len(five) == len(one)


def test_a_grid_drawn_before_a_rule_changed_is_refused_with_the_reason(config, client, analyst):
    batch = start(config, analyst)
    lead = result_for(batch, "Lead")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    rule_for(config["defs"]["Lead"])  # set after the analyst opened the typed-result grid
    late = client.post(url, grid(batch, [(lead, TYPED)]))
    assert late.status_code == 200 and b"calculated from readings" in late.content
    assert not ResultRevision.objects.exists()
    # The other way round: readings typed, then the rule is withdrawn. They are not dropped
    # silently and the row is not skipped as blank.
    CalculationRule.objects.update(is_active=False)
    gone = client.post(url, grid(batch, [(lead, WEIGHTS)]))
    assert gone.status_code == 200 and b"changed after this page was opened" in gone.content
    assert not ResultRevision.objects.exists()
    # Readings typed against grams, posted after the rule moved to milligrams: the reading
    # names are the same, the units are not, so the page's own calculation is checked.
    CalculationRule.objects.update(is_active=True, calculation="residue_mg")
    replaced = client.post(
        url, grid(batch, [(lead, {**WEIGHTS, "calculation_token": "residue_g:1"})])
    )
    assert replaced.status_code == 200 and not ResultRevision.objects.exists()
    assert b"it is now Solids by weight, balance in milligrams" in replaced.content
    fresh = next(f for f, r, _ in client.get(url).context["rows"] if r.pk == lead.pk)
    assert fresh["calculation_token"].value() == "residue_mg:1"


def test_rule_form_is_for_administrators_and_audits_each_change(config, client, admin_user):
    lead = config["defs"]["Lead"]
    new = reverse("analytical:config-new", args=["calculation-rule"])
    for role, status in (("analyst", 403), ("reviewer", 403), ("project_manager", 403)):
        client.force_login(make_user(f"{role}-rule", role))
        assert client.get(new).status_code == status
        assert client.post(new, {}).status_code == status
    client.force_login(admin_user)
    data = {
        "definition": lead.pk,
        "calculation": "residue_g",
        "rounding_mode": "decimal_places",
        "rounding_digits": "0",
        "threshold": "1000",
        "upper_rounding_mode": "significant_figures",
        "upper_rounding_digits": "3",
        "rounding_method": "half_even",
        "basis": "SYNTHETIC TEST: invented rule; not method guidance.",
        "is_active": "on",
    }
    # A second rounding without its threshold is refused beside the field, input kept.
    bad = client.post(new, {**data, "threshold": ""})
    assert bad.status_code == 200 and "threshold" in bad.context["form"].errors
    assert bad.context["form"]["rounding_digits"].value() == "0"
    assert client.post(new, data).status_code == 302
    rule = CalculationRule.objects.get()
    assert rule.rounding.describe() == (
        "whole numbers below 1000; 3 significant figures at or above 1000; half to even"
    )
    listing = client.get(reverse("analytical:qc-config")).content.decode()
    assert "Calculation rules" in listing and "Solids by weight, balance in grams" in listing
    # Everyone on staff can read the rules; only administrators get the edit link.
    edit = reverse("analytical:config-edit", args=["calculation-rule", rule.pk])
    assert edit in listing
    client.force_login(make_user("analyst-reads", "analyst"))
    readonly = client.get(reverse("analytical:qc-config")).content.decode()
    assert "Solids by weight, balance in grams" in readonly and edit not in readonly
    # A stale edit form is refused and keeps its input; a fresh one with a reason saves.
    client.force_login(admin_user)
    form = client.get(edit).context["form"]
    stale = {f.name: f.value() if f.value() is not None else "" for f in form}
    stale.update(rounding_digits="1", reason="Synthetic: SOP revision")
    CalculationRule.objects.filter(pk=rule.pk).update(rounding_digits=2)
    refused = client.post(edit, stale)
    assert refused.status_code == 200
    assert "changed after you opened" in str(refused.context["form"].errors)
    stale["original_state"] = client.get(edit).context["form"]["original_state"].value()
    assert client.post(edit, stale).status_code == 302
    rule.refresh_from_db()
    assert rule.rounding_digits == 1 and rule.threshold == 1000
    event = AuditEvent.objects.get(action="config.calculationrule.updated")
    assert (event.before["rounding_digits"], event.after["rounding_digits"]) == (2, 1)
    assert event.reason == "Synthetic: SOP revision"


def test_qc_batch_review_still_gates_a_calculated_result(config, analyst, reviewer):
    """A worksheet changes how a value is produced, not what review requires of it."""
    batch = start(config, analyst)
    rule_for(config["defs"]["Lead"], "dilution", rounding_mode="decimal_places", rounding_digits=4)
    rev = svc.enter_result(
        actor=analyst, result_id=result_for(batch, "Lead").pk, **diluted(reading="0.0125")
    )
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    assert svc.approval_blockers(rev)  # no QC recorded yet: held like any other result
    run_qc(config, analyst, batch)
    approved = svc.approve_result(actor=reviewer, revision_id=rev.pk)
    assert approved.status == "approved" and approved.worksheet.calculation == "dilution"


def test_untouched_dilution_rows_never_block_or_lose_an_entry(config, client, analyst):
    """The dilution row posts a limit the page filled in itself. That must not make an untouched
    row an entry, must not block other rows when the row is no longer current or its rule was
    withdrawn, and a limit the analyst changed must not be dropped silently."""
    batch = start(config, analyst)
    rule = rule_for(
        config["defs"]["Lead"], "dilution", rounding_mode="decimal_places", rounding_digits=4
    )
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    offered = {
        "calculation_token": "dilution:1",
        "reading_base_limit": "0.005",  # as the page filled it in
        "dilution_factor": "1",
    }
    typed = {**TYPED, "reporting_limit_applied": "0.01"}
    # Untouched dilution row beside an entered row: skipped.
    assert client.post(url, grid(batch, [(lead, offered), (copper, typed)])).status_code == 302
    assert list(ResultRevision.objects.values_list("result_id", flat=True)) == [copper.pk]
    # The analyst changed only the limit before dilution: an unfinished entry, said plainly.
    changed = client.post(url, grid(batch, [(lead, {**offered, "reading_base_limit": "0.010"})]))
    assert changed.status_code == 200 and b"Instrument reading is required." in changed.content
    # The rule is withdrawn after the page was drawn; the row was never touched. The other
    # row still saves, and nothing claims the analyst typed readings.
    other = start(config, analyst, samples=config["samples"][1:])
    lead2, copper2 = result_for(other, "Lead"), result_for(other, "Copper")
    other_url = reverse("analytical:batch-entry", args=[other.pk])
    CalculationRule.objects.filter(pk=rule.pk).update(is_active=False)
    withdrawn = client.post(other_url, grid(other, [(lead2, offered), (copper2, typed)]))
    assert withdrawn.status_code == 302
    assert not ResultRevision.objects.filter(result=lead2).exists()
    # A reading the analyst did type is still refused once the rule is gone.
    stale = client.post(other_url, grid(other, [(lead2, {**offered, "reading_reading": "0.02"})]))
    assert stale.status_code == 200 and b"changed after this page was opened" in stale.content
    # A row that is no longer current (submitted from elsewhere) reports that, not a rule change.
    CalculationRule.objects.filter(pk=rule.pk).update(is_active=True)
    rev = svc.enter_result(
        actor=analyst, result_id=lead2.pk, **diluted(reading="0.0125", calculation_token=None)
    )
    svc.submit_result(actor=analyst, revision_id=rev.pk)
    orphan = client.post(other_url, grid(other, [(lead2, offered), (copper2, typed)]))
    assert orphan.status_code == 302  # the untouched, no longer current row blocks nothing
    assert ResultRevision.objects.filter(result=copper2).count() == 2  # entered again above
    # Readings typed into a row that is no longer current are reported, not dropped.
    lost = client.post(other_url, grid(other, [(lead2, {**offered, "reading_reading": "0.03"})]))
    assert lost.status_code == 200 and b"can no longer be entered here" in lost.content
    assert b"changed after this page was opened" not in lost.content
    assert ResultRevision.objects.filter(result=lead2).count() == 1
