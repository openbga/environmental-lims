"""The schema permits honest manual origins without weakening line identity or history."""

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

pytestmark = pytest.mark.django_db


def test_manual_line_schema_and_guard_preserve_cancelled_history(lab, received, price_list):
    migration = importlib.import_module("commercial.migrations.0008_invoice_manual_adjustments")
    editor = SimpleNamespace(connection=connection)
    draft = create(lab, received, price_list)
    migration.refuse_reverse_with_manual_lines(apps, editor)
    line = InvoiceDraftLine.objects.create(
        draft=draft,
        position=3,
        kind="fee",
        description="Synthetic manual handling fee",
        quantity=3,
        unit_price=Decimal("0.10"),
        amount=Decimal("0.30"),
    )
    assert line.source_quote_line_id is None and line.sample_id is None
    with pytest.raises(RuntimeError, match="billing history"):
        migration.refuse_reverse_with_manual_lines(apps, editor)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=draft.revision, reason="Synthetic"
    )
    with pytest.raises(RuntimeError, match="billing history"):
        migration.refuse_reverse_with_manual_lines(apps, editor)
    assert InvoiceDraftLine.objects.filter(pk=line.pk).exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "test"},
        {"kind": "unknown"},
        {"source_requested_test_id": 999999},
        {"lab_id": "FABRICATED-SAMPLE"},
        {"test_description": "Fabricated test"},
        {"unit_price": Decimal("-0.10"), "amount": Decimal("-0.10")},
        {"kind": "adjustment"},
        {"quantity": 0, "amount": Decimal("0.00")},
        {"amount": Decimal("0.20")},
    ],
)
def test_manual_line_constraint_still_rejects_bad_identity_sign_or_amount(
    lab, received, price_list, changes
):
    draft = create(lab, received, price_list)
    values = dict(
        draft=draft,
        position=3,
        kind="fee",
        description="Synthetic manual fee",
        quantity=1,
        unit_price=Decimal("0.10"),
        amount=Decimal("0.10"),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceDraftLine.objects.create(**(values | changes))
