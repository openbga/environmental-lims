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


@pytest.mark.parametrize("weighting", ["unsupported", None, []])
def test_fit_rejects_unknown_weighting(weighting):
    with pytest.raises(ValidationError, match="Choose equal"):
        calculate_fit([point("1", "2"), point("2", "3")], weighting)
