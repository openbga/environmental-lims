---
date: 2026-11-12
type: changed
impact: medium
title: Staff menus now show only the work each role can do
pr: 2
---
The sidebar now shows only the work your roles allow, and sections with nothing to show are left out. For example, an approver sees Dashboard, Reports, Training and Nonconformances, and an analyst sees bench work without receipt, technical review or report management. Someone with several roles sees the combined menu.

## Technical notes

The menu is built from each account's roles and release permission. Access is still enforced on the server, and the client portal is unchanged.
