"""BOD bottle snapshots, exact means, analyst selection and existing result gates."""

from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.urls import reverse

from analytical import bod, calculations, services, worksheets
from analytical.models import CalculationRule, ResultRevision, ResultWorksheet
from core.models import AuditEvent

from .conftest import make_user
from .test_calculation_pages import grid, stamp
from .test_calculations import rule_for
from .test_workflow import result_for, run_qc, start, when


def bottle(identifier="A", **kw):
    return {
        "identifier": identifier,
        "initial_do": "8.00",
        "final_do": "3.00",
        "sample_volume": "50.0",
        "bottle_volume": "300.0",
        "seed_correction": "0.00",
        "seed_reference": "",
        "selected": True,
        "reason": "",
        **kw,
    }


def entry(**kw):
    return {
        "supplied_text": "",
        "reporting_limit_applied": "1",
        "dilution_factor": "1",
        "analysis_started_at": when(),
        "calculation_token": "bod_bottles:1",
        "bottle_data": [bottle()],
        **kw,
    }


def configure(config, **kw):
    return rule_for(config["defs"]["Lead"], "bod_bottles", **kw)


def posted(rows=None, **kw):
    rows = [bottle()] if rows is None else rows
    data = {
        "bottles-TOTAL_FORMS": len(rows),
        "bottles-INITIAL_FORMS": len(rows),
        "bottles-MIN_NUM_FORMS": 1,
        "bottles-MAX_NUM_FORMS": 12,
        "expected_revision": "0",
        "calculation_token": "bod_bottles:1",
        "reporting_limit_applied": "1",
        "dilution_factor": "1",
        "analysis_started_at": stamp(),
        **kw,
    }
    for i, row in enumerate(rows):
        data |= {
            f"bottles-{i}-{key}": ("include" if value else "exclude")
            if key == "selected"
            else value
            for key, value in row.items()
        }
    return data


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"initial_do": "NaN"}, "initial_do"),
        ({"initial_do": "1e9999999999999999999999999999999999999"}, "initial_do"),
        ({"initial_do": "1e-9999999999999999999999999999999999999"}, "initial_do"),
        ({"final_do": "Infinity"}, "final_do"),
        ({"initial_do": -1.0}, "initial_do"),
        ({"initial_do": "-1"}, "initial_do"),
        ({"sample_volume": "0"}, "sample_volume"),
        ({"bottle_volume": "0"}, "bottle_volume"),
        ({"sample_volume": "301"}, "sample_volume"),
        ({"seed_correction": "-1"}, "seed_correction"),
        ({"seed_correction": "1"}, "seed_reference"),
        ({"final_do": "9"}, "selected"),
        ({"selected": "true"}, "selected"),
        ({"selected": False}, "reason"),
        ({"identifier": " "}, "identifier"),
        ({"reason": "x" * 241}, "reason"),
    ],
)
def test_invalid_bottle_observations_are_rejected(changes, field):
    with pytest.raises(ValidationError) as exc:
        bod.parse_bottles([bottle(**changes)])
    assert f"bottle_0_{field}" in exc.value.message_dict


def test_bounds_unique_ids_and_no_selected_bottle():
    for rows in (
        [],
        [bottle(str(i)) for i in range(13)],
        [bottle(selected=False, reason="Recorded exception")],
    ):
        with pytest.raises(ValidationError):
            bod.parse_bottles(rows)
    with pytest.raises(ValidationError, match="unique"):
        bod.parse_bottles([bottle("Bottle A"), bottle(" bottle a ")])


@pytest.mark.django_db
def test_seeded_unequal_bottle_mean_prior_dilution_originals_and_reproduction(config, analyst):
    configure(config)
    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    rows = [
        bottle(
            initial_do=" 008.00 ", seed_correction="1.00", seed_reference="Synthetic seed record S1"
        ),
        bottle("B", sample_volume="100.0"),
        bottle("C", final_do="9.00", selected=False, reason="Synthetic: oxygen increased"),
    ]
    revision = services.enter_result(
        actor=analyst, result_id=result.pk, **entry(bottle_data=rows, dilution_factor="2")
    )
    # Values before common prior dilution: 24 and 15. Excluded signed value is retained.
    assert revision.value == Decimal("39.0")
    assert revision.worksheet.bottle_data == rows
    assert worksheets.reproduce(revision.worksheet).matches
    displayed = bod.display(revision.worksheet.bottle_data)
    assert displayed[0]["initial_do"] == " 008.00 "
    assert displayed[2]["corrected_depletion"] == -1
    assert AuditEvent.objects.get(action="result.entered").after["worksheet"]["bottle_data"] == rows
    with pytest.raises(DatabaseError), transaction.atomic():
        ResultWorksheet.objects.filter(pk=revision.worksheet.pk).update(bottle_data=[])
    # Reproduction uses stored rounding, not mutable rule configuration.
    CalculationRule.objects.filter(definition=config["defs"]["Lead"]).update(rounding_digits=1)
    assert worksheets.reproduce(revision.worksheet).matches


@pytest.mark.django_db
def test_mean_is_rounded_once_and_no_automatic_threshold_acceptance(config, analyst):
    configure(
        config,
        rounding_mode=calculations.PLACES,
        rounding_digits=1,
        rounding_method=calculations.HALF_UP,
    )
    result = result_for(start(config, analyst), "Lead")
    rows = [
        bottle(str(i), initial_do="1", final_do=final, sample_volume="300")
        for i, final in enumerate(("0.86", "0.86", "0.83"))
    ]
    revision = services.enter_result(
        actor=analyst, result_id=result.pk, **entry(bottle_data=rows, reporting_limit_applied="0.1")
    )
    assert revision.value == Decimal(
        "0.2"
    )  # mean .15; mean of individually rounded values would round to .1
    assert revision.worksheet.unrounded_value == Decimal("0.15")
    assert worksheets.reproduce(revision.worksheet).matches


@pytest.mark.django_db
def test_missing_rows_wrong_calculation_and_stale_revision_refuse_without_audit(config, analyst):
    configure(config)
    batch = start(config, analyst)
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    for change in (
        {"bottle_data": None},
        {"expected_revision": 1},
        {"calculation_token": "residue_g:1"},
    ):
        with pytest.raises(ValidationError):
            services.enter_result(actor=analyst, result_id=lead.pk, **entry(**change))
    with pytest.raises(ValidationError):
        services.enter_result(actor=analyst, result_id=copper.pk, **entry())
    assert not ResultRevision.objects.exists()
    assert not AuditEvent.objects.filter(action="result.entered").exists()
    first = services.enter_result(actor=analyst, result_id=lead.pk, **entry(expected_revision=0))
    with pytest.raises(ValidationError, match="changed"):
        services.enter_result(actor=analyst, result_id=lead.pk, **entry(expected_revision=0))
    assert ResultRevision.objects.count() == 1 and first.status == "draft"


@pytest.mark.django_db
def test_existing_submission_self_review_and_independent_approval_gates(config, both, reviewer):
    configure(config)
    batch = start(config, both)
    run_qc(config, both, batch)
    result = result_for(batch, "Lead")
    revision = services.enter_result(actor=both, result_id=result.pk, **entry())
    services.submit_result(actor=both, revision_id=revision.pk)
    with pytest.raises(ValidationError, match="awaiting review"):
        services.enter_result(actor=both, result_id=result.pk, **entry())
    with pytest.raises(PermissionDenied):
        services.approve_result(actor=both, revision_id=revision.pk)
    approved = services.approve_result(actor=reviewer, revision_id=revision.pk)
    assert approved.status == "approved" and worksheets.reproduce(approved.worksheet).matches


@pytest.mark.django_db
def test_bottle_page_errors_retained_input_add_remove_and_save(config, client, analyst):
    configure(config)
    result = result_for(start(config, analyst), "Lead")
    client.force_login(analyst)
    url = reverse("analytical:result-bottles", args=[result.pk])
    assert client.get(url).status_code == 200
    bad = client.post(url, posted([bottle(sample_volume="0")]))
    assert bad.status_code == 200 and "sample_volume" in bad.context["formset"][0].errors
    assert bad.context["formset"][0]["initial_do"].value() == "8.00"
    added = client.post(url, posted(action="add"))
    assert len(added.context["formset"]) == 2 and not ResultRevision.objects.exists()
    removed = client.post(url, posted([bottle(), bottle("B", final_do="bad")], action="remove-1"))
    assert len(removed.context["formset"]) == 1
    assert removed.context["formset"][0]["initial_do"].value() == "8.00"
    saved = client.post(url, posted())
    assert saved.status_code == 302
    revision = ResultRevision.objects.get(result=result)
    html = client.get(reverse("analytical:result-detail", args=[result.pk])).content.decode()
    assert "Same result" in html and "BOD5 bottle observations" in html
    assert 'name="supplied_text"' not in html and "Record BOD5 bottles" in html
    stale = client.post(url, posted())
    assert stale.status_code == 200 and "changed" in str(stale.context["form"].errors)
    assert ResultRevision.objects.count() == 1 and revision.worksheet.bottle_data == [bottle()]


@pytest.mark.django_db
def test_bounded_formset_explicit_selection_blank_additional_rows_and_changed_rule(
    config, client, analyst
):
    rule = configure(config)
    result = result_for(start(config, analyst), "Lead")
    client.force_login(analyst)
    url = reverse("analytical:result-bottles", args=[result.pk])
    oversized = client.post(url, posted(**{"bottles-TOTAL_FORMS": "100000"}))
    assert len(oversized.context["formset"]) == 12
    assert oversized.context["formset"].non_form_errors()
    missing = posted()
    del missing["bottles-0-selected"]
    assert client.post(url, missing).context["formset"][0].errors["selected"]
    blank = client.post(url, posted([bottle(), {}], **{"bottles-INITIAL_FORMS": 1}))
    assert blank.context["formset"][1].errors
    CalculationRule.objects.filter(pk=rule.pk).update(calculation="residue_g")
    changed = client.post(url, posted())
    assert changed.status_code == 200 and "changed" in str(changed.context["form"].errors)
    assert changed.context["formset"][0]["initial_do"].value() == "8.00"
    assert not ResultRevision.objects.exists()


@pytest.mark.django_db
def test_bod_grid_links_and_numeric_grid_refusal(config, client, analyst):
    configure(config)
    batch = start(config, analyst)
    result = result_for(batch, "Lead")
    client.force_login(analyst)
    url = reverse("analytical:batch-entry", args=[batch.pk])
    page = client.get(url)
    html = page.content.decode()
    assert reverse("analytical:result-bottles", args=[result.pk]) in html
    form = next(form for form, row, _ in page.context["rows"] if row.pk == result.pk)
    assert f'name="{form.prefix}-supplied_text"' not in html
    response = client.post(
        url,
        grid(
            batch,
            [
                (
                    result,
                    {"supplied_text": "30", "dilution_factor": "1", "reporting_limit_applied": "1"},
                )
            ],
        ),
    )
    assert response.status_code == 200 and not ResultRevision.objects.exists()


@pytest.mark.django_db
def test_bottle_route_and_service_forbid_reviewers_and_client_users(
    config, client, analyst, reviewer
):
    configure(config)
    result = result_for(start(config, analyst), "Lead")
    portal = make_user("bod_portal")
    portal.client = config["samples"][0].order.project.client
    portal.save(update_fields=["client"])
    url = reverse("analytical:result-bottles", args=[result.pk])
    for user in (reviewer, portal):
        client.force_login(user)
        assert client.get(url).status_code == 403
        assert client.post(url, posted()).status_code == 403
        with pytest.raises(PermissionDenied):
            services.enter_result(actor=user, result_id=result.pk, **entry())
    assert not ResultRevision.objects.exists()


@pytest.mark.django_db
def test_non_bod_calculation_refuses_rows_and_explicit_nd_preserves_worksheet(config, analyst):
    configure(config)
    rule_for(config["defs"]["Copper"], "residue_g")
    batch = start(config, analyst)
    lead, copper = result_for(batch, "Lead"), result_for(batch, "Copper")
    with pytest.raises(ValidationError, match="does not take bottle"):
        services.enter_result(
            actor=analyst,
            result_id=copper.pk,
            **entry(
                calculation_token="residue_g:1",
                readings={"gross": "2", "tare": "1", "volume": "100"},
            ),
        )
    first = services.enter_result(
        actor=analyst, result_id=lead.pk, **entry(reporting_limit_applied="100", report_nd=True)
    )
    assert first.is_nondetect and first.worksheet.reported_as_nd
    assert worksheets.reproduce(first.worksheet).matches
    second = services.enter_result(
        actor=analyst,
        result_id=lead.pk,
        **entry(
            expected_revision=1,
            bottle_data=[bottle(final_do="4.00")],
            reason="Synthetic correction",
        ),
    )
    assert second.value == 24
    first.refresh_from_db()
    assert first.status == "superseded" and first.worksheet.bottle_data == [bottle()]
    assert worksheets.reproduce(first.worksheet).matches
    altered = second.worksheet
    altered.bottle_data[0]["selected"] = "true"
    assert not worksheets.reproduce(altered).matches


@pytest.mark.django_db
@pytest.mark.parametrize(
    "text",
    [
        "1e9999999999999999999999999999999999999",
        "1e-9999999999999999999999999999999999999",
    ],
)
def test_bottle_exponent_overflow_returns_inline_error_without_writing(
    config, client, analyst, text
):
    configure(config)
    result = result_for(start(config, analyst), "Lead")
    client.force_login(analyst)
    audit_count = AuditEvent.objects.count()
    response = client.post(
        reverse("analytical:result-bottles", args=[result.pk]),
        posted([bottle(initial_do=text)]),
    )
    assert response.status_code == 200
    row = response.context["formset"][0]
    assert "outside the range" in str(row.errors["initial_do"])
    assert row["initial_do"].value() == text
    assert row["final_do"].value() == "3.00"
    assert not ResultRevision.objects.exists()
    assert AuditEvent.objects.count() == audit_count
    result.refresh_from_db()
    assert result.latest_number == 0


@pytest.mark.django_db
def test_remove_row_bounds_attacker_indices_and_retains_known_rows(config, client, analyst):
    configure(config)
    result = result_for(start(config, analyst), "Lead")
    client.force_login(analyst)
    url = reverse("analytical:result-bottles", args=[result.pk])
    giant = "9" * 5000
    rows = [bottle(), bottle("B", initial_do="7.00")]
    audit_count = AuditEvent.objects.count()
    malformed = client.post(url, posted(rows, action=f"remove-{giant}"))
    assert malformed.status_code == 200
    assert "No row was changed" in str(malformed.context["form"].errors)
    assert len(malformed.context["formset"]) == 2
    assert malformed.context["formset"][1]["initial_do"].value() == "7.00"
    removed = client.post(
        url,
        urlencode(
            posted(
                rows,
                action="remove-0",
                **{
                    f"bottles-{giant}-identifier": "Unexpected input",
                    "bottles-99-identifier": "Outside the submitted row count",
                },
            )
        ),
        content_type="application/x-www-form-urlencoded",
    )
    assert removed.status_code == 200
    assert len(removed.context["formset"]) == 1
    assert removed.context["formset"][0]["identifier"].value() == "B"
    assert removed.context["formset"][0]["initial_do"].value() == "7.00"
    assert not ResultRevision.objects.exists()
    assert AuditEvent.objects.count() == audit_count
