"""Sampling-day mass loading from an issued aqueous concentration and a supplied flow."""

from datetime import datetime
from decimal import Context, Decimal, localcontext

from analytical.models import VALUE_FIELD
from analytical.parsing import MAX_TEXT_LENGTH
from receipt.models import Sample

from .content import qc_test_key
from .location_history import issued_numeric_result

FLOW_MAX_DIGITS = VALUE_FIELD["max_digits"]
FLOW_DECIMAL_PLACES = VALUE_FIELD["decimal_places"]
_UG_UNITS = ("ug/L", "µg/L", "μg/L")
_FACTOR = Decimal("8.34")
_INCOMPLETE_QC = "The issued report has incomplete batch QC evidence for this test."


def daily_load_supported(matrix, unit):
    return matrix in ("Water", "Wastewater") and unit in ("mg/L", *_UG_UNITS)


def _batch_qc_reason(content, result):
    if result.get("source") == "external":
        # An accepted subcontract result carries the external lab's own qualifiers.
        return ""
    batches = content.get("batches")
    analytical = result.get("batch")
    if not isinstance(batches, list) or not isinstance(analytical, str) or not analytical:
        return _INCOMPLETE_QC
    required = {("analytical", analytical)}
    if "preparation" in result:
        prep = result["preparation"]
        if (
            not isinstance(prep, dict)
            or not isinstance(prep.get("batch"), str)
            or not prep["batch"]
        ):
            return _INCOMPLETE_QC
        required.add(("preparation", prep["batch"]))

    matched = set()
    test = qc_test_key(result, result["analyte"])
    for batch in batches:
        if not isinstance(batch, dict):
            return _INCOMPLETE_QC
        kind, number = batch.get("kind", "analytical"), batch.get("number")
        if not isinstance(kind, str) or not isinstance(number, str):
            return _INCOMPLETE_QC
        identity = (kind, number)
        if identity not in required:
            continue
        groups = batch.get("qc")
        if not isinstance(groups, dict):
            return _INCOMPLETE_QC
        for analyte, group in groups.items():
            if not isinstance(group, dict):
                return _INCOMPLETE_QC
            if qc_test_key(group, analyte) != test:
                continue
            issues = group.get("issues")
            if not isinstance(issues, list):
                return _INCOMPLETE_QC
            if any(
                isinstance(issue, dict) and issue.get("disposition") == "accept_qualified"
                for issue in issues
            ):
                return f"The {kind} batch QC qualifies this result."
            if issues:
                return _INCOMPLETE_QC
            matched.add(identity)
    return "" if matched == required else _INCOMPLETE_QC


def daily_load_input(content, sample, result):
    """Return the issued concentration in mg/L, or explain why the worksheet is unavailable."""
    if not all(isinstance(item, dict) for item in (content, sample, result)):
        return None, "The issued result information is incomplete."
    unit = result.get("unit")
    if not daily_load_supported(sample.get("matrix"), unit):
        return None, "Select a water or wastewater concentration in mg/L or µg/L."
    if sample.get("sample_type", "Field sample") != "Field sample":
        return None, "A daily load needs a field sample. This sample has another field designation."
    try:
        datetime.fromisoformat(sample["collected_at"])
    except KeyError, TypeError, ValueError:
        return None, "The issued sample has no usable collection date."
    if not isinstance(result.get("analyte"), str) or not result["analyte"].strip():
        return None, "The issued result does not identify an analyte."
    if result.get("result_basis", "") not in ("", "na"):
        return None, "The issued result has no usable aqueous reporting basis."

    value, reason = issued_numeric_result(result)
    if reason:
        return None, reason
    if value < 0:
        return None, "A daily load needs a nonnegative concentration."
    if abs(value.as_tuple().exponent) > MAX_TEXT_LENGTH:
        return None, "The concentration exponent exceeds the worksheet's calculation range."
    if sample.get("disposition") != Sample.Disposition.PROCEED.label:
        return None, "A receipt disposition of Proceed is required."
    preservation = result.get("preservation")
    if not isinstance(preservation, dict) or "decision" not in preservation:
        return None, "The issued preservation decision information is incomplete."
    decision = preservation["decision"]
    if decision is not None and (
        not isinstance(decision, dict) or decision.get("decision") != "proceed"
    ):
        return None, "The issued preservation decision does not allow an unqualified calculation."
    if reason := _batch_qc_reason(content, result):
        return None, reason
    if unit in _UG_UNITS:
        # Shift the decimal point exactly, including significant trailing zeros.
        with localcontext(Context(prec=MAX_TEXT_LENGTH)):
            value = value.scaleb(-3)
    return value.copy_abs(), ""


def calculate_daily_load(concentration_mg_l, flow_mgd):
    """Return lb/day using the permit example's 8.34 factor with no intermediate rounding."""
    for value in (concentration_mg_l, flow_mgd):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("Concentration and flow must be finite, nonnegative decimal numbers.")
    concentration = concentration_mg_l.as_tuple()
    if (
        len(concentration.digits) > MAX_TEXT_LENGTH
        or abs(concentration.exponent) > MAX_TEXT_LENGTH + 3
    ):
        raise ValueError("The concentration exceeds the worksheet's calculation range.")
    flow = flow_mgd.as_tuple()
    if (
        flow.exponent < -FLOW_DECIMAL_PLACES
        or flow_mgd.adjusted() >= FLOW_MAX_DIGITS - FLOW_DECIMAL_PLACES
    ):
        raise ValueError("Flow can have at most 14 whole digits and 10 decimal places.")

    # A decimal product can need as many coefficient digits as its factors have in total.
    with localcontext(
        Context(prec=MAX_TEXT_LENGTH + FLOW_MAX_DIGITS + len(_FACTOR.as_tuple().digits))
    ):
        return concentration_mg_l.copy_abs() * flow_mgd.copy_abs() * _FACTOR
