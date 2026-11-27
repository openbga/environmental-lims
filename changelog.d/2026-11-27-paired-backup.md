---
date: 2026-11-27
type: added
impact: medium
title: Back up the database and its file inventory together
area: Backup and recovery
---
Deployers can create a database backup and a matching inventory of verified files with one command while staff continue working. After restoring the archive, compare the files with the saved inventory.

## Technical notes

On a backup host with PostgreSQL 17 client tools, run `python manage.py backup_with_inventory --output /path/to/new-directory` in the source application environment. Keep `database.dump` and `source-files.json` together. On the restored database, run `python manage.py verify_stored_files --compare-manifest /path/to/new-directory/source-files.json`.
