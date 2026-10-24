# Configuration reference

The bridge reads its configuration from the process environment. The systemd
unit loads `.env` with `EnvironmentFile=`; when running in the foreground,
source it yourself:

```bash
set -a && . ./.env && set +a
python3 bridge.py
```

`.env` must be mode `0600` and must never be committed. The bridge warns at
startup if it is readable by group or others. Values are read once at startup;
most changes need a restart. Per-chat preferences changed with `/auto`,
`/policy`, `/delivery`, `/watch`, and `/alert` are stored in the state file and
take effect immediately.

## Required

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather). This is also the value redacted from logs and replies. |
| `TELEGRAM_CHAT_ID` | Comma-separated allow-list of chat ids. In a private chat this is your user id. To use a group, list the group id **and** each authorized user id. The bridge refuses to start without it. |

`OPENCODE_PASSWORD` is optional: if unset, it is read from
`~/.config/opencode/service.json`. Prefer setting it in `.env` if you do not
want the bridge reading that file.

## Connection

| Variable | Default | Meaning |
|---|---|---|
| `OPENCODE_URL` | `http://127.0.0.1:49374` | Base URL of the OpenCode server. A trailing slash is removed. |
| `PROJECT_DIR` | the directory `bridge.py` starts in | Default working directory for a new session. When `ALLOWED_ROOTS` is set, this directory must be inside one of the roots. |
| `STATE_FILE` | `<checkout>/.state.json` | Sessions, per-chat preferences, policy rules, macros, and schedules. Written atomically with mode `0600`; its parent is created if needed. |
| `PROMPT_TIMEOUT` | `7200` | Hard cap on one turn, in seconds (2 hours). |
| `HEADS_UP_AFTER` | `240` | Send a "still working" nudge after this many seconds. |
| `PROMPT_DELIVERY` | `queue` | `queue` holds a message sent during a turn until the turn ends. `steer` sends it to the running turn and cancels that turn. |

## Live streaming

| Variable | Default | Meaning |
|---|---|---|
| `STREAM_ENABLED` | `true` | Edit one message live as text and tool activity arrive. `false` sends only the final reply. |
| `EDIT_INTERVAL` | `2.0` | Seconds between edits of the live message. Telegram also enforces its own floor and `retry_after` windows. |
| `POLL_MESSAGES` | `2.0` | How often the fallback message poll refreshes tool activity. |
| `EDIT_RESEND` | `false` | When `true`, editing a message you already sent queues its new text as a fresh prompt. Leave off to prevent an accidental edit from repeating a side effect. |

## Permissions

| Variable | Default | Meaning |
|---|---|---|
| `APPROVAL_MODE` | `ask` | Initial mode: `ask`, `read` (auto-approve reads), or `all`. Per-chat `/auto` overrides it and is persisted. |
| `AUTO_DENY` | `true` | Load the built-in destructive-command deny-list. `/guard` edits the live copy; see [Security](../README.md#security). |
| `PERMISSION_POLL_INTERVAL` | `5.0` | Seconds between permission-request polls. |

## Scope and guard rails

| Variable | Default | Meaning |
|---|---|---|
| `ALLOWED_ROOTS` | empty | Comma-separated directories a session may target. Empty means **unrestricted**, matching `opencode <directory>` as the same user; the bridge warns at startup. Set it to confine sessions. Credential paths stay blocked even when this is empty. |
| `ATTACHMENT_MAX_AGE_HOURS` | `24.0` | Delete cached inbound attachments older than this. |
| `COST_ALERT` | `0.0` | Warn when a session passes this many dollars. `0` disables. |
| `MAX_OUTPUT` | `3500` | Maximum characters per reply before head/tail truncation. |
| `MAX_CHUNK` | `4096` | Telegram's hard message limit. Values above 4096 are clamped. |

`ALLOWED_ROOTS` is checked after symlink resolution. The OpenCode server does
not enforce it, so the bridge checks every local file command itself. The
server and the agent are separate processes: an agent with its own filesystem
access is still bound by the OS, not by this list. See
[Hardening](HARDENING.md) for the systemd boundary.

## Logging

| Variable | Default | Meaning |
|---|---|---|
| `LOG_FILE` | `<checkout>/bridge.log` | Rotating log file. Its parent is created and the file is chmodded to `0600` at startup. |
| `LOG_MAX_BYTES` | `2097152` | Size of each log file (2 MB). |
| `LOG_BACKUPS` | `3` | Number of rotated files to keep (`bridge.log.1` ...). |
| `LOG_PROMPT_CHARS` | `0` | Characters of each user prompt written to the journal/log. `0` logs no prompt text; set a positive value only when you need a preview. |

The log is useful for diagnosing a failed turn; it is also a privacy surface.
Keep `LOG_PROMPT_CHARS` at `0` on a shared or less-trusted machine. The bot
token and an OpenCode password of at least eight characters are redacted from
log messages and outbound replies, but that is a narrow filter, not a guarantee
for every secret an agent may print.

## Voice notes

| Variable | Default | Meaning |
|---|---|---|
| `STT_COMMAND` | empty | Command that reads an audio file and prints a transcript. `{file}` is replaced with the downloaded path (shell-quoted). The command itself is trusted configuration, not user input. |

Example:

```dotenv
STT_COMMAND=whisper -m base {file}
```

Without a command, the bot says transcription is not configured instead of
silently ignoring the voice note.

## Parsing and precedence

- `Config.from_env()` reads only the process environment. It does not parse
  `.env` itself; systemd does that for the service, and the foreground example
  above does it for a shell.
- A `#` preceded by whitespace starts an inline comment, so a copied line such
  as `PROMPT_TIMEOUT=7200  # two hours` works. A `#` inside a value with no
  preceding whitespace is preserved.
- Booleans accept `1/0`, `true/false`, `yes/no`, and `on/off`, case-insensitively.
- Paths expand `~`; relative paths are resolved against the process working
  directory.
- Invalid numbers, non-positive intervals, an empty required value, or a
  `PROJECT_DIR` outside `ALLOWED_ROOTS` stop startup with a message rather than
  failing later.
