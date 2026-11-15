"""Instrument inventories retain current membership and explicit recorded provenance."""

import csv
import io
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from analytical import run_lists, services
from analytical.models import Batch, PreparedAliquot, QCSample, Result, ResultRevision
from analytical.run_lists import COLUMNS, MAX_ROWS, export_run_list
from analytical.tests.conftest import make_user
from analytical.tests.test_batch_equipment import asset
from analytical.tests.test_disposed_samples import dispose
from analytical.tests.test_preparation import aliquot, enter, prep_world
from analytical.tests.test_workflow import entry, start
from core.models import AuditEvent

pytestmark = pytest.mark.django_db


def inventory(actor, batch):
    return list(
        csv.DictReader(io.StringIO(export_run_list(actor=actor, batch_id=batch.pk).content))
    )


def url(batch):
    return reverse("analytical:batch-run-list", args=[batch.pk])


def append_legacy_revision(previous, **changes):
    """Insert historical provenance without changing any immutable observation."""
    values = {
        field.attname: getattr(previous, field.attname)
        for field in ResultRevision._meta.concrete_fields
        if not field.primary_key
    }
    values.update(
        number=previous.number + 1,
        correction_reason="Synthetic historical repeat",
        **changes,
    )
    revision = ResultRevision.objects.create(**values)
    Result.objects.filter(pk=previous.result_id).update(latest_number=revision.number)
    return revision


def test_complete_current_inventory_is_ordered_exact_and_scoped(config, analyst):
    batch = start(config, analyst, samples=config["samples"])
    equipment = asset(analyst, 'ICP, "synthetic"')
    services.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=equipment)
    sample = config["samples"][0]
    result = batch.results.get(
        requested_test__sample=sample, requested_test__definition=config["defs"]["Lead"]
    )
    revision = services.enter_result(
        actor=analyst, result_id=result.pk, **entry("ND", reporting_limit_applied="9.125")
    )
    # The run list describes today's setup, not a historical result limit or method snapshot.
    definition = config["defs"]["Lead"]
    definition.reporting_limit = Decimal("1234567890.12345678")
    definition.unit = "ug/L"
    definition.save()
    config["method"].version = "revision 2"
    config["method"].save()
    sample.disposition = "hold"
    sample.save()
    dispose(sample, analyst)
    other = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    QCSample.objects.create(batch=other, label="UNRELATED", qc_type="lcs", created_by=analyst)
    qc = QCSample.objects.create(
        batch=batch,
        label='MS, "1"',
        qc_type="matrix_spike",
        parent_sample=sample,
        created_by=analyst,
    )
    blank = QCSample.objects.create(
        batch=batch, label="MB", qc_type="method_blank", created_by=analyst
    )

    rows = inventory(analyst, batch)
    assert tuple(rows[0]) == COLUMNS
    assert [(r["sample_id"], r["analyte"]) for r in rows[:4]] == [
        (s.lab_id, config["defs"][name].analyte.name)
        for s in config["samples"]
        for name in ("Copper", "Lead")
    ]
    assert {r["batch_id"] for r in rows} == {batch.number}
    assert {r["method_version"] for r in rows} == {"revision 2"}
    assert {r["equipment_id"] for r in rows} == {equipment.asset_id}
    lead = next(r for r in rows if r["requested_test_id"] == str(result.requested_test_id))
    assert lead["configured_reporting_limit"] == "1234567890.12345678"
    assert lead["unit"] == "ug/L" and lead["result_status"] == "draft"
    assert lead["sample_disposition"] == "hold" and lead["sample_disposed"] == "yes"
    assert revision.reporting_limit_applied == Decimal("9.125")
    assert [(r["qc_sample_id"], r["qc_type"]) for r in rows[4:]] == [
        (str(qc.pk), "matrix_spike"),
        (str(blank.pk), "method_blank"),
    ]
    assert rows[4]["parent_sample_id"] == sample.lab_id
    assert all(rows[4][key] == "" for key in COLUMNS[7:19])
    assert rows[5]["parent_sample_id"] == ""


def test_preparation_is_latest_explicit_link_in_this_batch_only(config, analyst):
    prep = prep_world(config, analyst)
    first, second = config["samples"]
    prepared = aliquot(prep, first, analyst, label='Digestate, "A"')
    aliquot(prep, second, analyst, label="Planned only")
    QCSample.objects.create(batch=prep, label="PREP QC", qc_type="method_blank", created_by=analyst)
    batch = start(config, analyst, samples=config["samples"])
    result = batch.results.filter(requested_test__sample=first).first()
    assert all(r["recorded_prepared_aliquot_id"] == "" for r in inventory(analyst, batch))
    enter(result, analyst, prepared)
    row = next(
        r
        for r in inventory(analyst, batch)
        if r["requested_test_id"] == str(result.requested_test_id)
    )
    assert (
        row["recorded_prepared_aliquot_id"],
        row["recorded_preparation_batch"],
        row["recorded_aliquot_label"],
    ) == (str(prepared.pk), prep.number, prepared.label)
    assert len(inventory(analyst, batch)) == 4  # no preparation QC rows
    # Same result's newest revision is now in a different analytical run. Do not fall back
    # to an older revision from this run or infer a candidate from preparation membership.
    other = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    revision = ResultRevision.objects.get(result=result, number=1)
    append_legacy_revision(revision, batch_id=other.pk)
    assert all(r["recorded_prepared_aliquot_id"] == "" for r in inventory(analyst, batch))


def test_latest_revision_without_aliquot_does_not_reuse_older_link(config, analyst):
    prep = prep_world(config, analyst)
    prepared = aliquot(prep, config["samples"][0], analyst)
    batch = start(config, analyst)
    result = batch.results.first()
    first = enter(result, analyst, prepared)
    append_legacy_revision(first, prepared_aliquot_id=None)
    assert first.prepared_aliquot_id == prepared.pk
    assert all(r["recorded_prepared_aliquot_id"] == "" for r in inventory(analyst, batch))


def test_missing_configured_limit_is_blank_and_never_uses_observed_limit(config, analyst):
    batch = start(config, analyst)
    result = batch.results.first()
    services.enter_result(actor=analyst, result_id=result.pk, **entry())
    definition = result.requested_test.definition
    definition.reporting_limit = None
    definition.save()
    row = next(
        row
        for row in inventory(analyst, batch)
        if row["requested_test_id"] == str(result.requested_test_id)
    )
    assert row["configured_reporting_limit"] == ""


@pytest.mark.parametrize("source", ["aliquot", "parent"])
def test_unsafe_recorded_aliquot_label_and_qc_parent_are_refused(config, analyst, source):
    prep = prep_world(config, analyst)
    first, second = config["samples"]
    prepared = aliquot(
        prep,
        first,
        analyst,
        label="＝Synthetic unsafe digestate" if source == "aliquot" else "Synthetic safe digestate",
    )
    batch = start(config, analyst)
    enter(batch.results.first(), analyst, prepared)
    second.lab_id = "＋Synthetic unsafe QC parent"
    second.save()
    QCSample.objects.create(
        batch=batch, label="MS", qc_type="matrix_spike", parent_sample=second, created_by=analyst
    )
    column = "recorded_aliquot_label" if source == "aliquot" else "parent_sample_id"
    with pytest.raises(ValidationError, match=f"column {column}"):
        export_run_list(actor=analyst, batch_id=batch.pk)


@pytest.mark.parametrize("status", ["awaiting", "draft", "submitted", "returned", "approved"])
def test_every_result_state_is_inventory(config, analyst, status):
    batch = start(config, analyst)
    result = batch.results.first()
    if status != "awaiting":
        services.enter_result(actor=analyst, result_id=result.pk, **entry())
        Result.objects.filter(pk=result.pk).update(status=status)
    assert status in {row["result_status"] for row in inventory(analyst, batch)}


@pytest.mark.parametrize(
    "role",
    [
        "custodian",
        "reviewer",
        "approver",
        "project_manager",
        "none",
        "client",
        "inactive",
        "superuser",
    ],
)
def test_service_and_endpoint_deny_nonanalysts_even_with_batch_id(config, analyst, client, role):
    batch = start(config, analyst)
    user = make_user(
        f"run-list-{role}",
        role if role not in {"none", "client", "inactive", "superuser"} else "analyst",
    )
    if role == "none":
        user.groups.clear()
    elif role == "client":
        user.client = config["samples"][0].order.project.client
    elif role == "inactive":
        user.is_active = False
    elif role == "superuser":
        user.groups.clear()
        user.is_superuser = True
    user.save()
    with pytest.raises(PermissionDenied):
        export_run_list(actor=user, batch_id=batch.pk)
    client.force_login(user)
    assert client.get(url(batch)).status_code == (302 if role == "inactive" else 403)


def test_anonymous_service_denied_and_endpoint_requires_login(config, analyst, client):
    batch = start(config, analyst)
    with pytest.raises(PermissionDenied):
        export_run_list(actor=AnonymousUser(), batch_id=batch.pk)
    assert client.get(url(batch)).status_code == 302


def test_admin_export_and_get_only_headers_filters_and_quoted_utf8(
    config, analyst, admin_user, client
):
    batch = start(config, analyst, samples=config["samples"])
    sample = config["samples"][0]
    sample.client_sample_id = 'Field, "Échantillon"'
    sample.save()
    client.force_login(admin_user)
    response = client.get(
        url(batch),
        {"q": "no match", "status": "approved", "page": "999", "per_page": "1", "selected": "999"},
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == f'attachment; filename="{batch.number}-run-list.csv"'
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response.content.count(b"\r\n") == 5
    assert b"\n" not in response.content.replace(b"\r\n", b"")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    assert len(rows) == 4 and rows[0]["client_sample_id"] == sample.client_sample_id
    assert client.post(url(batch)).status_code == 405
    assert client.head(url(batch)).status_code == 405


def test_missing_preparation_and_closed_empty_batches(config, analyst, client):
    batch = services.create_batch(actor=analyst, method=config["method"], matrix="water")
    exported = export_run_list(actor=analyst, batch_id=batch.pk)
    assert exported.content == ",".join(COLUMNS) + "\r\n"
    client.force_login(analyst)
    assert client.get(url(batch)).status_code == 200
    prep = prep_world(config, analyst)
    for pk in (prep.pk, 9_999_999):
        with pytest.raises(Http404):
            export_run_list(actor=analyst, batch_id=pk)
        assert client.get(reverse("analytical:batch-run-list", args=[pk])).status_code == 404
    # A closed inventory with remaining members is still downloadable.
    populated = start(config, analyst)
    services.close_batch(actor=analyst, batch_id=populated.pk)
    assert {row["batch_status"] for row in inventory(analyst, populated)} == {"closed"}


@pytest.mark.parametrize(
    "unsafe",
    [
        "=SUM(1)",
        "+1",
        "-1",
        "@SUM(1)",
        "  =1",
        "\u2003＠x",
        "＝1",
        "＋1",
        "－1",
        "a\tb",
        "a\rb",
        "a\nb",
        "\x01x",
        " \x1cx",
        "\x7fx",
        "\u0085x",
        "\ufeffx",
        "\u200bx",
    ],
)
def test_unsafe_text_refuses_without_rewriting_or_copying_payload(config, analyst, unsafe):
    batch = start(config, analyst)
    sample = config["samples"][0]
    sample.client_sample_id = unsafe
    sample.save()
    with pytest.raises(ValidationError) as caught:
        export_run_list(actor=analyst, batch_id=batch.pk)
    message = " ".join(caught.value.messages)
    assert "row 2, column client_sample_id" in message
    assert unsafe not in message
    sample.refresh_from_db()
    assert sample.client_sample_id == unsafe


def test_formula_qc_rejection_redirect_is_actionable_without_payload(config, analyst, client):
    batch = start(config, analyst)
    payload = '=HYPERLINK("https://example.invalid/<script>")'
    qc = QCSample.objects.create(batch=batch, label=payload, qc_type="lcs", created_by=analyst)
    client.force_login(analyst)
    response = client.get(url(batch))
    assert response.status_code == 302 and response.url == reverse(
        "analytical:batch-detail", args=[batch.pk]
    )
    assert "Content-Disposition" not in response
    messages = [str(message) for message in get_messages(response.wsgi_request)]
    assert len(messages) == 1
    assert "Run list not exported." in messages[0]
    assert "row 4, column qc_label" in messages[0] and "Correct the source text" in messages[0]
    assert payload not in messages[0] and "<script>" not in messages[0]
    qc.refresh_from_db()
    assert qc.label == payload


@pytest.mark.parametrize(
    ("source", "field", "column"),
    [
        ("method", "code", "method_code"),
        ("method", "version", "method_version"),
        ("analyte", "name", "analyte"),
        ("definition", "unit", "unit"),
        ("sample", "lab_id", "sample_id"),
        ("equipment", "asset_id", "equipment_id"),
    ],
)
def test_text_guard_covers_setup_and_sample_identifiers(config, analyst, source, field, column):
    batch = start(config, analyst)
    equipment = asset(analyst)
    services.set_batch_equipment(actor=analyst, batch_id=batch.pk, equipment=equipment)
    definition = config["defs"]["Lead"]
    record = {
        "method": config["method"],
        "analyte": definition.analyte,
        "definition": definition,
        "sample": config["samples"][0],
        "equipment": equipment,
    }[source]
    setattr(record, field, "＝unsafe")
    record.save()
    with pytest.raises(ValidationError, match=f"column {column}"):
        export_run_list(actor=analyst, batch_id=batch.pk)


def test_result_membership_alone_can_exceed_bound(config, analyst, monkeypatch):
    batch = start(config, analyst)
    monkeypatch.setattr(run_lists, "MAX_ROWS", 1)
    with pytest.raises(ValidationError, match="limited to 1 rows"):
        export_run_list(actor=analyst, batch_id=batch.pk)


def test_combined_row_bound_allows_exact_limit_and_refuses_whole_download(config, analyst, client):
    batch = start(config, analyst)
    QCSample.objects.bulk_create(
        [
            QCSample(batch=batch, label=f"QC-{i:04d}", qc_type="lcs", created_by=analyst)
            for i in range(MAX_ROWS - 2)
        ]
    )
    assert len(inventory(analyst, batch)) == MAX_ROWS
    QCSample.objects.create(batch=batch, label="ONE TOO MANY", qc_type="lcs", created_by=analyst)
    with pytest.raises(ValidationError, match="2,000 rows"):
        export_run_list(actor=analyst, batch_id=batch.pk)
    client.force_login(analyst)
    response = client.get(url(batch))
    assert response.status_code == 302 and "Content-Disposition" not in response
    assert "2,000 rows" in " ".join(str(m) for m in get_messages(response.wsgi_request))


def test_bounded_queries_and_no_state_or_audit_writes(config, analyst):
    prep = prep_world(config, analyst)
    prepared = aliquot(prep, config["samples"][0], analyst)
    batch = start(config, analyst, samples=config["samples"])
    enter(
        batch.results.filter(requested_test__sample=config["samples"][0]).first(), analyst, prepared
    )
    QCSample.objects.bulk_create(
        [
            QCSample(
                batch=batch,
                label=f"QC-{i}",
                qc_type="lcs",
                parent_sample=config["samples"][0],
                created_by=analyst,
            )
            for i in range(75)
        ]
    )
    models = (Batch, Result, ResultRevision, PreparedAliquot, QCSample, AuditEvent)
    before = [list(model.objects.order_by("pk").values()) for model in models]
    with CaptureQueriesContext(connection) as queries:
        assert len(inventory(analyst, batch)) == 79
    assert len(queries) <= 5
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    assert [list(model.objects.order_by("pk").values()) for model in models] == before
