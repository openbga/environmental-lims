---
date: 2026-11-25
type: changed
impact: medium
title: Verify nonconformance note attachments after a restore
area: Backup and restore
---
Deployers can trace a changed or missing file on a nonconformance note to the staff member and audit entry that recorded it. A surviving audit entry can also reveal that the entire note is missing.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Under Nonconformance event evidence, use the reported timeline or audit event ID to investigate a discrepancy.
