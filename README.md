# OpenCode Telegram bridge

[![CI](https://github.com/hix-pro/opencode-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/hix-pro/opencode-telegram/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/hix-pro/opencode-telegram)](https://github.com/hix-pro/opencode-telegram/releases/latest)
[![License: MIT](https://img.shields.io/github/license/hix-pro/opencode-telegram)](LICENSE)

> This project is not built by the OpenCode team and is not affiliated with
> them in any way. It is an independent community tool that talks to the
> OpenCode server via its local HTTP API.

Full remote control of a local OpenCode server from Telegram: live streaming of
what the agent is doing, session switching, model and agent switching, file and
shell commands, and permission approvals from your phone.

## How it works

```
Telegram ──getUpdates(long poll)──► bridge.py
                                       │
      ┌────────────────────────────────┼──────────────────────────────┐
      │ main thread                    │ worker thread                 │ events
      │ commands, /abort, /yes,        │ one turn at a time:          │ SSE ──┐
      │ inline button callbacks        │ edits one message live       │       │
      └────────────────────────────────┼──────────────────────────────┘       │
                                       │                                     │
                        POST /api/session/{id}/prompt                        │
                                       │                                     │
        ┌──────────────────────────────┴──────────────┐                      │
        │ watcher: auto-approves or asks with buttons│◄─────────────────────┘
        └─────────────────────────────────────────────┘
```

*Sketch of the main flows only; the table below is the authoritative list of
background threads.*

Five background threads, so nothing the agent does can block the chat (plus
one `turn-<chat>` thread per running turn):

| Thread | Job |
|---|---|
| main | long-polls Telegram, answers commands inline, handles button callbacks |
| worker | hands queued prompts to per-chat turn threads, one dispatch at a time |
| events | one SSE connection to `/api/event` feeding live text deltas |
| watcher | answers permission requests per policy, otherwise pings with buttons |
| scheduler | hands due `/schedule` prompts to the same queue turns use |

Because commands run on the main thread, `/abort`, `/yes`, and `/no` still work
while the agent is mid-turn.

## Setup

**1. Create a bot.** Message [@BotFather](https://t.me/BotFather), send `/newbot`,
and copy the token.

**2. Find your chat id.** Message your new bot once, then:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

Your numeric id is in `message.chat.id`.

**3. Configure.**

```bash
cp .env.example .env
chmod 600 .env        # it holds a bot token
$EDITOR .env          # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

Every setting is documented in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
The bridge warns at startup if an existing `.env` is readable by group or
others.

**4. Install and run.**

```bash
./install.sh
```

That checks your prerequisites, generates the systemd units from the repo's
templates (any checkout location works, spaces included), and starts the
service. It is safe to re-run after an upgrade and never overwrites your `.env`
or your state file.

To run it in the foreground instead:

```bash
set -a && . ./.env && set +a
python3 bridge.py
```

**5. Check it.**

Send `/start` (or `/help`) for the command list, then `/doctor`. It reports the
server, session, policy, event stream, permission watcher and scheduler in one
message.

**No setup needed beyond that:** on start the bridge publishes its command list
to Telegram, so every `/` you type autocompletes with a description, the
chat's Menu button opens as a command keyboard, and a **persistent main menu**
(Status · Sessions · New · Abort · Help) attaches to the bottom of the chat -
tap a button, or type `/` for the full list. Telegram can always collapse the
menu locally, so it never blocks typing.

### Day-to-day

```bash
systemctl --user status   opencode-telegram-bridge
systemctl --user restart  opencode-telegram-bridge
tail -f bridge.log                     # survives reboots
journalctl --user-unit=opencode-telegram-bridge -f --no-pager
```

Two logs are kept. `bridge.log` rotates at 2 MB and keeps three backups.
Journal retention depends on your systemd/journald configuration; the rotating
file is the persistent copy.

To remove the service: `./uninstall.sh` (add `--purge` to delete credentials and
state as well, `--purge-server` to take down OpenCode itself).

## Live streaming

While the agent works, the bot edits a single message in place: a spinner, the
model, elapsed time, a trail of the tools it is calling, and the text as it
arrives. Telegram allows roughly 30 edits per minute, so edits are throttled by
`EDIT_INTERVAL` and the stream is treated as an accelerator, not the source of
truth - completion is still detected from the session's idle marker.

If the SSE stream drops, the worker reconnects with capped backoff and the turn
carries on through polling.

## Long turns

Real jobs take a long time, so the bridge is built not to look dead while one
is running:

| Behaviour | Setting | Effect |
|---|---|---|
| Heads-up | `HEADS_UP_AFTER` (default 240s) | The live message says "still working after Nm" once, then keeps waiting |
| Hard cap | `PROMPT_TIMEOUT` (default 7200s) | Only for genuinely wedged turns, and it says the job may still be running rather than pretending it ended |
| Delivery | `PROMPT_DELIVERY` (default `queue`) | A message sent during a turn **waits its turn** instead of cancelling it |
| Survives a server restart | - | `send_prompt` retries connection failures with backoff |
| Survives a bridge restart | - | The in-flight turn is persisted; on boot the chat is told, and whether the job is still running |
| `/abort` | - | Stops the turn immediately, at any point |

`steer` is the server's default and is almost always wrong for a chat: a
follow-up message cancels the work in progress. Use `/delivery steer` only when
you deliberately want to redirect a running job.

## Commands

Anything that is not a slash command is sent to the agent as a prompt.

### Session

| Command | Effect |
|---|---|
| `/new [dir] [title]` | Start a session, optionally in another directory |
| `/sessions [n] [search]` | List recent sessions; the active one is marked `*` |
| `/use <id>` | Switch by full id, unique prefix, or `#3` |
| `/info` | Details for the active session |
| `/title <text>` | Rename it |
| `/del <id>` | Delete a session and its children |
| `/fork` | Branch the active session |
| `/compact` | Summarise old turns to free context |
| `/revert [go\|drop]` | Roll back the last turn |
| `/msgs [n]` | Recent messages |
| `/stats` | Aggregate token and cost usage across all sessions |
| `/export` | Dump the session as JSON |
| `/search <text>` | Find past sessions by title |

`/sessions` comes with a **Switch button per session**, so juggling sessions is
a tap instead of an id, and the switch is confirmed in the chat.

### Macros

Standing prompts get short slash commands. Record one, then run it by name:

```
/macro deploy deploy the app to staging
/deploy                  → runs the recorded prompt
/deploy canary           → appends the tail: "deploy the app to staging canary"
/macro deploy {args} ship it        ← an explicit {args} slot works too
/macro deploy            → deletes it (no body after the name)
```

Macros are global to the bot and saved in `.state.json`; they may not shadow a
built-in command, and their bodies may not be slash commands. Autocomplete
stays the curated command list — `/macro` lists what you have recorded.

### Schedules

Run a prompt on a timer. Schedules are per chat, survive restarts, and go
through the same queue as a typed message, so streaming, permissions and
`/abort` work normally.

| Command | Effect |
|---|---|
| `/schedule` | List this chat's schedules |
| `/schedule every 30m <prompt>` | Run every 30 minutes |
| `/schedule every 2h <prompt>` / `/schedule hourly <prompt>` | Run on an hourly interval |
| `/schedule daily 09:00 <prompt>` | Run at 09:00 local time |
| `/schedule rm <id>` | Remove one (ids are shown by `/schedule`) |

```
/schedule every 30m check the build and report failures
/schedule daily 09:00 summarise yesterday's issues
```

`daily` matches the local calendar, not a fixed 24-hour countdown. If the
bridge was down at the scheduled minute, it runs once when the bridge comes
back that day; a run that already happened is not repeated. Schedules are
stored in `.state.json` with macros and per-chat preferences.

### Model and agent

| Command | Effect |
|---|---|
| `/model [provider/model#variant]` | Show or switch model |
| `/models` | List available models |
| `/agent [name]` | Show or switch agent |
| `/agents` | Describe the configured agents |
| `/skills` | Skills available here |
| `/mcp` | MCP server status |
| `/run <name> [args]` | Run a slash command from config |

### Files and code

| Command | Effect |
|---|---|
| `/dir [path]` | Show or switch the working directory |
| `/projects` | Known projects |
| `/ls [path]` | List a directory |
| `/find <query>` | Search for files by name, scoped to the working directory |
| `/read <path>` | Print a file |
| `/write <path>` + lines below | Write a file |
| `/git` | Working tree status |
| `/diff` | Diff for this directory |
| `/web <query>` | Search the web |
| `/worktrees [new <name>\|rm <name>]` | List, create, or remove git worktrees |
| `/sh <command>` | Run a shell command (see the limit below) |

`/ls`, `/read`, `/find` and `/write` work on the local filesystem rather than the
server's HTTP API, which ignores the requested directory and resolves everything
against its own working directory. They are scoped to the working directory,
confined by `ALLOWED_ROOTS`, and refuse known credential/control paths such as
`.env`, `.ssh`, `.git`, shell startup files, systemd user units, and the
bridge's own state and logs. That refusal is an accident guard, not a sandbox.

**`/sh` replies in two messages.** The bridge acks immediately - "Running $ ..."
- and the output arrives as a second message when it finishes. The dispatcher
never waits for the command, so buttons and commands from any other chat work
while it runs. The OpenCode server still reaps a shell roughly a second after
it starts, so only commands that finish immediately return output. For
anything longer, send it as a message: the agent runs commands itself with no
such limit and streams the output as it goes.

### Policy and guard rails

| Command | Effect |
|---|---|
| `/auto [ask\|read\|all\|off]` | Auto-approval mode |
| `/policy [allow\|ask\|always\|deny <action>]` | Per-action auto-approval |
| `/policy clear <action>` / `/policy reset` | Remove one rule / all rules |
| `/guard` | Show denied patterns (applied to model and `/sh` commands) |
| `/guard add <regex>` | Deny a new pattern |
| `/guard rm <regex>` | Remove one |
| `/guard test <cmd>` | Would this be blocked? (runs nothing) |
| `/watch [on\|off]` | Live progress for this chat |
| `/delivery [queue\|steer]` | What happens if you message during a long turn |
| `/cost` | Spend and token usage |
| `/alert [0.25\|off]` | Warn when a session passes a spend level |

### Permissions

| Command | Effect |
|---|---|
| `/perms` | List pending requests |
| `/yes [id\|#n\|all]` | Approve once |
| `/yes always [id]` | Approve for good |
| `/no <id\|#n\|all>` | Reject |

Pending requests arrive as a message with **Allow once / Always allow / Reject**
buttons. Three layers decide an answer, in order:

1. **The guard** force-rejects denied commands, whatever the mode or rules say.
2. **Per-action rules** from `/policy` override the mode for one action.
3. **The mode** (`/auto ask|read|all`) applies to anything unruled.

```
/policy always edit     trust edits, still ask about commands
/policy allow read      approve reads silently
/policy deny bash       never approve commands automatically
/policy deny *          approve nothing without asking
```

Rules are per chat, so a shared bot can be strict for one person and relaxed
for another.

### Reactions

The two commands you reach for fastest need no typing at all. React - to any
message in the chat:

| Reaction | Effect |
|---|---|
| 👍 | Approve the only pending request; with several, point at `/yes all` |
| 👎 or 🛑 | Abort the running turn |

Only allow-listed people in allow-listed chats count, so a shared chat is not
opened to whoever finds the bot. Only a newly *added* reaction acts: removing
an emoji never repeats anything, and any other emoji is ignored.

Reacting **on a pending permission card** is scoped to that card: 👍 approves
*just that request*, 👎/🛑 rejects it. On any other message, 👍 approves only
when exactly one request is pending. If several are pending, it reports the
count and changes nothing; use a card or `/yes all` to approve a batch.

The mapping from a card to its request lives in memory, so after a bridge
restart a reaction on a card delivered *earlier* degrades to the single-request
rule above. It can approve one request but never a batch.

### Editing a sent message

By default, editing a message you already sent does nothing. This is
deliberate: an accidental edit should not queue a second prompt or repeat a
side effect.

Set `EDIT_RESEND=true` to make the chat history double as the queue: the new
wording is sent as a fresh prompt. Even then, the bridge's own message edits
never trigger it, and editing a slash command does nothing: it already ran, and
re-running one because its wording changed is a surprise, not a service.

### Help

| Command | Effect |
|---|---|
| `/status` | Server, session, policy, and bridge state |
| `/health` | Event stream, edit reliability, turn counts, errors, uptime |
| `/doctor` | Full self-check: config, server, session, disk, threads |
| `/selftest` | End-to-end checks against the **live** server |
| `/help` | Full command list |
| `/start` | Alias for `/help`; the usual first message from a new chat |

`/selftest` is the one that matters when something looks wrong. The rest of the
suite runs against a fake API, and a fake will happily accept a wrong content
type or an ignored query parameter - which is how three real defects survived a
green test run. `/selftest` exercises the real paths: a prompt round-trip, text
deltas arriving over the event stream, filesystem agreement between the bridge
and the agent, path confinement, and shell execution. It reports `PASS`/`FAIL`
per check, and it is written so that a check which cannot fail is treated as a
bug in the check.

### Attachments

Send a **photo**, **document**, or **voice note** and it is downloaded, attached
to your next message, and described to the agent. Use a caption to add
instructions: *"what's wrong in this screenshot?"*

Voice notes need a transcription tool. Point `STT_COMMAND` at any CLI that reads
an audio file and prints a transcript:

```bash
STT_COMMAND=whisper -m base {file}
```

Without it the bot says so rather than failing quietly.

## Resilience

What happens when things break, because they will:

| Failure | Behaviour |
|---|---|
| OpenCode server dies mid-turn | Five consecutive failed polls (~10s) and the turn is abandoned with an explanation, instead of silence for two hours |
| One flaky poll | Ignored; a single failure never aborts a healthy turn |
| Server restarts and loses the session | A new session is created and your message is resent, with an explanation |
| Bridge restarts mid-turn | The in-flight turn is on disk; on boot you are told, and whether the job survived |
| Telegram refuses a message edit | The final reply is sent as a new message instead - the answer is never dropped |
| Telegram throttles edits | Edit spacing widens automatically for the rest of the window |
| One chat is busy | Other chats keep working; a message to a busy chat is queued (up to 5) and says so |
| The bridge restarts | Approval mode, per-action rules, the guard's deny-list, cost alerts, per-chat preferences and the Telegram update offset all reload from disk |
| `/sh` runs a runaway command | It runs on its own thread with a 60s deadline; the chat stays responsive and `/abort` keeps working |
| Disk fills with screenshots | Cached attachments are pruned hourly (`ATTACHMENT_MAX_AGE_HOURS`) |
| SIGTERM | State is flushed and the process exits immediately rather than waiting to be killed |
| Poll thread hangs while systemd still says "active" | The heartbeat watchdog (below) restarts the unit within a minute; a Telegram outage never trips it |

`/doctor` checks all of this on demand.

Scheduled work is not a separate path: a due prompt joins the same queue a
message would, so it streams, can be `/abort`ed, and asks for permissions
exactly like everything else.

### The heartbeat watchdog

`Restart=always` recovers a bridge that dies. It cannot see a bridge that
*hangs* - no exception, no exit, just a poll thread wedged while systemd keeps
saying the unit is `active`. The bridge stamps `.bridge-heartbeat` once per
poll iteration (before the long poll, so a Telegram outage with failing calls
still beats the heart), and a one-minute systemd user timer runs
`scripts/heartbeat_watchdog.py`:

- **stale stamp under an active unit** → an optional webhook alert, then a
  restart. Set `WATCHDOG_ALERT_URL` in `.env` (the watchdog unit reads it)
  to push the alert anywhere that takes a JSON `POST`; unset means restart
  silently.
- **a boot that never wrote one stamp** → restart too (a wedge before the
  first poll); up to three minutes (the grace) a fresh boot warms quietly.
- **unit not running** → nothing. A stopped bridge stays stopped by design;
  the watchdog never resurrects it.

Manual smoke test at any time:

```bash
python3 scripts/heartbeat_watchdog.py .bridge-heartbeat
journalctl --user-unit opencode-watchdog.service -n 5
```

Retire it with `systemctl --user disable --now opencode-watchdog.timer`
(the repo units live in `systemd/`; `install.sh` sets the pair up).

### Proof a reaction arrived

Reaction routing was once physically dead in production, so every arriving
`message_reaction` update logs one receipt line. Test it by reacting to a
permission card, then:

```bash
grep "Reaction update" bridge.log
```

## Live tests

The unit suite runs against a fake API, and a fake will happily accept a wrong
content type or an ignored query parameter - which is precisely how three real
defects survived a green test run. `tests/test_live.py` checks the contract the
bridge actually depends on, and is skipped by default:

```bash
python3 -m pytest -m live
```

It covers prompt and streaming round-trips, the event stream delivering text
deltas, `/api/shell` completion semantics, concurrent sessions, aborting a
turn, and the known server limitations (`/api/shell` reaping long commands,
fs endpoints ignoring `directory`). Tested versions live in
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

## Security

This bridge is a remote shell. Treat these as non-negotiable:

- **`TELEGRAM_CHAT_ID` is mandatory.** The bridge refuses to start without it.
  Without the allow-list, anyone who finds your bot token gets code execution.
  Every update type is checked: the chat *and* the sender must be on the
  allow-list. In a private chat both are the same id; to use a group, list the
  group id and each authorized user id — being a member is not enough.
- **`ALLOWED_ROOTS` is optional but recommended.** Empty means sessions may
  target any directory on the machine, mirroring your terminal; the bridge
  warns about that at startup and during install. Set it to confine the
  bridge. It is enforced client-side on every file command, including
  symlinks; the server confines none of them.
- **`/sh` runs arbitrary commands.** Same blast radius as your own account.
  It is user-initiated and does not pass through the approval pipeline, but
  its command is checked against the guard before it starts. Remove a rule
  with `/guard rm` if you need to run something it blocks.
- **`/read` and `/write` refuse credential paths.** Even when
  `ALLOWED_ROOTS` is empty, known secrets and control files - `.env`,
  `.ssh`, `.git`, shell startup files, systemd user units, the bridge's own
  state and logs - are blocked. This is an accident guard, not a sandbox:
  `/sh` and the agent still run as the same user. Use the OS boundary in
  [docs/HARDENING.md](docs/HARDENING.md) for real containment.
- **`/auto all` is guarded, not sandboxed.** A deny-list of destructive
  commands (`rm -rf`, `mkfs`, `curl | sh`, `sudo`, `git push --force`, ...) is
  force-rejected regardless of the approval mode. `/guard` shows and edits it.
  The list is best-effort: regexes cannot cover every shell construction, and
  indirection such as `base64`, shell aliases or a helper binary can evade any
  pattern. The guard prevents accidents, not a determined attack.
- **Attachments are cached on disk.** Files you send are copied to
  `.attachments` beside the state file (owner-only `0700`/`0600`) and pruned
  after `ATTACHMENT_MAX_AGE_HOURS` (24 by default).
- **Keep secrets in the environment.** Don't hardcode the bot token, and don't
  paste `.env` into the chat the bot is serving. Keep `.env` at mode `0600`;
  startup warns if it is group- or world-readable.
- **Known bot tokens and the OpenCode password are redacted from replies
  and logs.** The password is filtered only when it is at least eight
  characters. This covers the bridge's own credentials, not every possible
  secret an agent might print; treat chat output as public.
- **Prompt previews are off by default.** `LOG_PROMPT_CHARS` is `0`, so the
  journal records the chat id and message length but not the text. Set it to a
  positive number to log that many leading characters for debugging.
- **Telegram is not private.** Bot tokens leak through logs, screenshots, and
  compromised devices.

For a threat model, a hardened configuration checklist and the limits of the
application-level controls, see [SECURITY.md](SECURITY.md). The units can be
sandboxed as well: `bash install.sh --harden-bridge --harden-server` renders
the OS-level boundaries described in
[docs/HARDENING.md](docs/HARDENING.md) — the bridge one (token-holding
process) applies with any configuration. The strongest boundary is identity:
[docs/HARDENING.md](docs/HARDENING.md) carries a migration under a dedicated
OS user (`scripts/migrate-dedicated-user.sh`).

## Requirements

- **Linux with systemd.** The installer generates systemd *user* units and a
  watchdog timer on it, and the watchdog needs systemd's timer units. Tested
  only on Linux: CI runs on Ubuntu, and the reference deployment is
  Ubuntu 24.04. macOS and Windows are untested.
- Python 3.10+ and `requests`
- An OpenCode server reachable at `OPENCODE_URL`
- A server password: `opencode service set password "<secret>"`

Tested versions are recorded in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

## Development

```bash
python3 -m pytest tests -q        # the offline suite
python3 -m pytest -m live          # contract checks against a real server
python3 -m ruff check .
mypy                               # the type gate (pipx install mypy)
bandit -c pyproject.toml -r bridge.py bridgekit scripts   # the security scan
```

CI runs the gates on Python 3.12 and the offline suite on 3.10–3.14, plus
shellcheck, a full-history secret scan, and CodeQL. A self-hosted live job is
available but disabled by default; see [docs/LIVE_TESTING.md](docs/LIVE_TESTING.md).

The pre-commit hook runs all of these: style, types, the security scan, and
the offline suite. `mypy`/`bandit` skip themselves with a note if they are
not installed, so the hook degrades gracefully rather than failing.

Every commit is guarded: `core.hooksPath` points at the tracked `hooks/`
directory, and the pre-commit hook runs ruff, mypy, bandit and the offline
suite. A red gate cannot land; fix the gate or the code instead of bypassing
it. The hook travels with the repo but not with git's default wiring - arm it
in a fresh clone with:

```bash
git config core.hooksPath hooks
```

Layout:

| File | Role |
|---|---|
| `bridge.py` | Entry point: lifecycle, polling, health, and `main()` |
| `bridgekit/updates.py` | Update ingress, authorization, reactions, attachments |
| `bridgekit/callbacks.py` | Inline-button callback registration |
| `bridgekit/turns.py` | Turn worker, streaming, recovery, and delivery |
| `bridgekit/watchers.py` | Permission watcher and scheduler |
| `bridgekit/config.py` | Environment loading, directory scoping |
| `bridgekit/state.py` | Per-chat session persistence and preferences |
| `bridgekit/api.py` | OpenCode HTTP client |
| `bridgekit/events.py` | SSE client for `/api/event` |
| `bridgekit/progress.py` | Live turn rendering |
| `bridgekit/policy.py` | Approval modes, per-action rules, and the command guard |
| `tests/test_hardening.py` | Defects found in the production audit |
| `tests/test_audit_*.py` | Regressions from the independent review |
| `bridgekit/attachments.py` | Photo, document, and voice handling |
| `bridgekit/keyboards.py` | Inline keyboards and callback routing |
| `bridgekit/localfs.py` | Local file access for /ls /read /find /write |
| `systemd/` | Unit templates - install.sh renders them into your user manager |
| `install.sh`, `uninstall.sh` | Install or remove the service |
| `tests/test_live.py` | Opt-in contract checks against a real server |
| `bridgekit/telegram.py` | Bot API client, chunking, edit throttling |
| `bridgekit/commands.py` | Command registry, context, dispatcher, help |
| `bridgekit/command_handlers/` | Command implementations grouped by concern |
| `bridgekit/formatting.py` | Render helpers |
| `bridgekit/schedule.py` | Recurring schedules |
| `bridgekit/selftest.py` | End-to-end checks behind `/selftest` |

Keep backups of state and local configuration outside the repository, and make
sure they are not group- or world-readable.

Adding a command is one decorated function in the relevant
`bridgekit/command_handlers/` module:

```python
from .. import commands as core


@core.command("ping", "Check the server", group="Help")
def cmd_ping(ctx: core.Context) -> str:
    return ctx.api.info().get("version", "?")
```

## Troubleshooting

**"Conflict: another instance is polling this bot token"** — something else is
calling `getUpdates` with the same token. Stop the other process.

**"OpenCode returned an error: 401"** — wrong password. Check `OPENCODE_PASSWORD`
or `~/.config/opencode/service.json`.

**"Could not reach OpenCode"** — check `opencode service status` and confirm the
URL and port match. It binds to `127.0.0.1` only by default, which is correct:
the bridge runs on the same machine.

**Replies never arrive** — the turn may still be running. The hard cap is 2h by
default and a heads-up fires after 4 minutes; `/abort` stops it immediately.
`/health` shows whether the event stream is connected and whether it is
reconnecting.

**"The bridge restarted Nm into a turn"** — the bridge process restarted while a
job was running. The message also says whether the job is still alive on the
server; resend your prompt to continue.

**A message seems to have cut my job short** — that is `steer` delivery. Check
`/delivery`; the default is `queue`.

**It still asks for things I thought I allowed** — rules beat the mode, not the
other way round. Check `/policy` for the action in the request, and `/auto` for
the mode underneath.

**The live message never updates** — set `STREAM_ENABLED=false` to fall back to
send-when-done, and check `/health` for stream reconnects.

**The final answer arrives late or not at all** — `/health` shows the edit
counters. Telegram allows roughly 30 message edits a minute; if the live message
is being edited faster than that the final edit can be refused, in which case
the bridge falls back to sending the answer as a new message. Widen
`EDIT_INTERVAL` (default 2s) if `throttled` keeps climbing.

**Wrong agent replies** — the session inherits OpenCode's configured default
model and agent. Change it per conversation with `/model` and `/agent`.

**The conversation disappeared** — `/sessions` lists what the server still has.
`/use` binds the chat to any of them.

## Related tools

| Tool | What it does |
| --- | --- |
| [EnvAudit](https://github.com/hix-pro/env-audit) | find missing, undocumented, and unused environment variables |
| [Migration Guard](https://github.com/hix-pro/migration-guard) | statically catch dangerous SQL migrations before they run |
| [API Watch](https://github.com/hix-pro/api-watch) | catch breaking changes in your OpenAPI and JSON contracts |
| [RepoRace](https://github.com/hix-pro/reporace) | see which GitHub repositories are accelerating |
| [TypeShift](https://github.com/hix-pro/typeshift) | decide the semver bump from a TypeScript declaration diff |
| [ActionLens](https://github.com/hix-pro/action-lens) | see what a GitHub Actions workflow really does, before it hits main |
| [ComposeMap](https://github.com/hix-pro/compose-map) | understand your Docker Compose stack and its production risks at a glance |
---

_Part of the **[hix tools](https://github.com/hix-pro#readme)** suite — local-first, no backend, CI-ready._

**Deep dive:** [Remote-controlling OpenCode from Telegram](https://hix-pro.github.io/blog/posts/06-opencode-telegram-remote-control.html)
