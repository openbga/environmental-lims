---
date: 2026-11-27
type: added
impact: medium
title: Compare restored file inventories in JSON
---
Deployers can save a JSON comparison of restored files and the inventory taken with their backup. Recovery scripts can identify categories with missing or replaced files and distinguish those differences from a file that failed verification.

## Technical notes

Run `python manage.py verify_stored_files --compare-manifest source-files.json --format json`. A completed scan reports `match`, `different` or `verification_failed`. Only a match exits zero. Input, migration or database errors that prevent a complete scan produce no JSON report.
