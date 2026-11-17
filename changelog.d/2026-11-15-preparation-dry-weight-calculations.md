---
date: 2026-11-15
type: added
impact: medium
title: Preparation factors and dry-weight results worked from bench readings
area: Batches & QC
link: analytical:batch-list
pr: 8
---
New calculations work out results read from a prepared solution (digest, extract or concentrated portion) from the sample volume or weight and the final volume. Soil results can be corrected to dry weight using the percent solids you enter and its source, and percent solids and moisture are worked out from dish weights. A laboratory chooses the calculation for each test.

## Technical notes

Adds migration 0011 in analytical (one nullable column on worksheets for the limit before rounding); it cannot be reversed while any worksheet holds such a limit. Existing calculations and worksheets are unchanged. Dry weight is never inferred.
