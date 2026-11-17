---
date: 2026-11-14
type: added
impact: high
title: Calculated results with kept readings and the laboratory's own rounding
area: Batches & QC
link: analytical:batch-list
pr: 7
---
An administrator can set a calculation rule for a test, such as reading times dilution factor or solids by weight. Entering that test then takes the bench readings, works out the result, rounds it once by the laboratory's own rule and keeps a worksheet with the result. Rounding is chosen per test, with no default, and typed or imported results are never rounded.

## Technical notes

Adds migration 0010 in analytical (three new tables for calculation rules, worksheets and readings; the worksheet tables cannot be changed or deleted). The reverse is refused while any worksheet exists. File imports and QC sample measurements still supply typed values.
