---
date: 2026-11-24
type: changed
impact: medium
title: Verify laboratory attachments after restore and upgrade
---
The downloadable upgrade report now covers evidence attached to equipment events, staff training records and nonconformance events. It checks that staff can retrieve the original files and that a signed-in client is denied each download.

## Technical notes

The rehearsal attaches three distinct files on the synthetic baseline. After restoring the backup and again after migrating it, CI checks the records, attachment metadata and bytes returned by the staff download route.
