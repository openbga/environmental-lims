---
date: 2026-11-16
type: added
impact: high
title: Prepare invoice drafts from received tests
area: Draft invoices
link: commercial:invoice-list
pr: 15
---
Project managers and administrators can select New draft invoice on a received chain of custody and price it from an active price list. Descriptions, quantities and prices can be changed with a reason, and a draft can be cancelled while its history stays. Drafts are internal worksheets; later receipt corrections or price changes do not rewrite them.

## Technical notes

Adds migration 0005 in commercial (two new tables). It cannot be reversed once a draft exists. Keep the schema on a code rollback. Every requested test needs a price; prices are whole-cent USD. The draft snapshots the billing and contact details, the PO, the price-list revision and the original sample and test identities.
