---
date: 2026-11-11
type: added
impact: high
title: First public release of OpenBGA Environmental LIMS
---
A free, open-source LIMS for commercial US water, wastewater and soil laboratories, with one laboratory per deployment. Staff handle sample receipt, batches with QC, independent technical review, explicit release authorization and versioned, immutable PDF reports. Clients sign in to a portal where each sees only its own projects, samples, quotes, submissions and reports.

## Technical notes

Django and PostgreSQL 17 with Bootstrap and HTMX, licensed AGPL-3.0-only. Run `migrate`, then `setup_lab` to create the laboratory, role groups and the first administrator. `seed_demo` loads synthetic demonstration accounts and records; keep it away from a real laboratory database. This software supports laboratory records and does not certify compliance or accreditation.
