from datetime import datetime

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from receipt.models import Client, Project
from reporting.location_history import location_history
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

    def freeze(project, *samples, amends=None, synthetic_notice=None):
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
