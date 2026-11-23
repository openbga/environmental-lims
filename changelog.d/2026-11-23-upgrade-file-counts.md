---
date: 2026-11-23
type: changed
impact: low
title: See stored file checks in upgrade reports
---
The downloadable upgrade rehearsal report lists how many documents and attachments the upgraded application checked in each category. A zero shows where the synthetic installation had no files to check.

## Technical notes

To save counts from your own restored database, run `python manage.py verify_stored_files --format json` and redirect standard output to a file. Check the exit status for success. Details of failing records go to standard error.
