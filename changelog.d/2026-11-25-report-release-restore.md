---
date: 2026-11-25
type: changed
impact: medium
title: Check restored reports against their release history
area: Backup and restore
---
After restoring a backup, deployers can check that each issued report PDF and its frozen results match the original release or amendment event. The check also identifies an event whose issued version is missing.

## Technical notes

Run python manage.py verify_stored_files on the restored database. A missing, duplicate or inconsistent event fails the check. The command reports the affected version or audit event ID under Report PDFs.
