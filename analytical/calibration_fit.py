"""Read-only fits for recorded calibration points."""

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


def _display_square_root(value):
    numerator = Decimal(value.numerator)
    denominator = Decimal(value.denominator)
    # A rational value can be close to the square of a display-rounding midpoint.
    # Allow enough digits to distinguish them before rounding the square root.
    precision = max(numerator.adjusted(), denominator.adjusted()) + 2 * DISPLAY_DIGITS + 6
    working = Context(prec=precision, rounding=ROUND_HALF_EVEN)
    display = Context(prec=DISPLAY_DIGITS, rounding=ROUND_HALF_EVEN)
    return str(display.plus(working.sqrt(working.divide(numerator, denominator))))


def _average_factor_fit(points, observations, concentrations):
    if 0 in concentrations:
        raise ValidationError(
            "Average calibration factor requires every concentration to be greater than zero."
        )
    if len(concentrations) < 2:
        raise ValidationError(
            "Average calibration factor needs at least two distinct concentrations."
        )

    # EPA SW-846 8000D Rev. 5, sections 11.4.2 and 11.5.1 (concentration-based external standards).
    factors = [y / x for x, y in observations]
    mean = sum(factors) / len(factors)
    variance = sum((factor - mean) ** 2 for factor in factors) / (len(factors) - 1)
    rows = []
    for point, (x, y), factor in zip(points, observations, factors, strict=True):
        predicted = mean * x
        rows.append(
            dict(
                point,
                factor=_display(factor),
                predicted=_display(predicted),
                residual=_display(y - predicted),
            )
        )
    return {
        "mean_factor": _display(mean),
        "factor_sd": _display_square_root(variance),
        "factor_rsd": _display_square_root(variance * (100 / mean) ** 2) if mean > 0 else None,
        "rows": rows,
    }


def _coefficients(weighted_points, quadratic):
    # Eliminate the intercept first. The remaining normal equations are exact even for
    # closely spaced concentrations. The distinct-level check ensures full rank.
    total_weight = sum(weight for weight, _, _ in weighted_points)
    weighted_x = sum(weight * x for weight, x, _ in weighted_points)
    weighted_y = sum(weight * y for weight, _, y in weighted_points)
    weighted_xx = sum(weight * x**2 for weight, x, _ in weighted_points)
    weighted_xy = sum(weight * x * y for weight, x, y in weighted_points)
    xx = total_weight * weighted_xx - weighted_x**2
    xy = total_weight * weighted_xy - weighted_x * weighted_y

    if quadratic:
        weighted_xxx = sum(weight * x**3 for weight, x, _ in weighted_points)
        weighted_xxxx = sum(weight * x**4 for weight, x, _ in weighted_points)
        weighted_xxy = sum(weight * x**2 * y for weight, x, y in weighted_points)
        x_x_squared = total_weight * weighted_xxx - weighted_x * weighted_xx
        x_squared_x_squared = total_weight * weighted_xxxx - weighted_xx**2
        x_squared_y = total_weight * weighted_xxy - weighted_xx * weighted_y
        a = (xx * x_squared_y - x_x_squared * xy) / (xx * x_squared_x_squared - x_x_squared**2)
        b = (xy - x_x_squared * a) / xx
    else:
        a = Fraction(0)
        b = xy / xx
    c = (weighted_y - b * weighted_x - a * weighted_xx) / total_weight
    return a, b, c


def _quadratic_trend(a, b, concentrations):
    at_lower = 2 * a * min(concentrations) + b
    at_upper = 2 * a * max(concentrations) + b
    if at_lower * at_upper < 0:
        return "Turns within the recorded range"
    if not at_lower and not at_upper:
        return "Constant"

    direction = "Increasing" if max(at_lower, at_upper) > 0 else "Decreasing"
    if not at_lower:
        return f"{direction} (stationary at the lower endpoint)"
    if not at_upper:
        return f"{direction} (stationary at the upper endpoint)"
    return direction


def calculate_fit(points, weighting, model="linear"):
    """Fit every recorded point, including replicate standards, with the selected model."""
    if model not in ("linear", "linear_origin", "quadratic", "average_factor"):
        raise ValidationError(
            "Choose a linear, zero-intercept linear, quadratic or average calibration factor fit."
        )
    average_factor = model == "average_factor"
    linear_origin = model == "linear_origin"
    if average_factor:
        if weighting not in ("", "equal"):
            raise ValidationError("Average calibration factor uses equal weighting.")
    elif linear_origin:
        if weighting not in ("", "equal"):
            raise ValidationError("A zero-intercept linear fit uses equal weighting.")
    elif not isinstance(weighting, str) or weighting not in _WEIGHT_POWERS:
        raise ValidationError("Choose equal, 1/x or 1/x² weighting.")

    # Use the record validator's bounds and exact decimal parser before rational arithmetic.
    _points(points)
    observations = [
        (_fraction(point["concentration"]), _fraction(point["response"])) for point in points
    ]
    concentrations = {x for x, _ in observations}
    if average_factor:
        return _average_factor_fit(points, observations, concentrations)

    quadratic = model == "quadratic"
    if linear_origin and 0 in concentrations:
        raise ValidationError(
            "A zero-intercept linear fit requires every concentration to be greater than zero."
        )
    if quadratic and len(concentrations) < 3:
        raise ValidationError("A quadratic fit needs at least three distinct concentrations.")
    if len(concentrations) < 2:
        raise ValidationError("A linear fit needs at least two distinct concentrations.")

    if linear_origin:
        # Fix the intercept before fitting, per EPA SW-846 8000D Rev. 5, section 11.5.2.1.
        a = c = Fraction(0)
        b = sum(x * y for x, y in observations) / sum(x**2 for x, _ in observations)
    else:
        power = _WEIGHT_POWERS[weighting]
        if power and 0 in concentrations:
            raise ValidationError(
                "Inverse weighting requires every concentration to be greater than zero."
            )
        weighted_points = [(1 / x**power, x, y) for x, y in observations]
        a, b, c = _coefficients(weighted_points, quadratic)

    rows = []
    for point, (x, y) in zip(points, observations, strict=True):
        predicted = (a * x + b) * x + c
        rows.append(dict(point, predicted=_display(predicted), residual=_display(y - predicted)))
    fit = {"slope": _display(b), "intercept": _display(c), "rows": rows}
    if quadratic:
        fit.update(quadratic=_display(a), trend=_quadratic_trend(a, b, concentrations))
    return fit
