---
date: 2026-11-24
type: added
impact: high
title: Download the original file from a confirmed result import
area: Batches & QC
link: analytical:batch-list
---
Open Import source files on an analytical batch to download the exact CSV, TSV or Excel workbook used in a new confirmed import. A workbook download includes every sheet. Earlier imports retain their existing audit and row details.

## Technical notes
Migration `analytical.0015` stores one copy of each confirmed upload in PostgreSQL, up to 1 MiB per import. Include this growth in database and backup sizing. The stored-file verifier checks the files alongside their import events. Keep the migrated schema when rolling back application code after files have been stored.
