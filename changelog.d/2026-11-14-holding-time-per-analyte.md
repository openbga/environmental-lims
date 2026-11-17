---
date: 2026-11-14
type: added
impact: high
title: Holding-time limits can be set for a single analyte
area: QC configuration
link: analytical:qc-config
pr: 6
---
A holding-time rule under QC configuration can now name a single analyte, for methods whose analytes have different limits (for example EPA 300.0, with 48 hours for nitrate and 28 days for chloride). It replaces the method-wide rule for that analyte; rules without an analyte apply as before.

## Technical notes

Adds migration 0009 in analytical (a nullable analyte column and a new uniqueness rule; needs PostgreSQL 15 or later). It cannot be reversed while any rule names an analyte, so create analyte rules only after no older instance runs against the database. Spreadsheet import takes an optional `analyte` column. The starter files now carry per-analyte limits for EPA 300.0 and EPA 365.1. Recorded evaluations are not recalculated.
