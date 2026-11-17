---
date: 2026-11-13
type: added
impact: medium
title: Sign-in activity is written to the application log
pr: 4
---
A deployer can now see who signed in and when attempts failed: completed sign-ins, failed attempts and sign-outs are written to the application log. Submitted passwords and codes are never logged.

## Technical notes

Lines are written to standard output by the `roots.signin` logger, with the stored username (an unknown or ambiguous identifier shows as unknown). Password attempts refused by the rate limit, rejected email sign-in codes and rejected two-factor codes count as failures. Every application log line also carries `render_request_id`, the validated hosting-platform request header, beside `request_id`; `docs/operations.md` explains how to search them.
