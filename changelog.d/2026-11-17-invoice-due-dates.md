---
date: 2026-11-17
type: added
impact: medium
title: Record and filter explicit due dates on invoice drafts
area: Draft invoices
link: commercial:invoice-list
pr: 23
---
Project managers and administrators can record an optional due date while editing a draft invoice; setting, changing or clearing it needs a reason, and the earlier value stays in the history. The Draft invoices list shows the date, can be filtered with Due from and Due through, and can be sorted by Due date.

## Technical notes

Adds migration 0009 in commercial (a nullable due-date column; existing drafts start undated). It cannot be reversed while any draft has a date. Dates are always entered by hand; payment terms never calculate them. Drafts stay internal, with no issuance, balance, overdue, tax or payment behavior.
