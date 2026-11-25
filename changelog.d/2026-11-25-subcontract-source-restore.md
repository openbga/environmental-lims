---
date: 2026-11-25
type: changed
impact: medium
title: Check restored subcontract PDFs against their entry history
area: Backup and restore
---
Deployers can check that the original and corrected PDFs for a subcontracted test match the sources that staff recorded. The check also identifies a result entry whose source is missing.

## Technical notes

Run python manage.py verify_stored_files on the restored database. Missing, duplicate or inconsistent entry history fails the check. Look under Subcontract source PDFs for the affected result or audit event ID.
