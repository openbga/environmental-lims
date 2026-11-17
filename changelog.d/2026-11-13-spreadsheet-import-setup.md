---
date: 2026-11-13
type: added
impact: high
title: Import setup records from spreadsheets
area: Spreadsheet import
link: dataimport:index
pr: 4
---
Under Setup, Spreadsheet import, project managers load clients and projects, and administrators also load analytes, methods, tests, QC requirements, holding-time rules, QC limits and preservation rules from CSV files. Each file is previewed row by row and saved all or nothing, and rows use the names and codes people use, never database IDs.

## Technical notes

New app `dataimport`; no migrations. Files are CSV only so that limits stay exact decimals. Each record's history names the file it came from.
