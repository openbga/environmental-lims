"""Workbook input reuses exact result, permission, provenance and atomic import gates."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from analytical import import_profiles as ip
from analytical import importing, services
from analytical.models import ResultRevision
from analytical.tests.test_import_profiles import analysed, make_profile
from analytical.tests.xlsx_factory import workbook
from core.models import AuditEvent
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db
HEADER = ["Sample Name", "Run Time", "Element", "Conc", "Units", "RL", "DF"]


@pytest.fixture
def batch(config, analyst):
    batch = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(
            RequestedTest.objects.filter(sample__in=config["samples"]).values_list("pk", flat=True)
        ),
    )
    return batch


def rows(value=("n", "0.1234567890"), **changes):
    row = ["VR-DEMO-1", analysed(), "Pb 220.353", value, "mg/L", ("n", "0.0050"), ("n", "1.00")]
    for field, replacement in changes.items():
        row[HEADER.index(field)] = replacement
    return [["Synthetic instrument table"], HEADER, row]


def parse(batch, profile, data, sheet="Results"):
    return importing.parse_profile_import(
        batch, profile, data, filename="synthetic.xlsx", worksheet=sheet
    )


def commit_args(analyst, batch, profile, data, preview):
    return dict(
        actor=analyst,
        batch_id=batch.pk,
        profile_id=profile.pk,
        data=data,
        filename="synthetic.xlsx",
        worksheet=preview.worksheet,
        mapping_hash=preview.mapping_hash,
        state_token=preview.state_token,
    )


def test_exact_workbook_http_preview_commit_and_repeat(config, analyst, client, batch):
    profile = make_profile(analyst)
    data = workbook({"Notes": [["Ignored sheet"]], "Results": rows()})
    client.force_login(analyst)
    url = reverse("analytical:batch-import-mapped", args=[batch.pk])
    response = client.post(
        url,
        {
            "profile": profile.pk,
            "worksheet": "Results",
            "step": "preview",
            "file": SimpleUploadedFile("synthetic.xlsx", data),
        },
    )
    preview = response.context["preview"]
    assert preview.ok, preview.file_errors
    assert preview.rows[0].values["result"] == "0.1234567890"
    assert preview.rows[0].source == "Results!row 3"
    assert not ResultRevision.objects.exists()
    fields = response.context["commit_form"].initial | {"step": "commit"}
    assert fields["worksheet"] == "Results"
    assert client.post(url, fields).status_code == 302
    revision = ResultRevision.objects.get()
    assert revision.value == Decimal("0.1234567890")
    assert revision.supplied_text == "0.1234567890"
    assert revision.source_detail == "synthetic.xlsx:Results!row 3"
    audit = AuditEvent.objects.get(action="batch.results_imported").after
    assert audit["worksheet"] == "Results" and audit["file_format"] == "xlsx"
    assert audit["file_sha256"] == importing.file_digest(data)
    assert audit["mapping_snapshot"] == ip.snapshot(profile)
    assert client.post(url, fields).status_code == 200
    assert ResultRevision.objects.count() == 1


def test_changed_worksheet_or_bytes_cannot_commit_an_old_preview(config, analyst, batch):
    profile = make_profile(analyst)
    data = workbook({"Results": rows(), "Other": rows(("n", "0.9990"))})
    preview = parse(batch, profile, data)
    args = commit_args(analyst, batch, profile, data, preview)
    for changed in (
        {"worksheet": "Other"},
        {"data": workbook({"Results": rows(("n", "0.8880"))})},
    ):
        with pytest.raises(ValidationError, match="changed after the preview"):
            importing.commit_profile_import(**(args | changed))
    assert not ResultRevision.objects.exists()


def test_other_worksheet_in_same_file_is_a_separate_import(config, analyst, batch):
    profile = make_profile(analyst)
    data = workbook({"Results": rows(), "Other": rows(**{"Sample Name": "VR-DEMO-2"})})
    for sheet in ("Results", "Other"):
        preview = parse(batch, profile, data, sheet)
        assert preview.ok, preview.file_errors
        importing.commit_profile_import(**commit_args(analyst, batch, profile, data, preview))
    assert ResultRevision.objects.count() == 2
    assert not parse(batch, profile, data).ok


@pytest.mark.parametrize("field", ["Sample Name", "Run Time", "Element", "Units"])
def test_numeric_identifiers_and_dates_are_not_inferred(config, analyst, batch, field):
    profile = make_profile(analyst)
    preview = parse(batch, profile, workbook({"Results": rows(**{field: ("n", "12345")})}))
    assert not preview.ok
    assert "Store these worksheet cells as text" in str(preview.rows[0].errors)
    assert field in str(preview.rows[0].errors)


def test_one_bad_workbook_result_blocks_all_and_role_is_checked(config, analyst, reviewer, batch):
    profile = make_profile(analyst)
    records = rows()
    records.append(["VR-DEMO-2", analysed(), "Pb 220.353", "bad", "mg/L", "0.0050", "1"])
    data = workbook({"Results": records})
    preview = parse(batch, profile, data)
    assert not preview.ok and preview.rows[0].ok
    args = commit_args(analyst, batch, profile, data, preview)
    with pytest.raises(ValidationError):
        importing.commit_profile_import(**args)
    with pytest.raises(PermissionDenied):
        importing.commit_profile_import(**(args | {"actor": reviewer}))
    assert not ResultRevision.objects.exists()
    assert not AuditEvent.objects.filter(action="batch.results_imported").exists()


def test_wide_workbook_sparse_cells_and_blank_budget(config, analyst, batch, monkeypatch):
    profile = make_profile(analyst, layout="wide", delimiter="tab")
    records = [
        ["Synthetic"],
        ["ID", "Run Time", "Dilution", "Pb", "Cu"],
        ["VR-DEMO-1", analysed(), ("n", "1"), ("n", "0.01200"), None],
    ]
    data = workbook({"Results": records})
    preview = parse(batch, profile, data)
    assert preview.ok and len(preview.rows) == 1 and len(preview.blanks) == 1
    assert preview.rows[0].values["result"] == "0.01200"
    assert "column Pb" in preview.rows[0].detail
    monkeypatch.setattr(importing, "MAX_ROWS", 1)
    refused = parse(batch, profile, data)
    assert not refused.ok and "split the file" in str(refused.file_errors)


def test_worksheet_is_explicit_and_not_valid_for_text(config, analyst, batch):
    profile = make_profile(analyst)
    data = workbook({"Results": rows()})
    assert not parse(batch, profile, data, "").ok
    assert not parse(batch, profile, data, "Missing").ok
    result = importing.parse_profile_import(
        batch, profile, b"a,b", filename="a.csv", worksheet="Results"
    )
    assert result.file_errors == ["Worksheet name is only used for .xlsx files."]
