---
date: 2026-11-16
type: added
impact: medium
title: Add fees and discounts to invoice drafts with a reason
area: Draft invoices
link: commercial:invoice-list
pr: 18
---
On an active draft invoice, Add fee or discount lets staff enter a fee or discount with a description, quantity, signed USD price and a reason. These lines are marked as added manually, and an unwanted amount can be corrected to zero with a reason while the line stays in the history.

## Technical notes

Adds migration 0008 in commercial (relaxes quote provenance for non-test lines only). It cannot be reversed while manual lines exist, including on cancelled drafts. Older code can read and cancel these drafts but refuses edits.
