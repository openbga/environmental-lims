---
date: 2026-11-22
type: changed
impact: medium
title: Check QC history during upgrade rehearsals
---
The GitHub Actions upgrade rehearsal now checks that QC samples, observations and corrective actions survive migration. It follows a failed observation through reanalysis and correction and verifies that both records still point to the original observation.
