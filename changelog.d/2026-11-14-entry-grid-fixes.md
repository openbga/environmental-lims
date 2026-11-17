---
date: 2026-11-14
type: fixed
impact: low
title: Result entry grid skips untouched rows and gives clearer errors
area: Batches & QC
link: analytical:batch-list
pr: 7
---
On the result entry grid, a row left with only the reporting limit the page filled in is now skipped instead of failing, and a blank result reads "Result is required." A stray template comment no longer prints above the grid.
