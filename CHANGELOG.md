# Changelog

## v2.1.0 — 2026-10-22

The audit round. Every defect found has a regression test; every feature is
user-facing.

### Fixed

- **Daily schedules** double-fired on rollover and skipped days. `Schedule.due`
  is boundary-based now, catches up after downtime, and never fires for
  occurrences older than the schedule itself. Five deterministic tests pin it.
- **Malformed Telegram 429 bodies** could crash a delivery. The send path
  negotiates the documented `retry_after` instead.
- **`/sh` ran on the poll thread** and could stall the whole bridge for
  minutes. The command is acked immediately; output arrives as a second
  message while polling keeps running.
- **Event-stream log noise**: one warning line per drop for days was drowning
  real problems. The first drop logs at WARNING, later consecutive drops at
  DEBUG (every tenth still visible), the recovery line counts the attempts,
  and `/health` reports the last error.

### Added

- **Command autocomplete** — the daily commands (curated in
  `commands.PRIMARY_COMMANDS`) are published to Telegram at startup with
  descriptions; `/` in the input bar suggests them.
- **Chat Menu button** — opens the command keyboard, the discoverable control
  when you have forgotten what the bot can do.
- **Reactions steer turns** — 👍 approves every pending permission request
  (once each); 👎 or 🛑 aborts the running turn. Allow-listed people in
  allow-listed chats only, and only a newly *added* reaction acts.
- **Editing a sent message resends it as a fresh prompt** — the chat history
  doubles as the queue. The bridge's own edits and slash-command edits never
  trigger it.
- Telegram receives the update types the bridge actually consumes
  (`allowed_updates`); reactions and edits are invisible to the Bot API
  without it.

### Housekeeping

- Everything is under git from the v2.0.0 baseline onward; secrets, state and
  logs are gitignored.
- A pre-commit hook runs ruff and the offline suite on every commit (bypass:
  `--no-verify`); the hook itself is tracked via `core.hooksPath = hooks/`.
- Legacy snapshots holding credential copies moved out of the repository tree
  to `~/backups/legacy-bridge-snapshots`, where `git clean` cannot touch them.
- 537 offline tests plus 10 live checks against the real server.

### Known limitations (tracked upstream, by design here)

- The OpenCode **server** reaps shell commands after about a second; `/sh`
  warns and hands long jobs to the agent instead
  (`test_shell_kills_long_commands` asserts the behavior).
- Server fs endpoints ignore the directory parameter; the bridge reads and
  writes the filesystem locally.

## v2.0.0

Baseline of the rewrite: multi-session Telegram ↔ OpenCode bridge with
streaming edits, permission gates, schedules and attachments. The README is
the complete reference.
