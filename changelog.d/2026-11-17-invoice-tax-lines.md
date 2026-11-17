---
date: 2026-11-17
type: added
impact: high
title: Record tax amounts on draft invoices
area: Draft invoices
link: commercial:invoice-list
---
Project managers and administrators can add a Tax line with an externally determined amount and its basis in the description. The amount is included in the draft total and retained through reasoned edits and cancellation. Review recorded tax when charges change; the application does not calculate or recalculate it.

## Technical notes

Migration commercial.0010 expands the invoice line constraint without changing existing records. Keep the schema on code rollback and use compatible code to display or edit tax lines. Reversal refuses while any tax line exists, including zero amounts and cancelled drafts; do not remove or relabel assessments to force reversal. No dependency or setting changes.
