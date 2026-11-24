---
date: 2026-11-24
type: added
impact: medium
title: Check stored files at the same snapshot as a backup
area: Backup and restore
---
Deployers can take a PostgreSQL backup and a matching inventory of stored files while staff continue working. After restoring the backup, they can compare inventories to detect missing or changed files.

## Technical notes

Pass the same exported PostgreSQL snapshot ID to pg_dump and to verify_stored_files with --snapshot. Keep the exporting transaction open until both commands finish. Generate the comparison inventory on the restored database with --format manifest.
