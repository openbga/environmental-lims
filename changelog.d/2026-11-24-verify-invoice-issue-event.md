---
date: 2026-11-24
type: changed
impact: medium
title: Check restored invoice PDFs against their issue history
area: Backup and restore
---
Deployers can check whether an invoice PDF in a restored backup matches the one originally issued, even after a void. The check also identifies invoices with missing or duplicate issue events.

## Technical notes

Run `python manage.py verify_stored_files` on the restored database. Each invoice number and PDF checksum must match the original issue event for its draft. A failed check reports the issued invoice record ID and prevents the command from writing a file inventory.
