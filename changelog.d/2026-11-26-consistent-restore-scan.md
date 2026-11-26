---
date: 2026-11-26
type: fixed
impact: medium
title: Check restored files at one point in time
area: Installation and upgrades
---
When staff add files during a restore check, the counts and inventory now reflect the files present at the first database read. Run the check again to include later files.

## Technical notes

A standalone verify_stored_files run uses one read-only PostgreSQL transaction across all categories. To capture an inventory at the same point as a backup, pass the same exported snapshot to the command and pg_dump.
