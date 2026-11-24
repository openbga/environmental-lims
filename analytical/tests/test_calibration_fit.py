from copy import deepcopy
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from analytical.calibration_fit import calculate_fit


def point(concentration, response, standard="Test standard"):
    return {"standard": standard, "concentration": concentration, "response": response}


@pytest.mark.parametrize(
    (
        "weighting",
        "slope",
        "intercept",
        "predicted",
        "residual",
        "back_calculated",
        "percent_error",
        "r_squared",
        "rse",
    ),
    [
        # Exact slope and intercept are 50/19 and 30/19, respectively.
        (
            "equal",
            "2.63157894737",
            "1.57894736842",
            ["4.21052631579", "6.84210526316", "12.1052631579", "6.84210526316"],
            ["-0.210526315789", "1.15789473684", "-0.105263157895", "-0.842105263158"],
            ["0.92", "2.44", "3.96", "1.68"],
            ["8", "22", "1", "16"],
            "0.939849624060",  # 125/133.
            "20.0624026477",  # sqrt((8² + 22² + 1² + 16²) / 2).
        ),
        # Exact slope and intercept are 46/17 and 24/17, respectively.
        (
            "inverse",
            "2.70588235294",
            "1.41176470588",
            ["4.11764705882", "6.82352941176", "12.2352941176", "6.82352941176"],
            ["-0.117647058824", "1.17647058824", "-0.235294117647", "-0.823529411765"],
            ["0.956521739130", "2.43478260870", "3.91304347826", "1.69565217391"],
            ["4.34782608696", "21.7391304348", "2.17391304348", "15.2173913043"],
            "0.928884986831",  # 1058/1139.
            "19.0760095378",  # 100 × sqrt(77) / 46.
        ),
        # Exact slope and intercept are 53/19 and 24/19, respectively.
        (
            "inverse_square",
            "2.78947368421",
            "1.26315789474",
            ["4.05263157895", "6.84210526316", "12.4210526316", "6.84210526316"],
            ["-0.0526315789474", "1.15789473684", "-0.421052631579", "-0.842105263158"],
            ["0.981132075472", "2.41509433962", "3.84905660377", "1.69811320755"],
            ["1.88679245283", "20.7547169811", "3.77358490566", "15.0943396226"],
            "0.918273945734",  # 2809/3059.
            "18.3901780091",  # 100 × sqrt(95) / 53.
        ),
    ],
)
def test_fit_uses_each_recorded_point_and_preserves_original_text(
    weighting, slope, intercept, predicted, residual, back_calculated, percent_error, r_squared, rse
):
    points = [
        point("1.00", "4", "Standard 1"),
        point("2.000", "8.000", "Standard 2"),
        point("4e0", "12", "Standard 4"),
        point("2", "6", "Standard 2 replicate"),
    ]
    original = deepcopy(points)

    fit = calculate_fit(points, weighting)

    correlation = {"pearson_r": "0.969458417912"} if weighting == "equal" else {}
    assert fit == {
        "slope": slope,
        "intercept": intercept,
        "back_calculation_unavailable": False,
        "r_squared": r_squared,
        **correlation,
        "rse": {"percent": rse, "reason": None, "points": 4, "terms": 2, "degrees_of_freedom": 2},
        "rows": [
            dict(
                row, predicted=fit_y, residual=dy, back_calculated=fit_x, absolute_percent_error=pct
            )
            for row, fit_y, dy, fit_x, pct in zip(
                original, predicted, residual, back_calculated, percent_error, strict=True
            )
        ],
    }
    assert points == original


@pytest.mark.parametrize(
    ("model", "weighting"),
    [
        ("linear", "equal"),
        ("linear", "inverse"),
        ("linear", "inverse_square"),
        ("linear_origin", "equal"),
        ("linear_origin", "inverse"),
        ("linear_origin", "inverse_square"),
        ("average_factor", "equal"),
    ],
)
def test_error_limits_compare_exact_values_and_cover_each_lowest_level_replicate(model, weighting):
    points = [
        point("2", "20100"),
        point("1.00", "9000"),
        point("1e0", "11000"),
        point("1", "8999.9999999999"),
        point("1", "11000.0000000001"),
        point("2.0", "19900"),
        point("2e0", "20000"),
    ]
    general, lowest = Decimal("0"), Decimal("10")

    fit = calculate_fit(points, weighting, model, max_error=general, lowest_max_error=lowest)

    assert [row["error_limit"] for row in fit["rows"]] == [general] + [lowest] * 4 + [general] * 2
    assert [row["within_error_limit"] for row in fit["rows"]] == [
        False,
        True,
        True,
        False,
        False,
        False,
        True,
    ]
    # The lowest-level replicates have exact errors of 10% and 10.000000000001%.
    assert all(Decimal(row["absolute_percent_error"]) == lowest for row in fit["rows"][1:5])


def test_error_limits_include_zero_concentration_replicates_without_assessing_them():
    points = [point("1", "1"), point("0", "0"), point("-0.00", "0"), point("2", "2")]
    general, lowest = Decimal("10"), Decimal("0")

    fit = calculate_fit(points, "equal", max_error=general, lowest_max_error=lowest)

    assert [row["error_limit"] for row in fit["rows"]] == [general, lowest, lowest, general]
    assert [row["within_error_limit"] for row in fit["rows"]] == [True, None, None, True]


@pytest.mark.parametrize(
    ("limits", "field"),
    [
        ({"max_error": 0.1}, "max_error"),
        ({"max_error": "0.1"}, "max_error"),
        ({"max_error": Decimal("Infinity")}, "max_error"),
        ({"max_error": Decimal("sNaN")}, "max_error"),
        ({"max_error": Decimal("-1")}, "max_error"),
        ({"max_error": Decimal("1e14")}, "max_error"),
        ({"max_error": Decimal("0"), "lowest_max_error": Decimal("1e-11")}, "lowest_max_error"),
        ({"max_error": Decimal("0e-1000000000")}, "max_error"),
        ({"lowest_max_error": Decimal("0")}, "max_error"),
    ],
)
def test_error_limits_require_bounded_nonnegative_decimals_and_a_general_limit(limits, field):
    with pytest.raises(ValidationError) as error:
        calculate_fit([point("1", "2"), point("2", "4")], "equal", **limits)

    assert field in error.value.error_dict


def test_zero_error_limit_with_a_large_positive_exponent_is_compared_as_exact_zero():
    limit = Decimal("-0e1000000000")
    fit = calculate_fit([point("1", "2"), point("2", "4")], "equal", max_error=limit)

    assert all(row["error_limit"] is limit for row in fit["rows"])
    assert all(row["within_error_limit"] is True for row in fit["rows"])


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
    assert fit["r_squared"] == "1"
    assert fit["rse"]["percent"] is None
    assert (
        fit["rse"]["reason"] == "there are too few recorded points for the number of fitted terms"
    )
    assert fit["rse"]["degrees_of_freedom"] == 0


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
    assert fit["pearson_r"] == ("-1" if response.startswith("-") else "1")
    assert fit["rows"][1]["predicted"] == displayed
    assert fit["rows"][1]["response"] == response
    assert fit["rows"][1]["residual"] == "0"


def test_equal_weights_allow_zero_concentration_and_flat_negative_response():
    limit = Decimal("10")
    fit = calculate_fit(
        [point("0e-1000000000", "-2.0"), point("1", "-2e0")], "equal", max_error=limit
    )

    assert fit["slope"] == "0"
    assert fit["intercept"] == "-2"
    assert fit["r_squared"] is None
    assert fit["pearson_r"] is None
    assert [(row["predicted"], row["residual"]) for row in fit["rows"]] == [("-2", "0")] * 2
    assert fit["back_calculation_unavailable"]
    assert all(
        row["back_calculated"] is row["absolute_percent_error"] is None for row in fit["rows"]
    )
    assert [row["error_limit"] for row in fit["rows"]] == [limit, limit]
    assert [row["within_error_limit"] for row in fit["rows"]] == [None, None]


@pytest.mark.parametrize("model", ["linear", "quadratic"])
def test_r_squared_is_zero_when_variable_responses_fit_a_constant(model):
    points = [point("0", "-1"), point("1", "3"), point("2", "-3"), point("3", "1")]

    fit = calculate_fit(points, "equal", model=model)

    assert fit["r_squared"] == "0"
    if model == "linear":
        assert fit["pearson_r"] == "0"
    else:
        assert "pearson_r" not in fit


def test_pearson_r_keeps_the_negative_slope_sign_with_replicate_standards():
    points = [point("1", "-4"), point("2", "-8"), point("4", "-12"), point("2", "-6")]

    fit = calculate_fit(points, "equal")

    assert fit["pearson_r"] == "-0.969458417912"  # −√(125/133).


@pytest.mark.parametrize(
    ("model", "r_squared", "pearson_r"),
    [("linear", "0.852631578947", "0.923380516877"), ("quadratic", "0.905263157895", None)],
)
def test_fit_statistics_preserve_response_differences_below_display_precision(
    model, r_squared, pearson_r
):
    points = [
        point("1", "99999999999999.9999999996"),
        point("2", "99999999999999.9999999997"),
        point("3", "99999999999999.9999999997"),
        point("4", "99999999999999.9999999999"),
    ]

    fit = calculate_fit(points, "equal", model=model)

    assert [row["predicted"] for row in fit["rows"]] == ["1.00000000000E+14"] * 4
    assert fit["r_squared"] == r_squared
    if pearson_r is not None:
        # √(81/95) differs at the last digit from taking the root of the displayed R².
        assert fit["pearson_r"] == pearson_r
    else:
        assert "pearson_r" not in fit


def test_r_squared_rounds_display_midpoints_to_even():
    signal = 999_999
    points = [
        point(concentration, str(mean + error))
        for concentration, mean, deviations in (
            ("1", -signal, (999_999, 1_995)),
            ("2", signal, (127, 62)),
        )
        for deviation in deviations
        for error in (-2 * deviation, 2 * deviation)
    ]

    fit = calculate_fit(points, "equal")

    # The symmetric pairs give R² = signal² / (signal² + Σ deviations²) = 0.4999990000005.
    assert fit["r_squared"] == "0.499999000000"


def test_zero_recorded_concentration_still_has_a_signed_back_calculation():
    points = [point("-0e-1000000000", "-1"), point("1", "4"), point("2", "5")]

    fit = calculate_fit(points, "equal")

    assert fit["slope"] == "3"
    assert fit["rows"][0]["concentration"] == "-0e-1000000000"
    assert [row["back_calculated"] for row in fit["rows"]] == [
        "-0.222222222222",
        "1.44444444444",
        "1.77777777778",
    ]
    assert [row["absolute_percent_error"] for row in fit["rows"]] == [
        None,
        "44.4444444444",
        "11.1111111111",
    ]
    assert fit["rse"]["percent"] is None
    assert fit["rse"]["reason"] == "a recorded concentration is zero"
    assert fit["rse"]["degrees_of_freedom"] == 1


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_rse_is_zero_for_an_exact_fit(weighting):
    fit = calculate_fit([point("1", "3"), point("2", "5"), point("4", "9")], weighting)

    assert fit["rse"]["percent"] == "0"


def test_linear_rse_uses_unrounded_inverses_at_the_record_limit():
    points = [
        point("99999999999999.9999999997", "1.00"),
        point("99999999999999.9999999998", "1.0000000001"),
        point("99999999999999.9999999999", "1.0000000004"),
    ]

    fit = calculate_fit(points, "equal")

    assert [row["back_calculated"] for row in fit["rows"]] == ["1.00000000000E+14"] * 3
    assert fit["rse"]["percent"] == "4.08248290464E-23"


@pytest.mark.parametrize(
    ("model", "rse"),
    [
        ("linear", "212.132034356"),  # sqrt((150² + 150²) / 1).
        ("average_factor", "150"),  # sqrt((150² + 150²) / 2).
    ],
)
def test_rse_keeps_signed_inverses_and_negative_fit_parameters(model, rse):
    points = [point("1", "1"), point("1", "-5"), point("2", "-4")]

    fit = calculate_fit(points, "equal", model=model)

    assert [row["back_calculated"] for row in fit["rows"]] == ["-0.5", "2.5", "2"]
    assert fit["rse"]["percent"] == rse
    if model == "average_factor":
        assert fit["factor_rsd"] is None


def test_back_calculation_uses_the_unrounded_intercept():
    fit = calculate_fit(
        [point("0", "99999999999999.9999999997"), point("1e-10", "99999999999999.9999999998")],
        "equal",
    )

    assert fit["intercept"] == "1.00000000000E+14"
    assert [row["back_calculated"] for row in fit["rows"]] == ["0", "1E-10"]
    assert [row["absolute_percent_error"] for row in fit["rows"]] == [None, "0"]


@pytest.mark.parametrize("model", ["linear", "linear_origin", "average_factor"])
def test_a_negative_slope_or_mean_can_be_inverted(model):
    fit = calculate_fit([point("1", "-2"), point("2", "-4")], "equal", model=model)

    parameter = "mean_factor" if model == "average_factor" else "slope"
    assert fit[parameter] == "-2"
    assert not fit["back_calculation_unavailable"]
    assert [row["back_calculated"] for row in fit["rows"]] == ["1", "2"]
    assert [row["absolute_percent_error"] for row in fit["rows"]] == ["0", "0"]


@pytest.mark.parametrize(
    ("model", "weighting", "responses"),
    [
        ("linear_origin", "equal", ("-2", "1")),
        ("linear_origin", "inverse", ("-1", "1")),
        ("linear_origin", "inverse_square", ("-1", "2")),
        ("average_factor", "equal", ("-1", "2")),
    ],
)
def test_zero_slope_or_mean_keeps_forward_predictions_and_residuals(model, weighting, responses):
    points = [point("1", responses[0]), point("2", responses[1])]

    fit = calculate_fit(points, weighting, model=model)

    assert fit["back_calculation_unavailable"]
    assert [row["predicted"] for row in fit["rows"]] == ["0", "0"]
    assert [row["residual"] for row in fit["rows"]] == list(responses)
    assert all(
        row["back_calculated"] is row["absolute_percent_error"] is None for row in fit["rows"]
    )


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
        "rse": {
            "percent": "104.277654371",
            "reason": None,
            "points": 4,
            "terms": 1,
            "degrees_of_freedom": 3,
        },
        "back_calculation_unavailable": False,
        "rows": [
            dict(
                original[0],
                predicted="3.05555555556",
                residual="-4.05555555556",
                back_calculated="-0.327272727273",
                absolute_percent_error="132.727272727",
            ),
            dict(
                original[1],
                predicted="6.11111111111",
                residual="-6.11111111111",
                back_calculated="0",
                absolute_percent_error="100",
            ),
            dict(
                original[2],
                predicted="9.16666666667",
                residual="2.83333333333",
                back_calculated="3.92727272727",
                absolute_percent_error="30.9090909091",
            ),
            dict(
                original[3],
                predicted="6.11111111111",
                residual="3.88888888889",
                back_calculated="3.27272727273",
                absolute_percent_error="63.6363636364",
            ),
        ],
    }
    assert points == original
    assert calculate_fit(points, "equal", model="average_factor")["mean_factor"] == "2"
    free_fit = calculate_fit(points, "equal")
    assert (free_fit["slope"], free_fit["intercept"]) == ("6.5", "-7.75")


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_zero_intercept_fit_calculates_residuals_before_display_rounding(weighting):
    points = [
        point("99999999999999.9999999998", "99999999999999.9999999997"),
        point("99999999999999.9999999999", "99999999999999.9999999998"),
    ]

    fit = calculate_fit(points, weighting, model="linear_origin")

    assert fit["slope"] == "1.00000000000"
    assert [row["residual"] for row in fit["rows"]] == ["-5.00000000000E-35", "5.00000000000E-35"]
    assert [row["back_calculated"] for row in fit["rows"]] == ["1.00000000000E+14"] * 2
    assert [row["absolute_percent_error"] for row in fit["rows"]] == ["5.00000000000E-47"] * 2
    assert fit["rse"]["percent"] == "7.07106781187E-47"


@pytest.mark.parametrize(
    ("weighting", "slope", "rse", "calculated_rows"),
    [
        (
            "inverse",
            "2.625",  # Sum(y) / sum(x) = 21/8.
            "115.470053838",
            [
                ("2.625", "-3.625", "-0.380952380952", "138.095238095"),
                ("5.25", "-5.25", "0", "100"),
                ("7.875", "4.125", "4.57142857143", "52.3809523810"),
                ("5.25", "4.75", "3.80952380952", "90.4761904762"),
            ],
        ),
        (
            "inverse_square",
            "2",  # Mean(y/x) = (-1 + 0 + 4 + 5) / 4.
            "147.196014439",
            [
                ("2", "-3", "-0.5", "150"),
                ("4", "-4", "0", "100"),
                ("6", "6", "6", "100"),
                ("4", "6", "5", "150"),
            ],
        ),
    ],
)
def test_weighted_zero_intercept_fit_uses_every_recorded_point(
    weighting, slope, rse, calculated_rows
):
    points = [
        point("1.00", "-1", "Standard 1"),
        point("2.000", "0.000", "Standard 2"),
        point("3e0", "12", "Standard 3"),
        point("2", "10", "Standard 2 replicate"),
    ]
    original = deepcopy(points)

    fit = calculate_fit(points, weighting, model="linear_origin")

    assert fit == {
        "slope": slope,
        "intercept": "0",
        "rse": {"percent": rse, "reason": None, "points": 4, "terms": 1, "degrees_of_freedom": 3},
        "back_calculation_unavailable": False,
        "rows": [
            dict(
                row,
                predicted=predicted,
                residual=residual,
                back_calculated=back_calculated,
                absolute_percent_error=percent_error,
            )
            for row, (predicted, residual, back_calculated, percent_error) in zip(
                original, calculated_rows, strict=True
            )
        ],
    }
    assert points == original


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_zero_intercept_fit_rejects_zero_concentration_among_distinct_positive_levels(weighting):
    points = [point("1", "2"), point("2", "3"), point("-0e-1000000000", "0")]

    with pytest.raises(ValidationError, match="every concentration to be greater than zero"):
        calculate_fit(points, weighting, model="linear_origin")


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
@pytest.mark.parametrize("points", [[point("1", "2")], [point("1.0", "2"), point("1e0", "3")]])
def test_zero_intercept_fit_needs_two_distinct_concentrations(points, weighting):
    with pytest.raises(ValidationError, match="at least two distinct concentrations"):
        calculate_fit(points, weighting, model="linear_origin")


@pytest.mark.parametrize("model", ["linear", "linear_origin", "quadratic"])
@pytest.mark.parametrize("weighting", ["unsupported", None, []])
def test_fit_rejects_unknown_weighting(weighting, model):
    with pytest.raises(ValidationError, match="Choose equal"):
        calculate_fit([point("1", "2"), point("2", "3")], weighting, model)


@pytest.mark.parametrize(
    (
        "weighting",
        "coefficients",
        "predicted",
        "residual",
        "back_calculated",
        "percent_error",
        "r_squared",
    ),
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
            [None, "2.37561212873", "2.78547827913", None, "1.88942741181"],
            [None, "18.7806064365", "7.15072402906", None, "5.52862940970"],
            "0.966229838710",  # 1917/1984.
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
            [None, "2.33393118776", "2.74955087163", None, "1.85944300043"],
            [None, "16.6965593882", "8.34830427899", None, "7.02784997867"],
            "0.962607891975",  # 61450/63837.
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
            [None, "2.30234010070", "2.72589074892", None, "1.83105446095"],
            [None, "15.1170050349", "9.13697503589", None, "8.44727695250"],
            "0.958989268490",  # 264869/276196.
        ),
    ],
)
def test_quadratic_fit_weights_every_observation_including_replicates(
    weighting, coefficients, predicted, residual, back_calculated, percent_error, r_squared
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
    assert fit["back_calculation_reason"] is None
    assert fit["r_squared"] == r_squared
    assert fit["rse"] == {
        "percent": None,
        "reason": "a recorded response is outside the fitted response range",
        "points": 5,
        "terms": 3,
        "degrees_of_freedom": 2,
    }
    assert fit["rows"] == [
        dict(
            row,
            predicted=fit_y,
            residual=dy,
            back_calculated=fit_x,
            absolute_percent_error=pct,
            back_calculation_reason=(
                "The observed response is outside the fitted response range."
                if fit_x is None
                else None
            ),
        )
        for row, fit_y, dy, fit_x, pct in zip(
            original, predicted, residual, back_calculated, percent_error, strict=True
        )
    ]
    assert points == original


@pytest.mark.parametrize("sign", ["", "-"])
@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_quadratic_error_limits_use_exact_response_bounds(weighting, sign):
    points = [
        point(level, sign + response)
        for level, response in [
            ("1", "1000000000000"),
            ("2", "4840000000000"),
            ("2", "3160000000000"),
            ("2", "4840000000000.0000000001"),
            ("2", "3159999999999.9999999999"),
            ("3", "9000000000001"),
            ("3", "8999999999999"),
        ]
    ]
    limit = Decimal("10")

    fit = calculate_fit(points, weighting, "quadratic", max_error=limit)

    assert [row["error_limit"] for row in fit["rows"]] == [limit] * len(points)
    assert [row["within_error_limit"] for row in fit["rows"]] == [
        True,
        True,
        False,
        False,
        False,
        None,
        True,
    ]
    # For y = ±10¹²x², these responses invert to 2.2 and just above 2.2.
    assert Decimal(fit["rows"][1]["absolute_percent_error"]) == limit
    assert Decimal(fit["rows"][3]["absolute_percent_error"]) == limit


@pytest.mark.parametrize("weighting", ["equal", "inverse", "inverse_square"])
def test_quadratic_fit_preserves_rank_and_direction_at_the_record_limit(weighting):
    points = [
        point("99999999999999.9999999997", "1.00"),
        point("99999999999999.9999999998", "1.0000000001"),
        point("99999999999999.9999999999", "1.0000000004"),
        point("99999999999999.9999999998", "1.00", "Middle standard replicate A"),
        point("99999999999999.9999999998", "1.0000000002", "Middle standard replicate B"),
    ]

    fit = calculate_fit(points, weighting, model="quadratic")

    assert fit["quadratic"] == "10000000000"
    assert fit["slope"] == "-2.00000000000E+24"
    assert fit["intercept"] == "1.00000000000E+38"
    assert fit["trend"] == "Increasing (stationary at the lower endpoint)"
    assert [row["predicted"] for row in fit["rows"]] == [
        "1",
        "1.0000000001",
        "1.0000000004",
        "1.0000000001",
        "1.0000000001",
    ]
    assert [row["residual"] for row in fit["rows"]] == ["0", "0", "0", "-1E-10", "1E-10"]
    assert [row["back_calculated"] for row in fit["rows"]] == ["1.00000000000E+14"] * 5
    assert [row["absolute_percent_error"] for row in fit["rows"]] == [
        "0",
        "0",
        "0",
        "1.00000000000E-22",
        "4.14213562373E-23",
    ]
    assert fit["rse"]["percent"] == "7.65366864730E-23"


@pytest.mark.parametrize(
    ("weighting", "rse"),
    [("equal", "16.1045407632"), ("inverse", "14.5445180523"), ("inverse_square", "13.9448554705")],
)
def test_quadratic_rse_uses_weights_only_in_the_fit_and_counts_each_replicate(weighting, rse):
    points = [
        point(x, y) for x, y in [("1", "2"), ("2", "4"), ("2.0", "6"), ("3", "12"), ("4", "17")]
    ]

    fit = calculate_fit(points, weighting, "quadratic")

    assert fit["rse"] == {
        "percent": rse,
        "reason": None,
        "points": 5,
        "terms": 3,
        "degrees_of_freedom": 2,
    }


def test_quadratic_rse_sums_rational_inverse_errors_including_the_range_endpoint():
    points = [
        point(x, y) for x, y in [("1", "1"), ("5", "25"), ("8", "64"), ("5", "1"), ("5", "49")]
    ]

    fit = calculate_fit(points, "equal", "quadratic")

    assert fit["quadratic"] == "1"
    assert [row["back_calculated"] for row in fit["rows"]] == ["1", "5", "8", "1", "7"]
    assert fit["rse"]["percent"] == "63.2455532034"  # sqrt((80² + 40²) / 2).


def test_quadratic_rse_handles_a_rational_squared_error_from_an_irrational_inverse():
    points = [
        point(x, y)
        for x, y in [("1", "14"), ("13", "49"), ("20", "841"), ("21", "1"), ("32", "961")]
    ]

    fit = calculate_fit(points, "equal", "quadratic")

    # The fitted curve is (x − 1)². At x = 1, the squared relative error is exactly 14.
    assert fit["trend"] == "Increasing (stationary at the lower endpoint)"
    assert [row["back_calculated"] for row in fit["rows"]] == [
        "4.74165738677",
        "8",
        "30",
        "2",
        "32",
    ]
    assert fit["rse"]["percent"] == "275.830773287"


@pytest.mark.parametrize(
    ("deviation", "rse"),
    [("123.4567890125", "0.0000123456789012"), ("123.4567890135", "0.0000123456789014")],
)
def test_quadratic_rse_rounds_exact_midpoints_and_keeps_three_terms_for_a_straight_line(
    deviation, rse
):
    middle = Decimal("1000000000")
    points = [
        point("1", "1"),
        point(str(middle), str(middle)),
        point(str(middle), str(middle + Decimal(deviation))),
        point(str(middle), str(middle - Decimal(deviation))),
        point("2000000000", "2000000000"),
    ]

    fit = calculate_fit(points, "equal", "quadratic")

    assert fit["quadratic"] == "0"
    assert fit["rse"]["terms"] == 3
    assert fit["rse"]["degrees_of_freedom"] == 2
    assert fit["rse"]["percent"] == rse


@pytest.mark.parametrize(
    ("upper_response", "rse"),
    [
        ("9999999999999.9999999999", "12.3456789013"),
        ("10000000000000.0000000001", "12.3456789012"),
    ],
)
def test_quadratic_rse_resolves_irrational_results_on_both_sides_of_a_midpoint(upper_response, rse):
    points = [
        point("1", "1"),
        point("1e12", "1e12"),
        point("1e13", upper_response),
        point("1e12", "1123456789012.5"),
        point("1e12", "876543210987.5"),
    ]

    fit = calculate_fit(points, "equal", "quadratic")

    # With an upper response of 1e13 the RSE is exactly 12.34567890125.
    assert fit["rse"]["percent"] == rse


@pytest.mark.parametrize(
    ("points", "reason"),
    [
        ([("1", "1"), ("2", "4"), ("3", "9"), ("4", "16")], None),
        (
            [("1", "1"), ("2", "4"), ("3", "9")],
            "there are too few recorded points for the number of fitted terms",
        ),
        (
            [("0", "0"), ("1", "1"), ("2", "4"), ("3", "9")],
            "a recorded concentration is zero",
        ),
        (
            [("1", "3"), ("2", "3"), ("3", "3"), ("4", "3")],
            "the fitted response is constant",
        ),
        (
            [("1", "1"), ("2", "0"), ("3", "1"), ("4", "4")],
            "the fitted curve turns within the recorded concentration range",
        ),
    ],
)
def test_quadratic_rse_reports_zero_for_an_exact_fit_or_explains_when_it_is_unavailable(
    points, reason
):
    fit = calculate_fit([point(x, y) for x, y in points], "equal", "quadratic")

    assert fit["rse"] == {
        "percent": "0" if reason is None else None,
        "reason": reason,
        "points": len(points),
        "terms": 3,
        "degrees_of_freedom": len(points) - 3,
    }


@pytest.mark.parametrize(
    ("responses", "trend", "reason"),
    [
        (("0", "2", "6"), "Increasing", None),
        (("6", "2", "0"), "Decreasing", None),
        (("-2", "-2", "-2"), "Constant", "The fitted response is constant."),
        (
            ("1", "0", "1"),
            "Turns within the recorded range",
            "The fitted curve turns within the recorded concentration range.",
        ),
        (
            ("0", "1", "0"),
            "Turns within the recorded range",
            "The fitted curve turns within the recorded concentration range.",
        ),
        (("0", "1", "4"), "Increasing (stationary at the lower endpoint)", None),
        (("0", "3", "4"), "Increasing (stationary at the upper endpoint)", None),
        (("0", "-1", "-4"), "Decreasing (stationary at the lower endpoint)", None),
        (("4", "1", "0"), "Decreasing (stationary at the upper endpoint)", None),
    ],
)
def test_quadratic_fit_trend_and_inverse_use_the_recorded_concentration_range(
    responses, trend, reason
):
    low, middle, high = responses
    points = [point("1", middle), point("2", high), point("0", low)]

    fit = calculate_fit(points, "equal", model="quadratic")

    assert fit["trend"] == trend
    assert fit["back_calculation_reason"] == reason
    assert fit["r_squared"] == (None if trend == "Constant" else "1")
    assert [row["back_calculation_reason"] for row in fit["rows"]] == [reason] * 3
    assert [row["back_calculated"] for row in fit["rows"]] == (
        [None, None, None] if reason else ["1", "2", "0"]
    )
    assert [row["absolute_percent_error"] for row in fit["rows"]] == (
        [None, None, None] if reason else ["0", "0", None]
    )


def test_quadratic_inverse_checks_unrounded_response_limits_for_a_decreasing_curve():
    points = [
        point("0", "10000000000000"),
        point("1", "9999999999999"),
        point("2", "9999999999996"),
        point("2", "9999999999996.0000000001", "Upper-level replicate A"),
        point("2", "9999999999995.9999999999", "Upper-level replicate B"),
    ]

    fit = calculate_fit(points, "equal", model="quadratic")

    assert fit["quadratic"] == "-1"
    assert fit["trend"] == "Decreasing (stationary at the lower endpoint)"
    assert [row["predicted"] for row in fit["rows"]] == ["1.00000000000E+13"] * len(points)
    assert fit["rows"][3]["back_calculated"] == "1.99999999997"
    assert fit["rows"][3]["absolute_percent_error"] == "1.25000000001E-9"
    assert fit["rows"][4]["back_calculated"] is fit["rows"][4]["absolute_percent_error"] is None
    assert fit["rows"][4]["back_calculation_reason"] == (
        "The observed response is outside the fitted response range."
    )


def test_quadratic_inverse_refines_irrational_roots_close_to_a_rounding_midpoint():
    points = [
        point("0", "0"),
        point("100", "100"),
        point("100", "100.0000000015", "Middle standard replicate A"),
        point("100", "99.9999999985", "Middle standard replicate B"),
        point("1e13", "10000000000000.0000000001"),
    ]

    fit = calculate_fit(points, "equal", model="quadratic")

    # A tiny positive quadratic term puts the inverse on the lower side of the
    # midpoint at 100.0000000015. The displayed concentrations differ asymmetrically,
    # even though both unrounded errors agree to twelve significant digits.
    assert fit["quadratic"] == "1.00000000001E-36"
    assert [row["back_calculated"] for row in fit["rows"][2:4]] == [
        "100.000000001",
        "99.9999999985",
    ]
    assert [row["absolute_percent_error"] for row in fit["rows"][2:4]] == ["1.50000000000E-9"] * 2


def test_quadratic_rational_inverse_rounds_display_midpoints_to_even():
    points = [
        point("100000000000", "4000000000000"),
        point("200000000000", "16000000000000"),
        point("300000000000", "36000000000000"),
        point("200000000000", "16000000000080.0000000001", "Middle standard replicate A"),
        point("200000000000", "15999999999919.9999999999", "Middle standard replicate B"),
        point("200000000000", "16000000000240.0000000009", "Middle standard replicate C"),
        point("200000000000", "15999999999759.9999999991", "Middle standard replicate D"),
    ]

    fit = calculate_fit(points, "equal", model="quadratic")

    # For y = 4e-10 x², the responses of A and C give exact .5 ties at display precision.
    assert fit["quadratic"] == "4E-10"
    assert fit["rows"][3]["back_calculated"] == "200000000000"
    assert fit["rows"][3]["absolute_percent_error"] == "2.5E-10"
    assert fit["rows"][5]["back_calculated"] == "200000000002"
    assert fit["rows"][5]["absolute_percent_error"] == "7.5E-10"


def test_quadratic_fit_needs_three_distinct_concentrations():
    points = [point("1", "2"), point("1.0", "3"), point("2", "4"), point("2e0", "5")]

    with pytest.raises(ValidationError, match="at least three distinct concentrations"):
        calculate_fit(points, "equal", model="quadratic")


def test_exactly_linear_quadratic_fit_inverts_only_within_the_recorded_range():
    points = [
        point("0", "3"),
        point("1", "2"),
        point("2", "1"),
        point("0", "4", "Zero standard replicate A"),
        point("0", "2", "Zero standard replicate B"),
        point("2", "0", "Upper-level replicate A"),
        point("2", "2", "Upper-level replicate B"),
    ]

    fit = calculate_fit(points, "equal", "quadratic")

    assert fit["quadratic"] == "0"
    assert fit["slope"] == "-1"
    assert fit["trend"] == "Decreasing"
    assert fit["back_calculation_reason"] is None
    assert "back_calculation_unavailable" not in fit
    assert fit["rse"]["reason"] == "a recorded concentration is zero"
    outside = "The observed response is outside the fitted response range."
    assert [row["back_calculation_reason"] for row in fit["rows"]] == [
        None,
        None,
        None,
        outside,
        None,
        outside,
        None,
    ]
    assert [row["back_calculated"] for row in fit["rows"]] == ["0", "1", "2", None, "1", None, "1"]
    assert [row["absolute_percent_error"] for row in fit["rows"]] == [
        None,
        "0",
        "0",
        None,
        None,
        None,
        "50",
    ]


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
        "back_calculation_unavailable": False,
        "rse": {
            "percent": "100.664459137",
            "reason": None,
            "points": 4,
            "terms": 1,
            "degrees_of_freedom": 3,
        },
        "rows": [
            dict(
                original[0],
                factor="-1",
                predicted="2.5",
                residual="-3.5",
                back_calculated="-0.4",
                absolute_percent_error="140",
            ),
            dict(
                original[1],
                factor="3",
                predicted="5",
                residual="1",
                back_calculated="2.4",
                absolute_percent_error="20",
            ),
            dict(
                original[2],
                factor="3",
                predicted="10",
                residual="2",
                back_calculated="4.8",
                absolute_percent_error="20",
            ),
            dict(
                original[3],
                factor="5",
                predicted="5",
                residual="5",
                back_calculated="4",
                absolute_percent_error="100",
            ),
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
    assert fit["rse"]["percent"] == "7.07106781187E-47"
    assert [row["factor"] for row in fit["rows"]] == ["1.00000000000"] * 2
    assert [row["residual"] for row in fit["rows"]] == ["-5.00000000000E-35", "5.00000000000E-35"]
    assert [row["back_calculated"] for row in fit["rows"]] == ["1.00000000000E+14"] * 2
    assert [row["absolute_percent_error"] for row in fit["rows"]] == ["5.00000000000E-47"] * 2


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
