---
date: 2026-11-15
type: added
impact: medium
title: Export an analytical batch run list for instrument setup
area: Batches & QC
link: analytical:batch-list
pr: 11
---
Analysts and administrators can use Export run list on a batch to download a generic CSV for preparing instrument work, listing every assigned test and directly attached QC sample with catalog limits and sample states. Map the columns to your instrument and check the run: the file does not decide injection order or add standards.

## Technical notes

The file is limited to 2,000 rows and ignores page filters. A closed batch exports its current content, not a historical snapshot.
