"""A rollback must not discard recorded billing dates, including cancelled drafts."""

import importlib
from datetime import date
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection

from commercial import invoices
from commercial.tests import test_invoices as source_fixtures

create = source_fixtures.create
received = source_fixtures.received

pytestmark = pytest.mark.django_db


def test_due_date_reverse_guard_preserves_active_and_cancelled_dates(lab, received, price_list):
    migration = importlib.import_module("commercial.migrations.0009_invoice_due_date")
    editor = SimpleNamespace(connection=connection)
    draft = create(lab, received, price_list)
    assert draft.due_date is None
    migration.refuse_reverse_with_due_dates(apps, editor)
    draft.due_date = date(2026, 12, 15)
    draft.save(update_fields=["due_date"])
    with pytest.raises(RuntimeError, match="billing history"):
        migration.refuse_reverse_with_due_dates(apps, editor)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=draft.revision, reason="Synthetic"
    )
    with pytest.raises(RuntimeError, match="billing history"):
        migration.refuse_reverse_with_due_dates(apps, editor)
    draft.refresh_from_db()
    assert draft.due_date == date(2026, 12, 15)
