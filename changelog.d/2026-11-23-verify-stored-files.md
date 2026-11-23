---
date: 2026-11-23
type: added
impact: medium
title: Check stored files after restoring a backup
---
Deployers can check issued documents, electronic deliverables, laboratory logos and evidence attachments on a restored database. The check lists how many records it examined in each category and identifies records with missing or mismatched files.

## Technical notes

With DATABASE_URL set to the restored database, run `python manage.py verify_stored_files`. It compares each stored file with its recorded SHA-256 and any recorded byte count. Keep the output with the restore record and investigate any nonzero exit. The command only reads laboratory records.
