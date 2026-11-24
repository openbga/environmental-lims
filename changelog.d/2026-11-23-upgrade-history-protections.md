---
date: 2026-11-23
type: changed
impact: medium
title: Verify history protections after restore and upgrade
---
Deployers can check the upgrade rehearsal report to see whether PostgreSQL protected audit events, custody events, issued report versions and stored report PDFs at both stages.

## Technical notes

The rehearsal attempts an update and a deletion on one baseline record in each category on the disposable restored copy. Each attempt must raise the expected database error after restore and again after migration before CI publishes the report.
