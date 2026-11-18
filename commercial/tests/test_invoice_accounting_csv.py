"""A staff accounting download exposes only the frozen charges of a currently issued invoice."""

import csv
from codecs import BOM_UTF8
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from commercial import invoices
from commercial.models import InvoiceDraft, InvoiceDraftLine
from core.models import AuditEvent
from receipt.models import Order

from .test_invoice_issuance import issue, void
from .test_invoice_issuance import issuer_lab as issuer_lab
from .test_invoice_issuance import stub_pdf as stub_pdf
from .test_invoice_manual import add
from .test_invoice_tax import tax
from .test_invoices import accepted_adjusted_quote as accepted_adjusted_quote
from .test_invoices import create, from_quote, save
from .test_invoices import received as received

pytestmark = pytest.mark.django_db


def accounting_url(draft):
    return reverse("commercial:invoice-accounting-csv", args=[draft.pk])


def read_rows(response):
    assert response.status_code == 200
    assert response.content.startswith(BOM_UTF8)
    return list(csv.DictReader(StringIO(response.content.decode("utf-8-sig"), newline="")))


@pytest.fixture
def issued_invoice(lab, received, price_list, issuer_lab, stub_pdf):
    return issue(lab, create(lab, received, price_list))


def test_csv_downloads_only_exact_frozen_charges(
    client, lab, received, accepted_adjusted_quote, issuer_lab, stub_pdf, settings
):
    settings.REPORT_SYNTHETIC_DATA_NOTICE = True
    settings.TIME_ZONE = "America/New_York"
    Order.objects.filter(pk=received.order_id).update(coc_reference="=Synthetic COC")
    draft = from_quote(lab, received, accepted_adjusted_quote)
    draft = save(
        lab,
        draft,
        due_date=date(2026, 12, 10),
        billing={
            **{field: getattr(draft, field) for field in invoices.BILLING_FIELDS},
            "client_name": "＝Synthetic Utilities",
            "account_code": "-00012",
            "po_number": "\u200b@Synthetic PO",
            "billing_email": "internal-billing@example.invalid",
        },
    )
    description = 'Priority "A", first line\nÉchantillon'
    draft = add(lab, draft, description=description, unit_price=Decimal("10.01"), quantity=3)
    draft = tax(lab, draft)
    with patch(
        "commercial.invoices.timezone.now", return_value=datetime(2026, 11, 18, 3, 30, tzinfo=UTC)
    ):
        issued = issue(lab, draft)
    number = issued.issued_invoice.number
    total = draft.total
    url = accounting_url(issued)
    client.force_login(lab["pm"])

    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)

    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == f'attachment; filename="{number}-accounting.csv"'
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    (invoice_query,) = [q["sql"] for q in queries if '"commercial_issuedinvoice"' in q["sql"]]
    assert '"commercial_issuedinvoice"."snapshot"' in invoice_query
    assert '"commercial_invoicedraft"."status"' in invoice_query
    assert '"commercial_issuedinvoice"."pdf"' not in invoice_query

    rows = read_rows(response)
    common = {
        "invoice_number": number,
        "issue_date": "2026-11-17",
        "due_date": "2026-12-10",
        "client_name": "'＝Synthetic Utilities",
        "client_account_code": "'-00012",
        "order_reference": "'=Synthetic COC",
        "purchase_order": "'\u200b@Synthetic PO",
        "currency": "USD",
        "synthetic_data": "true",
    }
    assert rows == [
        {
            **common,
            "line_position": str(position),
            "line_type": kind,
            "description": label,
            "quantity": quantity,
            "unit_price": price,
            "line_amount": amount,
        }
        for position, (kind, label, quantity, price, amount) in enumerate(
            [
                ("Test", "Agreed lead analysis", "1", "0.10", "0.10"),
                ("Test", "Agreed copper analysis", "1", "1.25", "1.25"),
                ("Fee", "Synthetic handling", "3", "0.10", "0.30"),
                ("Discount", "Synthetic agreed discount", "2", "-0.05", "-0.10"),
                ("Fee", "Synthetic waived fee", "1", "0.00", "0.00"),
                ("Discount", "Synthetic zero discount", "1", "0.00", "0.00"),
                ("Fee", description, "3", "10.01", "30.03"),
                ("Tax", "Tax", "1", "1.23", "1.23"),
            ],
            1,
        )
    ]
    assert sum(Decimal(row["line_amount"]) for row in rows) == total == Decimal("32.81")
    assert b"SYN-TAX-1" not in response.content
    assert b"internal-billing@example.invalid" not in response.content
    assert b"SYN-FIELD-1" not in response.content

    # Out-of-band writes verify the read is isolated even from changes to the draft worksheet.
    Order.objects.filter(pk=draft.order_id).update(coc_reference="Changed order")
    InvoiceDraft.objects.filter(pk=draft.pk).update(client_name="Changed client", due_date=None)
    InvoiceDraftLine.objects.filter(draft=draft).update(description="Changed live lines")
    settings.REPORT_SYNTHETIC_DATA_NOTICE = False
    settings.TIME_ZONE = "Asia/Tokyo"
    audit_count = AuditEvent.objects.count()
    assert client.get(url).content == response.content
    head = client.head(url)
    assert head.status_code == 200 and head.content == b""
    assert head["Content-Disposition"] == response["Content-Disposition"]
    assert head["Cache-Control"] == response["Cache-Control"]
    assert AuditEvent.objects.count() == audit_count


def test_only_staff_with_pricing_roles_can_download_csv(client, lab, issued_invoice):
    url = accounting_url(issued_invoice)
    assert client.get(url).status_code == 302
    for user in (lab["analyst"], lab["custodian"], lab["a"]["user"], lab["b"]["user"]):
        client.force_login(user)
        assert client.get(url).status_code == 403
        assert client.head(url).status_code == 403

    for user in (lab["admin"], lab["pm"]):
        client.force_login(user)
        assert client.get(url).status_code == 200
        assert client.post(url).status_code == 405


def test_export_only_currently_issued_invoice(
    client, lab, received, price_list, issuer_lab, stub_pdf, settings
):
    settings.REPORT_SYNTHETIC_DATA_NOTICE = False
    draft = create(lab, received, price_list)
    url = accounting_url(draft)
    client.force_login(lab["pm"])
    assert client.get(url).status_code == 404
    issued = issue(lab, draft)
    for row in read_rows(client.get(url)):
        assert row["due_date"] == row["client_account_code"] == row["purchase_order"] == ""
        assert row["synthetic_data"] == "false"

    void(lab, issued)
    assert client.get(url).status_code == 404
    assert client.head(url).status_code == 404
    assert client.get(reverse("commercial:invoice-pdf", args=[draft.pk])).status_code == 200

    replacement = create(lab, received, price_list)
    invoices.cancel_invoice_draft(
        actor=lab["pm"],
        draft_id=replacement.pk,
        expected_revision=replacement.revision,
        reason="Synthetic cancellation",
    )
    assert client.get(accounting_url(replacement)).status_code == 404
    assert client.get(url).status_code == 404
