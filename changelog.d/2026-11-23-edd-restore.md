---
date: 2026-11-23
type: changed
impact: low
title: Check electronic deliverable downloads after restore and upgrade
---
The upgrade rehearsal report now covers a synthetic electronic data deliverable. It records whether the owning client could download the original ZIP after a backup was restored and again after the application was upgraded.

## Technical notes

The rehearsal compares the stored export, its frozen recipient mappings and its link to the issued report version with the baseline. The downloadable upgrade notes include the export in the verified record counts.
