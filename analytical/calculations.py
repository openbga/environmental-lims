"""Versioned bench calculations and rounding. Pure arithmetic on exact decimals; no database.

A calculation is code with a key and a version. A stored worksheet names both, so the value it
reported can be worked out again from its readings for as long as that version stays registered
here. Never change the arithmetic, inputs or units of a registered version: register the next
version beside it and leave the old one in place.

The arithmetic is carried to PRECISION significant digits and rounded once, at the end, by the
laboratory's own rule (NIST GLP 9, 2019: "do not round intermediate calculations"). Nothing here
chooses a rounding for the laboratory.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

PRECISION = 50  # significant digits carried through the arithmetic
UNROUNDED_PLACES = 20  # decimal places of the unrounded value kept on a worksheet
MAX_SIGNIFICANT = 15
MAX_PLACES = 10  # the stored scale of a result value

SIGNIFICANT = "significant_figures"
PLACES = "decimal_places"
MODES = [(SIGNIFICANT, "Significant figures"), (PLACES, "Decimal places")]
HALF_EVEN = "half_even"
HALF_UP = "half_up"
METHODS = [
    (HALF_EVEN, "Half to even: a final 5 rounds to the nearest even digit"),
    (HALF_UP, "Half up: a final 5 always rounds away from zero"),
]
_ROUNDING = {HALF_EVEN: ROUND_HALF_EVEN, HALF_UP: ROUND_HALF_UP}


class CalculationError(ValueError):
    """The readings cannot be worked. `field` names the reading at fault, if one is."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Reading:
    """One input of a calculation. `unit` None means the unit of the test."""

    key: str
    label: str
    unit: str | None = None
    help: str = ""
    allows_nd: bool = False  # ND may be typed instead of a number
    positive: bool = False  # must be greater than zero
    non_negative: bool = False
    prefill_limit: bool = False  # the entry form offers the catalog reporting limit


@dataclass(frozen=True)
class Outcome:
    """What a calculation gives: the unrounded value (None for a non-detect reading) and, where
    the calculation sets it, the reporting limit applied."""

    value: Decimal | None
    limit: Decimal | None = None


@dataclass(frozen=True)
class Calculation:
    key: str
    version: int
    title: str
    formula: str  # shown with every worksheet, so write it for the bench
    source: str
    inputs: tuple
    work: object  # (readings: dict[str, Decimal | None], dilution: Decimal) -> Outcome
    result_unit: str | None = None  # the only test unit this arithmetic gives; None: any
    uses_dilution: bool = False
    sets_limit: bool = False

    @property
    def label(self):
        return f"{self.title} (version {self.version})"

    def unit_of(self, reading, test_unit):
        return reading.unit or test_unit

    def run(self, readings, dilution):
        """Work the readings at full precision. Callers pass exact Decimals keyed by reading."""
        with localcontext() as context:
            context.prec = PRECISION
            return self.work(readings, dilution)


def _diluted_reading(readings, dilution):
    limit = readings["base_limit"] * dilution
    reading = readings["reading"]
    return Outcome(None if reading is None else reading * dilution, limit)


def _residue(scale):
    def work(readings, dilution):
        return Outcome((readings["gross"] - readings["tare"]) * scale / readings["volume"])

    return work


def _residue_inputs(unit):
    return (
        Reading(
            "gross",
            "Dried residue with its filter or dish",
            unit,
            help="Weight after drying, as read from the balance.",
            non_negative=True,
        ),
        Reading(
            "tare",
            "Filter or dish alone",
            unit,
            help="Tare weight recorded before the sample was added.",
            non_negative=True,
        ),
        Reading(
            "volume", "Sample volume", "mL", help="Volume filtered or evaporated.", positive=True
        ),
    )


_RESIDUE_SOURCE = (
    "USGS methods I-3765-85 (suspended), I-1749-85 (dissolved) and I-3750-85 (total): solids in "
    "mg/L = 1,000 ÷ mL of sample × mg of residue. Read 2026-11-14. No blank correction is "
    "applied; record one in the comments and correct the weights if the method calls for it."
)

_REGISTERED = (
    Calculation(
        key="dilution",
        version=1,
        title="Reading times dilution factor",
        formula="Result = instrument reading × dilution factor. Reporting limit applied = "
        "reporting limit before dilution × dilution factor. A reading of ND is reported as ND "
        "at that limit.",
        source="General laboratory practice; no single public source. Check it against the method.",
        inputs=(
            Reading(
                "reading",
                "Instrument reading",
                help="As read for the diluted portion, in the unit of the test. ND if nothing "
                "was detected.",
                allows_nd=True,
            ),
            Reading(
                "base_limit",
                "Reporting limit before dilution",
                help="The limit that applies to an undiluted portion.",
                positive=True,
                prefill_limit=True,
            ),
        ),
        work=_diluted_reading,
        uses_dilution=True,
        sets_limit=True,
    ),
    Calculation(
        key="residue_g",
        version=1,
        title="Solids by weight, balance in grams",
        formula="Result (mg/L) = (dried residue with its filter or dish − filter or dish alone), "
        "in g × 1,000,000 ÷ sample volume in mL.",
        source=_RESIDUE_SOURCE,
        inputs=_residue_inputs("g"),
        work=_residue(Decimal(1_000_000)),
        result_unit="mg/L",
    ),
    Calculation(
        key="residue_mg",
        version=1,
        title="Solids by weight, balance in milligrams",
        formula="Result (mg/L) = (dried residue with its filter or dish − filter or dish alone), "
        "in mg × 1,000 ÷ sample volume in mL.",
        source=_RESIDUE_SOURCE,
        inputs=_residue_inputs("mg"),
        work=_residue(Decimal(1_000)),
        result_unit="mg/L",
    ),
)

REGISTRY = {(c.key, c.version): c for c in _REGISTERED}
CURRENT = {}
for _calculation in _REGISTERED:
    if _calculation.key not in CURRENT or CURRENT[_calculation.key].version < _calculation.version:
        CURRENT[_calculation.key] = _calculation


def current(key):
    """The version new entries use, or None for an unknown key."""
    return CURRENT.get(key)


def registered(key, version):
    """The exact version a stored worksheet names, or None when this release lacks it."""
    return REGISTRY.get((key, version))


def choices():
    return [(c.key, c.label) for c in CURRENT.values()]


# ------------------------------------------------------------------------- rounding


@dataclass(frozen=True)
class Rounding:
    """A laboratory's rounding of one calculated value. With a threshold, values whose size is
    at or above it use the upper mode and digits (for example whole numbers below 1,000 and
    three significant figures from 1,000 up)."""

    method: str
    mode: str
    digits: int
    threshold: Decimal | None = None
    upper_mode: str = ""
    upper_digits: int | None = None

    def band(self, value):
        """('base' | 'upper', mode, digits) for an unrounded value."""
        if self.threshold is not None and abs(value) >= self.threshold:
            return "upper", self.upper_mode, self.upper_digits
        return "base", self.mode, self.digits

    def apply(self, value):
        """(rounded value, band used). The rounded Decimal keeps the digits the rule asks for,
        so its plain text shows them: 3 significant figures of 42 is 42.0."""
        band, mode, digits = self.band(value)
        return round_to(value, mode=mode, digits=digits, method=self.method), band

    def as_json(self, band=None):
        out = {
            "method": self.method,
            "mode": self.mode,
            "digits": self.digits,
            "threshold": None if self.threshold is None else plain(self.threshold),
            "upper_mode": self.upper_mode,
            "upper_digits": self.upper_digits,
        }
        if band:
            out["band"] = band
        return out

    @classmethod
    def from_json(cls, data):
        threshold = data.get("threshold")
        return cls(
            method=data["method"],
            mode=data["mode"],
            digits=data["digits"],
            threshold=None if threshold is None else Decimal(threshold),
            upper_mode=data.get("upper_mode", ""),
            upper_digits=data.get("upper_digits"),
        )

    def describe(self):
        method = "half to even" if self.method == HALF_EVEN else "half up"
        base = digits_label(self.mode, self.digits)
        if self.threshold is None:
            return f"{base}, {method}"
        upper = digits_label(self.upper_mode, self.upper_digits)
        limit = plain(self.threshold)
        return f"{base} below {limit}; {upper} at or above {limit}; {method}"


def digits_label(mode, digits):
    if mode == SIGNIFICANT:
        return f"{digits} significant figure{'' if digits == 1 else 's'}"
    if digits == 0:
        return "whole numbers"
    return f"{digits} decimal place{'' if digits == 1 else 's'}"


def plain(value):
    """Plain text of an exact decimal without padding zeros: 1000.0000000000 -> 1000."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def round_to(value, *, mode, digits, method):
    """Round once with the named method. Exact Decimal arithmetic; never a float."""
    rounding = _ROUNDING[method]
    with localcontext() as context:
        context.prec = PRECISION + MAX_SIGNIFICANT
        if mode == PLACES:
            rounded = value.quantize(Decimal(1).scaleb(-digits), rounding=rounding)
        elif value == 0:
            rounded = Decimal(0)
        else:
            rounded = value.quantize(
                Decimal(1).scaleb(value.adjusted() - digits + 1), rounding=rounding
            )
            if rounded.adjusted() > value.adjusted():
                # 9.96 to two figures is 10, not 10.0: rounding carried into a new digit.
                rounded = rounded.quantize(Decimal(1).scaleb(rounded.adjusted() - digits + 1))
    return rounded.copy_abs() if rounded == 0 else rounded


def value_text(rounded):
    """The reported text of a rounded value: plain notation, with the digits rounding kept."""
    return format(rounded, "f")


def unrounded(value):
    """The unrounded value as kept on a worksheet (UNROUNDED_PLACES decimal places)."""
    with localcontext() as context:
        context.prec = PRECISION + UNROUNDED_PLACES
        return value.quantize(Decimal(1).scaleb(-UNROUNDED_PLACES), rounding=ROUND_HALF_EVEN)
