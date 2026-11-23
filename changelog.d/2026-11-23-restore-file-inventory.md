---
date: 2026-11-23
type: added
impact: medium
title: Compare stored files before and after a restore
---
Deployers can save a file inventory with a database backup and compare it with the restored copy. The comparison identifies the affected category when an entire file record is missing or a file has been replaced.

## Technical notes

Run `python manage.py verify_stored_files --format manifest` on the source and restored databases using the same application version. Pause writes while capturing the backup and its source inventory. Save each successful output separately and compare the two files.
