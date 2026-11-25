---
date: 2026-11-25
type: changed
impact: medium
title: Verify calibration and maintenance attachments after a restore
area: Backup and restore
---
Deployers can trace a missing or changed equipment attachment to the audit entry for the original work or correction. The check can identify a missing calibration certificate or maintenance file even when the equipment event was also lost.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Under Equipment event evidence, use the reported equipment event or audit event ID to investigate a discrepancy.
