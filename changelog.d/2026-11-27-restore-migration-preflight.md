---
date: 2026-11-27
type: fixed
impact: medium
title: Detect migration mismatches before verifying restored files
---
The stored-file check now stops if the restored database has missing or unknown migrations. Use the application version that created the backup to check its files.

## Technical notes

verify_stored_files reads migration history from the same PostgreSQL snapshot as the files, including when you supply --snapshot. A mismatch exits nonzero with the counts of unapplied and unknown migrations. All output modes stop before scanning files.
