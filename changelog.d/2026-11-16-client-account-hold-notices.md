---
date: 2026-11-16
type: added
impact: medium
title: Show client account-hold notices at receipt and report release
area: Clients
link: receipt:client-list
pr: 14
---
Project managers and administrators can record an account hold on a client with a required internal note. Staff then see the notice on the client record, the chain of custody, both receipt paths and the report release preview. It is informational: it does not block receipt or release, and hold notes never appear in the client portal or reports.

## Technical notes

Adds migration 0013 in receipt (new client fields with defaults; additive). Keep the schema on a code rollback, since older code cannot show the notices.
