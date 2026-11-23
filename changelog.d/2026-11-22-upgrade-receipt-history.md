---
date: 2026-11-22
type: changed
impact: medium
title: Check receipt records in the upgrade rehearsal
area: Deployment
---
The downloadable upgrade report now counts orders, samples and containers checked against the original installation. The rehearsal also follows a corrected client sample ID from receipt through a custody transfer.

## Technical notes

The populated PostgreSQL rehearsal compares all baseline orders, samples and containers after migration. It checks the original receipt, correction history and transfer for one synthetic sample.
