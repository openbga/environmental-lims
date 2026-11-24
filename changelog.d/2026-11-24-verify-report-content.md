---
date: 2026-11-24
type: changed
impact: medium
title: Verify issued report content after a restore
area: Backup and restore
---
Deployers can detect a change to the frozen report content used for client result downloads, even when the stored PDF is intact. The restore check identifies the affected original or amended report version.

## Technical notes

Run `python manage.py verify_stored_files` on the restored database. Each report version must pass both the PDF and content checks to pass under `Report PDFs`. A failure prevents the command from writing a file inventory.
