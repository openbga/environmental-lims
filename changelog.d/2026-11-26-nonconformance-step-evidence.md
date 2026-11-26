---
date: 2026-11-26
type: changed
impact: medium
title: Trace attachments from each nonconformance step after a restore
area: Backup and restore
---
Each new audit entry for opening a nonconformance, changing its status or reopening it identifies the timeline step and any file attached to it. After restoring a backup, deployers can use that entry to identify a missing or replaced attachment.

## Technical notes

Run python manage.py verify_stored_files on the restored database. For opening and status entries recorded before this change, compare against the file inventory saved with the backup to detect a missing attachment.
