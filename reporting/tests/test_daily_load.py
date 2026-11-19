from decimal import Decimal, localcontext

import pytest

from reporting.daily_load import calculate_daily_load, daily_load_input, daily_load_supported


@pytest.fixture
def issued():
    return (
        {
            "batches": [
                {"number": "RUN-A", "qc": {"Lead": {"test_definition_id": 17, "issues": []}}}
            ]
        },
        {
            "collected_at": "2026-11-01T23:15:00-05:00",
            "matrix": "Wastewater",
            "disposition": "Proceed",
        },
        {
            "analyte": "Lead",
            "batch": "RUN-A",
            "test_definition_id": 17,
            "unit": "mg/L",
            "reported": "2.400",
            "kind": "detected",
            "qualifiers": [],
            "preservation": {"decision": None},
        },
    )


@pytest.mark.parametrize("unit", ["ug/L", "µg/L", "μg/L"])
def test_microgram_spellings_and_exact_product_regardless_of_context(issued, unit):
    content, sample, result = issued
    with localcontext(prec=3):
        mg_l, reason = daily_load_input(
            content, sample, result | {"unit": unit, "reported": "2.34e-2"}
        )
        assert reason == ""
        assert mg_l == Decimal("0.0000234")
        assert calculate_daily_load(mg_l, Decimal("1.2345678901")) == Decimal(
            "0.0002409333311603556"
        )
    assert daily_load_input(content, sample | {"matrix": "Water"}, result) == (Decimal("2.400"), "")


def test_zero_input_is_an_explicit_numeric_result_or_flow(issued):
    content, sample, result = issued
    concentration, reason = daily_load_input(content, sample, result | {"reported": "-0.0"})
    assert reason == "" and concentration == 0
    assert calculate_daily_load(concentration, Decimal("1")) == 0
    assert calculate_daily_load(Decimal("2.4"), Decimal("0")) == 0


@pytest.mark.parametrize(
    ("sample_changes", "result_changes", "reason"),
    [
        ({"matrix": "Soil"}, {}, "water or wastewater"),
        ({}, {"unit": "µg/kg"}, "mg/L or µg/L"),
        ({}, {"unit": "MG/L"}, "mg/L or µg/L"),
        ({"collected_at": "2026-11-31"}, {}, "collection date"),
        ({"sample_type": "Equipment / rinsate blank"}, {}, "field sample"),
        ({"disposition": "Qualify"}, {}, "receipt disposition"),
        ({}, {"result_basis": "dry"}, "basis"),
        ({}, {"kind": "non_detect", "reported": "ND"}, "Non-detect"),
        ({}, {"reported": "<2.5"}, "No numeric result"),
        ({}, {"reported": "-.01"}, "nonnegative"),
        ({}, {"reported": "1E-99999999999"}, "exponent"),
        ({}, {"qualifiers": ["J"]}, "Qualified"),
        ({}, {"source": "external", "external_qualifiers": "J: estimated"}, "Qualified"),
        ({}, {"source": "external", "external_qualifiers": None}, "incomplete"),
        ({}, {"preservation": {"decision": {"decision": "qualify"}}}, "preservation decision"),
        ({}, {"preservation": {}}, "incomplete"),
        ({}, {"qualifiers": None}, "incomplete"),
    ],
)
def test_unavailable_issued_input_explains_why(issued, sample_changes, result_changes, reason):
    content, sample, result = issued
    value, message = daily_load_input(content, sample | sample_changes, result | result_changes)
    assert value is None and reason in message


def test_subcontracted_concentration_has_its_own_qualifiers_and_no_internal_batch(issued):
    _, sample, result = issued
    result.update(source="external", external_qualifiers="", batch=None)
    assert daily_load_input({"batches": []}, sample, result) == (Decimal("2.400"), "")


def test_issued_qc_follows_the_batch_and_definition_including_preparation(issued):
    content, sample, result = issued
    result["preparation"] = {"batch": "PREP-B"}
    qualified = [{"disposition": "accept_qualified"}]
    linked = {"issues": [], "test_definition_id": 17}
    qc = {"Renamed analyte": linked, "Lead": {"test_definition_id": 9, "issues": qualified}}
    prep = {"kind": "preparation", "number": "PREP-B", "qc": qc}
    content["batches"].extend(
        [
            prep,
            {
                "number": "RUN-OTHER",
                "qc": {"Lead": {"test_definition_id": 17, "issues": qualified}},
            },
        ]
    )

    assert daily_load_input(content, sample, result) == (Decimal("2.400"), "")
    linked["issues"] = qualified
    assert daily_load_input(content, sample, result) == (
        None,
        "The preparation batch QC qualifies this result.",
    )
    content["batches"][0]["qc"]["Lead"]["issues"] = qualified
    assert daily_load_input(content, sample, result) == (
        None,
        "The analytical batch QC qualifies this result.",
    )


def test_legacy_qc_uses_analyte_only_when_both_definition_ids_are_absent(issued):
    content, sample, result = issued
    group = content["batches"][0]["qc"]["Lead"]
    del group["test_definition_id"]
    assert daily_load_input(content, sample, result)[0] is None
    del result["test_definition_id"]
    assert daily_load_input(content, sample, result) == (Decimal("2.400"), "")
    group["issues"] = [{"disposition": "accept_qualified"}]
    assert "qualifies" in daily_load_input(content, sample, result)[1]


@pytest.mark.parametrize(
    "batches",
    [
        None,
        [],
        [{"number": "RUN-A", "qc": {}}],
        [{"number": "RUN-A", "qc": {"Lead": {"test_definition_id": 17}}}],
    ],
)
def test_missing_internal_qc_evidence_does_not_supply_a_number(issued, batches):
    _, sample, result = issued
    value, reason = daily_load_input({"batches": batches}, sample, result)
    assert value is None and "incomplete batch QC" in reason


def test_decimal_products_not_limited_to_default_context_precision():
    concentration = Decimal("12345678901234.5678901234")
    flow = Decimal("98765432109876.5432109876")
    assert calculate_daily_load(concentration, flow) == Decimal(
        "10169181436827617721863779359.6234561401805349144656"
    )


@pytest.mark.parametrize(
    "flow",
    [
        None,
        0.5,
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1E14"),
        Decimal("0.00000000001"),
        Decimal("0E+99999"),
    ],
)
def test_unusable_flow_never_produces_a_number(flow):
    with pytest.raises(ValueError):
        calculate_daily_load(Decimal("2.40"), flow)


def test_supported_units_do_not_assume_other_dimensions():
    assert daily_load_supported("Water", "µg/L")
    assert not daily_load_supported("Water", "mg/L as N")
    assert not daily_load_supported(None, None)
