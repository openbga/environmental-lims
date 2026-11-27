---
date: 2026-11-26
type: fixed
impact: medium
title: Verify the original issue time of restored invoices
area: Draft invoices
---
The restore check now flags an invoice whose issue time differs from its audit record. For a voided invoice, it also checks that the void history still identifies the original issuer and issue time.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Under Issued invoice PDFs, an invoice fails verification if its recorded issue time differs from the original issue event, or if either side of the void event gives a different issue time or issuer.
