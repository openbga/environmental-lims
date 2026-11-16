"""Account holds are audited staff information, independent of physical receipt gates."""

from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from core.models import AuditEvent
from receipt.forms import ClientForm
from receipt.models import Client
from receipt.services import create_client, receive_order_sample, update_client

pytestmark = pytest.mark.django_db
NOTE = "Synthetic account instruction: speak with the project manager."


@pytest.mark.parametrize("role", ["project_manager", "admin"])
def test_manager_creates_sets_and_clears_hold_with_audit_and_revision(intake, role):
    manager = intake["user"]("hold_manager", role)
    owner = create_client(actor=manager, name="Synthetic held client")
    assert not owner.account_hold and owner.account_hold_note == ""
    owner = update_client(
        actor=manager,
        client_id=owner.pk,
        expected_revision=1,
        reason="Synthetic account review",
        account_hold=True,
        account_hold_note=f"  {NOTE}  ",
    )
    assert owner.account_hold and owner.account_hold_note == NOTE and owner.revision == 2
    event = AuditEvent.objects.get(action="client.updated", object_id=str(owner.pk))
    assert event.actor == manager and event.reason == "Synthetic account review"
    assert event.before["account_hold"] is False and event.before["account_hold_note"] == ""
    assert event.after["account_hold"] is True and event.after["account_hold_note"] == NOTE
    with pytest.raises(ValidationError, match="Client changed"):
        update_client(
            actor=manager,
            client_id=owner.pk,
            expected_revision=1,
            reason="Stale clear",
            account_hold=False,
        )
    owner = update_client(
        actor=manager,
        client_id=owner.pk,
        expected_revision=2,
        reason="Account reviewed",
        account_hold=False,
    )
    assert not owner.account_hold and owner.account_hold_note == NOTE and owner.revision == 3
    cleared = AuditEvent.objects.filter(action="client.updated").latest("occurred_at")
    assert cleared.before["account_hold"] and not cleared.after["account_hold"]


@pytest.mark.parametrize("note", ["", " \n ", "x" * 501])
def test_hold_requires_a_bounded_nonblank_note_in_form_and_service(intake, note):
    manager = intake["user"]("hold_validation", "project_manager")
    form = ClientForm(
        {"name": "Synthetic held client", "account_hold": "on", "account_hold_note": note}
    )
    assert not form.is_valid() and "account_hold_note" in form.errors
    count = Client.objects.count()
    with pytest.raises(ValidationError):
        create_client(
            actor=manager, name="Synthetic invalid hold", account_hold=True, account_hold_note=note
        )
    assert Client.objects.count() == count
    with pytest.raises(ValidationError):
        update_client(
            actor=manager,
            client_id=intake["client"].pk,
            expected_revision=1,
            reason="Invalid hold",
            account_hold=True,
            account_hold_note=note,
        )
    intake["client"].refresh_from_db()
    assert not intake["client"].account_hold and intake["client"].revision == 1
    assert not AuditEvent.objects.filter(action__startswith="client.").exists()


@pytest.mark.parametrize("role", ["custodian", "analyst", "reviewer", None])
def test_other_staff_and_roleless_accounts_cannot_change_holds(client, intake, role):
    actor = intake["user"]("hold_refused", role)
    client.force_login(actor)
    for name, args in (
        ("receipt:client-create", []),
        ("receipt:client-edit", [intake["client"].pk]),
    ):
        assert client.get(reverse(name, args=args)).status_code == 403
        assert (
            client.post(
                reverse(name, args=args), {"account_hold": "on", "account_hold_note": NOTE}
            ).status_code
            == 403
        )
    with pytest.raises(PermissionDenied):
        update_client(
            actor=actor,
            client_id=intake["client"].pk,
            expected_revision=1,
            reason="Forbidden",
            account_hold=True,
            account_hold_note=NOTE,
        )


def test_portal_identity_with_manager_role_is_refused_and_audit_failure_rolls_back(client, intake):
    portal = intake["user"]("hold_portal", "project_manager", intake["client"])
    client.force_login(portal)
    for name in ("receipt:client-detail", "receipt:client-edit"):
        assert client.get(reverse(name, args=[intake["client"].pk])).status_code == 403
    with pytest.raises(PermissionDenied):
        create_client(actor=portal, name="No", account_hold=True, account_hold_note=NOTE)
    manager = intake["user"]("hold_rollback", "project_manager")
    with patch(
        "receipt.services.record_event", side_effect=RuntimeError("Synthetic audit failure")
    ):
        with pytest.raises(RuntimeError):
            update_client(
                actor=manager,
                client_id=intake["client"].pk,
                expected_revision=1,
                reason="Audit must succeed",
                account_hold=True,
                account_hold_note=NOTE,
            )
    intake["client"].refresh_from_db()
    assert not intake["client"].account_hold and intake["client"].account_hold_note == ""
    assert intake["client"].revision == 1


@pytest.mark.parametrize("role", ["project_manager", "admin"])
def test_hold_client_forms_preserve_invalid_input_and_reject_stale_edits(client, intake, role):
    client.force_login(intake["user"]("hold_form", role))
    create = reverse("receipt:client-create")
    response = client.post(create, {"name": "Synthetic HTTP hold", "account_hold": "on"})
    assert response.status_code == 200
    assert response.context["form"]["account_hold"].value()
    assert response.context["form"].errors["account_hold_note"]
    assert (
        client.post(
            create, {"name": "Synthetic HTTP hold", "account_hold": "on", "account_hold_note": NOTE}
        ).status_code
        == 302
    )
    owner = Client.objects.get(name="Synthetic HTTP hold")
    edit = reverse("receipt:client-edit", args=[owner.pk])
    body = {
        "name": owner.name,
        "is_active": "on",
        "expected_revision": 1,
        "account_hold_note": NOTE,
        "reason": "Cleared by project manager",
    }
    assert client.post(edit, body).status_code == 302
    assert client.post(edit, body | {"account_hold": "on"}).status_code == 200
    owner.refresh_from_db()
    assert not owner.account_hold and owner.account_hold_note == NOTE and owner.revision == 2


def test_current_notice_at_all_receipt_entry_paths_and_receipt_stays_available(client, intake):
    owner = intake["client"]
    owner.account_hold, owner.account_hold_note = True, NOTE
    owner.save()
    # The one-sample compatibility path still creates a receipt under the hold.
    sample = intake["sample"]()
    continued = receive_order_sample(
        actor=intake["actor"],
        order_id=sample.order_id,
        expected_revision=sample.order.revision,
        tests=[intake["definition"]],
        containers=[{"description": "Synthetic bottle", "condition": "Intact"}],
        relinquished_by="Synthetic courier",
        **{
            name: getattr(sample, name)
            for name in (
                "client_sample_id",
                "matrix",
                "collection_type",
                "collected_at",
                "collector_name",
                "collection_location",
                "received_at",
                "cooler_temperature",
                "seal_condition",
                "preservation_check",
                "disposition",
                "current_location",
            )
        },
    )
    assert continued.order_id == sample.order_id and continued.lab_id != sample.lab_id
    client.force_login(intake["actor"])
    paths = [
        reverse("receipt:client-detail", args=[owner.pk]),
        reverse("receipt:order-detail", args=[sample.order_id]),
        reverse("receipt:order-add-sample", args=[sample.order_id]),
    ]
    for path in paths:
        html = client.get(path).content.decode()
        assert NOTE in html and 'role="status"' in html
        assert "Informational notice" in html and "does not block" in html
    generic = reverse("receipt:sample-create")
    fragment = client.get(
        generic, {"project": intake["project"].pk}, HTTP_HX_TARGET="receipt-account-hold"
    )
    assert NOTE in fragment.content.decode() and "<html" not in fragment.content.decode()
    assert NOTE in client.post(generic, {"project": intake["project"].pk}).content.decode()
    assert (
        NOTE
        not in client.get(
            generic, {"project": "invalid"}, HTTP_HX_TARGET="receipt-account-hold"
        ).content.decode()
    )
    owner.account_hold = False
    owner.save()
    for path in paths:
        assert NOTE not in client.get(path).content.decode()
    assert (
        NOTE
        not in client.get(
            generic, {"project": intake["project"].pk}, HTTP_HX_TARGET="receipt-account-hold"
        ).content.decode()
    )
    portal = intake["user"]("hold_fragment_portal", "custodian", owner)
    client.force_login(portal)
    assert (
        client.get(
            generic, {"project": intake["project"].pk}, HTTP_HX_TARGET="receipt-account-hold"
        ).status_code
        == 403
    )


def test_planned_submission_receipt_inherits_current_staff_notice(client, intake):
    from commercial.models import Submission, SubmissionSample

    owner = intake["client"]
    owner.account_hold, owner.account_hold_note = True, NOTE
    owner.save()
    sample = intake["sample"]()
    submission = Submission.objects.create(
        client=owner,
        project=intake["project"],
        order=sample.order,
        status=Submission.Status.ACCEPTED,
        created_by=intake["actor"],
        coc_reference="SYNTHETIC-PLANNED",
        number="SYNTHETIC-HOLD",
    )
    expected = SubmissionSample.objects.create(
        submission=submission,
        client_sample_id="SYNTHETIC-EXPECTED",
        matrix="water",
    )
    path = reverse("commercial:submission-receive", args=[submission.pk, expected.pk])
    client.force_login(intake["actor"])
    page = client.get(path)
    assert page.status_code == 200 and NOTE in page.content.decode()
    assert NOTE in client.post(path, {}).content.decode()
    owner.account_hold = False
    owner.save()
    assert NOTE not in client.get(path).content.decode()
