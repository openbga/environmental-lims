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
    # Taken from this field of the entry itself, not typed as a reading of its own: the entry
    # already records it (percent solids, with its source). It is still kept on the worksheet.
    entry_field: str = ""


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
    sets_limit: bool = False  # works out the reporting limit applied, from the reading base_limit
    # The limit worked out is a quotient, so it is rounded by the laboratory's rule like the value.
    rounds_limit: bool = False
    limit_note: str = ""  # what an entry row shows where the limit would be typed
    soil_only: bool = False  # uses what only a soil entry records (percent solids)
    liquid_only: bool = False  # takes a volume of sample: not for a soil test
    # The weight basis a soil entry must declare for this arithmetic to mean what it says,
    # and the sentence that tells the analyst why.
    soil_basis: str = ""
    soil_basis_reason: str = ""

    @property
    def label(self):
        return f"{self.title} (version {self.version})"

    def unit_of(self, reading, test_unit):
        return reading.unit or test_unit

    def gives(self, test_unit):
        """Whether this arithmetic gives the unit of a test."""
        return self.result_unit is None or same_unit(self.result_unit, test_unit)

    @property
    def entry_fields(self):
        """The entry's own fields this calculation reads (see Reading.entry_field)."""
        return tuple(r.entry_field for r in self.inputs if r.entry_field)

    def run(self, readings, dilution):
        """Work the readings at full precision. Callers pass exact Decimals keyed by reading."""
        with localcontext() as context:
            context.prec = PRECISION
            return self.work(readings, dilution)


def same_unit(one, other):
    """Whether two unit texts name the same unit. Only the spelling of micro may differ (the
    micro sign, the Greek letter mu or a plain u); nothing is converted."""

    def spelled(unit):
        return (unit or "").strip().replace("\u00b5", "u").replace("\u03bc", "u")

    return spelled(one) == spelled(other)


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

# ------------------------------------------------------------- prepared portions
# The solution an instrument reads is not the sample: a measured portion was digested, extracted
# or diluted to a final volume first. These calculations take the reading back to the sample and
# take the reporting limit with it. Multiply first and divide once, so a value that is exact
# stays exact.

_ND_HELP = "ND if nothing was detected."
_LIMIT_NOTE = "limit in the solution × the same factors, rounded like the result"
_LIMIT_FORMULA = (
    "Reporting limit applied = reporting limit in the solution × the same factors, rounded by "
    "the same rule as the result. A reading of ND is reported as ND at that limit."
)


def _prepared(*, by, scale=Decimal(1), dry=False):
    """reading × final volume × scale × dilution ÷ the amount prepared; for a dry-weight
    result the amount is the weight as received × percent solids ÷ 100."""

    def work(readings, dilution):
        top = readings["final_volume"] * scale * dilution
        bottom = readings[by]
        if dry:
            top = top * 100
            bottom = bottom * readings["percent_solids"]
        reading = readings["reading"]
        return Outcome(
            None if reading is None else reading * top / bottom,
            readings["base_limit"] * top / bottom,
        )

    return work


def _solution_inputs(unit, where):
    return (
        Reading(
            "reading",
            "Instrument reading",
            unit,
            help=f"As read for {where}. {_ND_HELP}",
            allows_nd=True,
        ),
        Reading(
            "base_limit",
            "Reporting limit in the solution",
            unit,
            help="The limit that applies to the solution as measured, before the preparation "
            "factor and any dilution.",
            positive=True,
        ),
    )


_FINAL_VOLUME = Reading(
    "final_volume",
    "Final volume",
    "mL",
    help="Volume the prepared portion was brought to.",
    positive=True,
)
_VOLUME_SOURCE = (
    "EPA Method 200.7, Revision 4.4 (1994), section 12.3: multiply the solution concentration "
    "by the factor of the final volume over the aliquot prepared, and account for any further "
    "dilution. Read 2026-11-14. Scaling the reporting limit by the same factors is general "
    "practice; check both against the method."
)
_AS_WEIGHED_SOURCE = (
    "EPA Method 200.7, Revision 4.4 (1994), section 12.5: mg/kg = C × V × D ÷ W, with C in "
    "mg/L, V in L and W in kg. Read 2026-11-14. No percent solids correction is applied: the "
    "result is on the basis of the portion as weighed, which that method dries first. Its "
    "section 12.4 rounds the extract concentration before this calculation; to follow it, type "
    "the rounded concentration. Scaling the reporting limit by the same factors is general "
    "practice; check both against the method."
)
_DRY_SOURCE = (
    "SW-846 Method 6010D, Revision 5 (2018), section 12.2: concentration on a dry-weight basis "
    "= C × V ÷ (W × S), with C in mg/L, V in L, W the wet sample mass in kg and S the percent "
    "solids ÷ 100; section 12.1 applies dilution factors. Read 2026-11-14. Scaling the "
    "reporting limit by the same factors is general practice; check both against the method."
)
_DRY_REASON = (
    "This calculation corrects the result to dry weight with the percent solids. Choose Dry weight."
)

# (key suffix, unit read in the solution, unit of the result, scale, the scale in words)
_SOLID_UNITS = (
    ("mg_l", "mg/L", "mg/kg", Decimal(1), ""),
    ("ug_l", "ug/L", "ug/kg", Decimal(1), ""),
    ("ug_l_mg_kg", "ug/L", "mg/kg", Decimal("0.001"), " ÷ 1,000"),
)


def _prepared_solids():
    for suffix, read_in, result_unit, scale, scale_text in _SOLID_UNITS:
        units = f"{read_in} in solution to {result_unit}"
        yield Calculation(
            key=f"solid_{suffix}",
            version=1,
            title=f"Prepared solid as weighed, {units}",
            formula=f"Result ({result_unit}) = instrument reading in {read_in} × final volume in "
            f"mL ÷ sample weight in g{scale_text} × dilution factor. {_LIMIT_FORMULA} No percent "
            "solids correction: the result is on the basis of the portion as weighed.",
            source=_AS_WEIGHED_SOURCE,
            inputs=(
                *_solution_inputs(read_in, "the prepared solution"),
                _FINAL_VOLUME,
                Reading(
                    "weight",
                    "Sample weight",
                    "g",
                    help="Weight of the portion prepared, as weighed: as received, or dried "
                    "first where the method dries it. The result is on that basis.",
                    positive=True,
                ),
            ),
            work=_prepared(by="weight", scale=scale),
            result_unit=result_unit,
            uses_dilution=True,
            sets_limit=True,
            rounds_limit=True,
            limit_note=_LIMIT_NOTE,
        )
        yield Calculation(
            key=f"solid_dry_{suffix}",
            version=1,
            title=f"Prepared solid corrected to dry weight, {units}",
            formula=f"Result ({result_unit}, dry weight) = instrument reading in {read_in} × "
            f"final volume in mL ÷ (sample weight as received in g × percent solids ÷ 100)"
            f"{scale_text} × dilution factor. {_LIMIT_FORMULA}",
            source=_DRY_SOURCE,
            inputs=(
                *_solution_inputs(read_in, "the prepared solution"),
                _FINAL_VOLUME,
                Reading(
                    "weight",
                    "Sample weight as received",
                    "g",
                    help="Weight of the portion prepared, weighed as received (wet).",
                    positive=True,
                ),
                Reading(
                    "percent_solids",
                    "Percent solids",
                    "%",
                    help="Type the percent solids of this sample, from its own determination: "
                    "the share of the sample as received that is left after drying. Required "
                    "by this calculation, with its source reference.",
                    positive=True,
                    entry_field="percent_solids",
                ),
            ),
            work=_prepared(by="weight", scale=scale, dry=True),
            result_unit=result_unit,
            uses_dilution=True,
            sets_limit=True,
            rounds_limit=True,
            limit_note=_LIMIT_NOTE,
            soil_only=True,
            soil_basis="dry",
            soil_basis_reason=_DRY_REASON,
        )


# ------------------------------------------------------ percent solids and moisture


def _share_of_sample(part):
    """A part of the sample as received, in percent of its weight as received."""

    def work(readings, dilution):
        sample = readings["wet"] - readings["tare"]
        if sample <= 0:
            raise CalculationError(
                "Dish with the sample as received must weigh more than the dish alone.", "wet"
            )
        if readings["dry"] < readings["tare"]:
            raise CalculationError(
                "Dish with the dried sample cannot weigh less than the dish alone.", "dry"
            )
        if readings["dry"] > readings["wet"]:
            raise CalculationError(
                "Dish with the dried sample cannot weigh more than with the sample as received.",
                "dry",
            )
        return Outcome(part(readings) * 100 / sample)

    return work


_DRYING_INPUTS = (
    Reading("tare", "Dish alone", "g", help="Tare weight of the empty dish.", non_negative=True),
    Reading(
        "wet",
        "Dish with the sample as received",
        "g",
        help="Weight before drying, as read from the balance.",
        positive=True,
    ),
    Reading(
        "dry",
        "Dish with the dried sample",
        "g",
        help="Weight after drying, as read from the balance.",
        positive=True,
    ),
)
_WET_REASON = (
    "Percent solids and percent moisture are shares of the sample as received. Choose Wet weight."
)

# -------------------------------------------------------------------------- titrations


def _titration_volume(readings, endpoint="final"):
    for key in ("initial", endpoint):
        if readings[key] < 0:
            raise CalculationError("A burette reading cannot be negative.", key)
    if readings[endpoint] < readings["initial"]:
        raise CalculationError(
            "The endpoint burette reading cannot be less than the initial reading. "
            "This worksheet does not account for burette refills.",
            endpoint,
        )
    return readings[endpoint] - readings["initial"]


def _titration(*, alkalinity=False, low=False):
    def work(readings, dilution):
        # Validate the denominators and strengths here as well as through Reading, so an
        # unusable stored worksheet reports the actual reading at fault when reproduced.
        for key, label in (("sample_volume", "Sample volume"), ("normality", "Titrant normality")):
            if readings[key] <= 0:
                raise CalculationError(f"{label} must be greater than zero.", key)
        volume = _titration_volume(readings)
        if low:
            first = _titration_volume(readings, "first_endpoint")
            if readings["final"] <= readings["first_endpoint"]:
                raise CalculationError(
                    "The final burette reading must exceed the first endpoint reading: "
                    "acid is added between the two endpoints.",
                    "final",
                )
            if not Decimal("4.3") <= readings["first_ph"] <= Decimal("4.7"):
                raise CalculationError(
                    "The first endpoint pH must be from 4.3 to 4.7 for this calculation.",
                    "first_ph",
                )
            if readings["final_ph"] != readings["first_ph"] - Decimal("0.3"):
                raise CalculationError(
                    "The final endpoint pH must be exactly 0.3 below the first endpoint pH "
                    "for this calculation.",
                    "final_ph",
                )
            # C is the TOTAL volume from the initial reading, not the addition after B.
            # Do not force 2B-C to be nonnegative: that would impose an alkalinity policy.
            volume = 2 * first - volume
        elif alkalinity and readings["final_ph"] != Decimal("4.5"):
            raise CalculationError(
                "The endpoint pH must be 4.5 for this calculation; other endpoint procedures "
                "are not included.",
                "final_ph",
            )
        return Outcome(volume * readings["normality"] * 50_000 / readings["sample_volume"])

    return work


_BURETTE_INITIAL = Reading(
    "initial",
    "Initial burette reading",
    "mL",
    help="As read before titration. Use one uninterrupted burette run; no refill is inferred.",
    non_negative=True,
)
_BURETTE_FINAL = Reading(
    "final",
    "Final burette reading",
    "mL",
    help="As read at the endpoint, on the same burette run as the initial reading.",
    non_negative=True,
)
_ALKALINITY_SAMPLE = Reading(
    "sample_volume",
    "Sample volume",
    "mL",
    help="Volume of the unaltered sample titrated; do not add dilution water.",
    positive=True,
)
_HARDNESS_SAMPLE = Reading(
    "sample_volume",
    "Sample volume",
    "mL",
    help="Original sample aliquot titrated, excluding any added reagent or dilution water.",
    positive=True,
)
_ALKALINITY_NORMALITY = Reading(
    "normality",
    "Standardized acid normality",
    "N",
    help="Actual standardized acid strength in equivalents per liter; "
    "no nominal strength is assumed.",
    positive=True,
)
_ALKALINITY_SOURCE = (
    "EPA Method 310.1 (issued 1971, editorial revision 1978), sections 6.2 and 7.1: "
    "A × N × 50,000 ÷ sample mL, titrated to pH 4.5. Read 2026-11-15 from the EPA archive "
    "https://semspub.epa.gov/work/03/160260.pdf (printed pages 310.1-1 to 310.1-3). "
    "Only the pH 4.5 procedure is included; not the mineral-acid pH 3.9 variant. "
    "The sample is unaltered; no dilution factor or reporting-limit change is applied. "
    "This is the historical method equation, not a claim of current regulatory approval."
)
_TITRATIONS = (
    Calculation(
        key="alkalinity_titration",
        version=1,
        title="Alkalinity by titration to pH 4.5",
        formula="Alkalinity (mg/L as CaCO3) = (final − initial burette reading), in mL × "
        "standardized acid normality × 50,000 ÷ sample volume in mL. Endpoint pH is 4.5. "
        "No dilution factor or reporting-limit change is applied.",
        source=_ALKALINITY_SOURCE,
        inputs=(
            _BURETTE_INITIAL,
            _BURETTE_FINAL,
            _ALKALINITY_SAMPLE,
            _ALKALINITY_NORMALITY,
            Reading(
                "final_ph",
                "Endpoint pH",
                "pH",
                help="Measured endpoint pH: 4.5 for this procedure.",
            ),
        ),
        work=_titration(alkalinity=True),
        result_unit="mg/L as CaCO3",
        liquid_only=True,
    ),
    Calculation(
        key="alkalinity_low_titration",
        version=1,
        title="Low alkalinity by two-endpoint titration",
        formula="Alkalinity (mg/L as CaCO3) = (2 × B − C) × standardized acid normality × "
        "50,000 ÷ sample volume in mL. B = first endpoint − initial burette reading; "
        "C = final − initial burette reading (total titrant), all in mL. First endpoint pH "
        "is 4.3–4.7; final pH is exactly 0.3 lower. No dilution factor or reporting-limit "
        "change is applied.",
        source="EPA Method 310.1 (issued 1971, editorial revision 1978), sections 6.3 and 7.2: "
        "(2B − C) × N × 50,000 ÷ sample mL. B and C are total titrant to the first and "
        "second endpoints. Section 6.3 describes samples below 20 mg/L, a 100–200 mL sample, "
        "a first pH of 4.3–4.7 and a second pH exactly 0.3 lower. Read 2026-11-15 from "
        "https://semspub.epa.gov/work/03/160260.pdf (printed pages 310.1-2 and 310.1-3). "
        "The sample is unaltered; do not add dilution water. No dilution factor or "
        "reporting-limit change is applied. This worksheet checks the "
        "endpoint pH pair, not method suitability or laboratory acceptance. This is the "
        "historical method equation, not a claim of current regulatory approval.",
        inputs=(
            _BURETTE_INITIAL,
            Reading(
                "first_endpoint",
                "First endpoint burette reading",
                "mL",
                help="As read at the first recorded pH, on the same burette run.",
                non_negative=True,
            ),
            _BURETTE_FINAL,
            _ALKALINITY_SAMPLE,
            _ALKALINITY_NORMALITY,
            Reading(
                "first_ph",
                "First endpoint pH",
                "pH",
                help="Measured first endpoint pH, from 4.3 to 4.7.",
            ),
            Reading(
                "final_ph",
                "Final endpoint pH",
                "pH",
                help="Measured pH after adding acid: exactly 0.3 below the first endpoint pH.",
            ),
        ),
        work=_titration(low=True),
        result_unit="mg/L as CaCO3",
        liquid_only=True,
    ),
    Calculation(
        key="hardness_edta_titration",
        version=1,
        title="Total hardness by EDTA titration",
        formula="Total hardness (mg/L as CaCO3) = (final − initial burette reading), in mL × "
        "standardized EDTA normality × 50,000 ÷ sample volume in mL. No dilution factor, "
        "blank correction or reporting-limit change is applied.",
        source="EPA Method 130.2 (editorial revisions 1978 and 1982), section 8: A × N × "
        "50,000 ÷ sample mL, where A is mL EDTA and N is its normality. Read 2026-11-15 "
        "from https://www.nemi.gov/methods/method_pdf/4684/. Enter actual standardized "
        "normality; no nominal strength is assumed. No preparation, dilution or blank "
        "correction is included; the separate low-hardness blank procedure is not included. "
        "This is the historical method equation, not a claim of current regulatory approval.",
        inputs=(
            _BURETTE_INITIAL,
            _BURETTE_FINAL,
            _HARDNESS_SAMPLE,
            Reading(
                "normality",
                "Standardized EDTA normality",
                "N",
                help="Actual standardized EDTA strength in equivalents per liter, not molarity "
                "or mg CaCO3 per mL; no nominal strength is assumed.",
                positive=True,
            ),
        ),
        work=_titration(),
        result_unit="mg/L as CaCO3",
        liquid_only=True,
    ),
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
        limit_note="limit before dilution × dilution",
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
    Calculation(
        key="prep_volume",
        version=1,
        title="Preparation by volume: final volume over sample volume",
        formula="Result = instrument reading × final volume ÷ sample volume prepared × dilution "
        f"factor. {_LIMIT_FORMULA}",
        source=_VOLUME_SOURCE,
        inputs=(
            *_solution_inputs(None, "the prepared solution, in the unit of the test"),
            Reading(
                "sample_volume",
                "Sample volume prepared",
                "mL",
                help="Volume of sample taken for the preparation.",
                positive=True,
            ),
            _FINAL_VOLUME,
        ),
        work=_prepared(by="sample_volume"),
        uses_dilution=True,
        sets_limit=True,
        rounds_limit=True,
        limit_note=_LIMIT_NOTE,
        liquid_only=True,
    ),
    *_prepared_solids(),
    Calculation(
        key="percent_solids",
        version=1,
        title="Percent solids by weight",
        formula="Result (%) = (dish with the dried sample − dish alone) ÷ (dish with the sample "
        "as received − dish alone) × 100.",
        source="SW-846 Method 6010D, Revision 5 (2018), section 11.5.2: % dry weight = g of dry "
        "sample ÷ g of sample × 100; EPA Method 200.7, Revision 4.4 (1994), section 12.6. Read "
        "2026-11-14. The drying temperature and time are the method's.",
        inputs=_DRYING_INPUTS,
        work=_share_of_sample(lambda r: r["dry"] - r["tare"]),
        result_unit="%",
        soil_basis="wet",
        soil_basis_reason=_WET_REASON,
    ),
    Calculation(
        key="percent_moisture",
        version=1,
        title="Percent moisture by weight",
        formula="Result (%) = (dish with the sample as received − dish with the dried sample) ÷ "
        "(dish with the sample as received − dish alone) × 100.",
        source="EPA Contract Laboratory Program, Equations utilized in conjunction with SOM01.2 "
        "(2007-08-22), Exhibit D, Low/Medium Volatiles, EQ. 6: % moisture = (g of wet sample − "
        "g of dry sample) ÷ g of wet sample × 100. Read 2026-11-14. A share of the sample as "
        "received, not of its dry weight.",
        inputs=_DRYING_INPUTS,
        work=_share_of_sample(lambda r: r["wet"] - r["dry"]),
        result_unit="%",
        soil_basis="wet",
        soil_basis_reason=_WET_REASON,
    ),
    *_TITRATIONS,
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
