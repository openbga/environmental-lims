---
date: 2026-11-15
type: added
impact: medium
title: Calculate BOD5 from retained, analyst-selected bottles
area: Batches & QC
link: analytical:batch-list
pr: 10
---
A BOD5 test set up with the bottle calculation opens a worksheet from batch entry or the result record. Analysts keep 1 to 12 bottle observations, enter a seed correction and its source for each, and include or exclude each bottle; the result is the mean of the selected bottles. Excluded observations and the reasons for selection stay with the result.

## Technical notes

Adds migration 0012 in analytical (a nullable JSON column for bottle observations); it cannot be reversed once bottle observations exist. The seed correction is supplied, not estimated; bottle suitability, incubation and QC acceptance remain the laboratory's decisions.
