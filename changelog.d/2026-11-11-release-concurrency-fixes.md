---
date: 2026-11-11
type: fixed
impact: medium
title: Safer release, disposal and import when several people work at once
---
Removing someone's release permission can no longer be bypassed by a release already in progress, and the strict CSV import refuses a stale preview or a file already imported into the batch. Three cases that used to end in a server error now show a message instead.

## Technical notes

Adds migration 0012 in receipt, which makes sample disposal records final in the database; existing rows are not rewritten.
