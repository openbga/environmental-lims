"""Draft billing snapshots preserve exact amounts, attribution and source independence."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.urls import reverse
from django.utils import timezone

from commercial import invoices, pricing
from commercial.models import InvoiceDraft, InvoiceDraftLine, PriceList
from core.models import AuditEvent
from receipt.corrections import change_requested_tests
from receipt.models import Order, RequestedTest, Sample
from receipt.services import create_order, receive_sample

pytestmark = pytest.mark.django_db


@pytest.fixture
def received(lab):
    now = timezone.now() - timedelta(minutes=1)
    sample = receive_sample(
        actor=lab["pm"],
        project=lab["a"]["project"],
        coc_reference="SYN-COC-INVOICE",
        tests=[lab["tests"]["lead"], lab["tests"]["copper"]],
        container_description="250 mL HDPE",
        container_condition="Intact",
        relinquished_by="Synthetic Courier",
        client_sample_id="SYN-FIELD-1",
        matrix="water",
        collected_at=now - timedelta(hours=2),
        received_at=now,
        collection_type="grab",
        collector_name="Synthetic Sampler",
        collection_location="Synthetic outfall",
        cooler_temperature=Decimal("4.125"),
        seal_condition="Intact",
        preservation_check="Checked",
        disposition="hold",
        disposition_note="Synthetic receipt review",
        current_location="Bench",
    )
    return sample


def create(lab, received, price_list, **overrides):
    order = Order.objects.select_related("project__client").get(pk=received.order_id)
    args = dict(
        actor=lab["pm"],
        order_id=order.pk,
        expected_source_state=invoices.source_state(order),
        price_list_id=price_list.pk,
    )
    args.update(overrides)
    return invoices.create_invoice_draft(**args)


def save(lab, draft, **overrides):
    args = dict(
        actor=lab["pm"],
        draft_id=draft.pk,
        expected_revision=draft.revision,
        billing={name: getattr(draft, name) for name in invoices.BILLING_FIELDS},
        lines=[
            {
                "id": line.pk,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
            }
            for line in draft.lines.all()
        ],
        reason="Synthetic negotiated adjustment",
    )
    args.update(overrides)
    return invoices.save_invoice_draft(**args)


def edit_post(draft):
    data = {name: getattr(draft, name) for name in invoices.BILLING_FIELDS}
    data.update(
        expected_revision=draft.revision,
        reason="Correct billing snapshot",
        **{
            "lines-TOTAL_FORMS": draft.lines.count(),
            "lines-INITIAL_FORMS": draft.lines.count(),
            "lines-MIN_NUM_FORMS": 1,
            "lines-MAX_NUM_FORMS": 200,
        },
    )
    for i, line in enumerate(draft.lines.all()):
        data.update(
            {
                f"lines-{i}-id": line.pk,
                f"lines-{i}-description": line.description,
                f"lines-{i}-quantity": line.quantity,
                f"lines-{i}-unit_price": line.unit_price,
            }
        )
    return data


def test_create_snapshots_exact_prices_billing_fallback_and_audit(lab, received, price_list):
    client = lab["a"]["client"]
    client.account_number, client.contact_name, client.email = (
        "SYN-001",
        "A. Example",
        "billing@example.invalid",
    )
    client.address_line1, client.city, client.state, client.postal_code = (
        "1 Example Way",
        "Albany",
        "NY",
        "12207",
    )
    client.payment_terms = "net_30"
    client.notes, client.account_hold_note = "INTERNAL-CLIENT-NOTE", "INTERNAL-HOLD-NOTE"
    client.save()
    project = lab["a"]["project"]
    project.po_number = "SYN-STANDING-PO"
    project.save()
    draft = create(lab, received, price_list)
    assert draft.total == Decimal("61.25")
    assert draft.billing_contact_name == "A. Example" and draft.billing_email == client.email
    assert draft.billing_address == "1 Example Way\nAlbany, NY 12207"
    assert draft.po_number == "SYN-STANDING-PO" and draft.payment_terms == "net_30"
    assert draft.account_code == "SYN-001" and draft.created_by == lab["pm"]
    assert draft.price_list_name == price_list.name and draft.price_list_revision == 1
    assert list(draft.lines.values_list("amount", flat=True)) == [
        Decimal("32.50"),
        Decimal("28.75"),
    ]
    line = draft.lines.first()
    assert line.sample_disposition == "hold" and line.method_version == "1"
    event = AuditEvent.objects.get(action="invoice_draft.created")
    assert event.actor == lab["pm"] and len(event.after["lines"]) == 2
    assert "INTERNAL-CLIENT-NOTE" not in str(event.after)
    assert "INTERNAL-HOLD-NOTE" not in str(event.after)


def test_explicit_billing_contact_and_order_po_override_fallback(lab, received, price_list):
    client = lab["a"]["client"]
    client.billing_contact_name, client.billing_email, client.billing_address = (
        "Accounts",
        "accounts@example.invalid",
        "PO Box 123",
    )
    client.save()
    Order.objects.filter(pk=received.order_id).update(po_number="ORDER-PO")
    draft = create(lab, received, price_list)
    assert (
        draft.billing_contact_name,
        draft.billing_email,
        draft.billing_address,
        draft.po_number,
    ) == ("Accounts", "accounts@example.invalid", "PO Box 123", "ORDER-PO")


@pytest.mark.parametrize("role", ["analyst", "custodian", "a", "b"])
def test_services_refuse_unqualified_actors(lab, received, price_list, role):
    actor = lab[role]["user"] if role in ("a", "b") else lab[role]
    with pytest.raises(PermissionDenied):
        create(lab, received, price_list, actor=actor)
    draft = create(lab, received, price_list)
    with pytest.raises(PermissionDenied):
        save(lab, draft, actor=actor)
    with pytest.raises(PermissionDenied):
        invoices.cancel_invoice_draft(
            actor=actor, draft_id=draft.pk, expected_revision=1, reason="x"
        )


def test_admin_can_create_edit_cancel(lab, received, price_list):
    draft = create(lab, received, price_list, actor=lab["admin"])
    draft = save(lab, draft, actor=lab["admin"])
    invoices.cancel_invoice_draft(
        actor=lab["admin"], draft_id=draft.pk, expected_revision=2, reason="Duplicate scope"
    )
    draft.refresh_from_db()
    assert draft.cancelled_by == lab["admin"]


def test_missing_prices_inactive_empty_cancelled_and_line_limit_refused(lab, received, price_list):
    price_list.items.filter(test=lab["tests"]["copper"]).delete()
    with pytest.raises(ValidationError, match="no price.*Copper"):
        create(lab, received, price_list)
    assert not InvoiceDraft.objects.exists()
    PriceList.objects.filter(pk=price_list.pk).update(is_active=False)
    with pytest.raises(ValidationError, match="active price list"):
        create(lab, received, price_list)
    price_list.is_active = True
    price_list.save()
    empty = create_order(actor=lab["pm"], project=lab["a"]["project"], coc_reference="EMPTY")
    with pytest.raises(ValidationError, match="Receive samples"):
        create(
            lab,
            received,
            price_list,
            order_id=empty.pk,
            expected_source_state=invoices.source_state(empty),
        )
    Order.objects.filter(pk=received.order_id).update(
        cancelled_at=timezone.now(), cancelled_by=lab["pm"], cancel_reason="Synthetic cancellation"
    )
    with pytest.raises(ValidationError, match="cancelled"):
        create(lab, received, price_list)
    Order.objects.filter(pk=received.order_id).update(
        cancelled_at=None, cancelled_by=None, cancel_reason=""
    )
    with (
        patch("commercial.invoices.MAX_LINES", 1),
        pytest.raises(ValidationError, match="at most 1"),
    ):
        create(lab, received, price_list)


def test_edit_exact_totals_reason_revision_and_identity(lab, received, price_list):
    draft = create(lab, received, price_list)
    original = list(
        draft.lines.values(
            "source_requested_test_id",
            "sample_id",
            "definition_id",
            "test_description",
            "lab_id",
            "sample_disposition",
        )
    )
    lines = [
        {
            "id": line.pk,
            "description": f"Agreed: {line.description}",
            "quantity": 3,
            "unit_price": Decimal("0.10"),
        }
        for line in draft.lines.all()
    ]
    with pytest.raises(ValidationError, match="reason"):
        save(lab, draft, lines=lines, reason=" ")
    updated = save(lab, draft, lines=lines)
    assert updated.total == Decimal("0.60") and updated.revision == 2
    assert (
        list(
            updated.lines.values(
                "source_requested_test_id",
                "sample_id",
                "definition_id",
                "test_description",
                "lab_id",
                "sample_disposition",
            )
        )
        == original
    )
    event = AuditEvent.objects.get(action="invoice_draft.saved")
    assert event.before["total"] == "61.25" and event.after["total"] == "0.60"
    assert event.reason == "Synthetic negotiated adjustment"
    with pytest.raises(ValidationError, match="changed"):
        save(lab, draft, lines=lines)
    zero = [{**line, "unit_price": Decimal("0.00")} for line in lines]
    assert save(lab, updated, lines=zero).total == Decimal("0.00")


@pytest.mark.parametrize(
    "price,quantity",
    [
        (Decimal("-0.01"), 1),
        (Decimal("1.005"), 1),
        (Decimal("NaN"), 1),
        (Decimal("Infinity"), 1),
        (0.1, 1),
        (Decimal("1"), 0),
        (Decimal("1"), True),
        (Decimal("1"), 2147483648),
        (Decimal("9999999999.99"), 1000),
    ],
)
def test_invalid_line_values_do_not_mutate(lab, received, price_list, price, quantity):
    draft = create(lab, received, price_list)
    lines = [
        {"id": line.pk, "description": line.description, "quantity": quantity, "unit_price": price}
        for line in draft.lines.all()
    ]
    with pytest.raises(ValidationError):
        save(lab, draft, lines=lines)
    draft.refresh_from_db()
    assert draft.total == Decimal("61.25") and draft.revision == 1


def test_no_added_removed_or_foreign_lines(lab, received, price_list):
    draft = create(lab, received, price_list)
    lines = [
        {
            "id": line.pk,
            "description": line.description,
            "quantity": 1,
            "unit_price": line.unit_price,
        }
        for line in draft.lines.all()
    ]
    for changed in (
        lines[:1],
        lines + lines[:1],
        [lines[0], lines[0]],
        [{**lines[0], "id": 999999}, lines[1]],
    ):
        with pytest.raises(ValidationError):
            save(lab, draft, lines=changed)


def test_source_changes_after_snapshot_do_not_rewrite_or_block_corrections(
    lab, received, price_list
):
    draft = create(lab, received, price_list)
    line = draft.lines.first()
    requested = RequestedTest.objects.get(pk=line.source_requested_test_id)
    change_requested_tests(
        actor=lab["pm"],
        sample_id=received.pk,
        expected_revision=received.revision,
        remove=[requested],
        reason="Client withdrew never-started test",
    )
    assert not RequestedTest.objects.filter(pk=requested.pk).exists()
    lab["a"]["client"].name = "Changed client"
    lab["a"]["client"].save()
    Sample.objects.filter(pk=received.pk).update(client_sample_id="CHANGED", disposition="proceed")
    pricing.save_price_list(
        actor=lab["pm"],
        price_list_id=price_list.pk,
        expected_revision=1,
        name="New price list name",
        prices={lab["tests"]["lead"].pk: Decimal("999.99")},
        reason="New rates",
    )
    draft.refresh_from_db()
    line.refresh_from_db()
    assert draft.client_name == "Example Utilities" and draft.price_list_name == "Standard 2026"
    assert draft.total == Decimal("61.25") and line.client_sample_id == "SYN-FIELD-1"
    assert line.sample_disposition == "hold" and line.unit_price == Decimal("32.50")


def test_source_token_detects_stale_tests_samples_order_and_billing(lab, received, price_list):
    for model, pk, field, value in [
        (Sample, received.pk, "revision", 2),
        (Order, received.order_id, "po_number", "NEW-PO"),
    ]:
        order = Order.objects.select_related("project__client").get(pk=received.order_id)
        token = invoices.source_state(order)
        model.objects.filter(pk=pk).update(**{field: value})
        with pytest.raises(ValidationError, match="samples changed"):
            create(lab, received, price_list, expected_source_state=token)
    order = Order.objects.select_related("project__client").get(pk=received.order_id)
    token = invoices.source_state(order)
    RequestedTest.objects.create(sample=received, definition=lab["tests"]["nitrate"])
    with pytest.raises(ValidationError, match="samples changed"):
        create(lab, received, price_list, expected_source_state=token)
    order = Order.objects.select_related("project__client").get(pk=received.order_id)
    token = invoices.source_state(order)
    order.project.client.billing_email = "changed@example.invalid"
    order.project.client.save()
    with pytest.raises(ValidationError, match="samples changed"):
        create(lab, received, price_list, expected_source_state=token)


def test_cancel_preserves_lines_and_history_and_allows_replacement(lab, received, price_list):
    draft = create(lab, received, price_list)
    with pytest.raises(ValidationError, match="active draft"):
        create(lab, received, price_list)
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceDraft.objects.create(
            **{
                field.name: getattr(draft, field.name)
                for field in InvoiceDraft._meta.fields
                if field.name not in ("id", "number")
            },
            number="OTHER",
        )
    with pytest.raises(ValidationError, match="changed"):
        invoices.cancel_invoice_draft(
            actor=lab["pm"], draft_id=draft.pk, expected_revision=9, reason="Stale"
        )
    cancelled = invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Replace source scope"
    )
    assert cancelled.status == "cancelled" and cancelled.lines.count() == 2
    assert (
        AuditEvent.objects.filter(
            object_id=str(draft.pk), action__startswith="invoice_draft."
        ).count()
        == 2
    )
    with pytest.raises(ValidationError, match="cancelled"):
        save(lab, cancelled)
    with pytest.raises(ValidationError, match="cancelled"):
        invoices.cancel_invoice_draft(
            actor=lab["pm"], draft_id=draft.pk, expected_revision=2, reason="Repeat"
        )
    assert create(lab, received, price_list).pk != cancelled.pk


def test_audit_failure_rolls_back_create_edit_and_cancel(lab, received, price_list):
    with (
        patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")),
        pytest.raises(RuntimeError),
    ):
        create(lab, received, price_list)
    assert not InvoiceDraft.objects.exists() and not InvoiceDraftLine.objects.exists()
    draft = create(lab, received, price_list)
    with patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            save(
                lab,
                draft,
                billing={
                    **{name: getattr(draft, name) for name in invoices.BILLING_FIELDS},
                    "client_name": "Incorrect",
                },
            )
        with pytest.raises(RuntimeError):
            invoices.cancel_invoice_draft(
                actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Wrong"
            )
    draft.refresh_from_db()
    assert (
        draft.status == "draft" and draft.revision == 1 and draft.client_name == "Example Utilities"
    )


def test_views_deny_client_and_staff_roles_and_preserve_invalid_input(
    client, lab, received, price_list
):
    draft = create(lab, received, price_list)
    urls = [
        reverse("commercial:invoice-list"),
        reverse("commercial:invoice-create", args=[received.order_id]),
        *[
            reverse(f"commercial:invoice-{name}", args=[draft.pk])
            for name in ("detail", "edit", "cancel")
        ],
    ]
    for user in (lab["a"]["user"], lab["b"]["user"], lab["analyst"], lab["custodian"]):
        client.force_login(user)
        for url in urls:
            assert client.get(url).status_code == 403
            assert client.post(url, {}).status_code == 403
        order_html = client.get(reverse("receipt:order-detail", args=[received.order_id]))
        if user.client_id is None:
            assert (
                reverse("commercial:invoice-create", args=[received.order_id])
                not in order_html.content.decode()
            )
    client.force_login(lab["pm"])
    for url in urls:
        assert client.get(url).status_code == 200
    data = edit_post(draft)
    data.update(client_name="Typed billing name", reason="")
    data["lines-0-unit_price"] = "1.005"
    response = client.post(reverse("commercial:invoice-edit", args=[draft.pk]), data)
    assert response.status_code == 200
    assert response.context["form"]["client_name"].value() == "Typed billing name"
    assert response.context["formset"][0]["unit_price"].value() == "1.005"
    assert response.context["form"].errors["reason"]
    assert response.context["formset"][0].errors["unit_price"]


def test_create_missing_price_retains_selection_and_no_records(client, lab, received):
    client.force_login(lab["pm"])
    price_list = PriceList.objects.create(name="Empty active list")
    url = reverse("commercial:invoice-create", args=[received.order_id])
    token = client.get(url).context["form"]["expected_source_state"].value()
    response = client.post(url, {"price_list": price_list.pk, "expected_source_state": token})
    assert response.status_code == 200
    assert str(response.context["form"]["price_list"].value()) == str(price_list.pk)
    assert "no price" in str(response.context["form"].errors["price_list"])
    assert not InvoiceDraft.objects.exists()


def test_migration_rollback_preserves_active_and_cancelled_drafts(lab, received, price_list):
    migration = importlib.import_module("commercial.migrations.0005_invoice_drafts")
    editor = SimpleNamespace(connection=connection)
    migration.refuse_reverse_with_drafts(apps, editor)
    draft = create(lab, received, price_list)
    with pytest.raises(RuntimeError):
        migration.refuse_reverse_with_drafts(apps, editor)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Keep history"
    )
    with pytest.raises(RuntimeError):
        migration.refuse_reverse_with_drafts(apps, editor)


@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_leave_one_active_draft(lab, received, price_list):
    order = Order.objects.select_related("project__client").get(pk=received.order_id)
    token = invoices.source_state(order)
    barrier = Barrier(2)

    def attempt():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return invoices.create_invoice_draft(
                actor=lab["pm"],
                order_id=order.pk,
                expected_source_state=token,
                price_list_id=price_list.pk,
            ).pk
        except ValidationError as exc:
            return exc.messages
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(value, int) for value in results) == 1
    assert InvoiceDraft.objects.filter(order=order, status="draft").count() == 1
    assert AuditEvent.objects.filter(action="invoice_draft.created").count() == 1


def test_stale_browser_edit_preserves_input_and_cancel_reason_is_required(
    client, lab, received, price_list
):
    client.force_login(lab["pm"])
    draft = create(lab, received, price_list)
    data = edit_post(draft)
    save(lab, draft)
    data["billing_contact_name"] = "Typed before stale refusal"
    response = client.post(reverse("commercial:invoice-edit", args=[draft.pk]), data)
    assert response.status_code == 200
    assert "draft changed" in str(response.context["form"].non_field_errors())
    assert response.context["form"]["billing_contact_name"].value() == "Typed before stale refusal"
    response = client.post(
        reverse("commercial:invoice-cancel", args=[draft.pk]),
        {"expected_revision": 2, "reason": ""},
    )
    assert response.context["form"].errors["reason"]
    draft.refresh_from_db()
    assert draft.status == "draft" and draft.revision == 2


def test_list_searches_snapshot_values_filters_and_paginates_without_row_queries(
    client, lab, received, price_list
):
    from django.test.utils import CaptureQueriesContext

    draft = create(lab, received, price_list)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Snapshot list fixtures"
    )
    draft.refresh_from_db()
    fields = {
        field.attname: getattr(draft, field.attname)
        for field in InvoiceDraft._meta.fields
        if field.name not in ("id", "number", "created_at")
    }
    InvoiceDraft.objects.bulk_create(
        [InvoiceDraft(**fields, number=f"ID-SYN-{i:04}") for i in range(30)]
    )
    active = create(lab, received, price_list)
    lab["a"]["client"].name = "Renamed mutable client"
    lab["a"]["client"].save()
    client.force_login(lab["pm"])
    url = reverse("commercial:invoice-list")
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, {"q": "Example Utilities", "per_page": 25, "sort": "-total"})
    assert response.status_code == 200
    assert len(response.context["page_obj"]) == 25 and response.context["page_obj"].has_next()
    assert len(queries) < 30
    response = client.get(url, {"status": "draft"})
    assert list(response.context["page_obj"]) == [active]
    response = client.get(url, {"status": "invalid"})
    assert response.context["filter_form"].errors and not response.context["page_obj"]


@pytest.mark.django_db(transaction=True)
def test_disposition_change_between_source_and_sample_locks_is_rechecked(lab, received, price_list):
    from threading import Event

    from receipt.services import change_disposition

    order = Order.objects.select_related("project__client").get(pk=received.order_id)
    token = invoices.source_state(order)
    first_read, changed = Event(), Event()
    original_source_tests = invoices.source_tests
    calls = 0

    def pausing_source_tests(order, *, lock=False):
        nonlocal calls
        rows = original_source_tests(order, lock=lock)
        calls += 1
        if lock and calls == 1:
            first_read.set()
            assert changed.wait(timeout=10)
        return rows

    def correction():
        close_old_connections()
        try:
            assert first_read.wait(timeout=10)
            change_disposition(
                actor=lab["pm"],
                sample_id=received.pk,
                expected_revision=received.revision,
                disposition="proceed",
                reason="Synthetic receipt resolved",
            )
            changed.set()
        finally:
            connections.close_all()

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        patch("commercial.invoices.source_tests", side_effect=pausing_source_tests),
    ):
        future = pool.submit(correction)
        with pytest.raises(ValidationError, match="samples changed"):
            invoices.create_invoice_draft(
                actor=lab["pm"],
                order_id=order.pk,
                expected_source_state=token,
                price_list_id=price_list.pk,
            )
        future.result(timeout=10)
    assert not InvoiceDraft.objects.exists()
    received.refresh_from_db()
    assert received.disposition == "proceed"


def test_cancelled_draft_cancel_page_redirects_but_post_rechecks_state(
    client, lab, received, price_list
):
    draft = create(lab, received, price_list)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Scope replaced"
    )
    client.force_login(lab["pm"])
    url = reverse("commercial:invoice-cancel", args=[draft.pk])
    response = client.get(url, follow=True)
    assert response.redirect_chain == [(reverse("commercial:invoice-detail", args=[draft.pk]), 302)]
    assert "This draft is already cancelled." in response.content.decode()
    response = client.post(url, {"expected_revision": 2, "reason": "Repeated cancellation"})
    assert response.status_code == 200
    assert "cancelled draft cannot be changed" in str(response.context["form"].non_field_errors())
    assert AuditEvent.objects.filter(action="invoice_draft.cancelled").count() == 1
