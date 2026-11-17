---
date: 2026-11-12
type: added
impact: high
title: Laboratory address, logo and certificate numbers on reports
area: Laboratory profile
link: lab-profile
pr: 3
---
Administrators can record the laboratory's address, phone, email, website, logo and certificates under Setup, Laboratory profile. New reports then print a letterhead on the first page and a "Laboratory certificates" section near the end. A certificate prints only when marked for printing and within its validity, and the application does not check results against its scope.

## Technical notes

Adds migration 0005 in core (backward compatible; new columns have database defaults). Pillow is now a direct dependency (it was already installed). The logo is stored once per distinct file and cannot be changed or deleted afterwards. A laboratory that records nothing gets the report it had before, and issued reports and their PDFs are untouched.
