---
date: 2026-11-15
type: added
impact: high
title: Import results from a selected Excel worksheet
area: Import mappings
link: analytical:import-profile-list
pr: 12
---
Analysts can now import an XLSX worksheet using an existing long or wide import mapping. The preview shows the exact values to be stored with their worksheet and row, nothing is written until you confirm, and each worksheet of a workbook is a separate import that cannot be repeated.

## Technical notes

New dependency `defusedxml` (run `uv sync --frozen`); no migration. Identifiers and analysis times must be text. Formulas, merged cells, typed dates, macro-enabled, encrypted or externally linked workbooks and oversized files are refused; hidden rows and columns are included. The 2,000 result limit now also applies after wide rows are expanded in CSV and TSV imports.
