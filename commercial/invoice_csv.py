"""Accounting CSV for the charges in a single issued invoice snapshot."""

import csv
import io
from decimal import Decimal

from core.csv_cells import spreadsheet_text

COLUMNS = (
    "invoice_number",
    "line_position",
    "issue_date",
    "due_date",
    "client_name",
    "client_account_code",
    "order_reference",
    "purchase_order",
    "currency",
    "line_type",
    "description",
    "quantity",
    "unit_price",
    "line_amount",
    "synthetic_data",
)


def export_invoice_csv(snapshot):
    """Write the selected billing fields and signed amounts in invoice line order."""
    billing = snapshot["billing"]
    common = {
        "invoice_number": spreadsheet_text(snapshot["number"]),
        "issue_date": snapshot["issued_date"],
        "due_date": snapshot["due_date"],
        "client_name": spreadsheet_text(billing["client_name"]),
        "client_account_code": spreadsheet_text(billing["account_code"]),
        "order_reference": spreadsheet_text(snapshot["order_reference"]),
        "purchase_order": spreadsheet_text(billing["po_number"]),
        "currency": snapshot["currency"],
        "synthetic_data": str(snapshot["synthetic_notice"]).lower(),
    }
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for line in snapshot["lines"]:
        writer.writerow(
            {
                **common,
                "line_position": line["position"],
                "line_type": line["kind_label"],
                "description": spreadsheet_text(line["display_description"]),
                "quantity": line["quantity"],
                "unit_price": Decimal(line["unit_price"]),
                "line_amount": Decimal(line["amount"]),
            }
        )
    return output.getvalue()
