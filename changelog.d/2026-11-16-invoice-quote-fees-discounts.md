---
date: 2026-11-16
type: added
impact: medium
title: Carry accepted quote fees and discounts into invoice drafts
area: Draft invoices
link: commercial:invoice-list
pr: 17
---
An accepted quote that includes fees or discounts can now prepare a draft invoice when its tests and quantities match the received order. Each quoted fee or discount is copied onto the draft once, after the test lines, and staff can edit it with a reason. A draft holds at most 200 lines and cannot have a negative total.

## Technical notes

Adds migration 0007 in commercial (a line kind with a default of test; existing lines remain tests). It cannot be reversed while adjustment lines exist. A line's kind and source cannot be edited.
