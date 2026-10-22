# OpenCode Telegram bridge

Full remote control of a local OpenCode server from Telegram: live streaming of
what the agent is doing, the whole command surface the TUI has, session
switching, model and agent switching, and permission approvals from your phone.

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

Four threads, so nothing the agent does can block the chat:

| Thread | Job |
|---|---|
| main | long-polls Telegram, answers commands inline, handles button callbacks |
| worker | one turn at a time; sends the prompt and edits the reply in place |
| events | one SSE connection to `/api/event` feeding live text deltas |
| watcher | answers permission requests per policy, otherwise pings with buttons |

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
$EDITOR .env          # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

**4. Install and run.**

```bash
./install.sh
```

That checks your prerequisites, installs the systemd unit, and starts the
service. It is safe to re-run after an upgrade and never overwrites your `.env`
or your state file.

To run it in the foreground instead:

```bash
set -a && . ./.env && set +a
python3 bridge.py
```

**5. Check it.**

Send `/doctor` to your bot. It reports the server, session, policy, event
stream, permission watcher and scheduler in one message.

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

Two logs are kept. `bridge.log` rotates at 2 MB and keeps three backups; it
survives a reboot, which the journal on this machine does not.

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
| `/stats` | Token and cost usage per session |
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
| `/sh <command>` | Run a shell command (see the limit below) |

`/ls`, `/read`, `/find` and `/write` work on the local filesystem rather than the
server's HTTP API, which ignores the requested directory and resolves everything
against its own working directory. They are scoped to the working directory and
confined by `ALLOWED_ROOTS`.

**`/sh` replies in two messages.** The bridge acks immediately - "Running $ ..."
- and the output arrives as a second message when it finishes. The dispatcher
never waits for the command, so buttons and commands from any other chat work
while it runs. The OpenCode server still reaps a shell roughly a second after
it starts, so only commands that finish immediately return output. For
anything longer, send it as a message: the agent runs commands itself with no
such limit and streams the output as it goes.
| `/git` | Working tree status |
| `/diff` | Diff for this directory |
| `/worktrees` | Git worktrees |

### Policy and guard rails

| Command | Effect |
|---|---|
| `/auto [ask\|read\|all]` | Auto-approval mode |
| `/policy [allow\|ask\|always\|deny <action>]` | Per-action auto-approval |
| `/policy clear <action>` / `/policy reset` | Remove one rule / all rules |
| `/guard` | Show denied command patterns |
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
| 👍 | Approve **every** pending permission request (once each) |
| 👎 or 🛑 | Abort the running turn |

Only allow-listed people in allow-listed chats count, so a shared chat is not
opened to whoever finds the bot. Only a newly *added* reaction acts: removing
an emoji never repeats anything, and any other emoji is ignored.

Reacting **on a pending permission card** is scoped to that card: 👍 approves
*just that request*, 👎/🛑 rejects it. On any other message the reactions keep
their chat-wide meaning above.

### Editing a sent message

Edit a message you sent (fix a typo, refine the ask) and the new wording is
resent as a fresh prompt - the chat history doubles as the queue. The
bridge's own message edits never trigger this, and editing a slash command
does nothing: it already ran, and re-running one because its wording changed
is a surprise, not a service.

### Help

| Command | Effect |
|---|---|
| `/status` | Server, session, policy, and bridge state |
| `/health` | Event stream, edit reliability, turn counts, errors, uptime |
| `/doctor` | Full self-check: config, server, session, disk, threads |
| `/selftest` | End-to-end checks against the **live** server |
| `/help` | Full command list |

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

`/doctor` checks all of this on demand.

Scheduled work is not a separate path: a due prompt joins the same queue a
message would, so it streams, can be `/abort`ed, and asks for permissions
exactly like everything else.

## Live tests

The unit suite runs against a fake API, and a fake will happily accept a wrong
content type or an ignored query parameter - which is precisely how three real
defects survived a green test run. `tests/test_live.py` checks the contract the
bridge actually depends on, and is skipped by default:

```bash
python3 -m pytest -m live
```

It covers prompt and streaming round-trips, the event stream delivering text
deltas, `/api/shell` completion semantics, and the known server limitations
(`/api/shell` reaping long commands, fs endpoints ignoring `directory`).

## Security

This bridge is a remote shell. Treat these as non-negotiable:

- **`TELEGRAM_CHAT_ID` is mandatory.** The bridge refuses to start without it.
  Without the allow-list, anyone who finds your bot token gets code execution.
- **`ALLOWED_ROOTS` is optional but recommended.** Empty means sessions may
  target any directory on the machine, mirroring your terminal. Set it to
  confine the bridge. It is enforced client-side on every file command; the
  server confines none of them.
- **`/sh` runs arbitrary commands.** Same blast radius as your own account.
- **`/auto all` is guarded.** A deny-list of destructive commands
  (`rm -rf`, `mkfs`, `curl | sh`, `sudo`, `git push --force`, ...) is force-rejected
  regardless of the approval mode. `/guard` shows and edits it. The guard is the
  reason `all` mode is offered at all.
- **Keep secrets in the environment.** Don't hardcode the bot token, and don't
  paste `.env` into the chat the bot is serving.
- **Your messages are logged.** The first `LOG_PROMPT_CHARS` characters of each
  prompt go to the journal for debugging. Set it to `0` to log nothing.
- **Telegram is not private.** Bot tokens leak through logs, screenshots, and
  compromised devices.

## Requirements

- Python 3.10+ and `requests`
- An OpenCode server reachable at `OPENCODE_URL`
- A server password: `opencode service set password "<secret>"`

## Development

```bash
python3 -m pytest tests -q        # the offline suite, ~550 tests
python3 -m pytest -m live          # 10 checks against a real server
python3 -m ruff check .
```

Every commit is guarded: `core.hooksPath` points at the tracked `hooks/`
directory, and the pre-commit hook runs ruff plus the offline suite. A red
suite cannot land; bypass in emergencies with `git commit --no-verify`.

Layout:

| File | Role |
|---|---|
| `bridge.py` | Entry point: threads, polling, dispatch, turn pumping |
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
| `systemd/` | The service definition, so a rebuild is reproducible |
| `install.sh`, `uninstall.sh` | Install or remove the service |
| `tests/test_live.py` | Opt-in contract checks against a real server |
| `bridgekit/telegram.py` | Bot API client, chunking, edit throttling |
| `bridgekit/commands.py` | Command registry and handlers |
| `bridgekit/formatting.py` | Render helpers |
| `bridgekit/schedule.py` | Recurring schedules |
| `bridgekit/selftest.py` | End-to-end checks behind `/selftest` |
| `backup-essential.sh` | Copies credentials, envs and source to `~/backups/essential` |

Legacy snapshots (credential copies included) live outside the repository at
`~/backups/legacy-bridge-snapshots/`, where a `git clean` cannot touch them.

Adding a command is one decorated function in `commands.py`:

```python
@command("ping", "Check the server", group="Help")
def cmd_ping(ctx: Context) -> str:
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