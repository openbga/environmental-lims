---
date: 2026-11-16
type: added
impact: high
title: Record calibration curves and link them to results
area: Batches & QC
link: analytical:batch-list
pr: 13
---
Analysts can record an instrument-reported calibration curve for a batch and test and link it, with an optional calibration verification QC observation, when entering or correcting a result. Reviewers can open the standard points and source. If a linked QC observation is later superseded, the result is held from submission, approval and release until a new revision is entered.

## Technical notes

Adds migration 0013 in analytical (additive; the reverse is refused once evidence exists). Keep the new schema on a code rollback, and do not review or release linked work with older code. People who contributed the curve or check cannot review or release that result. Grid, file-import and BOD bottle entry do not link evidence. Curve fitting is not included.
