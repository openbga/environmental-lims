---
date: 2026-11-26
type: changed
impact: medium
title: Check historical report logos after a restore
area: Backup and restore
---
Deployers can identify an issued report whose original laboratory logo is missing or has different image details after a restore. The check also covers superseded report versions.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Under Report PDFs, the command identifies the version whose saved logo checksum, image type or dimensions do not match a stored logo. The Laboratory logos count still counts each stored image once.
