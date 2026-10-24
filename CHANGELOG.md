# Changelog

## v2.3.7 — 2026-10-24

A security scan flagged the bridge service as running without an OS
boundary. The deployment truth turned out more nuanced — the bridge has
carried an inline process-level sandbox in its base unit since v2.3.0 — but
the wiring behind that sandbox had three real weaknesses, and this release
closes all of them.

### Changed

- **The filesystem sandbox is no longer tied to `ALLOWED_ROOTS`.** It used
  to live in the conditional `@SANDBOX@` rendering of the base bridge unit,
  so an install without roots silently lost `ProtectSystem=strict` and
  `ProtectHome=read-only`. `bash install.sh --harden-bridge` now renders
  both directives — plus a repo-scoped `ReadWritePaths` — into
  `opencode-telegram-bridge.service.d/50-hardening.conf`, and the flag is
  valid with any configuration, including an empty one.
- **The bridge no longer receives the project roots as writable.** The old
  inline rendering added every `ALLOWED_ROOTS` entry to the bridge's
  `ReadWritePaths`, although nothing in the bridge writes into project
  directories: state, logs, heartbeat and the attachment cache all live in
  the repo, and downloads stage inside its private tmp. The token-holding
  process could nonetheless have written there. The drop-in grants only the
  repo directory and the `STATE_FILE`/`LOG_FILE` parents; pinned tests
  enforce that neither the template nor the rendered output can name the
  roots again.
- **The base unit's socket whitelist gains `AF_NETLINK`**, matching the set
  already proven on the server unit (used by `getaddrinfo` on this stack).

### Fixed

- `install.sh` wrote the `--harden-bridge` drop-in into
  `opencode-telegram-bridge.service.service.d` — a doubled suffix that
  systemd ignores, because the unit name already ends in `.service`. The
  drop-in now lands in `$UNIT_NAME.d`; `uninstall.sh` removes it, alongside
  the server drop-in.

### Documentation

- `docs/HARDENING.md`: the two-unit path model (the bridge's process-level
  sandbox lives in its base unit since v2.3.0; the drop-in is the
  filesystem half), install, verification and rollback instructions.
  README's security section points at both flags.

**Upgrade:** pull and run `bash install.sh --harden-bridge` once. Rollback
is one file: remove
`~/.config/systemd/user/opencode-telegram-bridge.service.d/50-hardening.conf`,
run `systemctl --user daemon-reload` and restart the service.

## v2.3.6 — 2026-10-24

Closing pass of the second reliability audit: the remaining unaudited scope
(`scripts/heartbeat_watchdog.py`, `bridgekit/selftest.py`,
`bridgekit/command_handlers/files.py`) plus release hygiene. The watchdog and
the files/shell handlers came out clean; `/selftest` had two small defects.

### Fixed

- **`/selftest` no longer leaks its workspace.** When no directory is supplied,
  the run created a `bridge-selftest-*` directory in `/tmp` and never removed
  it - one new directory per invocation. It is now cleaned up together with
  the session cleanup; a caller-supplied directory is left untouched.
- **The prompt round-trip detail reports true elapsed seconds.** The old
  expression (`time.time() - submitted // 1000`) mixed units, and the displayed
  duration was carried by an operator-precedence accident.

### Documentation

- README marks the "How it works" diagram as a sketch and points to the
  authoritative thread table.
- `.gitignore` groups `backup-essential.sh` with the local backups and
  snapshots instead of the runtime state and logs section.

### Deployment

- The runtime must satisfy the declared floor: the reference deployment now
  runs `requests 2.34.2` (system python previously had 2.31.0) and the bridge
  service was restarted onto this release on the same day.

## v2.3.5 — 2026-10-24

Reliability audit: every fix below comes out of a full code review and has a
regression test that failed before its fix.

### Fixed

- **A turn failure no longer takes down turn dispatch.** The turn-dispatcher
  thread is the only background loop without a watchdog, and one unguarded
  raise (a transport hiccup, a thread-limit `can't start new thread`) used to
  park every queued prompt forever while the bot still answered `/status`.
  The loop now guards its body, keeps dispatching, and logs the failure.
- **A `200` JSON body that is not an object** (a captive portal or gateway
  answering `null` or a string) used to escape as `AttributeError` from
  `getUpdates`, `sendMessage` and `getFile` and kill the calling thread.
  Those paths now raise a `TelegramError`, and only a true poll conflict
  (409) is fatal; other Telegram-level poll failures back off and retry.
- **A failed interrupt keeps its abort intent.** The abort flag used to be
  wiped by the next ordinary message even when the interrupt API call had
  failed, silently letting the turn the user tried to stop keep running.
  The flag now survives while the turn is actually busy and is cleared once
  nothing runs; the typed `/abort` shares the same path as the ⏹ button and
  the 👎 reaction (busy check, flag, pump retry) and reports failures
  instead of claiming "Interrupt sent."
- **A failed auto-approval answer is retried, not forgotten.** The watcher
  used to mark a pending request as seen even when answering it failed, so
  the agent stayed blocked until the prompt timeout. Repeated failures are
  logged and retried every tick; the chat hears about the first failure and
  then at most once a minute.
- **Pending permission requests are no longer evicted by cache churn.** The
  bounded request cache trimmed its oldest half regardless of pending
  state, which de-scoped a still-visible card: a 👍 on it could then approve
  a different request. Reconciliation now runs only after a fully-polled
  tick and drops ids the server no longer lists.
- **Two attachments sent within the same second no longer collide.** The
  cache filename carried only a seconds timestamp, so the second photo
  overwrote the file the first queued prompt still referenced. Filenames
  now embed a short uuid.
- **Lone surrogates from a quoted binary artifact** no longer turn the final
  delivery into a generic "the turn failed": they are replaced with U+FFFD
  before anything is sent, and UTF-16 length counting no longer raises.
- **`uninstall.sh` parses flags in any position.** `--purge-server` only
  worked as the second argument, so `uninstall.sh --purge-server` was
  silently ignored; unknown flags are now rejected with `--help` advice.

### Documentation

- README: `/stats` is the server's aggregate (not per-session) usage,
  `/auto` documents `off`, `/worktrees` documents `new`/`rm`, `/web` is
  listed, and the thread table counts five background threads plus the
  per-turn threads.
- `docs/HARDENING.md` now matches the rendered sandbox: it grants the whole
  `~/.cache` (shared pip/npm/browser caches) rather than `~/.cache/opencode`.

## v2.3.4 — 2026-10-23

Security hygiene and project operations.

### Added

- **The OpenCode password is now redacted from outbound replies and logs**,
  in addition to the Telegram bot token. Only passwords of eight or more
  characters are registered, so a short password cannot turn ordinary words
  into asterisks. This covers credentials the bridge already knows, not every
  secret an agent might print.
- **CodeQL analysis** on pushes, pull requests and a weekly schedule.
- **An opt-in live-test workflow** for a dedicated self-hosted runner. It is
  skipped unless the `LIVE_TESTS_ENABLED` repository variable is `true`; setup
  and the security boundary are documented in
  [docs/LIVE_TESTING.md](docs/LIVE_TESTING.md).
- Contributor documentation: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue
  forms and a pull-request checklist.

### Changed

- CI now runs the offline suite on Python 3.14 as well as 3.10–3.13.
- Documentation no longer suggests bypassing the pre-commit hook with
  `--no-verify`; a red gate must be fixed instead.

## v2.3.3 — 2026-10-23

Security hardening and the documentation gaps from the public review.

### Added

- **Credential-path guard for `/read` and `/write`.** Known secrets and
  control files (`.env`, `.ssh`, `.git`, cloud credential directories, shell
  startup files, systemd user units, and the bridge's own state/logs) are
  refused even when `ALLOWED_ROOTS` is empty. This is an accident guard, not
  a sandbox; `/sh` and the agent still run as the same user.
- **Startup warning for an unsafe `.env` mode.** The installer creates `.env`
  at `0600`, but an older copy at `0644` is now called out with the exact
  `chmod 600` command.
- **`docs/CONFIGURATION.md`** documents every environment setting, default,
  and parsing rule, and README now documents schedules, `/start`, and the
  configuration reference.

### Changed

- **Editing a sent message is opt-in.** `EDIT_RESEND` defaults to `false`, so
  an accidental edit no longer queues the new wording as a second prompt.
  Set `EDIT_RESEND=true` to restore the old behaviour.
- **The bot token is redacted before outbound `sendMessage` and
  `editMessageText` calls**, not only in logs and transport errors.
- **`LOG_PROMPT_CHARS` now defaults to `0`.** The journal records the chat id
  and message length, but not prompt text, unless an operator opts in.
- README no longer claims to expose the entire TUI command surface.

### Fixed

- `.env.example` now shows the real `EDIT_INTERVAL` default (`2.0`), accurate
  state/log paths, and the new `EDIT_RESEND` setting.
- `SECURITY.md` reported `v2.2.x` as the supported line; it now says `v2.3.x`.
- Stale comments claimed a chat-wide 👍 approved every pending request; the
  code has required one request or an explicit batch since v2.3.2.
- `.gitignore` no longer describes local backups as credential snapshots.

## v2.3.2 — 2026-10-23

Two safety fixes and documentation cleanup.

### Changed

- A chat-wide 👍 now approves a permission request only when exactly one is
  pending. With several pending it reports the count and changes nothing;
  use a card reaction or `/yes all` to approve a batch. Card-scoped reactions
  and `/yes all` are unchanged. This also removes the post-restart case where
  a stray 👍 could approve every pending request.
- `/sh` now checks the command guard before starting. A blocked command is
  refused with the matching rule and never reaches the server; remove a rule
  with `/guard rm` when the command is intentional.

### Fixed

- README command tables: `/git`, `/diff` and `/worktrees` are back in the
  Files and code table.
- README deployment notes no longer contain machine-specific journal or
  backup-path references.

## v2.3.1 — 2026-10-23

Internal refactor. No user-visible behavior, configuration, or wire-format
change.

### Changed

- `bridgekit/commands.py` is now the registry/context/dispatcher facade;
  handlers live in `bridgekit/command_handlers/` (`sessions`, `files`,
  `permissions`, `inspect`, `policy`, `operations`). It shrank from 1,697 to
  379 lines. The facade preserves `bridgekit.commands` imports and the
  `SHELL_PROBE` patch point used by tests.
- `bridge.py` now owns lifecycle and polling; update ingress, callbacks, turn
  execution and watchers moved to `bridgekit/updates.py`, `callbacks.py`,
  `turns.py` and `watchers.py`. It shrank from 1,416 to 612 lines. `Bridge`
  keeps thin wrappers so existing callbacks, tests and monkeypatches still
  dispatch through the same method names.

### Verified

- Full offline suite, 12 live tests, ruff, mypy, bandit and shellcheck.

## v2.3.0 — 2026-10-23

Containment and release confidence. v2.2.x closed the authorization gaps;
this release reduces what a compromised bridge or agent can touch, and makes
the supported surface explicit.

### Added

- **Sandboxed systemd units.** `install.sh` now renders the bridge and
  watchdog with `NoNewPrivileges`, an empty capability set,
  `ProtectSystem=strict`, `ProtectHome=read-only`, kernel/clock/hostname
  protection and a `@system-service` syscall filter. Writable paths are
  derived from the checkout, `STATE_FILE`/`LOG_FILE` and `ALLOWED_ROOTS`.
  The bridge's measured exposure fell from 9.8 to 3.3.
- **`install.sh --harden-server`.** An optional drop-in sandboxes
  `opencode-server` — the process that actually executes `/sh` and agent
  tools. The hardened unit scores 3.5. The installer refuses to install it
  without `ALLOWED_ROOTS`, which it uses to derive writable paths.
- **`docs/HARDENING.md`** explains both layers, the path model, measured
  scores, rollback, and what the sandbox does not cover.
- **`docs/COMPATIBILITY.md`** records tested bridge, Python, OpenCode and OS
  versions and how the live suite produces that table.
- Live tests for two concurrent sessions and abort-to-idle.
- CI: Python 3.10–3.13 matrix, shellcheck, full-history gitleaks scan, and
  Dependabot.
- Packaging guards for unit sandboxing and for pyproject/CHANGELOG version
  agreement.

### Changed

- **One authorization implementation.** `bridgekit/authz.py` owns the
  chat-plus-sender rule used by messages, edits, reactions and buttons. No
  behavior change.

### Notes

- With `ALLOWED_ROOTS=$HOME`, the sandbox protects system paths and
  privileges but not same-user secrets. Narrow the roots or use a dedicated
  account/container for real isolation; `docs/HARDENING.md` is explicit about
  the boundary.

## v2.2.5 — 2026-10-23

Uniform authorization and poll-loop resilience, prompted by a follow-up
security review. The threat model, hardening checklist and remaining limits
are in [SECURITY.md](SECURITY.md).

### Fixed

- **Messages trusted the chat, not the sender.** Any member of an
  allow-listed group could send prompts, run `/sh` or upload attachments.
  Every message now requires both the chat and the sender on the allow-list,
  matching edits, reactions and inline buttons. Private chats are unaffected;
  groups must list the group and each authorized user.
- **One malformed update could kill polling.** Dispatch is guarded per update:
  the failure is logged and the offset advances, so a poison update is not
  redelivered on every restart. Non-dict updates and updates without a valid
  id are skipped.
- **Check-then-use is now documented.** Application path confinement cannot
  stop a local process from swapping a symlink between validation and the
  server's file operation; `SECURITY.md` says so and recommends isolation.

### Added

- Authorization-matrix tests through the real dispatcher: an unlisted sender
  cannot send prompts, upload attachments, approve permissions or steer
  reactions, while an allow-listed sender in a listed group still works.
- Symlink-parent and symlink-loop tests for path confinement.

## v2.2.4 — 2026-10-23

Hardening from an external security review: the application-level controls now
match what the documentation claims.

### Fixed

- **Buttons checked the chat, not the sender.** In an allow-listed group any
  member could approve a permission request or abort a turn. Inline-button
  callbacks now require `from.id` on the allow-list, the same rule reactions
  and edits already used.
- **The attachment cache inherited the umask.** `.attachments` is now created
  `0700` and every download is chmodded `0600`, so copies of sent files are
  not world-readable.
- **Unrestricted mode was silent.** Starting with an empty `ALLOWED_ROOTS` now
  logs a warning, and `install.sh` warns when the `.env` has no roots set.

### Added

- Adversarial tests: a stranger in an allow-listed chat cannot press buttons,
  a symlinked directory or file cannot escape `ALLOWED_ROOTS`, and the
  attachment cache is owner-only.
- `SECURITY.md` threat model, the authorization rule for every update type, a
  hardened configuration checklist, and an explicit limitations section.

### Changed

- `README.md` security section: the deny-list is documented as defense in
  depth rather than a sandbox, and the button/reaction sender checks and
  attachment cache retention are called out.

## v2.2.3 — 2026-10-22

Release readiness: the repo becomes publishable as-is, with no behavioral
change on an existing install.

### Added

- **MIT license.** The tree was legally unshareable without one.
- **Portable units.** `install.sh` now *generates* both systemd units from
  `systemd/*.service.in` templates - any checkout location works, spaces
  included (a space-free alias symlink is created because systemd cannot
  quote `WorkingDirectory`/`EnvironmentFile`). The hardcoded
  `Requires=opencode-server.service` dependency became a soft `Wants=` so a
  foreign machine without the unit boots anyway. `uninstall.sh` removes the
  watchdog pair too.
- **Affiliation disclaimer** in the README, as the OpenCode team asks of
  independently built `opencode-*` projects.

### Fixed

- **The tests were pinned to one machine.** Six test files hardcoded
  `/home/demo`; two of them (config validation, `/use` switching) only
  passed because that path happened to exist there. Everything now runs on
  any machine: fake directories use hermetic helpers (`tmp_path`,
  `tempfile.gettempdir()`), and config tests no longer lean on a real home
  directory to satisfy existence validation.
- `.env.example` examples and the tests use neutral paths. The maintainer's
  personal backup script stays useful locally (gitignored) and out of the
  published tree.

## v2.2.2 — 2026-10-22

The ops round: two missing operational rolls. A hung bridge had nowhere to
go - `Restart=always` only sees death, not deadlock - and the reaction-
routing repair had no observable receipt. Plus the gates every future
commit now walks through.

### Added

- **Heartbeat watchdog.** The bridge stamps `.bridge-heartbeat` before every
  poll (success or failure; a Telegram outage keeps beating, so outages
  never trigger restarts). `systemd/opencode-watchdog.timer` runs
  `scripts/heartbeat_watchdog.py` every minute: a stale stamp under a unit
  that systemd calls `active` - or an up-but-silent boot past the grace
  period - gets one optional `WATCHDOG_ALERT_URL` webhook alert and a
  restart. Stopped units stay stopped. `install.sh` installs and enables the
  timer; the decision matrix is pinned by tests (5 new).
- **Reaction receipts.** Every arriving `message_reaction` logs one
  `Reaction update:` line; per-request reactions were once physically dead in
  production, so arrival stays provable (`grep "Reaction update" bridge.log`).

### Gates

- **mypy.** The shipped surface (`bridge.py`, `bridgekit`, `scripts`) checks
  clean: 0 issues in 17 files. Fixing the type lies made the runtime honest
  too (`Context.policy`/`Context.schedules` are real default factories now;
  async `/sh` raises a proper error instead of asserting under `-O`; a
  permission card without a message id logs and degrades to chat-wide
  instead of crashing the ask). Runs in the pre-commit hook, pipx-aware.
- **bandit.** Scans shipped code only - a repo-wide run just reports the
  tests' own asserts (proven: `targets=` in `[tool.bandit]` is ignored, so
  `exclude_dirs` + explicit hook paths it is). The two accepted findings
  stay suppressed inline (`# nosec` with justification comments): the
  operator-configured STT `shell=True`, and the watchdog's fixed-argv
  systemctl calls.
- The pre-commit gate is now **ruff → mypy → bandit → pytest** (583 tests);
  mypy/bandit self-skip with a note if absent.

Offline suite: 583 tests green (was 578). Live: the same 10 checks.

## v2.2.1 — 2026-10-22

The second audit round: five agents re-swept the tree for defects; every real
one found is fixed here with a regression test, carrying the offline suite
from 542 to 578 tests. No new features - only lies, leaks, and lost work.

### Fixed

- **Reactions never reached the bot.** `allowed_updates` was sent as repeated
  JSON parameters; the bot-api server reads only the first pair, so
  `message_reaction` was silently dropped and per-request reaction cards
  answered *nothing* in production. It is now one JSON-serialized parameter,
  pinned by a wire-format test against the exact bytes on the wire.
- **State corruption could crash-loop or un-acknowledge updates.**
  `save_policy` no longer wipes the update offset; wrong-type JSON blocks are
  tolerated per-field instead of killing startup; the stale `.session.json`
  mirror is gone and permissions to it tightened to `0600`; save failures
  warn instead of raising from a background thread.
- **The event stream could hand out mojibake.** SSE responses are decoded
  UTF-8 explicitly, and a connection that closes cleanly in under 30 seconds
  now counts as a drop (escalating backoff) instead of resetting the circuit
  breaker every half a minute.
- **Transient Telegram failures killed the bridge.** Polls on 429 (honoring
  `retry_after`) or 5xx now wait and retry; only 401/403/409 remain fatal.
  The offset is saved after every handled update, so a mid-batch crash no
  longer replays already-acked updates as duplicate turns.
- **Two dispatch bugs could drop or double messages.** The busy-check and
  backlog append are one locked section (a check-then-act gap used to strand
  a queued message forever), and the turn worker's finish path pops its
  registry entry before draining the backlog under the same lock.
- **Cost alerts were race-prone and permission answers died silently.** The
  alert flag is per turn (two concurrent turns used to suppress or double
  each other's alerts), and a failed permission answer now tells the user
  instead of leaving the card unanswered; the watcher also skips chats that
  have left the allow-list.
- **Polled text could overwrite stream deltas.** Stream and poll writers
  fought over the progress text; adoption is length-gated and locked at all
  three repair sites now.
- **The bot token leaked into logs and even into the chat.** Transport
  errors from `download()` (the only whose error text is posted to the chat)
  are re-raised as a user-safe TelegramError, and every catch-all Telegram
  log site formats messages through the new `Telegram.redact()`.
- **Commands behaved differently from how they read.** A macro could
  register under `/start` or `/help_` and then never run; an args-only
  macro (`{args}`) typed bare queued the command name as an agent prompt;
  `/sh` never reported its exit code; editing a menu label queued the bare
  label word as a prompt; `/alert` accepted negative thresholds; aborting an
  idle chat armed the abort flag and killed the *next* turn - often a
  scheduled job. All fixed, with the abort entry points unified.
- **The session-switch button got typed-`/use` parity**: a miss reports
  itself, the recent-list window matches the keyboard, and the target
  directory is containment-checked before switching.
- **The edit throttle now honors the window the server names.**
  `effective_interval()` used to report a fixed "twice the usual" for any
  429 while ignoring `retry_after`, and edits continued to POST inside the
  window (re-arming it). Edits now hold off until the window closes, the
  send path widens the same window, and the write-only `_penalty` field and
  never-enforced `EDIT_BURST` constant are gone.
- **A giant single line made `split_message` quadratic.** The hard split
  re-encoded each candidate prefix per step; the cut walk now carries the
  unit count, linear in the message size with identical chunk choices.
- **Docs and metadata matched reality again.** `pyproject.toml` finally says
  2.2.1, README counts the real suite, `install.sh` names
  `TELEGRAM_CHAT_ID`, and the README documents that per-request reaction
  cards degrade to chat-wide reactions after a restart.

### Known limitations (unchanged)

- STT concurrency uses `subprocess` with `shell=True` behind a fixed, quoted,
  allow-list-checked command string - accepted deliberately, since removing
  the shell would break the whisper wrapper's quoting.
- OpenCode server limitations tracked upstream: the server reaps shell
  commands after about a second, and its fs endpoints ignore the directory
  parameter; live tests fail loudly if either behavior changes.

## v2.2.0 — 2026-10-22

The UI round: everything acquired a button.

### Added

- **Persistent main menu** — an `is_persistent` reply keyboard (Status ·
  Sessions · New · Abort · Help) attaches at startup; a tap is canonicalised
  into the command it stands for, so tap and type behave identically. Any
  client can collapse it locally.
- **Session switcher** — `/sessions` renders a Switch row per session, resolved
  through the existing `use` callback, which now confirms the switch in the
  chat instead of succeeding silently.
- **Per-request reactions** — reacting on a pending permission card approves
  (👍) or rejects (👎/🛑) *just that request*. On any other message, reactions
  keep their chat-wide meaning (approve-all / abort turn).
- **Prompt macros** — `/macro name prompt` records a standing prompt in
  `.state.json`; `/name` expands it (explicit `{args}` slot or appended tail)
  and queues it like any prompt, so it streams, aborts and asks for
  permissions like everything else.

### Housekeeping

- Poll-loop tests drive `poll_loop` itself through realistic mixed batches
  (message + reaction + edit, offsets advancing and persisting between polls).
- `backup-essential.sh` writes `bridge-history.bundle` alongside the essential
  copies, so the audit history survives disk loss (verified with
  `git bundle verify`).
- README documents the pre-commit hook, macros, the menu and reaction scoping.

### Known limitations (unchanged)

- The OpenCode **server** still reaps shell commands after about a second, and
  the server fs endpoints still ignore the directory parameter; both are
  tracked by live tests that fail loudly if the server behaviour changes.

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
