# Hardening the deployment

The bridge has two security layers:

1. **Application authorization** — who may command the bot (`TELEGRAM_CHAT_ID`,
   chat + sender checks, `ALLOWED_ROOTS`, approval policy).
2. **OS sandboxing** — what the bridge and the OpenCode server can touch even
   if the first layer is bypassed or the model is compromised.

This document covers the second layer. It is not a substitute for the first.

## Why both units matter

`/sh`, the agent's tools and generated commands run through the **OpenCode
server**, not the bridge. Hardening only the bridge protects `/ls`, `/read`,
`/write` and `/find` (which use `localfs` directly) but leaves the execution
surface unrestricted. Both units need a boundary.

## What the units enforce

`install.sh` renders the bridge and watchdog units from `systemd/*.in`. The
bridge's process-level sandbox and the watchdog's are installed
automatically; the filesystem boundaries for both working units come from
the `--harden-bridge` / `--harden-server` drop-ins.

| Control | Effect |
|---|---|
| `NoNewPrivileges=yes` | no setuid/setgid escalation |
| `CapabilityBoundingSet=` | drops all Linux capabilities |
| `ProtectSystem=strict` | `/usr`, `/etc`, `/boot`, `/var` (except temp) read-only |
| `ProtectHome=read-only` | home is read-only; `ReadWritePaths` carves out what each unit needs |
| `ReadWritePaths=` | bridge: its repo dir and state/log parents only; server: checkout, state/log parents and every `ALLOWED_ROOTS` entry |
| `PrivateTmp=yes` (bridge) | private `/tmp`; the server keeps shared temp for tooling |
| `ProtectKernel*`, `ProtectClock`, `ProtectHostname` | kernel and host identity surfaces closed |
| `RestrictSUIDSGID`, `RestrictRealtime`, `LockPersonality` | common escalation paths removed |
| `RestrictAddressFamilies` | Unix/IPv4/IPv6 plus netlink on both units |
| `SystemCallFilter=@system-service` | broad but non-administrative syscall set |
| `UMask=0077` | new files are owner-only by default |

Measured on the reference deployment with `systemd-analyze security --user`
(both drop-ins applied):

| Unit | Before | After |
|---|---|---|
| `opencode-telegram-bridge.service` | 9.8 UNSAFE | 3.3 OK |
| `opencode-server.service` | unhardened | 3.5 OK |

Scores depend on the systemd version; the directives matter more than the
number.

## Install

```bash
# bridge + watchdog (always)
bash install.sh

# the bridge process itself (safe with any ALLOWED_ROOTS)
bash install.sh --harden-bridge

# the execution surface too (refuses to run with an empty ALLOWED_ROOTS)
bash install.sh --harden-server
```

`--harden-bridge` writes
`~/.config/systemd/user/opencode-telegram-bridge.service.d/50-hardening.conf`
and applies on the bridge's own restart. The bridge's **process-level**
sandbox (no new privileges, kernel surfaces, private tmp, empty capability
set, syscall and socket whitelists, `AF_NETLINK` included) is rendered into
the base unit itself since v2.3.0 and needs no flag. This drop-in adds the
**filesystem** half — `ProtectSystem=strict`, `ProtectHome=read-only` and a
writable set of only the repo directory (state, logs, heartbeat,
attachments) plus the `STATE_FILE`/`LOG_FILE` parents when configured
elsewhere. It is valid for any `ALLOWED_ROOTS`: it never grants the project
roots, because nothing in the bridge writes into them.

`--harden-server` writes
`~/.config/systemd/user/opencode-server.service.d/50-hardening.conf`, reloads
systemd, restarts the server and the bridge, and leaves the drop-in in place
across upgrades. It refuses to run while `ALLOWED_ROOTS` is empty, because the
server needs to write new project sessions. `uninstall.sh` removes both
drop-ins.

Verify:

```bash
systemd-analyze security --user opencode-telegram-bridge.service
systemd-analyze security --user opencode-server.service
systemctl --user status opencode-server opencode-telegram-bridge
python3 -m pytest -m live          # contract suite against the real server
```

`pytest -m live` is the practical smoke test after changing a unit: it writes
files, runs shell commands, streams an event turn, starts concurrent sessions
and aborts a turn through the same endpoints the bridge uses.

## The path model, and why `ALLOWED_ROOTS` is still the boundary

For the **server** drop-in, `install.sh` reads `ALLOWED_ROOTS` from `.env` and
adds each entry to `ReadWritePaths`. If it contains `$HOME` (the reference
deployment does), the sandbox still protects system paths, capabilities and
privilege escalation, but **the home tree remains writable** — including SSH
keys, browser profiles and cloud credentials. The **bridge** drop-in never
grants the roots: however broad the roots are, its writable universe stays
the repo directory (plus the `STATE_FILE`/`LOG_FILE` parents when
configured elsewhere) and its own private tmp, which is why it is safe to
apply unconditionally.

Narrow the roots to get real containment:

```dotenv
ALLOWED_ROOTS=/home/you/work,/home/you/scratch
```

then re-run `bash install.sh --harden-server` so the writable paths are
re-derived. The bridge's `ALLOWED_ROOTS` check and the unit's
`ReadWritePaths` then describe the same tree.

Independently of `ALLOWED_ROOTS`, `/read` and `/write` refuse known credential
and control paths (`.env`, `.ssh`, `.git`, shell startup files, systemd user
units, and the bridge's own state/logs). This is an application-level accident
guard; `/sh` and the agent still run as the same user, so only the OS boundary
below protects those files from a determined caller.

The server additionally gets its own state directories
(`~/.config/opencode`, `~/.local/share/opencode`, `~/.local/state/opencode`),
its broader cache tree `~/.cache` (tool caches - pip's, npm's, browser
profiles - all live there, and agent commands legitimately use them),
`~/.npm`, plus shared `/tmp` and `/var/tmp` for builds and test artifacts.

## What this does not protect against

- **Same-user readable data.** If `ALLOWED_ROOTS=$HOME`, the sandbox cannot
  hide your SSH keys, browser data, keyring or `.env` from a compromised
  agent. A dedicated OS user is required for that boundary.
- **Check-then-use races.** The bridge resolves and validates a path, then the
  server performs the operation. A local process that can swap a symlink in
  between is outside the application boundary.
- **A malicious dependency invoked by the agent.** It runs with the server's
  permissions inside the sandbox; the sandbox limits what it can reach, not
  what it can do with reachable files.
- **The model itself.** Approval policy and the command guard remain the
  controls for what the agent is asked to do.

For high-risk use, run the bridge and the OpenCode server under a dedicated
account or in a container/VM, mount only the project tree, inject only the
credentials that turn needs, and keep no SSH agent socket or cloud
credentials in the environment.

## Rollback

Server drop-in:

```bash
rm ~/.config/systemd/user/opencode-server.service.d/50-hardening.conf
systemctl --user daemon-reload
systemctl --user restart opencode-server opencode-telegram-bridge
```

Bridge drop-in:

```bash
rm ~/.config/systemd/user/opencode-telegram-bridge.service.d/50-hardening.conf
systemctl --user daemon-reload
systemctl --user restart opencode-telegram-bridge
```

(Both `systemctl restart` calls are package-safe: the process-level sandbox
in the bridge's base unit stays regardless; only the filesystem half and its
writable set revert.)

`rmdir` the now-empty `*.service.d` directories afterwards if you want them
gone.
