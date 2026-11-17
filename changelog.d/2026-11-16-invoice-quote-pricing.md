---
date: 2026-11-16
type: added
impact: medium
title: Price invoice drafts from a matching accepted quote
area: Draft invoices
link: commercial:invoice-list
pr: 16
---
When preparing a draft invoice, staff can choose a matching accepted quote version instead of a price list. The quote must match the received order's tests and quantities with one line per test, and each test takes the quoted price and description. The accepted version stays linked through later edits and cancellation.

## Technical notes

Adds migration 0006 in commercial (nullable quote provenance; existing drafts are kept). It cannot be reversed while quote provenance exists, and older code cannot correctly show or edit quote-priced drafts. Matching is done per order; quote quantities are not used up across orders.
