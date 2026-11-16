"""Invoice quote options must obey pricing roles and the source order's client/project."""

import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from commercial import invoices
from commercial.models import InvoiceDraft, Quote, QuoteVersion
from receipt.models import Order, Project

pytestmark = pytest.mark.django_db


def accepted(lab, *, client=None, project=None, status="accepted", decision=1, issued=True):
    quote = Quote.objects.create(
        number=f"SYN-{Quote.objects.count():03}",
        title="Live header",
        client=client or lab["a"]["client"],
        project=project,
        status=status,
        decision_version=decision,
        decided_at=timezone.now(),
        decided_by=lab["a"]["user"],
        created_by=lab["pm"],
    )
    return QuoteVersion.objects.create(
        quote=quote,
        number=1,
        title="Frozen synthetic scope",
        created_by=lab["pm"],
        issued_at=timezone.now() if issued else None,
        issued_by=lab["pm"] if issued else None,
    )


def test_quote_lookup_scopes_order_search_and_accepted_version(client, lab):
    order = Order.objects.create(
        created_by=lab["pm"], project=lab["a"]["project"], coc_reference="SYN-LOOKUP"
    )
    general = accepted(lab)
    specific = accepted(lab, project=order.project)
    accepted(lab, project=Project.objects.create(client=order.project.client, name="Other site"))
    accepted(lab, client=lab["b"]["client"])
    accepted(lab, status="issued")
    accepted(lab, decision=2)
    accepted(lab, issued=False)
    url = reverse("lookup", args=["invoice-quotes"])
    client.force_login(lab["pm"])
    rows = client.get(url, {"order": order.pk, "q": "frozen synthetic"}).json()["results"]
    assert [r["value"] for r in rows] == [str(specific.pk), str(general.pk)]
    assert all("Frozen synthetic scope" in r["text"] for r in rows)
    assert client.get(url, {"order": order.pk, "q": general.quote.number}).json()["results"][0][
        "value"
    ] == str(general.pk)
    for order_id in ["", "not-a-uuid", str(uuid4())]:
        assert client.get(url, {"order": order_id}).json() == {"results": [], "more": False}
    for user in [lab["analyst"], lab["custodian"], lab["a"]["user"], lab["b"]["user"]]:
        client.force_login(user)
        assert client.get(url, {"order": order.pk}).status_code == 403
    client.force_login(lab["admin"])
    assert client.get(url, {"order": order.pk}).status_code == 200


def test_quote_lookup_pages_without_losing_order_scope(client, lab):
    order = Order.objects.create(
        created_by=lab["pm"], project=lab["a"]["project"], coc_reference="SYN-PAGING"
    )
    versions = [accepted(lab) for _ in range(22)]
    accepted(lab, client=lab["b"]["client"])
    client.force_login(lab["pm"])
    url = reverse("lookup", args=["invoice-quotes"])
    first = client.get(url, {"order": order.pk}).json()
    second = client.get(url, {"order": order.pk, "page": 2}).json()
    assert first["more"] and len(first["results"]) == 20
    assert not second["more"] and len(second["results"]) == 2
    assert {r["value"] for r in first["results"] + second["results"]} == {
        str(v.pk) for v in versions
    }


def test_quote_provenance_blocks_migration_reversal_even_after_cancellation(lab):
    migration = importlib.import_module("commercial.migrations.0006_invoice_quote_pricing")
    editor = SimpleNamespace(connection=connection)
    migration.refuse_reverse_with_quote_drafts(apps, editor)
    version = accepted(lab)
    order = Order.objects.create(
        created_by=lab["pm"], project=lab["a"]["project"], coc_reference="SYN-REVERSE"
    )
    draft = InvoiceDraft.objects.create(
        number="ID-SYN-REVERSE",
        order=order,
        quote_version=version,
        quote_snapshot={"number": version.quote.number, "version": 1},
        total="0.00",
        created_by=lab["pm"],
    )
    with pytest.raises(RuntimeError, match="retain billing provenance"):
        migration.refuse_reverse_with_quote_drafts(apps, editor)
    invoices.cancel_invoice_draft(
        actor=lab["pm"], draft_id=draft.pk, expected_revision=1, reason="Retain source history"
    )
    with pytest.raises(RuntimeError, match="retain billing provenance"):
        migration.refuse_reverse_with_quote_drafts(apps, editor)
