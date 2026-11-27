---
date: 2026-11-27
type: added
impact: medium
title: Compare key record counts after restoring a backup
area: Backup and recovery
---
Paired backups now include counts for audit and custody events, result revisions, report versions and files, and issued invoices. After restoring the archive, deployers can see which groups differ from the source.

## Technical notes

backup_with_inventory writes source-records.json alongside the archive and file inventory. Run python manage.py verify_backup_records --compare /path/to/backup/source-records.json against the restored database before upgrading. The command compares counts and the greatest identifier in each group that records one.
