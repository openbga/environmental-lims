from datetime import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from receipt.models import Client, Project
from reporting.location_history import comparison_context, location_history
from reporting.models import Report, ReportFile, ReportVersion
from reporting.results_csv import issued_result_rows

LOCATION = "North outfall's 100% line"
COLLECTED = "2026-11-01T01:15:00-05:00"


@pytest.fixture
def project(db):
    client = Client.objects.create(name="Synthetic Utilities")
    return Project.objects.create(client=client, name="Synthetic Stormwater")


@pytest.fixture
def issue(db):
    """Store representative frozen records without needing their mutable source records."""
    issuer = get_user_model().objects.create(username="history-issuer")

    def freeze(project, *samples, amends=None, synthetic_notice=None, batches=None):
        report = (
            amends.report
            if amends
            else Report.objects.create(project=project, title="Synthetic", created_by=issuer)
        )
        number = report.latest_version + 1
        version = ReportVersion.objects.create(
            report=report,
            number=number,
            amends=amends,
            amendment_reason="Synthetic correction" if amends else "",
            released_by=issuer,
            released_at=timezone.now(),
            pdf_size=1,
        )
        content = {"report": {"number": report.number}, "samples": samples}
        if batches is not None:
            content["batches"] = batches
        if synthetic_notice is not None:
            content["synthetic_notice"] = synthetic_notice
        ReportFile.objects.create(
            version=version,
            pdf=b"%",
            snapshot={
                "release": {"version": number},
                "content": content,
            },
        )
        report.latest_version = number
        report.save(update_fields=["latest_version"])
        return version

    return freeze


def sample(lab_id, *results, location=LOCATION, collected_at=COLLECTED):
    return {
        "lab_id": lab_id,
        "client_sample_id": f"CS-{lab_id}",
        "matrix": "Soil",
        "collected_at": collected_at,
        "collection_location": location,
        "results": list(results) if results else [result()],
    }


def result(analyte="Lead (synthetic)", **fields):
    return {
        "analyte": analyte,
        "reported": "+1.20000000000000001e-3",
        "unit": "µg/kg",
        "reporting_limit": "0.0100",
        "method_code": "DEMO-ICP",
        **fields,
    }


def history(project, **options):
    return location_history(
        **{"client_id": project.client_id, "project_id": project.pk, "location": LOCATION} | options
    )


def test_history_is_scoped_to_current_owned_frozen_versions(project, issue):
    original = issue(project, sample("superseded"))
    current = issue(
        project,
        sample("current"),
        sample("wrong-case", location=LOCATION.lower()),
        sample("trailing-space", location=LOCATION + " "),
        sample("blank", location=""),
        sample("legacy", location=None),
        amends=original,
    )
    old_location = issue(project, sample("old-location"))
    issue(project, sample("moved", location="Another outfall"), amends=old_location)
    other_project = Project.objects.create(client=project.client, name="Other owned project")
    issue(other_project, sample("other-project"))
    other_client = Client.objects.create(name="Other Synthetic Utilities")
    foreign_project = Project.objects.create(client=other_client, name="Synthetic Stormwater")
    issue(foreign_project, sample("other-client"))

    (row,) = history(project)
    assert row["lab_sample_id"] == "current"
    assert row["report_id"] == current.report_id
    assert row["report_number"] == current.report.number
    assert row["report_version"] == 2
    assert not history(project, project_id=foreign_project.pk)
    assert not history(project, client_id=other_client.pk)
    assert not history(project, location="")
    assert not history(project, location="  ")


def test_history_preserves_frozen_result_and_external_text(project, issue):
    released = issue(
        project,
        sample(
            "01",
            result(qualifiers=["J", "B"]),
            result("Cadmium (synthetic)", result_basis="dry", method_version="r1"),
            result(
                "Mercury (synthetic)",
                source="external",
                reported="ND",
                external_qualifiers="J: estimated, as reported",
                external={
                    "laboratory": "Synthetic Partner Lab",
                    "report_reference": "0000000042",
                    "external_sample_id": "0010",
                },
            ),
        ),
    )
    rows = list(history(project))
    for row, export in zip(rows, issued_result_rows(released.file.snapshot), strict=True):
        assert row["collected_at"].isoformat() == export["collected_at"]
        assert all(
            row[key] == value
            for key, value in export.items()
            if key != "collected_at" and key in row
        )
    legacy, dry, external = rows
    assert legacy["client_sample_id"] == "CS-01"
    assert legacy["result"] == "+1.20000000000000001e-3"
    assert legacy["qualifiers"] == "J, B"
    assert legacy["reporting_limit"] == "0.0100"
    assert legacy["result_basis"] == "Basis not recorded"
    assert legacy["method_version"] is None
    assert dry["result_basis"] == "Dry weight"
    assert external["result"] == "ND"
    assert external["source"] == "external"
    assert external["subcontract_report_reference"] == "0000000042"
    assert external["qualifiers"] == "J: estimated, as reported"


def test_analyte_search_matches_literal_case_insensitive_substrings(project, issue):
    issue(project, sample("01", result("Synthetic Lead %_"), result("Synthetic Lead XY")))

    assert [row["analyte"] for row in history(project, analyte="lEaD %_")] == ["Synthetic Lead %_"]
    assert not history(project, analyte="'Lead'")


def test_history_uses_frozen_synthetic_notice(project, issue, settings):
    settings.REPORT_SYNTHETIC_DATA_NOTICE = False
    issue(project, sample("marked"), synthetic_notice=True)
    issue(project, sample("unmarked"), synthetic_notice=False)
    issue(project, sample("legacy"))

    assert {row["lab_sample_id"]: row["synthetic_notice"] for row in history(project)} == {
        "marked": True,
        "unmarked": False,
        "legacy": False,
    }


def test_collection_order_uses_instants_and_keeps_distinct_report_appearances(project, issue):
    first = issue(project, sample("same-sample"))
    second = issue(project, sample("same-sample"))
    early = issue(project, sample("early", collected_at="2026-11-01T01:45:00-04:00"))
    unknown = issue(project, sample("legacy", collected_at=None))

    descending = list(history(project))
    ascending = list(history(project, descending=False))
    assert [row["report_id"] for row in descending] == [
        version.report_id for version in (first, second, early, unknown)
    ]
    assert [row["report_id"] for row in ascending] == [
        version.report_id for version in (early, first, second, unknown)
    ]
    assert descending[0]["collected_at"] == datetime.fromisoformat(COLLECTED)
    assert descending[-1]["collected_at"] is None


def test_pagination_returns_a_bounded_page_and_consistent_total(
    project, issue, django_assert_num_queries
):
    issue(project, sample("01", *(result(f"Analyte {index:03}") for index in range(103))))

    with django_assert_num_queries(1) as queries:
        first = history(project)
    assert "file.pdf" not in queries.captured_queries[0]["sql"]
    assert len(first) == first.paginator.per_page == 25
    assert first.paginator.count == 103
    assert first.paginator.num_pages == 5
    assert first.next_page_number() == 2
    assert first[0]["analyte"] == "Analyte 000"
    assert history(project, page="malformed").number == 1
    assert history(project, page=2)[0]["analyte"] == "Analyte 025"
    assert history(project, per_page=50, page=2)[0]["analyte"] == "Analyte 050"

    last = history(project, per_page=100, page=10**30)
    assert last.number == 2
    assert [row["analyte"] for row in last] == ["Analyte 100", "Analyte 101", "Analyte 102"]
    with pytest.raises(ValueError, match="Invalid page size"):
        history(project, per_page=101)


def test_comparison_matches_the_exact_issued_series_before_pagination(project, issue):
    anchor = result(kind="detected", qualifiers=[], result_basis="dry")
    issue(
        project,
        sample("off-location", anchor, location=LOCATION + " "),
        sample("other-matrix", anchor) | {"matrix": "Water"},
        sample(
            "mixed",
            anchor | {"analyte": anchor["analyte"].upper()},
            anchor | {"analyte": anchor["analyte"] + " "},
            anchor | {"unit": "mg/kg"},
            anchor | {"result_basis": "wet"},
            anchor | {"result_basis": "unknown"},
            anchor | {"source": "external"},
            *[anchor] * 26,
        ),
    )
    context = comparison_context(anchor, "Soil")
    second = history(project, comparison=context, page=2)
    assert second.paginator.count == 26
    (row,) = second
    assert row["comparison_available"]
    assert (row["lab_sample_id"], row["sample_position"], row["result_position"]) == (
        "mixed",
        3,
        32,
    )
    assert not row.get("comparison")
    assert not history(project, comparison=context, client_id=project.client_id + 1)


def test_comparison_reads_exact_unqualified_reported_text_and_never_imputes(project, issue):
    anchor = result(kind="detected", qualifiers=[], result_basis="dry")
    threshold = Decimal("1.00000000000000001")
    cases = [
        ({"reported": "+1.00000000000000002", "value": "0"}, "Above limit"),
        ({"reported": "١.٢٠٠"}, "Above limit"),
        ({"reported": "100000000000000001E-17"}, "At limit"),
        ({"reported": "1.00000000000000000"}, "Below limit"),
        (
            {"kind": "non_detect", "reported": "ND", "reporting_limit": "0.01"},
            "Not compared · Non-detect",
        ),
        ({"qualifiers": ["J"]}, "Not compared · Qualified result"),
        ({"reported": "<1.2"}, "Not compared · No numeric result"),
        ({"reported": "NaN"}, "Not compared · No numeric result"),
        ({"reported": "3_5"}, "Not compared · No numeric result"),
        ({"reported": None}, "Not compared · No numeric result"),
        ({"kind": "text"}, "Not compared · Result information incomplete"),
        ({"qualifiers": None}, "Not compared · Result information incomplete"),
    ]
    issue(project, sample("exact", *(anchor | fields for fields, _ in cases)))
    page = history(project, comparison=comparison_context(anchor, "Soil"), upper_limit=threshold)
    assert [row["comparison"] for row in page] == [label for _, label in cases]
    assert page[0]["result"] == "+1.00000000000000002"


def test_comparison_groups_only_recorded_soil_basis_and_known_aqueous_matrices(project, issue):
    anchor = result(kind="detected", qualifiers=[], reported="0")
    for matrix in ("Soil", "Water", "Wastewater", "Unrecognized"):
        issue(
            project,
            sample(
                matrix,
                anchor,
                anchor | {"result_basis": "na"},
                anchor | {"result_basis": "wet"},
                anchor | {"source": "external", "external_qualifiers": ""},
                anchor | {"source": "external", "external_qualifiers": "J: estimated"},
                anchor | {"source": "external"},
            )
            | {"matrix": matrix},
        )
        context = comparison_context(anchor, matrix)
        rows = history(project, comparison=context, upper_limit=Decimal("-0"))
        if matrix in ("Water", "Wastewater"):
            assert context["basis_label"] == "Not applicable"
            assert [row["comparison"] for row in rows] == [
                "At limit",
                "At limit",
                "At limit",
                "Not compared · Qualified result",
                "Not compared · Result information incomplete",
            ]
        else:
            assert context["basis"] == "unknown"
            assert len(rows) == (5 if matrix == "Soil" else 6)
            assert all(
                row["comparison"] == "Not compared · Reporting basis unavailable" for row in rows
            )
        assert all(row["matrix"] == matrix for row in rows)


def test_comparison_refuses_missing_identity_and_nonfinite_limits(project):
    assert comparison_context(result(unit=" "), "Water") is None
    assert comparison_context(result(analyte=None), "Soil") is None
    assert comparison_context(result(), None) is None
    for upper_limit in (Decimal("NaN"), Decimal("-Infinity"), 1.25):
        with pytest.raises(ValueError, match="finite decimal"):
            history(
                project, comparison=comparison_context(result(), "Water"), upper_limit=upper_limit
            )


def test_comparison_observes_only_qualifications_frozen_for_this_sample_and_test(project, issue):
    detected = result(kind="detected", qualifiers=[], result_basis="dry", reported="12")
    accepted = {"issues": [{"disposition": "accept_qualified"}]}
    clean = {"issues": [{"disposition": "reanalyze"}]}
    # The issued QC uses the renamed analyte. Its definition still identifies the right test.
    batches = [
        {
            "number": "A-1",
            "qc": {
                "Renamed": accepted | {"test_definition_id": 17},
                "Other": clean | {"test_definition_id": 18},
            },
        },
        {"number": "P-1", "qc": {"Renamed": accepted | {"test_definition_id": 19}}},
        {"number": "A-legacy", "qc": {detected["analyte"]: accepted}},
        {"number": "P-other", "qc": {"Other analyte": accepted}},
    ]
    qualified = "Not compared · Qualified result"
    cases = [
        ("scope", {"batch": "A-1", "test_definition_id": 17}, qualified),
        ("same-batch-other-test", {"batch": "A-1", "test_definition_id": 18}, "Above limit"),
        ("other-batch", {"batch": "A-2", "test_definition_id": 17}, "Above limit"),
        (
            "prep",
            {"batch": "A-2", "preparation": {"batch": "P-1"}, "test_definition_id": 19},
            qualified,
        ),
        ("legacy", {"batch": "A-legacy"}, qualified),
        ("unlinked-test", {"batch": "A-legacy", "test_definition_id": 42}, "Above limit"),
        ("unlinked-name", {"preparation": {"batch": "P-other"}}, "Above limit"),
        ("preservation", {"preservation": {"decision": {"decision": "qualify"}}}, qualified),
        ("proceed", {"preservation": {"decision": {"decision": "proceed"}}}, "Above limit"),
    ]
    samples = [sample(label, detected | fields) for label, fields, _ in cases]
    issue(
        project, *samples, sample("receipt", detected) | {"disposition": "Qualify"}, batches=batches
    )
    issue(project, sample("separate-issue", detected | {"batch": "A-1", "test_definition_id": 17}))
    rows = history(
        project, comparison=comparison_context(detected, "Soil"), upper_limit=Decimal("10")
    )
    assert {row["lab_sample_id"]: row["comparison"] for row in rows} == {
        **{label: outcome for label, _, outcome in cases},
        "receipt": qualified,
        "separate-issue": "Above limit",
    }
