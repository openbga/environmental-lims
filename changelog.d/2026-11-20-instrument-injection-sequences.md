---
date: 2026-11-20
type: added
impact: high
title: Record actual instrument injection order
area: Batches & QC
link: analytical:batch-list
---
Analysts can record the injection order from an instrument run on an analytical batch. Staff can see the nearest verification before and after each sample and open earlier versions when an analyst corrects a transcription.

## Technical notes

Migration analytical.0014 adds an append-only table for sequence versions. Keep the migrated schema if rolling back application code after staff have recorded a sequence.
