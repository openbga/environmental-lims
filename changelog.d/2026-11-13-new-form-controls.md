---
date: 2026-11-13
type: changed
impact: medium
title: Searchable selects, shared date pickers and remembered list settings
pr: 5
---
Selects offer search on long lists, chips for several choices and a clear button, and long lists of records load as you type. Dates and times use one picker with typed entry, Today and Now shortcuts and the laboratory's time zone, and file uploads use a drop zone. Your list page size, columns, sort order and sidebar state are remembered between visits.

## Technical notes

Adds migration 0006 in core (additive; a per-user preferences table).
