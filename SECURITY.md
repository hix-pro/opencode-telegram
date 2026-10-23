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

## Threat model

The bridge is a remote-control surface for a local OpenCode server, reachable
from Telegram. It is a convenience tool, not a sandbox.

**What it can do.** Read and write files, run shell commands, start sessions,
approve model-requested actions and stream agent output. Anyone who can
command the bridge can do anything the account running it can do.

**Trust boundaries.**

- *Telegram → bridge.* Everything arriving from Telegram is untrusted input.
  The `TELEGRAM_CHAT_ID` allow-list is the gate; updates from other chats are
  dropped silently. Every update also requires the sender to be allow-listed,
  so a group chat id does not trust its members: list the group and each
  authorized user.
- *Bot token and OpenCode password.* Both are root credentials for this tool.
  A leaked token plus a known chat id is remote code execution. They are read
  from the environment and never written to state or logs.
- *Bridge → OpenCode server.* The local server is trusted and normally
  unauthenticated on loopback. Path checks are client-side because the server
  does not confine file operations.
- *Model output.* Model-requested commands are untrusted. In `read` and `all`
  modes the guard rejects known-destructive patterns; in `ask` mode a human
  approves each request.

**Assets at risk.** Source code, credentials in the environment, SSH keys,
shell history, cached attachments and anything else the running account can
reach.

## Authorization model

Every inbound update follows the same rule: drop it unless the chat id is in
`TELEGRAM_CHAT_ID`.

- messages and edited messages: chat id and sender
- reactions: chat id and reacting user
- inline buttons: chat id and pressing user (added in v2.2.4; earlier releases
  checked only the chat)
- anything else: ignored

`/sh` is user-initiated and runs directly; it does not pass through the
approval policy. The command guard applies to commands the model asks to run.

## Hardened configuration

Recommended for any machine holding real code or credentials:

- Use a private one-to-one chat with an account you control; keep groups off
  the allow-list.
- Set `ALLOWED_ROOTS` to the project directories the bridge actually needs.
- Keep `APPROVAL_MODE=ask` unless you accept the risk of unattended runs.
- Leave the command guard enabled (`/guard`), even in `ask` mode.
- Set `LOG_PROMPT_CHARS=0` if prompts may contain secrets, and keep the log
  file (`0600`) to yourself.
- Keep production credentials, cloud tokens and SSH agent sockets out of the
  bridge's environment.
- Run the bridge as a dedicated OS user, or in a container/VM, with only the
  directories it needs mounted.
- Review `.attachments` retention (`ATTACHMENT_MAX_AGE_HOURS`, 24 h default)
  and clear the cache when sharing the machine.

## Known limitations

- **The destructive-command deny-list is defense in depth, not a boundary.**
  Quoting, indirection, aliases and new binaries can evade regexes. It stops
  ordinary accidents; it does not stop a determined attacker or a
  sufficiently creative model.
- **`ALLOWED_ROOTS` is enforced by the bridge, not the OS.** A bug in a path
  check is a bypass; tests cover traversal and symlink escapes, and OS-level
  isolation is recommended for high-risk use.
- **Local files are readable by the bridge's user.** State and logs are
  `0600`; the attachment cache is `0700`/`0600`. They are not encrypted.
- **A compromised Telegram account is game over.** The bridge cannot tell you
  from someone using your session; enable Telegram 2FA and review active
  devices.
- **Path checks are check-then-use.** The bridge resolves and validates a
  path, then the OpenCode server performs the operation. A local process that
  can swap a symlink between those two steps is outside the application
  boundary. Keep `ALLOWED_ROOTS` in trees untrusted processes cannot write,
  or isolate the bridge.
- **Live integration tests are opt-in.** `pytest -m live` needs a real
  OpenCode server; CI runs the hermetic suite only.
