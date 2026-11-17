"""Tax assessments cannot acquire fabricated source identities or lose their history."""

import importlib
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction

from commercial import invoices
from commercial.models import InvoiceDraftLine
from commercial.tests import test_invoices as source_fixtures

create = source_fixtures.create
received = source_fixtures.received
accepted_invoice_quote = source_fixtures.accepted_invoice_quote

pytestmark = pytest.mark.django_db


def test_tax_migration_guard_retains_zero_and_cancelled_assessments(lab, received, price_list):
    migration = importlib.import_module("commercial.migrations.0010_invoice_tax_lines")
    editor = SimpleNamespace(connection=connection)
    draft = create(lab, received, price_list)
    migration.refuse_reverse_with_tax_lines(apps, editor)
    line = InvoiceDraftLine.objects.create(
        draft=draft,
        position=3,
        kind="tax",
        description="Synthetic assessment reference TAX-0",
        quantity=1,
        unit_price=Decimal("0.00"),
        amount=Decimal("0.00"),
    )
    with pytest.raises(RuntimeError, match="billing history"):
        migration.refuse_reverse_with_tax_lines(apps, editor)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=draft.revision, reason="Synthetic"
    )
    with pytest.raises(RuntimeError, match="billing history"):
        migration.refuse_reverse_with_tax_lines(apps, editor)
    line.refresh_from_db()
    assert line.kind == "tax" and line.amount == Decimal("0.00")


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 2, "amount": Decimal("0.20")},
        {"quantity": 0, "amount": Decimal("0.00")},
        {"unit_price": Decimal("-0.10"), "amount": Decimal("-0.10")},
        {"amount": Decimal("0.20")},
        {"source_requested_test_id": 12345},
        {"lab_id": "SYN-FABRICATED"},
        {"client_sample_id": "SYN-FABRICATED"},
        {"test_description": "Synthetic fabricated test"},
        {"matrix": "water"},
        {"method_code": "SYN-METHOD"},
        {"method_version": "1"},
        {"sample_disposition": "accepted"},
        {"unit": "mg/L"},
    ],
)
def test_database_rejects_invalid_tax_shape(lab, received, price_list, changes):
    draft = create(lab, received, price_list)
    values = dict(
        draft=draft,
        position=3,
        kind="tax",
        description="Synthetic assessment reference TAX-1",
        quantity=1,
        unit_price=Decimal("0.10"),
        amount=Decimal("0.10"),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceDraftLine.objects.create(**(values | changes))


def test_tax_cannot_claim_sample_test_or_quote_origin(
    lab, received, price_list, accepted_invoice_quote
):
    draft = create(lab, received, price_list)
    values = dict(
        draft=draft,
        position=3,
        kind="tax",
        description="Synthetic assessment reference TAX-2",
        quantity=1,
        unit_price=Decimal("0.10"),
        amount=Decimal("0.10"),
    )
    for reference in (
        {"sample": received},
        {"definition": lab["tests"]["lead"]},
        {"source_quote_line": accepted_invoice_quote.lines.first()},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            InvoiceDraftLine.objects.create(**(values | reference))
