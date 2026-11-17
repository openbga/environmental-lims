---
date: 2026-11-13
type: added
impact: high
title: Acceptance run command that writes evidence after install and upgrade
pr: 4
---
After installing or upgrading, a deployer can run a scripted check of the main controls, such as role refusals, client isolation, independent review and report immutability. It uses synthetic records in a scratch database and writes an evidence package to keep; it is not a statement of validation, compliance or accreditation.

## Technical notes

Run `python manage.py run_acceptance --operator "Your Name" --output /path/to/new-directory`. It runs 23 steps under the deployment's own settings (use `DEBUG=false`), creates and drops a database named `acceptance_<database name>`, and never touches the laboratory's records. The database role needs `CREATEDB`; otherwise supply an empty database in `ACCEPTANCE_DATABASE_URL`. The package holds the report as HTML and PDF, JSON, report PDFs and a SHA-256 manifest. See `docs/operations.md`.
