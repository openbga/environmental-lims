---
date: 2026-11-26
type: changed
impact: medium
title: Verify result revision history after a restore
area: Installation and upgrades
---
The upgrade report now confirms that PostgreSQL protected the observation text on an open result revision and the review note on an approved revision. It also confirms that both revisions rejected deletion after restore and again after migration.

## Technical notes

The rehearsal checks revisions from the backed-up synthetic installation. Both stages run on the disposable restored database.
