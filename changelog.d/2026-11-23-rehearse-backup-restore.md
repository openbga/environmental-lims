---
date: 2026-11-23
type: changed
impact: medium
title: Verify a restored backup before upgrading
area: Deployment
---
The downloadable upgrade rehearsal report shows which synthetic records survived a PostgreSQL restore and the subsequent upgrade. Both stages also check staff and client PDF downloads.

## Technical notes

CI takes a logical backup of the populated distribution-branch database and restores it into a second, empty database. The previous application checks the restored copy before the candidate applies its migrations and repeats the checks. Each stage must verify the same baseline record counts before CI publishes the report.
