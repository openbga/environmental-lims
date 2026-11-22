"""Inspect temporary fits against recorded calibration standards."""

from datetime import timedelta

import pytest
from helpers import field, pick
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)

SOURCE = "Synthetic instrument export 100"
MODEL = "Instrument fit y = 2.8000 x - 3.1000"
BACK_CALCULATED = "Back-calculated concentration (ug/L)"
ABSOLUTE_ERROR = "Absolute percent error (%)"
MAXIMUM_ERROR = "Maximum absolute percent error"
LOWEST_MAXIMUM_ERROR = "Maximum error at lowest concentration"
APPLIED_LIMIT = "Maximum error (%)"
LIMIT_COMPARISON = "Limit comparison"
R_SQUARED = "Coefficient of determination (R²)"
PEARSON_R = "Pearson correlation coefficient (r)"
RSE = "Relative standard error (RSE)"
RSE_DF = "RSE degrees of freedom"


def recorded_curve(reference, points):
    from django.utils import timezone

    from accounts.models import User
    from analytical.calibration import record_calibration
    from analytical.services import create_batch
    from catalog.models import TestDefinition

    analyst = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    )
    batch = create_batch(actor=analyst, method=definition.method, matrix=definition.matrix)
    return record_calibration(
        actor=analyst,
        batch_id=batch.pk,
        definition_id=definition.pk,
        reference=reference,
        calibrated_at=timezone.now() - timedelta(hours=1),
        model_text=MODEL,
        response_unit="counts",
        source_reference=SOURCE,
        points=points,
    )


def test_fit_stays_separate_from_recorded_calibration(sign_in_as):
    page = sign_in_as("reviewer")
    points = [
        {"standard": "Blank", "concentration": "0.0000", "response": "-1.0000"},
        {"standard": "Low", "concentration": "1.0000", "response": "3.0000"},
        {"standard": "High", "concentration": "2.0000", "response": "4.0000"},
    ]
    curve = recorded_curve("SYNTHETIC-LINEAR-WORKSHEET", points)

    page.set_viewport_size({"width": 390, "height": 844})
    path = f"/analytical/calibrations/{curve.pk}/"
    assert page.goto(path).ok
    form = page.get_by_role("region", name="Calibration fit worksheet", exact=True)
    table = page.get_by_role("table", name="Standard points")
    residual = table.get_by_role("columnheader", name="Residual (counts)", exact=True)
    back_calculated = table.get_by_role("columnheader", name=BACK_CALCULATED, exact=True)
    error = table.get_by_role("columnheader", name=ABSOLUTE_ERROR, exact=True)
    applied_limit = table.get_by_role("columnheader", name=APPLIED_LIMIT, exact=True)
    comparison = table.get_by_role("columnheader", name=LIMIT_COMPARISON, exact=True)
    button = form.get_by_role("button", name="Calculate fit", exact=True)
    weighting = field(form, "Weighting")
    maximum = field(form, MAXIMUM_ERROR)
    lowest_maximum = field(form, LOWEST_MAXIMUM_ERROR)
    expect(form.locator("select[name=model]")).to_have_value("linear")
    expect(residual).to_have_count(0)
    expect(back_calculated).to_have_count(0)
    expect(error).to_have_count(0)
    expect(comparison).to_have_count(0)
    r_squared = form.get_by_text(R_SQUARED, exact=True)
    pearson_r = form.get_by_text(PEARSON_R, exact=True)
    expect(r_squared).to_have_count(0)
    expect(pearson_r).to_have_count(0)
    expect(form.get_by_text(RSE, exact=True)).to_have_count(0)

    weighting.press("Tab")
    expect(maximum).to_be_focused()
    maximum.press("Tab")
    expect(lowest_maximum).to_be_focused()
    lowest_maximum.press("Tab")
    expect(button).to_be_focused()
    button.press("Enter")
    expect(form).to_contain_text("Choose a weighting to calculate the fit.")
    expect(residual).to_have_count(0)

    pick(weighting, "equal")
    with page.expect_request(
        lambda request: request.is_navigation_request() and f"{path}?" in request.url
    ) as load:
        button.press("Enter")
    assert load.value.method == "GET"
    expect(residual).to_be_visible()
    expect(back_calculated).to_be_visible()
    expect(error).to_be_visible()
    expect(comparison).to_have_count(0)
    expect(r_squared.locator("xpath=following-sibling::dd")).to_have_text("0.892857142857")
    expect(pearson_r.locator("xpath=following-sibling::dd")).to_have_text("0.944911182523")
    expect(form.get_by_text(RSE, exact=True)).to_be_visible()
    expect(form).to_contain_text("Unavailable because a recorded concentration is zero.")
    expect(form).to_contain_text("3 recorded points − 2 fitted terms = 1")
    expected_blank = "Blank 0.0000 -1.0000 -0.5 -0.5 -0.2 Unavailable at zero concentration"
    expect(table.get_by_role("row", name=expected_blank, exact=True)).to_be_visible()
    expect(
        table.get_by_role("row", name="Low 1.0000 3.0000 2 1 1.4 40", exact=True)
    ).to_be_visible()
    expect(
        table.get_by_role("row", name="High 2.0000 4.0000 4.5 -0.5 1.8 10", exact=True)
    ).to_be_visible()
    scroll_area = table.locator("xpath=..")
    button.press("Tab")
    expect(scroll_area).to_be_focused()
    for _ in range(20):
        page.keyboard.press("ArrowRight")
    expect(error).to_be_in_viewport(ratio=0.95)
    expect(table.get_by_role("rowheader", name="Blank", exact=True)).to_be_in_viewport(ratio=0.95)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    pick(weighting, "inverse")
    button.press("Enter")
    expect(form.locator("#fit-results")).to_be_focused()
    expect(form).to_contain_text("Fit unavailable")
    expect(form).to_contain_text(
        "Inverse weighting requires every concentration to be greater than zero."
    )
    expect(residual).to_have_count(0)
    expect(back_calculated).to_have_count(0)
    expect(error).to_have_count(0)
    expect(r_squared).to_have_count(0)
    expect(pearson_r).to_have_count(0)
    expect(table.get_by_role("row", name="Blank 0.0000 -1.0000", exact=True)).to_be_visible()

    assert page.goto(f"{path}?weighting=equal").ok
    expect(form.locator("select[name=model]")).to_have_value("linear")
    expect(form).to_contain_text("2.5 (counts per ug/L)")
    expect(
        table.get_by_role("row", name="Low 1.0000 3.0000 2 1 1.4 40", exact=True)
    ).to_be_visible()

    lowest_maximum.fill("0")
    button.press("Enter")
    expect(maximum).to_be_focused()
    expect(form).to_contain_text(
        "Enter a general maximum to compare errors at the lowest concentration."
    )
    expect(lowest_maximum).to_have_value("0")
    expect(comparison).to_have_count(0)
    expect(table.get_by_role("row", name="Blank 0.0000 -1.0000", exact=True)).to_be_visible()

    maximum.fill("NaN")
    button.press("Enter")
    expect(form).to_contain_text("Enter a number.")
    expect(maximum).to_have_value("NaN")
    expect(lowest_maximum).to_have_value("0")
    expect(comparison).to_have_count(0)

    maximum.fill("10.000")
    with page.expect_request(
        lambda request: request.is_navigation_request() and f"{path}?" in request.url
    ) as load:
        button.press("Enter")
    assert load.value.method == "GET"
    expect(maximum).to_have_value("10.000")
    expect(lowest_maximum).to_have_value("0")
    expect(applied_limit).to_be_visible()
    expect(comparison).to_be_visible()
    for standard, limit, outcome in (
        ("Blank", "0", "Cannot compare"),
        ("Low", "10.000", "Over limit"),
        ("High", "10.000", "At or below limit"),
    ):
        row = table.get_by_role("row").filter(
            has=page.get_by_role("rowheader", name=standard, exact=True)
        )
        expect(row.get_by_role("cell").nth(-2)).to_have_text(limit)
        expect(row.get_by_role("cell").last).to_have_text(outcome)

    help_button = page.get_by_role("button", name="Help", exact=True)
    help_button.press("Enter")
    drawer = page.locator("#vr-documentation")
    drawer.locator("summary").filter(
        has=page.get_by_text("Calibration fit worksheet", exact=True)
    ).press("Enter")
    for label in (PEARSON_R, R_SQUARED, RSE, RSE_DF, MAXIMUM_ERROR, LOWEST_MAXIMUM_ERROR):
        term = drawer.get_by_text(label, exact=True).and_(drawer.locator("dt"))
        expect(term).to_be_visible()
    drawer.locator("summary").filter(has=page.get_by_text("Standard points", exact=True)).press(
        "Enter"
    )
    for label in (
        "Back-calculated concentration",
        "Absolute percent error",
        APPLIED_LIMIT,
        LIMIT_COMPARISON,
    ):
        term = drawer.get_by_text(label, exact=True).and_(drawer.locator("dt"))
        expect(term).to_be_visible()
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()
    expect(help_button).to_be_focused()

    assert page.goto(path).ok
    expect(residual).to_have_count(0)
    facts = page.get_by_role("region", name="Calibration facts")
    expect(facts).to_contain_text(MODEL)
    expect(facts).to_contain_text(SOURCE)
    curve.refresh_from_db()
    assert (curve.model_text, curve.source_reference, curve.points) == (MODEL, SOURCE, points)
    assert not page.console_errors


def test_quadratic_fit_shows_coefficients_residuals_and_guarded_inverses(sign_in_as):
    page = sign_in_as("reviewer")
    # The residual vector is orthogonal to 1, x, and x², so the exact fit is 2x² + 3x − 1.
    rows = [
        (
            "Blank",
            "0.0000",
            "-2.0000",
            "-1",
            "-1",
            "The observed response is outside the fitted response range.",
            "Unavailable",
        ),
        (
            "Blank replicate",
            "0.0000",
            "-1.0000",
            "-1",
            "0",
            "0",
            "Unavailable at zero concentration",
        ),
        ("Level 1", "1.0000", "7.0000", "4", "3", "1.38600093633", "38.6000936329"),
        ("Level 2", "2.0000", "10.0000", "13", "-3", "1.71221445045", "14.3892774775"),
        ("Level 3", "3.0000", "27.0000", "26", "1", "3.06608438062", "2.20281268728"),
        ("Level 4", "4.0000", "43.0000", "43", "0", "4", "0"),
        ("Level 5", "5.0000", "64.0000", "64", "0", "5", "0"),
    ]
    points = [
        {"standard": standard, "concentration": concentration, "response": observed}
        for standard, concentration, observed, *_ in rows
    ]
    curve = recorded_curve("SYNTHETIC-QUADRATIC-WORKSHEET", points)
    page.set_viewport_size({"width": 390, "height": 844})
    path = f"/analytical/calibrations/{curve.pk}/"
    assert page.goto(path).ok

    form = page.get_by_role("region", name="Calibration fit worksheet", exact=True)
    table = page.get_by_role("table", name="Standard points")
    history = page.get_by_role("region", name="History", exact=True)
    facts = page.get_by_role("region", name="Calibration facts", exact=True)
    button = form.get_by_role("button", name="Calculate fit", exact=True)
    residual = table.get_by_role("columnheader", name="Residual (counts)", exact=True)
    recorded_facts = facts.inner_text()
    recorded_history = history.inner_text()
    expect(residual).to_have_count(0)

    pick(field(form, "Model"), "quadratic")
    pick(field(form, "Weighting"), "equal")
    with page.expect_request(
        lambda request: request.is_navigation_request() and f"{path}?" in request.url
    ) as load:
        button.press("Enter")
    assert load.value.method == "GET"
    expect(form).to_contain_text("y = a × x² + b × x + intercept")
    expect(form).to_contain_text("2 (counts per (ug/L)²)")
    expect(form).to_contain_text("3 (counts per ug/L)")
    expect(form).to_contain_text("-1 (counts)")
    expect(form.get_by_text("Increasing", exact=True)).to_be_visible()
    r_squared = form.get_by_text(R_SQUARED, exact=True).locator("xpath=following-sibling::dd")
    expect(r_squared).to_have_text("0.994592924455")
    rse = form.get_by_text(RSE, exact=True).locator("xpath=following-sibling::dd")
    expect(rse).to_contain_text("Unavailable because")
    expect(form).to_contain_text("7 recorded points − 3 fitted terms = 4")
    expect(table.get_by_role("columnheader", name="Predicted response (counts)")).to_be_visible()
    expect(residual).to_be_visible()
    expect(table.get_by_role("columnheader", name=BACK_CALCULATED, exact=True)).to_be_visible()
    error = table.get_by_role("columnheader", name=ABSOLUTE_ERROR, exact=True)
    expect(error).to_be_visible()
    for row in rows:
        expect(table.get_by_role("row", name=" ".join(row), exact=True)).to_be_visible()
    button.press("Tab")
    expect(table.locator("xpath=..")).to_be_focused()
    for _ in range(20):
        page.keyboard.press("ArrowRight")
    expect(error).to_be_in_viewport(ratio=0.95)
    expect(table.get_by_role("rowheader", name="Blank", exact=True)).to_be_in_viewport(ratio=0.95)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer).to_contain_text("Quadratic")
    expect(drawer).to_contain_text("Direction over recorded range")
    expect(drawer).to_contain_text("Inspect the average calibration factor")
    expect(drawer).to_contain_text("Relative standard deviation (RSD)")
    drawer.locator("summary").filter(
        has=page.get_by_text("Calibration fit worksheet", exact=True)
    ).press("Enter")
    expect(drawer.get_by_text("unique inverse within the recorded range")).to_be_visible()
    expect(drawer.get_by_text("Quadratic uses three", exact=False)).to_be_visible()
    drawer.locator("summary").filter(has=page.get_by_text("Standard points", exact=True)).press(
        "Enter"
    )
    expect(drawer.get_by_text("A stationary endpoint is allowed.", exact=False)).to_be_visible()
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()

    assert page.goto(path).ok
    expect(form.locator("select[name=model]")).to_have_value("linear")
    expect(residual).to_have_count(0)
    assert facts.inner_text() == recorded_facts
    assert history.inner_text() == recorded_history
    curve.refresh_from_db()
    assert (curve.model_text, curve.source_reference, curve.points) == (MODEL, SOURCE, points)
    assert not page.console_errors


@pytest.mark.browser_context_args(color_scheme="dark")
def test_quadratic_rse_uses_each_recorded_point(sign_in_as):
    page = sign_in_as("reviewer")
    # Symmetric residuals at level 2 leave the weighted fit at 2x² + 3x − 1.
    points = [
        {"standard": "Level 1", "concentration": "1", "response": "4"},
        {"standard": "Level 2", "concentration": "2", "response": "12"},
        {"standard": "Level 2 replicate", "concentration": "2", "response": "14"},
        {"standard": "Level 3", "concentration": "3", "response": "26"},
    ]
    curve = recorded_curve("SYNTHETIC-QUADRATIC-RSE", points)
    page.set_viewport_size({"width": 1280, "height": 860})
    path = f"/analytical/calibrations/{curve.pk}/"
    assert page.goto(f"{path}?model=quadratic&weighting=inverse_square#fit-results").ok

    worksheet = page.get_by_role("region", name="Calibration fit worksheet", exact=True)
    rse = worksheet.get_by_text(RSE, exact=True).locator("xpath=following-sibling::dd")
    expect(rse).to_have_text("6.43264264107%")
    expect(worksheet).to_contain_text("4 recorded points − 3 fitted terms = 1")
    expect(worksheet).to_contain_text("RSE uses unrounded back-calculated concentrations.")
    assert not page.console_errors


def test_linear_through_origin_shows_fixed_intercept_and_only_recorded_rows(sign_in_as):
    page = sign_in_as("reviewer")
    # The fitted slope is 4 with a fixed zero intercept, 8 when free, and the mean factor is 3.
    rows = [
        ("Low", "1.00", "1.000", "4", "-3", "0.25", "75"),
        ("Low replicate", "1.0", "3", "4", "-1", "0.75", "25"),
        ("High", "2", "10.0", "8", "2", "2.5", "25"),
    ]
    points = [
        {"standard": standard, "concentration": concentration, "response": observed}
        for standard, concentration, observed, *_ in rows
    ]
    curve = recorded_curve("SYNTHETIC-ZERO-INTERCEPT-WORKSHEET", points)
    page.set_viewport_size({"width": 390, "height": 844})
    path = f"/analytical/calibrations/{curve.pk}/"
    assert page.goto(path).ok
    form = page.get_by_role("region", name="Calibration fit worksheet", exact=True)
    table = page.get_by_role("table", name="Standard points")
    model = field(form, "Model")
    weighting = field(form, "Weighting")
    button = form.get_by_role("button", name="Calculate fit", exact=True)
    residual = table.get_by_role("columnheader", name="Residual (counts)", exact=True)
    expect(residual).to_have_count(0)

    pick(model, label="Linear through origin")
    weighting_select = form.locator("select[name=weighting]")
    expect(weighting_select).to_have_value("")
    with page.expect_request(
        lambda request: request.is_navigation_request() and f"{path}?" in request.url
    ) as load:
        button.press("Enter")
    assert load.value.method == "GET"
    expect(form.get_by_text("y = slope × x", exact=True)).to_be_visible()
    expect(form).to_contain_text("4 (counts per ug/L)")
    expect(form).to_contain_text("0 (counts), fixed")
    expect(form).to_contain_text("Unweighted (equal weights)")
    expect(form.get_by_text(R_SQUARED, exact=True)).to_have_count(0)
    expect(form.get_by_text(RSE, exact=True)).to_have_count(0)
    expect(residual).to_be_visible()
    expect(table.get_by_role("row")).to_have_count(len(rows) + 1)
    for row in rows:
        expect(table.get_by_role("row", name=" ".join(row), exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer).to_contain_text("Linear through origin")
    expect(drawer).to_contain_text("adds no observation at zero")
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()

    field(form, MAXIMUM_ERROR).fill("50")
    field(form, LOWEST_MAXIMUM_ERROR).fill("20")
    weight_label = form.locator("#fit-results").get_by_text("Weighting", exact=True)
    for value, label, slope, high_row in (
        (
            "inverse_square",
            "1/x² (inverse concentration squared)",
            "3",
            "High 2 10.0 6 4 3.33333333333 66.6666666667 50 Over limit",
        ),
        (
            "inverse",
            "1/x (inverse concentration)",
            "3.5",
            "High 2 10.0 7 3 2.85714285714 42.8571428571 50 At or below limit",
        ),
    ):
        pick(weighting, value)
        button.press("Enter")
        expect(weighting_select).to_have_value(value)
        expect(weight_label.locator("xpath=following-sibling::dd")).to_have_text(label)
        expect(form).to_contain_text(f"{slope} (counts per ug/L)")
        expect(form).to_contain_text("0 (counts), fixed")
        expect(form).to_contain_text("EPA Method 8000D advises against this combination.")
        expect(form.get_by_text(R_SQUARED, exact=True)).to_have_count(0)
        expect(form.get_by_text(PEARSON_R, exact=True)).to_have_count(0)
        expect(form.get_by_text(RSE, exact=True)).to_have_count(0)
        expect(table.get_by_role("row")).to_have_count(len(rows) + 1)
        expect(table.get_by_role("row", name=high_row, exact=True)).to_be_visible()

    pick(model, label="Linear")
    button.press("Enter")
    expect(weighting_select).to_have_value("inverse")
    r_squared = form.get_by_text(R_SQUARED, exact=True).locator("xpath=following-sibling::dd")
    expect(r_squared).to_have_text("0.927536231884")
    expect(form.get_by_text(PEARSON_R, exact=True)).to_have_count(0)
    rse = form.get_by_text(RSE, exact=True).locator("xpath=following-sibling::dd")
    expect(rse).to_have_text("17.6776695297%")
    expect(form).to_contain_text("3 recorded points − 2 fitted terms = 1")
    curve.refresh_from_db()
    assert (curve.model_text, curve.source_reference, curve.points) == (MODEL, SOURCE, points)
    assert not page.console_errors


@pytest.mark.browser_context_args(java_script_enabled=False)
def test_average_factor_submits_with_keyboard_without_javascript(sign_in_as):
    page = sign_in_as("reviewer")
    rows = [
        ("Low", "1.00", "1.000", "1", "3", "-2", "0.333333333333", "66.6666666667"),
        ("Low replicate", "1.0", "3", "3", "3", "0", "1", "0"),
        ("High", "2", "10.0", "5", "6", "4", "3.33333333333", "66.6666666667"),
    ]
    points = [
        {"standard": standard, "concentration": concentration, "response": response}
        for standard, concentration, response, *_ in rows
    ]
    curve = recorded_curve("SYNTHETIC-AVERAGE-FACTOR-WORKSHEET", points)
    page.set_viewport_size({"width": 390, "height": 844})
    path = f"/analytical/calibrations/{curve.pk}/"
    assert page.goto(path).ok
    form = page.get_by_role("region", name="Calibration fit worksheet", exact=True)
    table = page.get_by_role("table", name="Standard points")
    model = field(form, "Model")
    weighting = field(form, "Weighting")
    maximum = field(form, MAXIMUM_ERROR)
    lowest_maximum = field(form, LOWEST_MAXIMUM_ERROR)
    button = form.get_by_role("button", name="Calculate fit", exact=True)
    factor = table.get_by_role("columnheader", name="Calibration factor (counts per ug/L)")
    expect(factor).to_have_count(0)

    model.select_option("average_factor")
    model.press("Tab")
    expect(weighting).to_be_focused()
    expect(weighting).to_have_value("")
    weighting.press("Tab")
    expect(maximum).to_be_focused()
    maximum.press("Tab")
    expect(lowest_maximum).to_be_focused()
    lowest_maximum.press("Tab")
    expect(button).to_be_focused()
    with page.expect_request(
        lambda request: request.is_navigation_request() and f"{path}?" in request.url
    ) as load:
        button.press("Enter")
    assert load.value.method == "GET"
    expect(form).to_contain_text("y = mean factor × x")
    expect(form.get_by_text("Intercept", exact=True)).to_have_count(0)
    expect(form).to_contain_text("3 (counts per ug/L)")
    expect(form).to_contain_text("2 (counts per ug/L)")
    expect(form).to_contain_text("66.6666666667%")
    expect(form.get_by_text(R_SQUARED, exact=True)).to_have_count(0)
    rse = form.get_by_text(RSE, exact=True).locator("xpath=following-sibling::dd")
    expect(rse).to_have_text("66.6666666667%")
    expect(form).to_contain_text("3 recorded points − 1 fitted term = 2")
    expect(factor).to_be_visible()
    for row in rows:
        expect(table.get_by_role("row", name=" ".join(row), exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    weighting.select_option("inverse")
    button.press("Enter")
    expect(form).to_contain_text("Average calibration factor uses equal weights.")
    expect(weighting).to_have_value("inverse")
    expect(factor).to_have_count(0)
    expect(table.get_by_role("row", name="Low 1.00 1.000", exact=True)).to_be_visible()
    curve.refresh_from_db()
    assert (curve.model_text, curve.source_reference, curve.points) == (MODEL, SOURCE, points)
    assert not page.console_errors
