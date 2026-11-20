from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from analytical.calibration_fit import calculate_fit


def point(concentration, response, standard="Test standard"):
    return {"standard": standard, "concentration": concentration, "response": response}


@pytest.mark.parametrize(
    ("weighting", "slope", "intercept", "predicted", "residual"),
    [
        # Exact slope and intercept are 50/19 and 30/19, respectively.
        (
            "equal",
            "2.63157894737",
            "1.57894736842",
            ["4.21052631579", "6.84210526316", "12.1052631579", "6.84210526316"],
            ["-0.210526315789", "1.15789473684", "-0.105263157895", "-0.842105263158"],
        ),
        # Exact slope and intercept are 46/17 and 24/17, respectively.
        (
            "inverse",
            "2.70588235294",
            "1.41176470588",
            ["4.11764705882", "6.82352941176", "12.2352941176", "6.82352941176"],
            ["-0.117647058824", "1.17647058824", "-0.235294117647", "-0.823529411765"],
        ),
        # Exact slope and intercept are 53/19 and 24/19, respectively.
        (
            "inverse_square",
            "2.78947368421",
            "1.26315789474",
            ["4.05263157895", "6.84210526316", "12.4210526316", "6.84210526316"],
            ["-0.0526315789474", "1.15789473684", "-0.421052631579", "-0.842105263158"],
        ),
    ],
)
def test_fit_uses_each_recorded_point_and_preserves_original_text(
    weighting, slope, intercept, predicted, residual
):
    points = [
        point("1.00", "4", "Standard 1"),
        point("2.000", "8.000", "Standard 2"),
        point("4e0", "12", "Standard 4"),
        point("2", "6", "Standard 2 replicate"),
    ]
    original = deepcopy(points)

    fit = calculate_fit(points, weighting)

    assert fit == {
        "slope": slope,
        "intercept": intercept,
        "rows": [
            {**row, "predicted": fit_y, "residual": error}
            for row, fit_y, error in zip(original, predicted, residual, strict=True)
        ],
    }
    assert points == original


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_fit_distinguishes_adjacent_concentrations_at_the_record_limit(weighting):
    points = [
        point("99999999999999.9999999998", "1.00"),
        point("99999999999999.9999999999", "1.0000000001"),
    ]

    fit = calculate_fit(points, weighting)

    assert fit["slope"] == "1"
    assert fit["intercept"] == "-1.00000000000E+14"
    assert [row["predicted"] for row in fit["rows"]] == ["1", "1.0000000001"]
    assert [row["residual"] for row in fit["rows"]] == ["0", "0"]


@pytest.mark.parametrize(
    ("response", "displayed"),
    [
        ("123.4567890125", "123.456789012"),
        ("123.4567890135", "123.456789014"),
        ("-123.4567890125", "-123.456789012"),
        ("-123.4567890135", "-123.456789014"),
    ],
)
def test_fit_rounds_for_display_only_and_breaks_ties_to_even(response, displayed):
    fit = calculate_fit([point("0", "0"), point("1", response)], "equal")

    assert fit["slope"] == displayed
    assert fit["rows"][1]["predicted"] == displayed
    assert fit["rows"][1]["response"] == response
    assert fit["rows"][1]["residual"] == "0"


def test_equal_weights_allow_zero_concentration_and_flat_negative_response():
    fit = calculate_fit([point("0e-1000000000", "-2.0"), point("1", "-2e0")], "equal")

    assert fit["slope"] == "0"
    assert fit["intercept"] == "-2"
    assert [(row["predicted"], row["residual"]) for row in fit["rows"]] == [("-2", "0")] * 2


@pytest.mark.parametrize("weighting", ["inverse", "inverse_square"])
def test_inverse_weights_require_strictly_positive_concentrations(weighting):
    with pytest.raises(ValidationError, match="every concentration to be greater than zero"):
        calculate_fit([point("-0.0", "1"), point("1", "2")], weighting)


@pytest.mark.parametrize("points", [[point("1", "2")], [point("1.0", "2"), point("1e0", "3")]])
def test_fit_needs_two_distinct_concentrations(points):
    with pytest.raises(ValidationError, match="at least two distinct concentrations"):
        calculate_fit(points, "equal")


@pytest.mark.parametrize(
    ("points", "message"),
    [
        (None, "between 1 and 50 standard points"),
        ([point("1", "2")] * 51, "between 1 and 50 standard points"),
        ([point("1", "2"), {"concentration": "2"}], "needs standard, concentration and response"),
        ([point("1", "2"), point("2", 3)], "bounded exact decimal text"),
        ([point("1", "2"), point("2", "1" * 61)], "bounded exact decimal text"),
        ([point("1", "2"), point("2", "NaN")], "not a plain number"),
        ([point("1", "2"), point("-2", "3")], "concentration cannot be negative"),
        ([point("1", "2"), point("2", "1e14")], "too large to store"),
        ([point("1", "2"), point("2", "1e-11")], "more than 10 decimal places"),
    ],
)
def test_fit_enforces_record_bounds(points, message):
    with pytest.raises(ValidationError, match=message):
        calculate_fit(points, "equal")


@pytest.mark.parametrize("weighting", ["equal", ""])
def test_zero_intercept_fit_uses_every_recorded_point_and_preserves_original_text(weighting):
    points = [
        point("1.00", "-1", "Standard 1"),
        point("2.000", "0.000", "Standard 2"),
        point("3e0", "12", "Standard 3"),
        point("2", "10", "Standard 2 replicate"),
    ]
    original = deepcopy(points)

    fit = calculate_fit(points, weighting, model="linear_origin")

    assert fit == {
        "slope": "3.05555555556",  # Exact slope is 55/18.
        "intercept": "0",
        "rows": [
            dict(original[0], predicted="3.05555555556", residual="-4.05555555556"),
            dict(original[1], predicted="6.11111111111", residual="-6.11111111111"),
            dict(original[2], predicted="9.16666666667", residual="2.83333333333"),
            dict(original[3], predicted="6.11111111111", residual="3.88888888889"),
        ],
    }
    assert points == original
    assert calculate_fit(points, "equal", model="average_factor")["mean_factor"] == "2"
    free_fit = calculate_fit(points, "equal")
    assert (free_fit["slope"], free_fit["intercept"]) == ("6.5", "-7.75")


def test_zero_intercept_fit_calculates_residuals_before_display_rounding():
    points = [
        point("99999999999999.9999999998", "99999999999999.9999999997"),
        point("99999999999999.9999999999", "99999999999999.9999999998"),
    ]

    fit = calculate_fit(points, "equal", model="linear_origin")

    assert fit["slope"] == "1.00000000000"
    assert [row["residual"] for row in fit["rows"]] == ["-5.00000000000E-35", "5.00000000000E-35"]


def test_zero_intercept_fit_rejects_zero_concentration_among_distinct_positive_levels():
    points = [point("1", "2"), point("2", "3"), point("-0e-1000000000", "0")]

    with pytest.raises(ValidationError, match="every concentration to be greater than zero"):
        calculate_fit(points, "equal", model="linear_origin")


@pytest.mark.parametrize("points", [[point("1", "2")], [point("1.0", "2"), point("1e0", "3")]])
def test_zero_intercept_fit_needs_two_distinct_concentrations(points):
    with pytest.raises(ValidationError, match="at least two distinct concentrations"):
        calculate_fit(points, "equal", model="linear_origin")


@pytest.mark.parametrize("weighting", ["inverse", "inverse_square", "unsupported", None, []])
def test_zero_intercept_fit_requires_equal_weighting(weighting):
    with pytest.raises(ValidationError, match="A zero-intercept linear fit uses equal weighting"):
        calculate_fit([point("1", "2"), point("2", "3")], weighting, model="linear_origin")


@pytest.mark.parametrize("weighting", ["unsupported", None, []])
def test_fit_rejects_unknown_weighting(weighting):
    with pytest.raises(ValidationError, match="Choose equal"):
        calculate_fit([point("1", "2"), point("2", "3")], weighting)


@pytest.mark.parametrize(
    ("weighting", "coefficients", "predicted", "residual"),
    [
        (
            "equal",
            ("0.854838709677", "0.467741935484", "1.06451612903"),
            ["2.38709677419", "5.41935483871", "10.1612903226", "16.6129032258", "5.41935483871"],
            [
                "-0.387096774194",
                "1.58064516129",
                "-1.16129032258",
                "0.387096774194",
                "-0.419354838710",
            ],
        ),
        (
            "inverse",
            ("0.670731707317", "1.40243902439", "0.0731707317073"),
            ["2.14634146341", "5.56097560976", "10.3170731707", "16.4146341463", "5.56097560976"],
            [
                "-0.146341463415",
                "1.43902439024",
                "-1.31707317073",
                "0.585365853659",
                "-0.560975609756",
            ],
        ),
        (
            "inverse_square",
            ("0.534482758621", "2.03448275862", "-0.517241379310"),
            ["2.05172413793", "5.68965517241", "10.3965517241", "16.1724137931", "5.68965517241"],
            [
                "-0.0517241379310",
                "1.31034482759",
                "-1.39655172414",
                "0.827586206897",
                "-0.689655172414",
            ],
        ),
    ],
)
def test_quadratic_fit_weights_every_observation_including_replicates(
    weighting, coefficients, predicted, residual
):
    points = [
        point("1.00", "2", "Standard 1"),
        point("2.000", "7.000", "Standard 2"),
        point("3e0", "9", "Standard 3"),
        point("4", "17", "Standard 4"),
        point("2", "5", "Standard 2 replicate"),
    ]
    original = deepcopy(points)

    fit = calculate_fit(points, weighting, model="quadratic")

    assert (fit["quadratic"], fit["slope"], fit["intercept"]) == coefficients
    assert fit["trend"] == "Increasing"
    assert fit["rows"] == [
        {**row, "predicted": fit_y, "residual": error}
        for row, fit_y, error in zip(original, predicted, residual, strict=True)
    ]
    assert points == original


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_quadratic_fit_preserves_rank_and_direction_at_the_record_limit(weighting):
    points = [
        point("99999999999999.9999999997", "1.00"),
        point("99999999999999.9999999998", "1.0000000001"),
        point("99999999999999.9999999999", "1.0000000004"),
    ]

    fit = calculate_fit(points, weighting, model="quadratic")

    assert fit["quadratic"] == "10000000000"
    assert fit["slope"] == "-2.00000000000E+24"
    assert fit["intercept"] == "1.00000000000E+38"
    assert fit["trend"] == "Increasing (stationary at the lower endpoint)"
    assert [row["predicted"] for row in fit["rows"]] == ["1", "1.0000000001", "1.0000000004"]
    assert [row["residual"] for row in fit["rows"]] == ["0", "0", "0"]


@pytest.mark.parametrize(
    ("responses", "trend"),
    [
        (("0", "2", "6"), "Increasing"),
        (("6", "2", "0"), "Decreasing"),
        (("0", "-1", "-2"), "Decreasing"),  # Exactly linear.
        (("-2", "-2", "-2"), "Constant"),
        (("1", "0", "1"), "Turns within the recorded range"),
        (("0", "1", "0"), "Turns within the recorded range"),
        (("0", "1", "4"), "Increasing (stationary at the lower endpoint)"),
        (("0", "3", "4"), "Increasing (stationary at the upper endpoint)"),
        (("0", "-1", "-4"), "Decreasing (stationary at the lower endpoint)"),
        (("4", "1", "0"), "Decreasing (stationary at the upper endpoint)"),
    ],
)
def test_quadratic_fit_trend_uses_the_recorded_concentration_range(responses, trend):
    low, middle, high = responses
    points = [point("1", middle), point("2", high), point("0", low)]

    assert calculate_fit(points, "equal", model="quadratic")["trend"] == trend


def test_quadratic_fit_needs_three_distinct_concentrations():
    points = [point("1", "2"), point("1.0", "3"), point("2", "4"), point("2e0", "5")]

    with pytest.raises(ValidationError, match="at least three distinct concentrations"):
        calculate_fit(points, "equal", model="quadratic")


@pytest.mark.parametrize("weighting", ["inverse", "inverse_square"])
def test_quadratic_inverse_weights_require_strictly_positive_concentrations(weighting):
    points = [point("-0.0", "1"), point("1", "2"), point("2", "3")]

    with pytest.raises(ValidationError, match="every concentration to be greater than zero"):
        calculate_fit(points, weighting, model="quadratic")


@pytest.mark.parametrize("model", ["cubic", None, []])
def test_fit_rejects_unknown_model(model):
    with pytest.raises(
        ValidationError,
        match="Choose a linear, zero-intercept linear, quadratic or average calibration factor fit",
    ):
        calculate_fit([point("1", "2"), point("2", "3")], "equal", model=model)


@pytest.mark.parametrize("weighting", ["equal", ""])
def test_average_factor_uses_each_signed_response_and_preserves_original_text(weighting):
    points = [
        point("1.00", "-1", "Standard 1"),
        point("2.000", "6.000", "Standard 2"),
        point("4e0", "12", "Standard 4"),
        point("2", "10", "Standard 2 replicate"),
    ]
    original = deepcopy(points)

    fit = calculate_fit(points, weighting, model="average_factor")

    assert fit == {
        "mean_factor": "2.5",
        "factor_sd": "2.51661147842",  # Sample variance is 19/3.
        "factor_rsd": "100.664459137",
        "rows": [
            dict(original[0], factor="-1", predicted="2.5", residual="-3.5"),
            dict(original[1], factor="3", predicted="5", residual="1"),
            dict(original[2], factor="3", predicted="10", residual="2"),
            dict(original[3], factor="5", predicted="5", residual="5"),
        ],
    }
    assert points == original


def test_average_factor_retains_dispersion_and_residuals_at_the_record_limit():
    points = [
        point("99999999999999.9999999998", "99999999999999.9999999997"),
        point("99999999999999.9999999999", "99999999999999.9999999998"),
    ]

    fit = calculate_fit(points, "equal", model="average_factor")

    assert fit["mean_factor"] == "1.00000000000"
    assert fit["factor_sd"] == "7.07106781187E-49"
    assert fit["factor_rsd"] == "7.07106781187E-47"
    assert [row["factor"] for row in fit["rows"]] == ["1.00000000000"] * 2
    assert [row["residual"] for row in fit["rows"]] == ["-5.00000000000E-35", "5.00000000000E-35"]


def test_average_factor_rounds_sample_standard_deviation_ties_to_even():
    points = [point("1", "376.5432109875"), point("2", "1000"), point("3", "1870.3703670375")]

    fit = calculate_fit(points, "equal", model="average_factor")

    assert fit["mean_factor"] == "500"
    assert fit["factor_sd"] == "123.456789012"
    assert fit["factor_rsd"] == "24.6913578025"


@pytest.mark.parametrize(
    ("responses", "mean", "sd", "rsd"),
    [
        (("-1", "2"), "0", "1.41421356237", None),
        (("-2", "0"), "-1", "1.41421356237", None),
        (("3", "6"), "3", "0", "0"),
    ],
)
def test_average_factor_reports_rsd_only_for_a_positive_mean(responses, mean, sd, rsd):
    first, second = responses
    fit = calculate_fit([point("1", first), point("2", second)], "equal", model="average_factor")

    assert (fit["mean_factor"], fit["factor_sd"], fit["factor_rsd"]) == (mean, sd, rsd)


def test_average_factor_requires_every_concentration_to_be_positive():
    points = [point("1", "2"), point("2", "3"), point("-0e-1000000000", "0")]

    with pytest.raises(ValidationError, match="every concentration to be greater than zero"):
        calculate_fit(points, "equal", model="average_factor")


def test_average_factor_needs_two_distinct_concentrations():
    points = [point("1.0", "2"), point("1e0", "3")]

    with pytest.raises(ValidationError, match="at least two distinct concentrations"):
        calculate_fit(points, "equal", model="average_factor")


@pytest.mark.parametrize("weighting", ["inverse", "inverse_square", "unsupported", None, []])
def test_average_factor_rejects_non_equal_weighting(weighting):
    with pytest.raises(ValidationError, match="Average calibration factor uses equal weighting"):
        calculate_fit([point("1", "2"), point("2", "3")], weighting, model="average_factor")
