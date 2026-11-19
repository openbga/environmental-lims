"""Read-only straight-line fits for recorded calibration points."""

from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction

from django.core.exceptions import ValidationError

from .calibration import _points

DISPLAY_DIGITS = 12
_WEIGHT_POWERS = {"equal": 0, "inverse": 1, "inverse_square": 2}


def _fraction(text):
    value = Decimal(text)
    # Exact zero can be supplied with an exponent outside the bounds for nonzero values.
    return Fraction(value) if value else Fraction(0)


def _display(value):
    context = Context(prec=DISPLAY_DIGITS, rounding=ROUND_HALF_EVEN)
    return str(context.divide(Decimal(value.numerator), Decimal(value.denominator)))


def calculate_fit(points, weighting):
    """Fit a slope and intercept using every recorded point, including replicate standards."""
    if not isinstance(weighting, str) or weighting not in _WEIGHT_POWERS:
        raise ValidationError("Choose equal, 1/x or 1/x² weighting.")
    power = _WEIGHT_POWERS[weighting]

    # Use the record validator's bounds and exact decimal parser before rational arithmetic.
    _points(points)
    observations = [
        (_fraction(point["concentration"]), _fraction(point["response"])) for point in points
    ]
    concentrations = {x for x, _ in observations}
    if len(concentrations) < 2:
        raise ValidationError("A linear fit needs at least two distinct concentrations.")
    if power and 0 in concentrations:
        raise ValidationError(
            "Inverse weighting requires every concentration to be greater than zero."
        )

    # The weighted normal equations remain exact even for closely spaced concentrations.
    weighted_points = [(1 / x**power, x, y) for x, y in observations]
    total_weight = sum(weight for weight, _, _ in weighted_points)
    weighted_x = sum(weight * x for weight, x, _ in weighted_points)
    weighted_y = sum(weight * y for weight, _, y in weighted_points)
    weighted_xx = sum(weight * x * x for weight, x, _ in weighted_points)
    weighted_xy = sum(weight * x * y for weight, x, y in weighted_points)
    slope = (total_weight * weighted_xy - weighted_x * weighted_y) / (
        total_weight * weighted_xx - weighted_x**2
    )
    intercept = (weighted_y - slope * weighted_x) / total_weight

    rows = []
    for point, (x, y) in zip(points, observations, strict=True):
        predicted = slope * x + intercept
        rows.append(dict(point, predicted=_display(predicted), residual=_display(y - predicted)))
    return {"slope": _display(slope), "intercept": _display(intercept), "rows": rows}
