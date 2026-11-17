"""Explicit manual tax assessments retain exact amounts, origin and revision/audit gates."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from commercial import invoices
from commercial.models import InvoiceDraft
from core.models import AuditEvent

from .test_invoice_manual import PROVENANCE, add, add_post
from .test_invoices import accepted_adjusted_quote as accepted_adjusted_quote
from .test_invoices import create, edit_post, from_quote, save
from .test_invoices import received as received

pytestmark = pytest.mark.django_db


def tax(lab, draft, **overrides):
    return add(
        lab,
        draft,
        **{
            "kind": "tax",
            "description": "Synthetic assessment from supplied worksheet SYN-TAX-1",
            "quantity": 1,
            "unit_price": Decimal("1.23"),
            **overrides,
        },
    )


def tax_post(draft, **overrides):
    return add_post(
        draft,
        **{
            "kind": "tax",
            "description": "Synthetic assessment from supplied worksheet SYN-TAX-1",
            "quantity": "1",
            "unit_price": "1.23",
            **overrides,
        },
    )


@pytest.mark.parametrize("source,role", [("list", "pm"), ("quote", "admin")])
def test_tax_totals_are_exact_and_other_charge_changes_never_recalculate_assessments(
    lab, received, price_list, accepted_adjusted_quote, source, role
):
    draft = (
        create(lab, received, price_list)
        if source == "list"
        else from_quote(lab, received, accepted_adjusted_quote)
    )
    before = invoices._state(draft)
    provenance = list(draft.lines.values(*PROVENANCE))
    original_total = draft.total
    draft = tax(lab, draft, actor=lab[role])
    assessment = draft.lines.last()
    draft = tax(
        lab,
        draft,
        unit_price=Decimal("0.00"),
        description="Synthetic explicit zero assessment from SYN-TAX-2",
    )
    draft = add(lab, draft)  # 3 x 0.10 fee
    draft = add(lab, draft, kind="adjustment", quantity=2, unit_price=Decimal("-0.05"))
    assert draft.total == original_total + Decimal("1.43")
    rows = list(draft.lines.values("id", "description", "quantity", "unit_price"))
    rows[0]["quantity"] = 2
    original_amount = draft.lines.first().unit_price
    draft = save(lab, draft, lines=rows)
    assert draft.total == original_total + original_amount + Decimal("1.43")
    assert list(draft.lines.values(*PROVENANCE))[: len(provenance)] == provenance
    assert list(draft.lines.filter(kind="tax").values_list("amount", flat=True)) == [
        Decimal("1.23"),
        Decimal("0.00"),
    ]
    for line in draft.lines.filter(kind="tax"):
        assert line.quantity == 1 and line.amount == line.unit_price
        assert all(getattr(line, name) in (None, "") for name in PROVENANCE[3:])
    event = AuditEvent.objects.filter(action="invoice_draft.line_added").first()
    assert event.actor == lab[role] and event.before == before
    assert event.after["lines"][-1]["kind"] == "tax"
    assert event.after["lines"][-1]["source_quote_line"] is None
    assert event.after["quote_snapshot"] == before["quote_snapshot"]
    assert event.after["price_list"] == before["price_list"]
    assert assessment.pk == draft.lines.get(kind="tax", unit_price=Decimal("1.23")).pk


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 2},
        {"quantity": True},
        {"unit_price": Decimal("-0.01")},
        {"unit_price": Decimal("0.001")},
        {"unit_price": Decimal("NaN")},
        {"unit_price": Decimal("Infinity")},
        {"unit_price": 0.1},
        {"unit_price": invoices.MAX_PRICE + Decimal("0.01")},
        {"description": "  "},
        {"reason": "  "},
    ],
)
def test_invalid_tax_assessment_is_atomic(lab, received, price_list, changes):
    draft = create(lab, received, price_list)
    before = invoices._state(draft)
    with pytest.raises(ValidationError):
        tax(lab, draft, **changes)
    draft.refresh_from_db()
    assert invoices._state(draft) == before
    assert not AuditEvent.objects.filter(action="invoice_draft.line_added").exists()


@pytest.mark.parametrize("operation", ["add", "edit", "cancel"])
def test_tax_changes_and_audit_failure_roll_back_together(lab, received, price_list, operation):
    draft = tax(lab, create(lab, received, price_list))
    before = invoices._state(draft)
    with (
        patch("commercial.invoices.record_event", side_effect=RuntimeError("audit unavailable")),
        pytest.raises(RuntimeError),
    ):
        if operation == "add":
            tax(lab, draft)
        elif operation == "edit":
            rows = list(draft.lines.values("id", "description", "quantity", "unit_price"))
            rows[-1]["unit_price"] = Decimal("0.00")
            save(lab, draft, lines=rows)
        else:
            invoices.cancel_invoice_draft(
                actor=lab["pm"],
                draft_id=draft.pk,
                expected_revision=draft.revision,
                reason="Synthetic cancellation",
            )
    draft.refresh_from_db()
    assert invoices._state(draft) == before


@pytest.mark.parametrize("role", ["analyst", "custodian", "a", "b"])
def test_tax_service_and_routes_refuse_unqualified_roles(client, lab, received, price_list, role):
    draft = create(lab, received, price_list)
    actor = lab[role]["user"] if role in ("a", "b") else lab[role]
    with pytest.raises(PermissionDenied):
        tax(lab, draft, actor=actor)
    client.force_login(actor)
    url = reverse("commercial:invoice-add-line", args=[draft.pk])
    assert client.get(url).status_code == client.post(url, tax_post(draft)).status_code == 403
    assert not draft.lines.filter(kind="tax").exists()


@pytest.mark.parametrize("field,value", [("quantity", "2"), ("unit_price", "-0.01")])
def test_tax_add_and_edit_show_inline_errors_and_preserve_input(
    client, lab, received, price_list, field, value
):
    draft = create(lab, received, price_list)
    client.force_login(lab["pm"])
    response = client.post(
        reverse("commercial:invoice-add-line", args=[draft.pk]),
        tax_post(draft, **{field: value}),
    )
    assert response.status_code == 200
    form = response.context["form"]
    assert form.errors[field] and form[field].value() == value
    assert form["description"].value() == tax_post(draft)["description"]
    assert f'id="id_{field}_error"' in response.content.decode()
    draft = tax(lab, draft)
    data = edit_post(draft)
    data[f"lines-2-{field}"] = value
    response = client.post(reverse("commercial:invoice-edit", args=[draft.pk]), data)
    assert response.status_code == 200
    form = response.context["formset"][2]
    assert form.errors[field] and form[field].value() == value
    draft.refresh_from_db()
    assert draft.lines.get(kind="tax").amount == Decimal("1.23")


@pytest.mark.parametrize("field,value", [("quantity", 2), ("unit_price", Decimal("-0.01"))])
def test_tax_edit_service_rechecks_assessment_rules(lab, received, price_list, field, value):
    draft = tax(lab, create(lab, received, price_list))
    before = invoices._state(draft)
    rows = list(draft.lines.values("id", "description", "quantity", "unit_price"))
    rows[-1][field] = value
    with pytest.raises(ValidationError):
        save(lab, draft, lines=rows)
    draft.refresh_from_db()
    assert invoices._state(draft) == before


def test_tax_origin_and_kind_cannot_be_injected_or_changed_and_zero_retains_line(
    client, lab, received, price_list
):
    draft = create(lab, received, price_list)
    client.force_login(lab["admin"])
    response = client.post(
        reverse("commercial:invoice-add-line", args=[draft.pk]),
        tax_post(
            draft,
            source_quote_line_id=1,
            sample_id=received.pk,
            definition_id=lab["tests"]["lead"].pk,
            source_requested_test_id=1,
        ),
    )
    assert response.status_code == 302
    draft.refresh_from_db()
    provenance = list(draft.lines.values(*PROVENANCE))
    assert all(
        value in (None, "") for name, value in provenance[-1].items() if name in PROVENANCE[3:]
    )
    rows = list(draft.lines.values("id", "description", "quantity", "unit_price"))
    rows[-1].update(
        kind="fee", source_quote_line_id=1, sample_id=received.pk, unit_price=Decimal("0.00")
    )
    draft = save(lab, draft, lines=rows)
    assert list(draft.lines.values(*PROVENANCE)) == provenance
    assert draft.lines.get(kind="tax").amount == Decimal("0.00")
    with pytest.raises(ValidationError, match="cannot be removed"):
        save(lab, draft, lines=rows[:-1])
    for route in ("commercial:invoice-detail", "commercial:invoice-edit"):
        html = client.get(reverse(route, args=[draft.pk])).content.decode()
        assert "Review tax separately" in html and "never recalculated" in html
        assert "Manually added" in html


def test_tax_reuses_stale_and_cancelled_draft_gates(lab, received, price_list):
    original = create(lab, received, price_list)
    draft = tax(lab, original)
    with pytest.raises(ValidationError, match="changed"):
        tax(lab, original)
    with pytest.raises(ValidationError, match="changed"):
        save(lab, original)
    with pytest.raises(ValidationError, match="changed"):
        invoices.cancel_invoice_draft(
            actor=lab["pm"],
            draft_id=draft.pk,
            expected_revision=1,
            reason="Stale cancel",
        )
    before = list(draft.lines.values())
    invoices.cancel_invoice_draft(
        actor=lab["pm"],
        draft_id=draft.pk,
        expected_revision=draft.revision,
        reason="Synthetic retained cancellation",
    )
    draft.refresh_from_db()
    assert list(draft.lines.values()) == before
    with pytest.raises(ValidationError, match="cancelled"):
        tax(lab, draft)
    with pytest.raises(ValidationError, match="cancelled"):
        save(lab, draft)
    assert (
        AuditEvent.objects.get(action="invoice_draft.cancelled").after["lines"][-1]["kind"] == "tax"
    )


def test_tax_is_never_accepted_as_a_quote_source(lab, received, accepted_adjusted_quote):
    version = accepted_adjusted_quote
    rows = list(version.lines.all())
    source = next(row for row in rows if row.kind == "fee")
    source.kind, source.quantity = "tax", 1
    source.amount = source.unit_price
    with (
        patch.object(type(version.lines), "select_for_update", return_value=rows),
        pytest.raises(ValidationError, match="invalid source line"),
    ):
        from_quote(lab, received, version)
    assert not InvoiceDraft.objects.exists()
