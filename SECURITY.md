# Security policy

## Supported versions

Only the latest tagged release receives security fixes (currently `v2.2.x`).

## Reporting a vulnerability

Please do **not** open a public issue for a vulnerability.

Use GitHub's private vulnerability reporting on this repository:

<https://github.com/hix-pro/opencode-telegram/security/advisories/new>

Include the version, a minimal reproduction, and the impact you believe it
has. You can expect an acknowledgement within a few days.

## Scope

The bridge is a remote-control surface for a local OpenCode server, reachable
from Telegram. Reports of particular interest:

- bot-token or OpenCode-password disclosure in logs, errors or replies
- allow-list bypass: a chat or user that should not be able to command the bridge
- path-escape flaws: reading or writing outside `ALLOWED_ROOTS`
- a crafted Telegram update that escalates a reaction or button callback
- approval confusion: a permission card approving a request it does not belong to

Out of scope: exposures limited to your own machine or account (a token left
on disk, a compromised Telegram account), and issues in OpenCode itself —
report those to the OpenCode project.
