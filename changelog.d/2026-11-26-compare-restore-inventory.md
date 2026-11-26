---
date: 2026-11-26
type: added
impact: medium
title: Compare restored files with the backup inventory
area: Backup and restore
---
Deployers can check a restored database directly against the file inventory saved with its backup. The check names each category whose count changed or whose files differ at the same count.

## Technical notes

Run python manage.py verify_stored_files --compare-manifest source-files.json on the isolated restore before upgrading it. The command exits with an error if file verification fails, the inventory is unreadable or incompatible, or the inventories differ. Use a version 1 inventory with the same file categories as the restored application.
