"""Read-only fits for recorded calibration points."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction
from math import isqrt

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator

from .calibration import _points
from .models import VALUE_FIELD

DISPLAY_DIGITS = 12
_WEIGHT_POWERS = {"equal": 0, "inverse": 1, "inverse_square": 2}


def _fraction(value):
    value = Decimal(value)
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


def _display_back_calculation(concentration, back_calculated):
    percent_error = (
        100 * abs(back_calculated - concentration) / concentration if concentration else None
    )
    return {
        "back_calculated": _display(back_calculated),
        "absolute_percent_error": _display(percent_error) if percent_error is not None else None,
    }


def _back_calculation(concentration, response, slope, intercept=0):
    if not slope:
        return {"back_calculated": None, "absolute_percent_error": None}
    return _display_back_calculation(concentration, (response - intercept) / slope)


def _predict(concentration, coefficients):
    a, b, c = coefficients
    return (a * concentration + b) * concentration + c


def _error_limits_by_concentration(concentrations, max_error, lowest_max_error):
    if max_error is None:
        if lowest_max_error is not None:
            message = "Enter a general maximum to compare errors at the lowest concentration."
            raise ValidationError({"max_error": message})
        return {}

    validate_decimal = DecimalValidator(**VALUE_FIELD)
    for field, limit in (("max_error", max_error), ("lowest_max_error", lowest_max_error)):
        if limit is None:
            continue
        if not isinstance(limit, Decimal) or not limit.is_finite() or limit < 0:
            raise ValidationError({field: "Enter a finite, nonnegative decimal."})
        try:
            validate_decimal(limit)
        except ValidationError as error:
            raise ValidationError({field: error}) from error

    limits = dict.fromkeys(concentrations, max_error)
    if lowest_max_error is not None:
        limits[min(concentrations)] = lowest_max_error
    return limits


def _add_error_comparison(row, concentration, response, coefficients, error_limit, bounds=None):
    row.update(error_limit=error_limit, within_error_limit=None)
    if not concentration or row["back_calculated"] is None:
        return

    # On an invertible branch, a concentration interval maps to an exact response
    # interval. This also works when the back-calculated concentration is irrational.
    deviation = concentration * _fraction(error_limit) / 100
    lower, upper = concentration - deviation, concentration + deviation
    if bounds is not None:
        lower, upper = max(lower, bounds[0]), min(upper, bounds[1])
    low_response, high_response = sorted(
        (_predict(lower, coefficients), _predict(upper, coefficients))
    )
    row["within_error_limit"] = low_response <= response <= high_response


@dataclass(frozen=True)
class _IrrationalQuadraticInverse:
    vertex: Fraction
    branch: int
    distance_squared: Fraction

    def bounds(self, bits):
        scale = 1 << bits
        distance = isqrt(
            (self.distance_squared.numerator << (2 * bits)) // self.distance_squared.denominator
        )
        near = self.vertex + self.branch * Fraction(distance, scale)
        far = near + Fraction(self.branch, scale)
        return min(near, far), max(near, far)


def _quadratic_inverse(response, coefficients, increasing):
    a, b, c = coefficients
    if not a:
        return (response - c) / b

    vertex = -b / (2 * a)
    # The sign of f'(x) = 2a(x - vertex) identifies the root on the recorded branch.
    branch = 1 if (a > 0) == increasing else -1
    distance_squared = (b**2 + 4 * a * (response - c)) / (4 * a**2)
    numerator = isqrt(distance_squared.numerator)
    denominator = isqrt(distance_squared.denominator)
    if (
        numerator**2 == distance_squared.numerator
        and denominator**2 == distance_squared.denominator
    ):
        return vertex + branch * Fraction(numerator, denominator)
    return _IrrationalQuadraticInverse(vertex, branch, distance_squared)


def _display_quadratic_inverse(concentration, inverse):
    if isinstance(inverse, Fraction):
        return _display_back_calculation(concentration, inverse)

    # Enclose the irrational root with exact fractions until both ends round alike
    # for concentration and percent error. The root cannot equal a decimal rounding tie.
    bits = 64
    while True:
        lower, upper = inverse.bounds(bits)
        # Error is monotone on either side of the recorded concentration.
        if not lower < concentration < upper:
            displayed = _display_back_calculation(concentration, lower)
            if displayed == _display_back_calculation(concentration, upper):
                return displayed
        bits *= 2


def _quadratic_back_calculation(concentration, response, coefficients, response_endpoints, reason):
    first_response, last_response = response_endpoints
    if reason is None and not min(response_endpoints) <= response <= max(response_endpoints):
        reason = "The observed response is outside the fitted response range."
    if reason is not None:
        return None, {
            "back_calculated": None,
            "absolute_percent_error": None,
            "back_calculation_reason": reason,
        }

    inverse = _quadratic_inverse(response, coefficients, first_response < last_response)
    values = _display_quadratic_inverse(concentration, inverse)
    return inverse, dict(values, back_calculation_reason=None)


def _display_quadratic_rse(observations, inverses, degrees_of_freedom):
    exact = Fraction(0)
    irrational_errors = []
    for (x, _), inverse in zip(observations, inverses, strict=True):
        if isinstance(inverse, Fraction):
            exact += ((inverse - x) / x) ** 2
        elif x == inverse.vertex:
            # At a stationary endpoint the squared error can be rational even if the root isn't.
            exact += inverse.distance_squared / x**2
        else:
            irrational_errors.append((x, inverse))

    scale_squared = Fraction(100**2, degrees_of_freedom)
    if not irrational_errors:
        return _display_square_root(scale_squared * exact)

    # Squaring (vertex + branch × sqrt(distance_squared) - x) gives a radical
    # term with coefficient 2 × branch × (vertex - x). It is negative for every
    # remaining error because all x lie on the selected branch. Like radicals
    # cannot cancel, and distinct square-free radicals are linearly independent
    # over the rationals. The sum cannot equal a rational rounding midpoint squared.
    bits = 64
    while True:
        lower_error = upper_error = exact
        for x, inverse in irrational_errors:
            lower, upper = inverse.bounds(bits)
            lower_error += (max(lower - x, x - upper, 0) / x) ** 2
            upper_error += (max(x - lower, upper - x) / x) ** 2
        displayed = _display_square_root(scale_squared * lower_error)
        if displayed == _display_square_root(scale_squared * upper_error):
            return displayed
        bits *= 2


def _quadratic_relative_standard_error(observations, inverses, curve_reason):
    # EPA SW-846 8000D Rev. 5, section 11.5.4.2. Each recorded point counts once.
    points = len(observations)
    terms = 3
    degrees_of_freedom = points - terms
    percent = None
    if any(not x for x, _ in observations):
        reason = "a recorded concentration is zero"
    elif curve_reason is not None:
        reason = curve_reason
    elif degrees_of_freedom <= 0:
        reason = "there are too few recorded points for the number of fitted terms"
    elif any(inverse is None for inverse in inverses):
        reason = "a recorded response is outside the fitted response range"
    else:
        reason = None
        percent = _display_quadratic_rse(observations, inverses, degrees_of_freedom)
    return {
        "percent": percent,
        "reason": reason,
        "points": points,
        "terms": terms,
        "degrees_of_freedom": degrees_of_freedom,
    }


def _relative_standard_error(observations, slope, intercept=0, *, terms, coefficient_label):
    # EPA SW-846 8000D Rev. 5, section 11.5.4.2. Each recorded point counts once.
    points = len(observations)
    degrees_of_freedom = points - terms
    percent = reason = None
    if any(not x for x, _ in observations):
        reason = "a recorded concentration is zero"
    elif not slope:
        reason = f"the {coefficient_label} is zero"
    elif degrees_of_freedom <= 0:
        reason = "there are too few recorded points for the number of fitted terms"
    else:
        # The relative inverse concentration error equals the residual divided by slope × x.
        squared_error = sum(
            ((y - (slope * x + intercept)) / (slope * x)) ** 2 for x, y in observations
        )
        percent = _display_square_root(100**2 * squared_error / degrees_of_freedom)
    return {
        "percent": percent,
        "reason": reason,
        "points": points,
        "terms": terms,
        "degrees_of_freedom": degrees_of_freedom,
    }


def _average_factor_fit(points, observations, concentrations, error_limits):
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
    rse = _relative_standard_error(
        observations, mean, terms=1, coefficient_label="mean calibration factor"
    )
    rows = []
    for point, (x, y), factor in zip(points, observations, factors, strict=True):
        predicted = mean * x
        row = dict(
            point,
            factor=_display(factor),
            predicted=_display(predicted),
            residual=_display(y - predicted),
            **_back_calculation(x, y, mean),
        )
        if error_limits:
            _add_error_comparison(row, x, y, (0, mean, 0), error_limits[x])
        rows.append(row)
    return {
        "mean_factor": _display(mean),
        "factor_sd": _display_square_root(variance),
        # For a positive mean, the factor RSD and the inverse concentration RSE are equal.
        "factor_rsd": rse["percent"] if mean > 0 else None,
        "back_calculation_unavailable": not mean,
        "rse": rse,
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


def _r_squared(weighted_points, predictions):
    total_weight = sum(weight for weight, _, _ in weighted_points)
    mean = sum(weight * y for weight, _, y in weighted_points) / total_weight
    total_squared_deviation = sum(weight * (y - mean) ** 2 for weight, _, y in weighted_points)
    if not total_squared_deviation:
        return None

    squared_residuals = sum(
        weight * (y - predicted) ** 2
        for (weight, _, y), predicted in zip(weighted_points, predictions, strict=True)
    )
    return 1 - squared_residuals / total_squared_deviation


def _quadratic_trend(a, b, lower, upper):
    at_lower = 2 * a * lower + b
    at_upper = 2 * a * upper + b
    if at_lower * at_upper < 0:
        return (
            "Turns within the recorded range",
            "the fitted curve turns within the recorded concentration range",
        )
    if not at_lower and not at_upper:
        return "Constant", "the fitted response is constant"

    direction = "Increasing" if max(at_lower, at_upper) > 0 else "Decreasing"
    if not at_lower:
        return f"{direction} (stationary at the lower endpoint)", None
    if not at_upper:
        return f"{direction} (stationary at the upper endpoint)", None
    return direction, None


def calculate_fit(points, weighting, model="linear", *, max_error=None, lowest_max_error=None):
    """Fit every recorded standard, with optional exact Decimal limits on percent error."""
    if model not in ("linear", "linear_origin", "quadratic", "average_factor"):
        raise ValidationError(
            "Choose a linear, zero-intercept linear, quadratic or average calibration factor fit."
        )
    average_factor = model == "average_factor"
    linear_origin = model == "linear_origin"
    if linear_origin and weighting == "":
        weighting = "equal"
    if average_factor:
        if weighting not in ("", "equal"):
            raise ValidationError("Average calibration factor uses equal weighting.")
    elif not isinstance(weighting, str) or weighting not in _WEIGHT_POWERS:
        raise ValidationError("Choose equal, 1/x or 1/x² weighting.")

    # Use the record validator's bounds and exact decimal parser before rational arithmetic.
    _points(points)
    observations = [
        (_fraction(point["concentration"]), _fraction(point["response"])) for point in points
    ]
    concentrations = {x for x, _ in observations}
    error_limits = _error_limits_by_concentration(concentrations, max_error, lowest_max_error)
    if average_factor:
        return _average_factor_fit(points, observations, concentrations, error_limits)

    quadratic = model == "quadratic"
    if linear_origin and 0 in concentrations:
        raise ValidationError(
            "A zero-intercept linear fit requires every concentration to be greater than zero."
        )
    if quadratic and len(concentrations) < 3:
        raise ValidationError("A quadratic fit needs at least three distinct concentrations.")
    if len(concentrations) < 2:
        raise ValidationError("A linear fit needs at least two distinct concentrations.")

    power = _WEIGHT_POWERS[weighting]
    if power and 0 in concentrations:
        raise ValidationError(
            "Inverse weighting requires every concentration to be greater than zero."
        )
    weighted_points = [(1 / x**power, x, y) for x, y in observations]
    if linear_origin:
        # With the intercept fixed, weighted least squares has one normal equation.
        a = c = Fraction(0)
        b = sum(weight * x * y for weight, x, y in weighted_points) / sum(
            weight * x**2 for weight, x, _ in weighted_points
        )
    else:
        a, b, c = _coefficients(weighted_points, quadratic)

    coefficients = (a, b, c)
    bounds = None
    if quadratic:
        bounds = (min(concentrations), max(concentrations))
        trend, curve_reason = _quadratic_trend(a, b, *bounds)
        reason = f"{curve_reason.capitalize()}." if curve_reason else None
        response_endpoints = tuple(_predict(x, coefficients) for x in bounds)
        inverses = []

    predictions = [_predict(x, coefficients) for x, _ in observations]
    rows = []
    for point, (x, y), predicted in zip(points, observations, predictions, strict=True):
        row = dict(point, predicted=_display(predicted), residual=_display(y - predicted))
        if quadratic:
            inverse, values = _quadratic_back_calculation(
                x, y, coefficients, response_endpoints, reason
            )
            inverses.append(inverse)
            row.update(values)
        else:
            row.update(_back_calculation(x, y, b, c))
        if error_limits:
            _add_error_comparison(row, x, y, coefficients, error_limits[x], bounds)
        rows.append(row)
    fit = {"slope": _display(b), "intercept": _display(c), "rows": rows}
    if not linear_origin:
        r_squared = _r_squared(weighted_points, predictions)
        fit["r_squared"] = _display(r_squared) if r_squared is not None else None
        if model == "linear" and weighting == "equal":
            # For unweighted least squares with a free intercept, R² = r²
            # and Pearson r has the same sign as the fitted slope.
            if r_squared is None:
                fit["pearson_r"] = None
            else:
                magnitude = _display_square_root(r_squared)
                fit["pearson_r"] = f"-{magnitude}" if b < 0 else magnitude
    if quadratic:
        fit.update(
            quadratic=_display(a),
            trend=trend,
            back_calculation_reason=reason,
            rse=_quadratic_relative_standard_error(observations, inverses, curve_reason),
        )
    else:
        fit["back_calculation_unavailable"] = not b
    if model == "linear":
        fit["rse"] = _relative_standard_error(
            observations, b, c, terms=2, coefficient_label="fitted slope"
        )
    return fit
