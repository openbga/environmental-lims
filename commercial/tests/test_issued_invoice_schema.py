"""An issued PDF survives write attempts through querysets and schema rollback."""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import DatabaseError, connection, transaction

from commercial import invoices
from commercial.models import IssuedInvoice
from commercial.tests import test_invoices as fixtures
from core.models import Lab

received = fixtures.received


@pytest.mark.django_db
def test_issued_document_is_append_only_in_postgres_and_protects_migration(
    lab, received, price_list
):
    Lab.objects.create(name="Synthetic Invoice Laboratory")
    draft = fixtures.create(lab, received, price_list)
    migration = importlib.import_module("commercial.migrations.0011_issued_invoices")
    schema = SimpleNamespace(connection=connection)
    migration.preserve_issued_invoices(apps, schema)

    draft = invoices.issue_invoice(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=draft.revision
    )
    document = draft.issued_invoice
    rows = IssuedInvoice.objects.filter(pk=document.pk)
    with pytest.raises(DatabaseError, match="Historical records"), transaction.atomic():
        rows.update(pdf=b"changed")
    with pytest.raises(DatabaseError, match="Historical records"), transaction.atomic():
        rows.delete()
    with pytest.raises(RuntimeError, match="billing history"):
        migration.preserve_issued_invoices(apps, schema)

    invoices.void_invoice(
        actor=lab["pm"],
        draft_id=draft.pk,
        expected_revision=draft.revision,
        reason="Synthetic correction",
    )
    with pytest.raises(RuntimeError, match="billing history"):
        migration.preserve_issued_invoices(apps, schema)
    assert rows.get().pdf == document.pdf
