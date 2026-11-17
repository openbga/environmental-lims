---
date: 2026-11-12
type: added
impact: low
title: Shared demonstration accounts and a scheduled reset for public demos
---
A public demonstration can list its demo accounts and shared password on the sign-in page, and its data can be erased and seeded again on a schedule. Demo accounts cannot change their password, email address or two-factor settings. None of this applies to a laboratory's own deployment.

## Technical notes

Adds migration 0004 in accounts (a locked-credentials flag). `seed_demo --announce-accounts` posts the accounts and `DEMO_PASSWORD` (never `demo_admin`; set a different `DEMO_ADMIN_PASSWORD` first). `reset_demo` erases and reseeds in one transaction and runs only with `ALLOW_DEMO_RESET=true`, only on the demonstration laboratory and only when the schema matches the release. A separate demonstration-only deployment blueprint schedules it daily. Never set `ALLOW_DEMO_RESET` where real records are kept.
