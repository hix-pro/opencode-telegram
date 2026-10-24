# Compatibility

The bridge talks to the local OpenCode HTTP API, not to a Python package, so
"compatibility" means the server contract the live suite exercises.

| Component | Supported / tested |
|---|---|
| Bridge | v2.3.5 |
| Python | 3.10, 3.11, 3.12, 3.13, 3.14 (CI matrix); 3.12 on the reference deployment |
| OpenCode server | 2.1.2 (live suite, 2026-10-23) |
| Telegram Bot API | long polling; `message_reaction` updates for reaction approval |
| OS | Linux with systemd user services (reference: Ubuntu 24.04) |

## How this table is produced

Only versions that actually ran the suite are listed. The hermetic suite runs
on every push; the live suite is opt-in:

```bash
python3 -m pytest -m live
```

The live tests create a throwaway session in a temp directory, exercise
prompt/stream/shell/filesystem contracts, run two sessions concurrently and
abort a turn, then delete the session. Record the server version from
`/doctor` or `opencode --version` when reporting a result.

## Known server behaviours

These are asserted by the live suite because the bridge depends on them:

- `/api/shell` reaps long commands after roughly a second; `/sh` warns about
  this and long work should be sent as a prompt.
- Filesystem endpoints ignore the `directory` parameter, which is why the
  bridge performs `/ls`, `/read`, `/write` and `/find` locally with its own
  `ALLOWED_ROOTS` check.
- The event stream carries text deltas (`message.part.updated`); the bridge
  falls back to polling if the stream disconnects.

## Reporting a mismatch

Open an issue with:

- bridge version (`/doctor` or the release tag),
- OpenCode version (`opencode --version`),
- Python version and OS,
- the failing live test name and its output.

If a contract changed, the live suite is the first thing to update; the
hermetic tests deliberately fake the API and cannot catch wire-format drift.
