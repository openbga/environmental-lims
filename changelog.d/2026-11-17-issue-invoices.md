---
date: 2026-11-17
type: added
impact: high
title: Issue a numbered invoice and download its PDF
area: Draft invoices
link: commercial:invoice-list
---
Project managers and administrators can issue a reviewed draft and download the original invoice for delivery. To correct an issued invoice, void it with a reason and prepare a new draft from the order. The original PDF and history remain available.

## Technical notes

Migration `commercial.0011` stores issued invoice snapshots and PDF bytes in PostgreSQL. Keep this schema on rollback after the first issuance. See the invoice backup and restore checks in the operations guide.
