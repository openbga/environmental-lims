---
date: 2026-11-25
type: changed
impact: medium
title: Check restored training attachments against their original records
area: Backup and restore
---
Deployers can detect a missing training attachment even when the restored database has also lost the training record. The restore check compares each attached file with the event that recorded it, including corrections.

## Technical notes

Run python manage.py verify_stored_files on the restored database and look under Training record evidence. Use the reported training record or audit event ID to investigate.
