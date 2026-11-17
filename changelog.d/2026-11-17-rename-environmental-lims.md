---
date: 2026-11-17
type: changed
impact: high
title: The application is now called OpenBGA Environmental LIMS
pr: 19
---
The product is now named OpenBGA Environmental LIMS. The new name and logo lock-up appear in the top bar, page titles, footer and in-app help. On narrow screens the top bar shows the mark and keeps the full name for screen readers.

## Technical notes

No behavior, data or migration changes. The default `SOURCE_CODE_URL` and the project name in `pyproject.toml` now use the new repository name, and the default acceptance evidence folder is `environmental-lims-acceptance-<UTC time>`. A deployment that sets `SOURCE_CODE_URL` itself should check that it points to its published source. Existing service, host, database and logger names are unchanged.
