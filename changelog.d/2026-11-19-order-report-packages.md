---
date: 2026-11-19
type: added
impact: high
title: Download an issued report package with the QC summary requested at receipt
area: Reports
---
Clients can download the stored PDF and results CSV together from the report’s version history. Staff can request a QC summary on a chain of custody and check which samples it covers before release. Each issued version keeps its own selection.

## Technical notes

Receipt migration 0014 adds an order preference that defaults to off for existing and new orders. Packages for versions issued before the migration contain the original PDF and standard results CSV.
