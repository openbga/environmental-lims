---
date: 2026-11-11
type: added
impact: low
title: Demonstration data can include a full synthetic working week
---
Evaluators can load a full synthetic laboratory: two clients, about 50 samples in every state, a batch awaiting review, a QC failure needing a disposition, control-chart history, an MDL study, and draft, released and amended reports.

## Technical notes

Run `python manage.py seed_demo --full`. It takes about 20 seconds and adds nothing when run again. Plain `seed_demo` is unchanged, and the seed refuses a laboratory that is not the demonstration laboratory.
