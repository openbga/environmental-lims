---
date: 2026-11-25
type: fixed
impact: medium
title: Trace missing invoices and verify who issued them after a restore
area: Backup and restore
---
Deployers can find a missing invoice from its issue history even when the restored database has also lost the draft. For issued and voided invoices, the restore check now verifies who issued the original document.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Under Issued invoice PDFs, use the reported invoice, draft or audit event ID to investigate a discrepancy. Intact backups keep the same version 1 file inventory.
