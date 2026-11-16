"""An account hold informs staff without altering analytical release or client artifacts."""

import json

import pytest
from django.urls import reverse

from receipt.services import update_client
from reporting.content import assemble
from reporting.edd import services as edd
from reporting.pdf import render_html
from reporting.services import release_report

from .test_edd_views import PROJECT, TEST

pytestmark = pytest.mark.django_db
NOTE = "SYNTHETIC INTERNAL ACCOUNT HOLD NOTE 7193"


def test_hold_is_current_staff_notice_and_does_not_change_release_content_or_gates(
    world, people, client
):
    report, owner, actor = world["report_a"], world["client_a"], people["approver"]
    before = assemble(report, actor=actor)
    assert before.releasable
    update_client(
        actor=people["pm"],
        client_id=owner.pk,
        expected_revision=1,
        reason="Synthetic account review",
        account_hold=True,
        account_hold_note=NOTE,
    )
    after = assemble(report, actor=actor)
    assert after.releasable and after.blockers == before.blockers
    assert before.fingerprint == after.fingerprint and before.content == after.content
    client.force_login(actor)
    for name in ("reporting:report-detail", "reporting:report-preview"):
        response = client.get(reverse(name, args=[report.pk]))
        assert response.status_code == 200 and NOTE in response.content.decode()
        assert "Informational notice" in response.content.decode()
    # A preview obtained before the hold is set can still release unchanged analytical content.
    version = release_report(
        actor=actor, report_id=report.pk, expected_fingerprint=before.fingerprint
    )
    frozen = version.file.snapshot
    assert NOTE not in json.dumps(frozen) and "account_hold" not in json.dumps(frozen)
    assert NOTE not in render_html(frozen)
    assert NOTE.encode() not in bytes(version.file.pdf)
    update_client(
        actor=people["pm"],
        client_id=owner.pk,
        expected_revision=2,
        reason="Account review complete",
        account_hold=False,
    )
    for name in ("reporting:report-detail", "reporting:report-preview"):
        assert NOTE not in client.get(reverse(name, args=[report.pk])).content.decode()
    # Clearing the notice creates no amendment or changed report content.
    assert assemble(report, actor=actor).fingerprint == before.fingerprint


def test_portal_report_pdf_and_edd_export_never_receive_internal_hold(world, people, client):
    owner, report = world["client_a"], world["report_a"]
    update_client(
        actor=people["pm"],
        client_id=owner.pk,
        expected_revision=1,
        reason="Synthetic hold",
        account_hold=True,
        account_hold_note=NOTE,
    )
    fp = assemble(report, actor=people["approver"]).fingerprint
    version = release_report(actor=people["approver"], report_id=report.pk, expected_fingerprint=fp)
    edd.save_project_settings(actor=people["pm"], project_id=world["project_a"].pk, **PROJECT)
    edd.save_test_mapping(actor=people["pm"], definition_id=world["definition"].pk, **TEST)
    edd.save_version_context(
        actor=people["pm"],
        version_id=version.pk,
        samples={"VR-A1": {"sys_loc_code": "SYNTH-MW-01"}},
        results={},
    )
    export = edd.generate_export(actor=people["pm"], version_id=version.pk)
    assert NOTE.encode() not in bytes(export.data) and "account_hold" not in json.dumps(
        export.mapping
    )
    client.force_login(world["user_a"])
    for name, args in (
        ("portal:home", []),
        ("portal:project-list", []),
        ("portal:sample-list", []),
        ("portal:report-list", []),
        ("portal:report-detail", [report.pk]),
        ("portal:report-pdf", [report.pk, 1]),
    ):
        response = client.get(reverse(name, args=args))
        assert response.status_code == 200 and NOTE.encode() not in response.content
        assert b"account_hold" not in response.content
    assert client.get(reverse("reporting:report-preview", args=[report.pk])).status_code == 403


def test_draft_pdf_preview_has_no_hold_note(world, people, client, monkeypatch):
    owner, report = world["client_a"], world["report_a"]
    owner.account_hold, owner.account_hold_note = True, NOTE
    owner.save()
    observed = []

    def render(snapshot, *, preview=False):
        observed.append(snapshot)
        assert preview and NOTE not in render_html(snapshot)
        return b"%PDF-synthetic-preview"

    monkeypatch.setattr("reporting.views.render_pdf", render)
    client.force_login(people["approver"])
    response = client.get(reverse("reporting:report-preview-pdf", args=[report.pk]))
    assert response.status_code == 200 and observed
    assert NOTE not in json.dumps(observed[0]) and "account_hold" not in json.dumps(observed[0])
