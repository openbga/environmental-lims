---
date: 2026-11-26
type: fixed
impact: medium
title: Check invoice void history after a restore
area: Backup and restore
---
Deployers can find a restored invoice whose status disagrees with its void history, including one still marked issued after a recorded void. The check also verifies who voided the invoice and the reason they gave.

## Technical notes

Run python manage.py verify_stored_files against the restored database. The Issued invoice PDFs category identifies the invoice, draft or audit event to investigate. Valid inventories keep the same version 1 digest.
