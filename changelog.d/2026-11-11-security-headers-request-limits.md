---
date: 2026-11-11
type: security
impact: high
title: Stricter browser security headers and request size limit
---
Browser protections are stricter: every page now sends a Content Security Policy, requests that declare a body over 12 MiB are refused, and searching in the administration screens can no longer be used to probe stored authenticator secrets.

## Technical notes

No migration. New optional settings, documented in `docs/operations.md` and `.env.example`: `CONTENT_SECURITY_POLICY` (replaces the default policy), `MAX_REQUEST_BODY_BYTES` (12 MiB by default), `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS` and `ALLAUTH_TRUSTED_CLIENT_IP_HEADER` (set it only where the platform supplies a client-address header that clients cannot forge). A deployment that adds its own scripts or embeds content may need to adjust the policy.
