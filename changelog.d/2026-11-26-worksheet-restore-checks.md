---
date: 2026-11-26
type: changed
impact: medium
title: Check worksheet history after restoring a backup
area: Installation and upgrades
---
The upgrade report now confirms that a restored result worksheet and one of its original readings each rejected an edit and a deletion. The rehearsal repeats both checks after applying the candidate migrations.

## Technical notes

Read the application job’s upgrade notes for the tested revisions. Both stages run on a disposable copy of the synthetic database.
