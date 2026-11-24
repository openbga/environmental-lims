---
date: 2026-11-24
type: changed
impact: medium
title: Check restored deliverable ZIPs against their export history
area: Backup and restore
---
Deployers can check that a restored electronic data deliverable is the ZIP originally generated for an issued report version. The check also flags an export whose ZIP record is missing.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Each ZIP must match one export event by ZIP checksum, report number, version number and report content checksum. A discrepancy fails the EDD export ZIPs check and prevents the command from writing a file inventory.
