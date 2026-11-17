"""Manual fees/discounts retain source snapshots, exact totals and atomic history."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections
from django.urls import reverse

from commercial import invoices
from core.models import AuditEvent

from .test_invoices import accepted_adjusted_quote as accepted_adjusted_quote
from .test_invoices import create, edit_post, from_quote, save
from .test_invoices import received as received

pytestmark = pytest.mark.django_db

PROVENANCE = (
    "id",
    "position",
    "kind",
    "source_quote_line_id",
    "source_requested_test_id",
    "sample_id",
    "definition_id",
    "lab_id",
    "client_sample_id",
    "sample_disposition",
    "test_description",
    "method_code",
    "method_version",
    "matrix",
    "unit",
)


def add(lab, draft, **overrides):
    args = dict(
        actor=lab["pm"],
        draft_id=draft.pk,
        expected_revision=draft.revision,
        kind="fee",
        description="Synthetic manual handling",
        quantity=3,
        unit_price=Decimal("0.10"),
        reason="Synthetic agreed charge",
    )
    args.update(overrides)
    return invoices.add_invoice_draft_line(**args)


def add_post(draft, **overrides):
    data = dict(
        expected_revision=draft.revision,
        kind="fee",
        description="Synthetic manual handling",
        quantity="3",
        unit_price="0.10",
        reason="Synthetic agreed charge",
    )
    data.update(overrides)
    return data


@pytest.mark.parametrize("source", ["list", "quote"])
@pytest.mark.parametrize("role", ["pm", "admin"])
def test_manual_lines_exact_totals_audit_and_original_sources(
    lab, received, price_list, accepted_adjusted_quote, source, role
):
    draft = (
        create(lab, received, price_list)
        if source == "list"
        else from_quote(lab, received, accepted_adjusted_quote)
    )
    initial_state = invoices._state(draft)
    initial_total = draft.total
    original = list(draft.lines.values(*PROVENANCE))
    draft = add(lab, draft, actor=lab[role])
    draft = add(
        lab,
        draft,
        actor=lab[role],
        kind="adjustment",
        description="Synthetic manual discount",
        quantity=2,
        unit_price=Decimal("-0.05"),
        reason="Synthetic agreed discount",
    )
    assert draft.total == initial_total + Decimal("0.20") and draft.revision == 3
    assert list(draft.lines.values(*PROVENANCE))[: len(original)] == original
    manual = list(draft.lines.all())[len(original) :]
    assert [row.position for row in manual] == [len(original) + 1, len(original) + 2]
    assert [row.amount for row in manual] == [Decimal("0.30"), Decimal("-0.10")]
    for row in manual:
        assert row.source_quote_line_id is None
        assert all(getattr(row, name) in (None, "") for name in PROVENANCE[4:])
    events = list(AuditEvent.objects.filter(action="invoice_draft.line_added"))
    assert [event.actor for event in events] == [lab[role], lab[role]]
    assert events[0].before == initial_state
    assert events[1].before == events[0].after
    assert events[1].after["total"] == str(draft.total)
    assert events[1].reason == "Synthetic agreed discount"
    assert events[1].after["lines"][-1]["source_quote_line"] is None
    assert events[1].after["lines"][-1]["kind"] == "adjustment"
    assert events[1].after["price_list"] == initial_state["price_list"]
    assert events[1].after["quote_snapshot"] == initial_state["quote_snapshot"]


@pytest.mark.parametrize("role", ["analyst", "custodian", "a", "b"])
def test_manual_line_service_and_get_post_refuse_unqualified_roles(
    client, lab, received, price_list, role
):
    draft = create(lab, received, price_list)
    actor = lab[role]["user"] if role in ("a", "b") else lab[role]
    with pytest.raises(PermissionDenied):
        add(lab, draft, actor=actor)
    client.force_login(actor)
    url = reverse("commercial:invoice-add-line", args=[draft.pk])
    assert client.get(url).status_code == client.post(url, add_post(draft)).status_code == 403
    assert draft.lines.count() == 2
    assert not AuditEvent.objects.filter(action="invoice_draft.line_added").exists()


@pytest.mark.parametrize("role", ["pm", "admin"])
def test_manual_line_route_and_detail_edit_provenance(client, lab, received, price_list, role):
    draft = create(lab, received, price_list)
    client.force_login(lab[role])
    url = reverse("commercial:invoice-add-line", args=[draft.pk])
    response = client.get(url)
    assert response.context["form"]["quantity"].value() == 1
    assert response.context["form"]["expected_revision"].value() == 1
    html = response.content.decode()
    assert 'id="id_unit_price_unit">USD</span>' in html
    assert 'type="text" name="unit_price"' in html
    assert "inputmode" not in response.context["form"].fields["unit_price"].widget.attrs
    response = client.post(
        url,
        add_post(
            draft,
            source_quote_line_id=123456,
            source_requested_test_id=123456,
            sample_id=received.pk,
            definition_id=lab["tests"]["lead"].pk,
            lab_id="Fabricated source",
        ),
    )
    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.total == Decimal("61.55")
    row = draft.lines.last()
    assert all(getattr(row, name) in (None, "") for name in PROVENANCE[3:])
    html = client.get(response.url).content.decode()
    assert "Manually added" in html and "Add fee or discount" in html
    assert "No requested test" in html
    html = client.get(reverse("commercial:invoice-edit", args=[draft.pk])).content.decode()
    assert "Manually added" in html and "zero price with a reason" in html


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "test"},
        {"kind": "other"},
        {"unit_price": Decimal("-0.01")},
        {"kind": "adjustment", "unit_price": Decimal("0.01")},
        {"unit_price": Decimal("0.001")},
        {"unit_price": Decimal("NaN")},
        {"unit_price": Decimal("Infinity")},
        {"unit_price": 0.10},
        {"unit_price": "0.10"},
        {"unit_price": invoices.MAX_PRICE + Decimal("0.01")},
        {"unit_price": invoices.MAX_PRICE, "quantity": 1000},
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": True},
        {"quantity": 1.5},
        {"quantity": invoices.MAX_QUANTITY + 1},
        {"reason": "  "},
        {"description": "  "},
        {"description": "x" * 401},
        {"kind": "adjustment", "unit_price": Decimal("-99.00")},
    ],
)
def test_manual_line_invalid_input_is_atomic(lab, received, price_list, changes):
    draft = create(lab, received, price_list)
    before = invoices._state(draft)
    with pytest.raises(ValidationError):
        add(lab, draft, **changes)
    draft.refresh_from_db()
    assert invoices._state(draft) == before
    assert not AuditEvent.objects.filter(action="invoice_draft.line_added").exists()


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"kind": "test"}, "kind"),
        ({"unit_price": "-0.01"}, "unit_price"),
        ({"kind": "adjustment", "unit_price": "0.01"}, "unit_price"),
        ({"unit_price": "0.001"}, "unit_price"),
        ({"quantity": "1.5"}, "quantity"),
        ({"quantity": "0"}, "quantity"),
        ({"reason": ""}, "reason"),
    ],
)
def test_invalid_add_route_keeps_input_and_inline_errors(
    client, lab, received, price_list, changes, field
):
    draft = create(lab, received, price_list)
    client.force_login(lab["pm"])
    data = add_post(draft, **changes)
    response = client.post(reverse("commercial:invoice-add-line", args=[draft.pk]), data)
    assert response.status_code == 200
    form = response.context["form"]
    assert form.errors[field]
    assert form["description"].value() == data["description"]
    assert form[field].value() == data[field]
    assert f'id="id_{field}_error"' in response.content.decode()
    assert draft.lines.count() == 2


def test_add_line_rolls_back_when_audit_fails(lab, received, price_list):
    draft = create(lab, received, price_list)
    before = invoices._state(draft)
    with (
        patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")),
        pytest.raises(RuntimeError),
    ):
        add(lab, draft)
    draft.refresh_from_db()
    assert invoices._state(draft) == before


def test_all_lines_share_bound_and_append_uses_largest_position(lab, received, price_list):
    draft = create(lab, received, price_list)
    original = draft.lines.last()
    original.position = 10
    original.save(update_fields=["position"])
    with patch("commercial.invoices.MAX_LINES", 3):
        draft = add(lab, draft)
        assert draft.lines.last().position == 11
        with pytest.raises(ValidationError, match="at most 3"):
            add(lab, draft, kind="adjustment", unit_price=Decimal("-0.01"))
    assert draft.lines.count() == 3 and draft.revision == 2


def test_total_overflow_refused_before_writing(lab, received, price_list):
    draft = create(lab, received, price_list)
    with (
        patch("commercial.invoices.MAX_AMOUNT", Decimal("61.40")),
        pytest.raises(ValidationError, match="draft total is too large"),
    ):
        add(lab, draft)
    draft.refresh_from_db()
    assert draft.lines.count() == 2 and draft.revision == 1


def test_edits_and_cancellation_keep_manual_and_quoted_provenance(
    lab, received, accepted_adjusted_quote
):
    draft = add(lab, from_quote(lab, received, accepted_adjusted_quote))
    provenance = list(draft.lines.values(*PROVENANCE))
    rows = [
        dict(
            id=line.pk,
            description=line.description,
            quantity=line.quantity,
            unit_price=Decimal("0.00") if line == draft.lines.last() else line.unit_price,
            kind="test",
            source_quote_line_id=accepted_adjusted_quote.lines.first().pk,
            sample_id=received.pk,
        )
        for line in draft.lines.all()
    ]
    draft = save(lab, draft, lines=rows)
    assert list(draft.lines.values(*PROVENANCE)) == provenance
    assert draft.lines.last().amount == Decimal("0.00")
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=3, reason="Retain synthetic record"
    )
    assert list(draft.lines.values(*PROVENANCE)) == provenance
    with pytest.raises(ValidationError, match="cancelled"):
        add(lab, draft, expected_revision=4)


def test_stale_add_edit_cancel_and_cancelled_route_preserve_input(
    client, lab, received, price_list
):
    draft = create(lab, received, price_list)
    old_edit = edit_post(draft)
    current = add(lab, draft)
    with pytest.raises(ValidationError, match="changed"):
        add(lab, draft)
    with pytest.raises(ValidationError, match="changed"):
        save(lab, draft)
    with pytest.raises(ValidationError, match="changed"):
        invoices.cancel_invoice_draft(
            actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Stale cancel"
        )
    client.force_login(lab["pm"])
    url = reverse("commercial:invoice-add-line", args=[draft.pk])
    response = client.post(url, add_post(draft))
    assert "draft changed" in str(response.context["form"].non_field_errors())
    assert response.context["form"]["description"].value() == "Synthetic manual handling"
    response = client.post(reverse("commercial:invoice-edit", args=[draft.pk]), old_edit)
    assert "draft changed" in str(response.context["form"].non_field_errors())
    current = save(lab, current)
    with pytest.raises(ValidationError, match="changed"):
        add(lab, draft, expected_revision=2)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=3, reason="Current cancel"
    )
    assert client.get(url).status_code == 302
    response = client.post(url, add_post(draft, expected_revision=4))
    assert "cancelled draft" in str(response.context["form"].non_field_errors())
    assert draft.lines.count() == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("other", ["add", "edit", "cancel"])
def test_concurrent_add_and_other_changes_have_one_winner(lab, received, price_list, other):
    draft = create(lab, received, price_list)
    barrier = Barrier(2)

    def attempt(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if action == "edit":
                save(lab, draft)
            elif action == "cancel":
                invoices.cancel_invoice_draft(
                    actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Cancel race"
                )
            else:
                add(lab, draft)
            return True
        except ValidationError as exc:
            assert "changed" in str(exc)
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, ["add", other])) == [False, True]
    draft.refresh_from_db()
    assert draft.revision == 2
    assert draft.lines.count() in (2, 3)
    assert (
        AuditEvent.objects.filter(
            action__in=[
                "invoice_draft.line_added",
                "invoice_draft.saved",
                "invoice_draft.cancelled",
            ]
        ).count()
        == 1
    )
