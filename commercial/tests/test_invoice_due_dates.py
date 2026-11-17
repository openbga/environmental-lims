"""Manually recorded internal dates retain audit provenance and bounded list behavior."""

from datetime import date, datetime
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from commercial import invoices
from commercial.models import InvoiceDraft
from commercial.tests.test_invoices import create, edit_post, save
from commercial.tests.test_invoices import received as received
from core.models import AuditEvent
from receipt.models import Order

pytestmark = pytest.mark.django_db


def events(draft):
    return AuditEvent.objects.filter(object_type="commercial.invoicedraft", object_id=str(draft.pk))


def test_set_change_clear_preserves_terms_amounts_and_history(lab, received, price_list):
    lab["a"]["client"].payment_terms = "net_30"
    lab["a"]["client"].save()
    draft = create(lab, received, price_list)
    original_lines = list(draft.lines.values())
    assert draft.due_date is None
    assert events(draft).get().after["due_date"] is None
    for revision, value in enumerate([date(2020, 2, 29), date(2026, 12, 31), None], 2):
        previous = draft.due_date
        draft = save(lab, draft, due_date=value, actor=lab["admin"])
        draft.refresh_from_db()
        assert draft.due_date == value and draft.revision == revision
        assert draft.payment_terms == "net_30"
        assert list(draft.lines.values()) == original_lines
        event = events(draft).latest("occurred_at")
        assert event.actor == lab["admin"] and event.reason == "Synthetic negotiated adjustment"
        assert event.before["due_date"] == (previous.isoformat() if previous else None)
        assert event.after["due_date"] == (value.isoformat() if value else None)
        assert event.after["total"] == event.before["total"]


def test_omitted_date_preserves_existing_and_terms_never_infer_it(lab, received, price_list):
    draft = create(lab, received, price_list)
    billing = {name: getattr(draft, name) for name in invoices.BILLING_FIELDS}
    billing["payment_terms"] = "net_30"
    draft = save(lab, draft, billing=billing)
    assert draft.due_date is None
    draft = save(lab, draft, due_date=date(2026, 1, 1))
    billing["payment_terms"] = "net_60"
    draft = save(lab, draft, billing=billing)
    assert draft.due_date == date(2026, 1, 1)
    assert "due_date" not in invoices.BILLING_FIELDS


@pytest.mark.parametrize("value", ["2026-11-17", "", 0, True, datetime(2026, 11, 17), object()])
def test_non_date_service_values_leave_entire_state_unchanged(lab, received, price_list, value):
    draft = create(lab, received, price_list)
    before, count = invoices._state(draft), events(draft).count()
    with pytest.raises(ValidationError, match="calendar date"):
        save(lab, draft, due_date=value)
    draft.refresh_from_db()
    assert invoices._state(draft) == before and events(draft).count() == count


@pytest.mark.parametrize("value", [date.min, date.max])
def test_calendar_date_extremes_are_allowed(lab, received, price_list, value):
    draft = save(lab, create(lab, received, price_list), due_date=value)
    draft.refresh_from_db()
    assert draft.due_date == value


def test_stale_cancelled_and_audit_failure_do_not_write_dates(lab, received, price_list):
    draft = create(lab, received, price_list)
    current = save(lab, draft, due_date=date(2026, 11, 17))
    before, count = invoices._state(current), events(current).count()
    with pytest.raises(ValidationError, match="changed"):
        save(lab, draft, due_date=None)
    with (
        patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")),
        pytest.raises(RuntimeError),
    ):
        save(lab, current, due_date=None)
    current.refresh_from_db()
    assert invoices._state(current) == before and events(current).count() == count
    current = invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=current.pk, expected_revision=current.revision, reason="Retain"
    )
    with pytest.raises(ValidationError, match="cancelled"):
        save(lab, current, due_date=None)
    current.refresh_from_db()
    assert current.due_date == date(2026, 11, 17)
    assert events(current).count() == count + 1


@pytest.mark.parametrize("role", ["analyst", "custodian", "a", "b"])
def test_forbidden_service_and_http_edits_do_not_write(client, lab, received, price_list, role):
    actor = lab[role]["user"] if role in ("a", "b") else lab[role]
    draft = create(lab, received, price_list)
    before, count = invoices._state(draft), events(draft).count()
    with pytest.raises(PermissionDenied):
        save(lab, draft, actor=actor, due_date=date(2026, 11, 17))
    client.force_login(actor)
    data = {**edit_post(draft), "due_date": "2026-11-17"}
    assert client.post(reverse("commercial:invoice-edit", args=[draft.pk]), data).status_code == 403
    assert (
        client.get(reverse("commercial:invoice-list"), {"due_from": "2026-11-17"}).status_code
        == 403
    )
    draft.refresh_from_db()
    assert invoices._state(draft) == before and events(draft).count() == count


def test_edit_http_retains_invalid_and_stale_input_then_sets_and_clears(
    client, lab, received, price_list
):
    draft = create(lab, received, price_list)
    client.force_login(lab["pm"])
    url = reverse("commercial:invoice-edit", args=[draft.pk])
    assert client.get(url).context["form"]["due_date"].value() is None
    data = {**edit_post(draft), "due_date": "2026-02-30", "client_name": "Typed client"}
    response = client.post(url, data)
    assert response.context["form"].errors["due_date"]
    assert response.context["form"]["due_date"].value() == "2026-02-30"
    assert response.context["form"]["client_name"].value() == "Typed client"
    draft.refresh_from_db()
    assert draft.revision == 1 and draft.due_date is None and events(draft).count() == 1
    data["due_date"] = "2020-02-29"
    assert client.post(url, data).status_code == 302
    draft.refresh_from_db()
    assert draft.due_date == date(2020, 2, 29)
    response = client.post(url, {**data, "due_date": "2026-12-31"})
    assert "changed" in str(response.context["form"].non_field_errors())
    assert response.context["form"]["due_date"].value() == "2026-12-31"
    assert client.post(url, {**edit_post(draft), "due_date": ""}).status_code == 302
    draft.refresh_from_db()
    assert draft.due_date is None and draft.revision == 3
    detail = client.get(reverse("commercial:invoice-detail", args=[draft.pk]))
    assert b"Not recorded" in detail.content and b"Due date" in detail.content
    assert events(draft).filter(action="invoice_draft.saved").count() == 2


@pytest.fixture
def dated_drafts(lab, received, price_list):
    """Enough distinct order snapshots to prove sorting precedes pagination."""
    source = create(lab, received, price_list)
    rows = [source]
    for i in range(1, 28):
        order = Order.objects.create(
            project=lab["a"]["project"], coc_reference=f"SYN-DUE-{i}", created_by=lab["pm"]
        )
        values = {
            field.name: getattr(source, field.name)
            for field in InvoiceDraft._meta.fields
            if field.name not in ("id", "number", "order", "due_date")
        }
        rows.append(
            InvoiceDraft.objects.create(
                **values,
                number=f"ID-SYN-DUE-{i:02}",
                order=order,
                due_date=date(2026, 11, i),
            )
        )
    return rows


def listed(client, **params):
    return client.get(reverse("commercial:invoice-list"), params)


def test_due_filters_are_inclusive_independent_and_exclude_blank(client, lab, dated_drafts):
    client.force_login(lab["pm"])
    for params, days in [
        ({"due_from": "2026-11-25"}, [25, 26, 27]),
        ({"due_through": "2026-11-03"}, [1, 2, 3]),
        ({"due_from": "2026-11-03", "due_through": "2026-11-03"}, [3]),
        ({"due_from": "2026-11-03", "due_through": "2026-11-05"}, [3, 4, 5]),
    ]:
        response = listed(client, **params, sort="due_date")
        assert [draft.due_date.day for draft in response.context["page_obj"]] == days
        assert b"Due from" in response.content and b"Due through" in response.content
    response = listed(client, due_from="2026-11-27", status="cancelled")
    assert response.context["page_obj"].paginator.count == 0


@pytest.mark.parametrize(
    "params,error",
    [
        ({"due_from": "2026-02-30"}, "due_from"),
        ({"due_through": "invalid"}, "due_through"),
        ({"due_from": "2026-11-03", "due_through": "2026-11-01"}, "due_through"),
    ],
)
def test_invalid_ranges_show_errors_and_no_rows(client, lab, dated_drafts, params, error):
    client.force_login(lab["pm"])
    response = listed(client, **params)
    assert response.status_code == 200
    assert response.context["filter_form"].errors[error]
    assert response.context["page_obj"].paginator.count == 0
    assert response.context["filter_form"][error].value() == params[error]


def test_due_sort_null_order_and_pagination_use_whole_queryset(client, lab, dated_drafts):
    client.force_login(lab["pm"])
    page = listed(client, sort="due_date").context["page_obj"]
    assert page.paginator.count == 28
    assert [draft.due_date.day for draft in page] == list(range(1, 26))
    page = listed(client, sort="due_date", page=2).context["page_obj"]
    assert [draft.due_date for draft in page] == [date(2026, 11, 26), date(2026, 11, 27), None]
    page = listed(client, sort="-due_date").context["page_obj"]
    assert page[0].due_date is None
    assert [draft.due_date.day for draft in list(page)[1:]] == list(range(27, 3, -1))
