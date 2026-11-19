from datetime import date
from decimal import Decimal, localcontext

import pytest

from receipt.models import Client, Project
from reporting import monthly_concentrations as monthly
from reporting.models import Report, ReportVersion

from .test_location_history import LOCATION
from .test_location_history import issue as issue
from .test_location_history import project as project

MONTH = date(2026, 11, 1)
ANALYTE = "Lead (synthetic)"
BATCHES = [{"number": "RUN-A", "qc": {ANALYTE: {"issues": []}}}]


def sample(lab_id, end="2026-11-01T23:50:00-05:00", reported="1.00", unit="mg/L", **changes):
    return {
        "lab_id": lab_id,
        "collection_location": LOCATION,
        "matrix": "Wastewater",
        "collection_type": "Grab",
        "collected_at": end,
        "disposition": "Proceed",
        "results": [
            {
                "analyte": ANALYTE,
                "unit": unit,
                "kind": "detected",
                "qualifiers": [],
                "reported": reported,
                "batch": "RUN-A",
                "preservation": {"decision": None},
                "method_code": "Synthetic",
                "method_version": "2.1",
            }
        ],
        **changes,
    }


def worksheet(project, **overrides):
    return monthly.monthly_concentrations(
        **{
            "client_id": project.client_id,
            "project_id": project.pk,
            "location": LOCATION,
            "matrix": "Wastewater",
            "analyte": ANALYTE,
            "month": MONTH,
        }
        | overrides
    )


def test_current_owned_series_uses_only_frozen_local_dates(
    project, issue, django_assert_num_queries
):
    amended = issue(project, sample("first draft value", reported="999"), batches=BATCHES)
    current = issue(
        project,
        sample("first", reported=".100", source="Surface water"),
        sample("outside start", end="2026-10-31T23:59:59-05:00"),
        sample("outside end", end="2026-12-01T00:00:00+02:00"),
        sample("wrong case", collection_location=LOCATION.lower()),
        sample("wrong matrix", matrix="Water"),
        amends=amended,
        synthetic_notice=True,
        batches=BATCHES,
    )
    last = sample(
        "last",
        end="2026-11-30T23:58:00-05:00",
        reported="225.00",
        unit="μg/L",
        collection_type="Composite",
        collection_started_at="2026-11-30T00:01:00-05:00",
    )
    other_result = last["results"][0] | {"analyte": ANALYTE.upper(), "reported": "1000"}
    last["results"].append(other_result)
    issue(project, last, batches=BATCHES)
    other_project = Project.objects.create(client=project.client, name="Other synthetic project")
    issue(other_project, sample("other project"), batches=BATCHES)
    foreign_client = Client.objects.create(name="Other Synthetic Utilities")
    foreign_project = Project.objects.create(client=foreign_client, name=project.name)
    issue(foreign_project, sample("other client"), batches=BATCHES)

    with django_assert_num_queries(1) as queries, localcontext(prec=2):
        summary = worksheet(project)
    assert "file.pdf" not in queries.captured_queries[0]["sql"]
    assert summary["days"] == 2
    assert summary["reason"] == ""
    assert summary["mean"] == "0.16250"
    assert summary["maximum"] == "0.22500"
    assert summary["synthetic_notice"]
    first, last = summary["rows"]
    assert first["report_id"] == current.report_id
    assert first["report_number"] == current.report.number
    assert first["report_version"] == 2
    assert first["method_code"] == "Synthetic" and first["method_version"] == "2.1"
    assert first["sample_source"] == "Surface water"
    assert first["day"] == MONTH and first["reported"] == ".100"
    assert last["day"] == date(2026, 11, 30) and last["unit"] == "μg/L"
    assert not worksheet(project, client_id=foreign_client.pk)["rows"]
    assert not worksheet(project, project_id=foreign_project.pk)["rows"]
    assert not worksheet(project, location=" ")["rows"]


def test_multiple_results_across_reports_block_the_whole_series(project, issue):
    issue(project, sample("first"), sample("good day", end="2026-11-02"), batches=BATCHES)
    second = sample("second", reported="ND")
    second["results"][0]["kind"] = "non_detect"
    issue(project, second, batches=BATCHES)
    sheet = worksheet(project)
    assert sheet["days"] == 2
    assert sheet["mean"] is sheet["maximum"] is None
    first, second, other_day = sheet["rows"]
    assert "Multiple" in first["reason"] and "Multiple" in second["reason"]
    assert "Non-detect" in second["reason"]
    assert other_day["reason"] == ""
    assert not sheet["flow_keys"] and all(row["flow_key"] is None for row in sheet["rows"])


def test_weighted_grabs_across_reports_use_only_one_value_per_sampled_day(project, issue):
    second_day = date(2026, 11, 2)
    issue(
        project,
        sample(
            "composite",
            end="2026-11-02T19:00:00-05:00",
            reported="1.00",
            collection_type="Composite",
            collection_started_at="2026-11-02T09:00:00-05:00",
        ),
        batches=BATCHES,
    )
    issue(project, sample("first", reported="1.00"), batches=BATCHES)
    issue(project, sample("second", reported="4000.0", unit="µg/L"), batches=BATCHES)

    sheet = worksheet(project)
    first, second, composite = sheet["rows"]
    assert sheet["days"] == 2 and sheet["mean"] is sheet["maximum"] is None
    assert "Enter the flow" in sheet["reason"]
    assert sheet["flow_keys"] == {
        first["flow_key"]: "first (2026-11-01T23:50:00-05:00)",
        second["flow_key"]: "second (2026-11-01T23:50:00-05:00)",
    }
    assert first["flow_key"] != second["flow_key"]
    assert composite["flow_key"] is None
    assert sheet["series_token"] == worksheet(project)["series_token"]

    with localcontext(prec=1):
        calculated = monthly.apply_grab_flows(
            sheet, {first["flow_key"]: Decimal(".5"), second["flow_key"]: Decimal("1.5")}
        )
    assert calculated["reason"] == ""
    assert calculated["mean"] == "2.125"
    assert calculated["maximum"] == "3.25"
    assert calculated["daily_values"] == [
        {"day": MONTH, "value": "3.25"},
        {"day": second_day, "value": "1.00"},
    ]
    assert sheet["mean"] is sheet["maximum"] is None and not sheet["daily_values"]


def test_weighted_values_are_compared_and_averaged_as_exact_fractions(project, issue):
    second_day = date(2026, 11, 2)
    issue(
        project,
        sample("1", reported="0"),
        sample("2", reported="1"),
        sample("3", end="2026-11-02T10:52:00-05:00", reported="0"),
        sample("4", end="2026-11-02T13:02:00-05:00", reported="1"),
        batches=BATCHES,
    )
    sheet = worksheet(project)
    inputs = map(Decimal, ("1", "2", "99999999999999.9999999999", "99999999999999.9999999999"))
    calculated = monthly.apply_grab_flows(sheet, dict(zip(sheet["flow_keys"], inputs, strict=True)))
    assert calculated["daily_values"] == [
        {"day": MONTH, "value": "2/3"},
        {"day": second_day, "value": "0.5"},
    ]
    assert calculated["mean"] == "7/12"
    assert calculated["maximum"] == "2/3"


def test_flow_application_requires_every_key_and_positive_bounded_decimal(project, issue):
    issue(project, sample("first"), sample("second"), batches=BATCHES)
    sheet = worksheet(project)
    first, second = sheet["flow_keys"]

    for flows in ({first: Decimal("1")}, {first: Decimal("1"), "unexpected": Decimal("2")}):
        with pytest.raises(ValueError, match="one flow for each"):
            monthly.apply_grab_flows(sheet, flows)
    for value in ("1", 1.0, Decimal("0"), Decimal("-1"), Decimal("Infinity"), Decimal("sNaN")):
        with pytest.raises(ValueError, match="positive decimal"):
            monthly.apply_grab_flows(sheet, {first: value, second: Decimal("1")})
    for value in (Decimal("1E14"), Decimal("0.00000000001")):
        with pytest.raises(ValueError, match="14 whole digits and 10 decimal places"):
            monthly.apply_grab_flows(sheet, {first: value, second: Decimal("1")})
    with pytest.raises(ValueError, match="one flow for each"):
        monthly.apply_grab_flows(
            sheet, {first: Decimal("1"), second: Decimal("1"), "unexpected": Decimal("1")}
        )


def test_flow_series_token_includes_the_single_observation_days(project, issue):
    original = issue(project, sample("one day", end="2026-11-02", reported="2.00"), batches=BATCHES)
    issue(project, sample("first"), sample("second"), batches=BATCHES)
    sheet = worksheet(project)
    issue(project, sample("elsewhere", collection_location="Other location"), batches=BATCHES)
    assert worksheet(project)["series_token"] == sheet["series_token"]

    issue(
        project,
        sample("one day", end="2026-11-02", reported="2.00"),
        amends=original,
        batches=BATCHES,
    )
    amended = worksheet(project)
    assert amended["flow_keys"] == sheet["flow_keys"]
    assert amended["series_token"] != sheet["series_token"]


@pytest.mark.parametrize("first_kind", ["Grab", "Composite"])
def test_multiple_composites_or_mixed_samples_block_weighting(project, issue, first_kind):
    start = {"collection_started_at": "2026-11-01T00:00:00-05:00"}
    issue(
        project,
        sample("first", collection_type=first_kind, **(start if first_kind == "Composite" else {})),
        sample("second", collection_type="Composite", **start),
        batches=BATCHES,
    )
    sheet = worksheet(project)
    assert sheet["days"] == 1 and sheet["mean"] is sheet["maximum"] is None
    assert not sheet["flow_keys"] and all(
        "eligible grab samples" in row["reason"] for row in sheet["rows"]
    )


@pytest.mark.parametrize("collected", ["2026-11-01", "2026-11-01T20:00:00"])
def test_grab_that_needs_flow_requires_issued_time_and_offset(project, issue, collected):
    issue(project, sample("no exact instant", end=collected), batches=BATCHES)
    assert worksheet(project)["mean"] == "1.00"

    issue(project, sample("second"), batches=BATCHES)
    sheet = worksheet(project)
    assert sheet["mean"] is sheet["maximum"] is None and not sheet["flow_keys"]
    assert "time zone offset" in sheet["rows"][0]["reason"]


def test_incomplete_other_day_blocks_an_otherwise_eligible_multi_grab_day(project, issue):
    issue(
        project,
        sample("first"),
        sample("second"),
        sample("unusable", end="2026-11-02", results=[]),
        batches=BATCHES,
    )
    sheet = worksheet(project)
    assert sheet["mean"] is sheet["maximum"] is None
    assert not sheet["flow_keys"] and all(row["flow_key"] is None for row in sheet["rows"])


def test_repeated_result_on_same_sample_blocks_the_whole_series(project, issue):
    repeated = sample("repeated")
    repeated["results"].append(repeated["results"][0].copy())
    issue(project, repeated, batches=BATCHES)
    sheet = worksheet(project)
    assert sheet["mean"] is sheet["maximum"] is None and not sheet["flow_keys"]
    assert all("same lab sample ID" in row["reason"] for row in sheet["rows"])


def test_worksheet_does_not_offer_more_flows_than_can_be_submitted(project, issue, monkeypatch):
    monkeypatch.setattr(monthly, "MAX_MONTHLY_FLOWS", 2)
    issue(project, sample("1"), sample("2"), sample("3"), batches=BATCHES)
    sheet = worksheet(project)
    assert len(sheet["rows"]) == 3 and sheet["days"] == 1
    assert sheet["mean"] is sheet["maximum"] is None and not sheet["flow_keys"]
    assert "2 grab flow limit" in sheet["reason"]


def test_equal_maximum_preserves_first_issued_precision(project, issue):
    issue(project, sample("second day", end="2026-11-02", reported="1.00"), batches=BATCHES)
    issue(project, sample("first day", end="2026-11-01", reported="1.0"), batches=BATCHES)
    sheet = worksheet(project)
    assert sheet["mean"] == sheet["maximum"] == "1.00"


def test_ineligible_units_qualifications_qc_and_field_designations_remain_visible(project, issue):
    qualified = sample("qualifier", end="2026-11-02")
    qualified["results"][0]["qualifiers"] = ["J"]
    preservation = sample("preservation", end="2026-11-03")
    preservation["results"][0]["preservation"] = {}
    issue(
        project,
        sample("unit", unit="mg/L as N"),
        qualified,
        preservation,
        sample("receipt", end="2026-11-04", disposition="Qualify"),
        sample("duplicate", end="2026-11-05", sample_type="Field duplicate"),
        batches=BATCHES,
    )
    issue(project, sample("missing QC", end="2026-11-06"))
    sheet = worksheet(project)
    assert sheet["days"] == 6 and sheet["mean"] is sheet["maximum"] is None
    reasons = {row["lab_sample_id"]: row["reason"] for row in sheet["rows"]}
    assert "mg/L" in reasons["unit"]
    assert "Qualified" in reasons["qualifier"]
    assert "preservation" in reasons["preservation"]
    assert "receipt" in reasons["receipt"]
    assert "field sample" in reasons["duplicate"]
    assert "incomplete batch QC" in reasons["missing QC"]


def test_composites_overlapping_either_month_boundary_and_unplaced_rows(project, issue):
    def composite(lab_id, start, end):
        return sample(lab_id, end, collection_type="Composite", collection_started_at=start)

    issue(
        project,
        sample("single", end="2026-11-04"),
        sample("no day", end="2026-02-30"),
        composite("month start", "2026-10-31", "2026-11-01"),
        composite("month end", "2026-11-30", "2026-12-01"),
        composite("entire month", "2026-10-28", "2026-12-02"),
        composite("invalid interval", "2026-11-01T14:00", "2026-11-01T13:00"),
        composite("no start", None, "2026-12-01"),
        composite("no end", "2026-10-01", None),
        composite("ended before", None, "2026-10-01"),
        composite("started after", "2026-12-01", None),
        batches=BATCHES,
    )
    sheet = worksheet(project)
    assert sheet["days"] == 1 and sheet["mean"] is sheet["maximum"] is None
    single, *blocked = sheet["rows"]
    assert single["day"] == date(2026, 11, 4)
    assert all(row["day"] is None and row["reason"] for row in blocked)
    assert {row["lab_sample_id"] for row in blocked} == {
        "no day",
        "month start",
        "month end",
        "entire month",
        "invalid interval",
        "no start",
        "no end",
    }


@pytest.mark.parametrize(
    ("reported", "mean", "maximum"),
    [
        (["0", "1", "1"], "2/3", "1"),
        (["0", "1", "2"], "1", "2"),
        (["1E-60", "1E60"], "5" + "0" * 59 + "." + "0" * 60 + "5", "1" + "0" * 60),
    ],
)
def test_exact_outputs_independent_of_decimal_context(project, issue, reported, mean, maximum):
    samples = [
        sample(str(i), end=f"2026-11-{i:02d}", reported=value)
        for i, value in enumerate(reported, 1)
    ]
    issue(project, *samples, batches=BATCHES)
    with localcontext(prec=1):
        sheet = worksheet(project)
    assert sheet["mean"] == mean and sheet["maximum"] == maximum


def test_excess_series_never_shows_partial_results(project, issue, monkeypatch):
    monkeypatch.setattr(monthly, "MAX_MONTHLY_ROWS", 1)
    issue(project, sample("1"), sample("2", end="2026-11-02"), batches=BATCHES)
    sheet = worksheet(project)
    assert sheet["days"] is sheet["mean"] is sheet["maximum"] is None
    assert not sheet["rows"] and "limit" in sheet["reason"]


def test_incomplete_month_selection_does_not_query(project):
    with pytest.raises(ValueError, match="first calendar day"):
        worksheet(project, month=date(2026, 11, 15))


def test_reissued_sample_on_conflicting_dates_cannot_be_counted_twice(project, issue):
    issue(project, sample("reused"), batches=BATCHES)
    issue(project, sample("reused", end="2026-11-02"), batches=BATCHES)
    sheet = worksheet(project)
    assert sheet["mean"] is sheet["maximum"] is None
    assert all("same lab sample ID" in row["reason"] for row in sheet["rows"])


def test_noncanonical_or_unrecognized_calendar_stamps_never_supply_a_daily_value(project, issue):
    issue(
        project,
        sample("compact", end="20261201T230000-0500"),
        sample("nonstandard separator", end="2026-12-01X23:00-05:00"),
        sample("unusual offset", end="2026-12-01T23:00+22:00"),
        batches=BATCHES,
    )
    sheet = worksheet(project)
    assert sheet["days"] == 0 and sheet["mean"] is sheet["maximum"] is None
    assert all(row["reason"] and row["day"] is None for row in sheet["rows"])


def test_damaged_issued_result_collections_withhold_otherwise_valid_numbers(project, issue):
    issue(project, sample("good"), batches=BATCHES)
    issue(project, sample("missing analyte", results=[{"unit": "mg/L"}]))
    issue(project, sample("invalid results", results={"unexpected": "object"}))
    issue(project, None)
    sheet = worksheet(project)
    assert sheet["days"] == 1 and sheet["mean"] is sheet["maximum"] is None
    assert len(sheet["rows"]) == 4
    assert all(row["reason"] for row in sheet["rows"] if row["lab_sample_id"] != "good")


def test_missing_current_file_identifies_report_and_withholds_pdf_link(project, issue):
    good = issue(project, sample("known"), batches=BATCHES)
    report = Report.objects.create(project=project, created_by=good.released_by, latest_version=1)
    ReportVersion.objects.create(
        report=report,
        number=1,
        released_by=good.released_by,
        released_at=good.released_at,
        pdf_size=1,
    )
    sheet = worksheet(project)
    assert sheet["mean"] is sheet["maximum"] is None
    present, unavailable = sheet["rows"]
    assert present["pdf_available"] and not unavailable["pdf_available"]
    assert unavailable["report_number"] == report.number and unavailable["report_version"] == 1
