"""Staff calibration entry, scoped provenance choices and stale correction behavior."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from analytical import calibration, services
from analytical.calibration_forms import CalibrationForm
from analytical.models import Result, RunCalibration
from core.models import AuditEvent
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db


def setup_run(lab, analyst):
    batch = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=RequestedTest.objects.filter(sample=lab["samples"][0]).values_list(
            "pk", flat=True
        ),
    )
    return batch, Result.objects.get(batch=batch, requested_test__definition=lab["defs"]["Lead"])


def curve_data(lab, **changes):
    return {
        "definition_id": lab["defs"]["Lead"].pk,
        "reference": "SYNTHETIC-CURVE-01",
        "calibrated_at": timezone.now() - timedelta(hours=3),
        "model_text": "Instrument-reported linear fit; synthetic",
        "response_unit": "counts",
        "source_reference": "SYNTHETIC instrument record 01",
        "points": [{"standard": "SYN-STD-1", "concentration": "0.0100", "response": "-0.0200"}],
        **changes,
    }


def record(lab, analyst, batch, **changes):
    return calibration.record_calibration(
        actor=analyst, batch_id=batch.pk, **curve_data(lab, **changes)
    )


def entry(**changes):
    return {
        "expected_revision": "0",
        "supplied_text": "0.0200",
        "reporting_limit_applied": "0.005",
        "dilution_factor": "1",
        "analysis_started_at": (timezone.localtime() - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
        **changes,
    }


def worksheet_html(response):
    return response.content.decode().split('id="fit-worksheet"', 1)[1].split("</section>", 1)[0]


def test_record_and_read_frozen_evidence(lab, analyst, reviewer, client):
    batch, _ = setup_run(lab, analyst)
    client.force_login(analyst)
    url = reverse("analytical:calibration-create", args=[batch.pk])
    assert client.get(url).status_code == 200
    data = curve_data(lab)
    data["calibrated_at"] = data["calibrated_at"].strftime("%Y-%m-%dT%H:%M%z")
    data["points"] = "standard,concentration,response\nSYN-STD-1,0.0100,-0.0200\n"
    response = client.post(url, data)
    assert response.status_code == 302
    curve = RunCalibration.objects.get()
    assert curve.points[0]["response"] == "-0.0200"
    client.force_login(reviewer)
    html = client.get(response.url).content.decode()
    for text in ("SYNTHETIC-CURVE-01", "0.0100", "-0.0200", "counts", "Calibration facts"):
        assert text in html
    batch_html = client.get(reverse("analytical:batch-detail", args=[batch.pk])).content.decode()
    assert curve.reference in batch_html and "Record calibration</a>" not in batch_html
    assert client.get(url).status_code == 403


def test_clients_cannot_read_or_record(lab, analyst, client):
    batch, result = setup_run(lab, analyst)
    curve = record(lab, analyst, batch)
    portal = User.objects.create_user(
        username="synthetic-portal", client=lab["samples"][0].order.project.client
    )
    client.force_login(portal)
    for name, pk in (
        ("calibration-detail", curve.pk),
        ("calibration-create", batch.pk),
        ("result-detail", result.pk),
    ):
        url = reverse(f"analytical:{name}", args=[pk])
        assert client.get(url).status_code == 403
        assert client.post(url, {}).status_code == 403
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    assert client.get(url, {"weighting": "equal"}).status_code == 403
    assert client.head(url, {"model": "linear", "weighting": "equal"}).status_code == 403
    assert client.get(url, {"model": "quadratic", "weighting": "equal"}).status_code == 403
    assert client.get(url, {"model": "average_factor"}).status_code == 403
    assert client.get(url, {"model": "linear_origin"}).status_code == 403


def test_fit_requires_a_choice_and_keeps_evidence_and_result_links(lab, analyst, reviewer, client):
    batch, result = setup_run(lab, analyst)
    points = [
        {"standard": "SYN-STD-1", "concentration": "1.00", "response": "2.000"},
        {"standard": "SYN-STD-2", "concentration": "2.0", "response": "4"},
        {"standard": "SYN-STD-3", "concentration": "3", "response": "8.0"},
    ]
    curve = record(lab, analyst, batch, points=points)
    client.force_login(analyst)
    result_url = reverse("analytical:result-detail", args=[result.pk])
    assert client.post(result_url, entry(calibration_id=curve.pk)).status_code == 302
    revision = result.revisions.get()
    evidence = revision.calibration_evidence
    curve_fields = RunCalibration.objects.values().get(pk=curve.pk)

    client.force_login(reviewer)
    audit_count = AuditEvent.objects.count()
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    initial = client.get(url, {"per_page": 50})
    assert initial.status_code == 200 and not initial.context["fit_form"].is_bound
    assert b"Calibration fit worksheet" in initial.content
    assert initial.context["fit_form"]["model"].value() == "linear"
    assert b"Predicted response (counts)" not in initial.content
    assert b"Back-calculated concentration (" not in initial.content
    assert b"Absolute percent error (%)" not in initial.content
    assert "Relative standard error (RSE)" not in worksheet_html(initial)
    slopes = set()
    for weighting in ("equal", "inverse", "inverse_square"):
        response = client.get(url, {"weighting": weighting})
        assert response.status_code == 200
        assert response.context["fit_form"]["model"].value() == "linear"
        assert response.context["fit_form"]["weighting"].value() == weighting
        fit = response.context["fit"]
        slopes.add(fit["slope"])
        worksheet = worksheet_html(response)
        if weighting == "equal":
            assert fit["slope"] == "3"
            assert 'class="vr-id">16.1440701613</span>%' in worksheet
        assert "y = slope × x + intercept" in response.content.decode()
        assert b"Predicted response (counts)" in response.content
        assert b"Residual (counts)" in response.content
        assert f"Back-calculated concentration ({curve.unit})" in response.content.decode()
        assert b"Absolute percent error (%)" in response.content
        assert "Relative standard error (RSE)" in worksheet
        assert "3 recorded points − 2 fitted terms = 1" in worksheet
        assert b"SYNTHETIC instrument record 01" in response.content
        assert b"Instrument-reported linear fit; synthetic" in response.content
        assert b">1.00</td>" in response.content and b">2.000</td>" in response.content
        assert b'name="weighting"' in response.content
        assert b"Result revisions using" in response.content
    assert len(slopes) == 3
    assert client.head(url, {"weighting": "equal"}).status_code == 200
    assert client.post(url, {"weighting": "equal"}).status_code == 405
    assert RunCalibration.objects.values().get(pk=curve.pk) == curve_fields
    assert result.revisions.get().calibration_evidence == evidence
    assert AuditEvent.objects.count() == audit_count


def test_quadratic_fit_displays_coefficients_direction_and_signed_residuals(
    lab, analyst, reviewer, client
):
    batch, result = setup_run(lab, analyst)
    points = [
        {"standard": "SYN-STD-1", "concentration": "1.00", "response": "2.000"},
        {"standard": "SYN-STD-2", "concentration": "2.0", "response": "4"},
        {"standard": "SYN-STD-3", "concentration": "3", "response": "7.0"},
        {"standard": "SYN-STD-4", "concentration": "3.00", "response": "9.0"},
    ]
    curve = record(lab, analyst, batch, points=points)
    curve_fields = RunCalibration.objects.values().get(pk=curve.pk)
    result_url = reverse("analytical:result-detail", args=[result.pk])
    client.force_login(analyst)
    assert client.post(result_url, entry(calibration_id=curve.pk)).status_code == 302
    revision = result.revisions.values().get()

    client.force_login(reviewer)
    audit_count = AuditEvent.objects.count()
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    response = client.get(url, {"model": "quadratic", "weighting": "inverse_square"})
    assert response.status_code == 200
    assert response.context["fit_form"]["model"].value() == "quadratic"
    assert response.context["fit_form"]["weighting"].value() == "inverse_square"
    html = response.content.decode()
    for shown in (
        "y = a × x² + b × x + intercept",
        "Quadratic coefficient (a)",
        f">1</span> (counts per ({curve.unit})²)",
        "Linear coefficient (b)",
        f">-1</span> (counts per {curve.unit})",
        "Intercept",
        ">2</span> (counts)",
        "Direction over recorded range",
        "Increasing",
        "Predicted response (counts)",
        "Residual (counts)",
        "Instrument-reported linear fit; synthetic",
        ">1.00</td>",
        ">2.000</td>",
        ">-1</td>",
        ">1</td>",
        "Result revisions using this calibration",
    ):
        assert shown in html
    assert "Back-calculated concentration (" not in html
    assert "Absolute percent error (%)" not in html
    assert "Relative standard error (RSE)" not in worksheet_html(response)
    assert client.post(url, {"model": "quadratic", "weighting": "equal"}).status_code == 405
    assert RunCalibration.objects.values().get(pk=curve.pk) == curve_fields
    assert result.revisions.values().get() == revision
    assert AuditEvent.objects.count() == audit_count


def test_linear_through_origin_fits_all_rows_and_keeps_evidence_read_only(
    lab, analyst, reviewer, client
):
    batch, result = setup_run(lab, analyst)
    points = [
        {"standard": "SYN-LOW", "concentration": "1.00", "response": "1.000"},
        {"standard": "SYN-LOW-REP", "concentration": "1.0", "response": "3"},
        {"standard": "SYN-HIGH", "concentration": "2", "response": "10.0"},
    ]
    curve = record(lab, analyst, batch, points=points)
    client.force_login(analyst)
    result_url = reverse("analytical:result-detail", args=[result.pk])
    assert client.post(result_url, entry(calibration_id=curve.pk)).status_code == 302
    revision = result.revisions.values().get()
    audit_count = AuditEvent.objects.count()

    client.force_login(reviewer)
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    params = {"model": "linear_origin"}
    response = client.get(url, params)
    assert response.status_code == 200
    fit = response.context["fit"]
    assert fit["slope"] == "4" and fit["intercept"] == "0"
    html = response.content.decode()
    for shown in (
        "y = slope × x",
        f">4</span> (counts per {curve.unit})",
        ">0</span> (counts), fixed",
        "Predicted response (counts)",
        "Residual (counts)",
        f"Back-calculated concentration ({curve.unit})",
        "Absolute percent error (%)",
        ">0.25</td>",
        ">75</td>",
    ):
        assert shown in html
    assert "Calibration factor (counts per" not in html
    assert "Relative standard error (RSE)" not in worksheet_html(response)
    for weighting in ("", "equal"):
        response = client.get(url, {**params, "weighting": weighting})
        assert response.context["fit"] == fit
        assert response.context["fit_form"]["weighting"].value() == weighting
        assert response.context["fit_weighting_label"] == "Unweighted (equal weights)"
    for weighting in ("inverse", "inverse_square"):
        response = client.get(url, {**params, "weighting": weighting})
        assert response.status_code == 200 and response.context["fit"] is None
        form = response.context["fit_form"]
        assert form["weighting"].value() == weighting
        assert form.errors["weighting"] == ["Linear through origin uses equal weights."]
        assert response.context["points"] == points
        assert b"Predicted response (counts)" not in response.content

    assert client.post(url, params).status_code == 405
    curve.refresh_from_db()
    assert curve.points == points
    assert result.revisions.values().get() == revision
    assert AuditEvent.objects.count() == audit_count


def test_fit_errors_keep_recorded_points_visible(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    curve = record(
        lab,
        analyst,
        batch,
        points=[
            {"standard": "SYN-BLANK", "concentration": "0.000", "response": "-0.0200"},
            {"standard": "SYN-LOW", "concentration": "1.00", "response": "2.000"},
        ],
    )
    client.force_login(analyst)
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    assert client.get(url, {"weighting": "equal"}).context["fit"]
    for weighting in ("", "quadratic", "inverse", "inverse_square"):
        response = client.get(url, {"weighting": weighting})
        assert response.status_code == 200 and response.context["fit"] is None
        assert b"Predicted response (counts)" not in response.content
        assert b"Back-calculated concentration (" not in response.content
        assert b"Absolute percent error (%)" not in response.content
        assert b"SYN-BLANK" in response.content and b"-0.0200" in response.content
        if weighting.startswith("inverse"):
            assert b"Fit unavailable" in response.content
            assert (
                b"Inverse weighting requires every concentration to be greater than zero."
                in response.content
            )
            assert response.context["fit_form"]["weighting"].value() == weighting
        else:
            assert b'aria-invalid="true"' in response.content
            assert response.context["fit_form"].errors["weighting"]

    for params, error in (
        ({"model": "", "weighting": "equal"}, b"Choose a calibration fit model."),
        ({"model": "cubic", "weighting": "equal"}, b"Choose a calibration fit model."),
        ({"model": "quadratic"}, b"Choose a weighting to calculate the fit."),
        (
            {"model": "quadratic", "weighting": "equal"},
            b"A quadratic fit needs at least three distinct concentrations.",
        ),
        (
            {"model": "linear_origin"},
            b"A zero-intercept linear fit requires every concentration to be greater than zero.",
        ),
    ):
        response = client.get(url, params)
        assert response.status_code == 200 and response.context["fit"] is None
        assert error in response.content
        assert b"Predicted response (counts)" not in response.content
        assert b"SYN-BLANK" in response.content and b"-0.0200" in response.content

    one_point = record(lab, analyst, batch, reference="SYNTHETIC-SINGLE-POINT")
    url = reverse("analytical:calibration-detail", args=[one_point.pk])
    response = client.get(url, {"weighting": "equal"})
    assert response.status_code == 200 and response.context["fit"] is None
    assert b"A linear fit needs at least two distinct concentrations." in response.content
    assert b"SYN-STD-1" in response.content
    response = client.get(url, {"model": "linear_origin"})
    assert response.status_code == 200 and response.context["fit"] is None
    assert b"A linear fit needs at least two distinct concentrations." in response.content
    assert b"SYN-STD-1" in response.content


def test_average_factor_uses_each_point_and_keeps_evidence_read_only(
    lab, analyst, reviewer, client
):
    batch, result = setup_run(lab, analyst)
    curve = record(
        lab,
        analyst,
        batch,
        points=[
            {"standard": "SYN-LOW", "concentration": "1.00", "response": "1.000"},
            {"standard": "SYN-LOW-REP", "concentration": "1.0", "response": "3"},
            {"standard": "SYN-HIGH", "concentration": "2", "response": "10.0"},
        ],
    )
    client.force_login(analyst)
    result_url = reverse("analytical:result-detail", args=[result.pk])
    assert client.post(result_url, entry(calibration_id=curve.pk)).status_code == 302
    revision = result.revisions.values().get()
    curve_fields = RunCalibration.objects.values().get(pk=curve.pk)
    audit_count = AuditEvent.objects.count()

    client.force_login(reviewer)
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    params = {"model": "average_factor"}
    implicit = client.get(url, params)
    assert implicit.status_code == 200
    fit = implicit.context["fit"]
    assert (fit["mean_factor"], fit["factor_sd"], fit["factor_rsd"]) == ("3", "2", "66.6666666667")
    for weighting in ("", "equal"):
        response = client.get(url, {**params, "weighting": weighting})
        assert response.context["fit"] == fit
        assert response.context["fit_form"]["weighting"].value() == weighting

    html = implicit.content.decode()
    for shown in (
        "y = mean factor × x",
        "Mean calibration factor",
        "Sample standard deviation",
        "Relative standard deviation (RSD)",
        "Unweighted (equal weights)",
        f"Calibration factor (counts per {curve.unit})",
        "Predicted response (counts)",
        "Residual (counts)",
        f"Back-calculated concentration ({curve.unit})",
        "Absolute percent error (%)",
        ">0.333333333333</td>",
        ">66.6666666667</td>",
        ">1.00</td>",
        ">1.000</td>",
        "Result revisions using this calibration",
        "Instrument-reported linear fit; synthetic",
    ):
        assert shown in html
    worksheet = worksheet_html(implicit)
    assert "Relative standard error (RSE)" in worksheet
    assert "3 recorded points − 1 fitted term = 2" in worksheet
    assert worksheet.count('class="vr-id">66.6666666667</span>%') == 2
    assert client.post(url, params).status_code == 405
    assert RunCalibration.objects.values().get(pk=curve.pk) == curve_fields
    assert result.revisions.values().get() == revision
    assert AuditEvent.objects.count() == audit_count


def test_average_factor_errors_keep_raw_observations_visible(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    client.force_login(analyst)
    points = [
        {"standard": "SYN-BLANK", "concentration": "0.000", "response": "-0.0200"},
        {"standard": "SYN-LOW", "concentration": "1.00", "response": "2.000"},
    ]
    curve = record(lab, analyst, batch, points=points)
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    params = {"model": "average_factor"}
    for weighting in ("inverse", "inverse_square"):
        response = client.get(url, {**params, "weighting": weighting})
        assert response.status_code == 200 and response.context["fit"] is None
        assert response.context["fit_form"].errors["weighting"] == [
            "Average calibration factor uses equal weights."
        ]
        assert response.context["fit_form"]["weighting"].value() == weighting
        assert b'aria-invalid="true"' in response.content
        assert b"Predicted response (counts)" not in response.content
        assert b"SYN-BLANK" in response.content and b"-0.0200" in response.content

    response = client.get(url, params)
    assert response.status_code == 200 and response.context["fit"] is None
    assert b"Fit unavailable" in response.content
    assert (
        b"Average calibration factor requires every concentration to be greater than zero."
        in response.content
    )
    assert b"SYN-BLANK" in response.content and b"-0.0200" in response.content

    one_level = record(
        lab,
        analyst,
        batch,
        reference="SYNTHETIC-ONE-LEVEL",
        points=[points[1], {**points[1], "standard": "SYN-LOW-REP", "concentration": "1"}],
    )
    response = client.get(reverse("analytical:calibration-detail", args=[one_level.pk]), params)
    assert response.status_code == 200 and response.context["fit"] is None
    assert (
        b"Average calibration factor needs at least two distinct concentrations."
        in response.content
    )
    assert b"SYN-LOW-REP" in response.content and b"2.000" in response.content


@pytest.mark.parametrize(
    ("responses", "shown"),
    [
        (("2", "4"), 'class="vr-id">0</span>%'),
        (("-2", "4"), "Unavailable when the mean factor is zero or negative."),
        (("-2", "-4"), "Unavailable when the mean factor is zero or negative."),
    ],
)
def test_average_factor_shows_rsd_state(lab, analyst, client, responses, shown):
    batch, _ = setup_run(lab, analyst)
    points = [
        {"standard": f"SYN-{x}", "concentration": str(x), "response": response}
        for x, response in enumerate(responses, start=1)
    ]
    curve = record(lab, analyst, batch, points=points)
    client.force_login(analyst)
    url = reverse("analytical:calibration-detail", args=[curve.pk])
    response = client.get(url, {"model": "average_factor"})
    assert response.status_code == 200 and response.context["fit"]
    assert shown in response.content.decode()


def test_linear_refit_explains_the_error_at_zero_concentration(lab, analyst, reviewer, client):
    batch, _ = setup_run(lab, analyst)
    curve = record(
        lab,
        analyst,
        batch,
        points=[
            {"standard": "SYN-ZERO", "concentration": "0.000", "response": "-1.00"},
            {"standard": "SYN-LOW", "concentration": "1.00", "response": "4"},
            {"standard": "SYN-HIGH", "concentration": "2", "response": "5"},
        ],
    )
    client.force_login(reviewer)
    url = reverse("analytical:calibration-detail", args=[curve.pk])

    response = client.get(url, {"model": "linear", "weighting": "equal"})

    assert response.status_code == 200
    html = response.content.decode()
    assert '<th scope="col" class="text-end">Absolute percent error (%)</th>' in html
    assert '<th scope="row"><span class="vr-preserve-space">SYN-ZERO</span></th>' in html
    assert f"Back-calculated concentration ({curve.unit})" in html
    assert ">-0.222222222222</td>" in html
    assert ">0.000</td>" in html and ">-1.00</td>" in html
    assert html.count(">Unavailable at zero concentration</td>") == 1
    assert ">44.4444444444</td>" in html
    worksheet = worksheet_html(response)
    assert "Relative standard error (RSE)" in worksheet
    assert "Unavailable because a recorded concentration is zero." in worksheet
    assert not response.context["fit_errors"]


@pytest.mark.parametrize(
    ("model", "responses", "reason"),
    [
        ("linear", ("3", "3", "3"), "fitted slope"),
        ("linear_origin", ("-2", "1"), "fitted slope"),
        ("average_factor", ("-1", "2"), "mean calibration factor"),
    ],
)
def test_flat_fit_explains_the_unavailable_refit(lab, analyst, client, model, responses, reason):
    batch, _ = setup_run(lab, analyst)
    points = [
        {"standard": f"SYN-{x}", "concentration": str(x), "response": response}
        for x, response in enumerate(responses, start=1)
    ]
    curve = record(lab, analyst, batch, points=points)
    client.force_login(analyst)

    response = client.get(
        reverse("analytical:calibration-detail", args=[curve.pk]),
        {"model": model, "weighting": "equal"},
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "Predicted response (counts)" in html
    assert "Residual (counts)" in html
    assert f"Back-calculated concentration ({curve.unit})" in html
    assert "Absolute percent error (%)" in html
    assert (
        f"Back-calculated concentration and absolute percent error are unavailable because the "
        f"{reason} is zero."
    ) in html
    assert html.count('aria-describedby="refit-unavailable-reason">Unavailable</td>') == (
        2 * len(responses)
    )
    if model != "linear_origin":
        worksheet = worksheet_html(response)
        assert "Relative standard error (RSE)" in worksheet
        assert f"Unavailable because the {reason} is zero." in worksheet
    assert not response.context["fit_errors"]


def test_linear_rse_explains_insufficient_degrees_of_freedom(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    points = [
        {"standard": "SYN-LOW", "concentration": "1", "response": "3"},
        {"standard": "SYN-HIGH", "concentration": "2", "response": "5"},
    ]
    curve = record(lab, analyst, batch, points=points)
    client.force_login(analyst)

    response = client.get(
        reverse("analytical:calibration-detail", args=[curve.pk]),
        {"model": "linear", "weighting": "equal"},
    )

    assert response.status_code == 200
    worksheet = worksheet_html(response)
    assert "Relative standard error (RSE)" in worksheet
    assert "2 recorded points − 2 fitted terms = 0" in worksheet
    assert (
        "Unavailable because there are too few recorded points for the number of fitted terms."
        in worksheet
    )
    assert not response.context["fit_errors"]


def test_csv_bounds_errors_keep_input(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    data = curve_data(lab)
    for points in (
        "standard,concentration,response\n",
        "x,y,z\na,1,2",
        "standard,concentration,response\n" + "s,1,2\n" * 51,
    ):
        form = CalibrationForm({**data, "points": points}, batch=batch)
        assert not form.is_valid() and "points" in form.errors
    client.force_login(analyst)
    data["points"] = "standard,concentration,response\nSYN,-1,2"
    response = client.post(reverse("analytical:calibration-create", args=[batch.pk]), data)
    assert response.status_code == 200 and b"SYN,-1,2" in response.content
    assert not RunCalibration.objects.exists()


def test_explicit_link_correction_and_stale_token(lab, analyst, client):
    batch, result = setup_run(lab, analyst)
    curve = record(lab, analyst, batch)
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    page = client.get(url)
    assert page.context["form"]["expected_revision"].value() == 0
    assert page.context["form"]["calibration_id"].value() is None
    assert client.post(url, entry(calibration_id=curve.pk)).status_code == 302
    first = result.revisions.get()
    assert first.calibration_id == curve.pk
    detail = client.get(url)
    assert curve.reference.encode() in detail.content
    assert detail.context["form"]["calibration_id"].value() is None
    stale = client.post(url, entry(calibration_id=curve.pk, reason="Synthetic correction"))
    assert stale.status_code == 200 and result.revisions.count() == 1
    saved = client.post(url, entry(expected_revision=1, reason="Synthetic unlinked correction"))
    assert saved.status_code == 302
    assert result.revisions.get(number=2).calibration_id is None
    assert result.revisions.get(number=1).calibration_id == curve.pk
    assert client.get(reverse("analytical:calibration-detail", args=[curve.pk])).status_code == 200


def test_unoffered_curve_and_verification_cannot_be_posted(lab, analyst, client):
    batch, result = setup_run(lab, analyst)
    copper = record(lab, analyst, batch, definition_id=lab["defs"]["Copper"].pk)
    other = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    foreign = record(lab, analyst, other)
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    choices = dict(client.get(url).context["form"].fields["calibration_id"].choices)
    assert str(copper.pk) not in choices and str(foreign.pk) not in choices
    for data in (
        entry(calibration_id=copper.pk),
        entry(calibration_id=foreign.pk),
        entry(calibration_verification_id=999999),
        entry(expected_revision=""),
    ):
        response = client.post(url, data)
        assert response.status_code == 200 and response.context["form"].errors
    assert not result.revisions.exists()


def test_verification_is_visible_and_evidence_contributors_cannot_review(
    lab, analyst, analyst2, both, reviewer, client
):
    batch, result = setup_run(lab, analyst)
    curve = record(lab, both, batch)
    sample = services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="calibration_verification", label="SYN-CCV"
    )
    measurement = services.record_qc_measurement(
        actor=analyst2,
        qc_sample_id=sample.pk,
        definition_id=lab["defs"]["Lead"].pk,
        observed_text="10.0100",
        reporting_limit_applied="0.005",
        spike_value="10",
        analysis_started_at=timezone.now() - timedelta(hours=2),
    )
    client.force_login(analyst)
    url = reverse("analytical:result-detail", args=[result.pk])
    assert (
        client.post(
            url, entry(calibration_id=curve.pk, calibration_verification_id=measurement.pk)
        ).status_code
        == 302
    )
    revision = result.revisions.get()
    assert revision.calibration_verification_id == measurement.pk
    html = client.get(url).content.decode()
    assert "SYN-CCV" in html and "10.0100" in html and "true value" in html
    # Freeze the identifying text even when the mutable QC sample label changes.
    sample.label = "SYN-RENAMED"
    sample.save(update_fields=["label"])
    assert "SYN-CCV" in client.get(url).content.decode()
    services.submit_result(actor=analyst, revision_id=revision.pk)
    from analytical.selectors import review_queue_queryset

    for contributor in (both, analyst2):
        queued = review_queue_queryset(contributor).get(pk=batch.pk)
        assert (queued.awaiting, queued.own_count, queued.reviewable) == (1, 1, 0)
    queued = review_queue_queryset(reviewer).get(pk=batch.pk)
    assert (queued.awaiting, queued.own_count, queued.reviewable) == (1, 0, 1)
    client.force_login(both)
    queue = client.get(reverse("analytical:review-list"))
    assert queue.context["page_obj"][0].reviewable == 0
    assert b"selected calibration evidence" in queue.content
    review = client.get(reverse("analytical:review-batch", args=[batch.pk]))
    assert review.status_code == 200 and b"Your work" in review.content
    assert curve.reference.encode() in review.content and b"SYN-CCV" in review.content


def test_batch_calibration_pagination_is_independent(lab, analyst, client):
    batch, _ = setup_run(lab, analyst)
    for number in range(26):
        record(lab, analyst, batch, reference=f"SYNTHETIC-CURVE-{number:02}")
    client.force_login(analyst)
    url = reverse("analytical:batch-detail", args=[batch.pk])
    response = client.get(url, {"cal_page": 2})
    assert response.status_code == 200
    assert response.context["calibration_page"].number == 2
    assert response.context["page_obj"].number == 1
    assert len(response.context["calibration_page"]) == 1
    assert b"cal_page=1" in response.content
    assert b"SYNTHETIC-CURVE-00" in response.content


def test_bottle_form_does_not_offer_calibration_evidence(lab, analyst, client):
    from .test_calculations import rule_for

    batch, result = setup_run(lab, analyst)
    rule_for(lab["defs"]["Lead"], "bod_bottles")
    client.force_login(analyst)
    response = client.get(reverse("analytical:result-bottles", args=[result.pk]))
    assert response.status_code == 200
    form = response.context["form"]
    assert "calibration_id" not in form.fields
    assert "calibration_verification_id" not in form.fields
    assert form["expected_revision"].value() == result.latest_number
    assert b'name="calibration_id"' not in response.content
    assert b'name="calibration_verification_id"' not in response.content
