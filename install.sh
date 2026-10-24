#!/usr/bin/env bash
# Install the bridge as a user service.
#
# Idempotent: safe to re-run after an upgrade. It never touches your .env or
# your state file - those are yours, and overwriting them would drop your
# sessions, schedules and permission rules.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME=opencode-telegram-bridge.service
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }

HARDEN_SERVER=0
HARDEN_BRIDGE=0
for arg in "$@"; do
    case "$arg" in
        --harden-server) HARDEN_SERVER=1 ;;
        --harden-bridge) HARDEN_BRIDGE=1 ;;
        -h|--help)
            echo "Usage: bash install.sh [--harden-server] [--harden-bridge]"
            echo
            echo "  --harden-server  also sandbox the opencode-server unit."
            echo "                   The server executes agent commands; this"
            echo "                   limits what a compromised turn can touch."
            echo "  --harden-bridge  sandbox the bridge unit itself: the process"
            echo "                   that holds the bot token and reads Telegram's"
            echo "                   untrusted traffic can then only write its own"
            echo "                   state, logs, heartbeat, attachments and /tmp."
            exit 0
            ;;
        *)
            red "unknown option: $arg"
            exit 2
            ;;
    esac
done

echo "Installing the OpenCode Telegram bridge"

# ------------------------------------------------------------------- checks
command -v python3 >/dev/null || { red "python3 is not installed"; exit 1; }
python3 -c 'import requests' 2>/dev/null || {
    red "the requests package is missing"
    echo "  fix with: python3 -m pip install --user requests"
    exit 1
}

if [ ! -f "$HERE/.env" ]; then
    if [ -f "$HERE/.env.example" ]; then
        info "no .env found - creating one from .env.example"
        cp "$HERE/.env.example" "$HERE/.env"
        chmod 600 "$HERE/.env"
        echo
        echo "  Before starting, fill these in:"
        echo "    TELEGRAM_BOT_TOKEN   from @BotFather"
        echo "    TELEGRAM_CHAT_ID     your Telegram numeric id"
        echo "    OPENCODE_PASSWORD    from ~/.config/opencode/service.json"
        echo
    else
        red "no .env and no .env.example - cannot continue"
        exit 1
    fi
fi

# The default is unrestricted; say so loudly rather than letting a public bot
# token become a whole-home shell by accident. Applies to existing .env files
# too, so upgrades get the warning as well.
if ! grep -qE '^[[:space:]]*ALLOWED_ROOTS=.+' "$HERE/.env"; then
    echo
    red "ALLOWED_ROOTS is not set - the bridge is unrestricted."
    info "Confine it to the directories you actually need, e.g.:"
    info "  ALLOWED_ROOTS=$HOME/projects"
fi

# The unit files are rendered from the repo's templates so a checkout may
# live anywhere. But systemd's WorkingDirectory=/EnvironmentFile= take raw
# paths and cannot be quoted (only ExecStart= arguments can), so a checkout
# inside a path with spaces gets a space-free alias symlink first - the same
# trick this machine's own install needed. Paths without spaces are used raw.
UNIT_REPO="$HERE"
if [[ "$HERE" =~ [[:space:]] ]]; then
    UNIT_ALIAS="$HOME/.opencode-bridge"
    ln -sfn "$HERE" "$UNIT_ALIAS"
    UNIT_REPO="$UNIT_ALIAS"
    info "space-free unit alias: $UNIT_ALIAS -> $HERE"
fi
PYTHON_BIN="$(command -v python3)"
render_unit() {
    python3 - "$1" "$2" "$UNIT_REPO" "$PYTHON_BIN" "$HERE/.env" << 'PY'
import sys
from pathlib import Path

src, dst, repo, python, env_path = sys.argv[1:6]

values = {}
env = Path(env_path)
if env.exists():
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value


def resolved(raw):
    return str(Path(raw).expanduser().resolve())


def quoted(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rw_lines():
    paths = [resolved(repo)]
    for entry in values.get("ALLOWED_ROOTS", "").split(","):
        if entry.strip():
            paths.append(resolved(entry.strip()))
    for key in ("STATE_FILE", "LOG_FILE"):
        raw = values.get(key, "").strip()
        if raw:
            paths.append(resolved(Path(raw).expanduser().parent))
    lines = []
    for path in dict.fromkeys(paths):
        prefix = "" if Path(path).exists() else "-"
        lines.append(f"ReadWritePaths={prefix}{quoted(path)}")
    return "\n".join(lines)


# The filesystem half of the sandbox is only meaningful (and only safe to
# enable) when ALLOWED_ROOTS narrows the tree. Unrestricted installs keep the
# app-level allow-list and the non-filesystem hardening in the template.
if values.get("ALLOWED_ROOTS", "").strip():
    sandbox = "\n".join(
        ["ProtectSystem=strict", "ProtectHome=read-only", rw_lines()]
    )
else:
    sandbox = (
        "# Filesystem sandbox disabled: ALLOWED_ROOTS is empty. The application\n"
        "# allow-list is the only boundary. Set roots and re-run install.sh."
    )

out = (
    Path(src)
    .read_text()
    .replace("@REPO@", repo)
    .replace("@PYTHON@", python)
    .replace("@RW_PATHS@", rw_lines())
    .replace("@SANDBOX@", sandbox)
)
Path(dst).write_text(out)
PY
}

# The bridge's own writable set is different from the server's: it never
# writes into ALLOWED_ROOTS, so those roots are deliberately not granted.
# Only the repo directory and, if configured elsewhere, the state/log
# parents are writable; /tmp and /var/tmp are added inside the template.
render_harden() {
    python3 - "$1" "$2" "$UNIT_REPO" "$HERE/.env" << 'PY'
import sys
from pathlib import Path

src, dst, repo, env_path = sys.argv[1:5]

values = {}
env = Path(env_path)
if env.exists():
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value


def resolved(raw):
    return str(Path(raw).expanduser().resolve())


def quoted(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


paths = [resolved(repo)]
for key in ("STATE_FILE", "LOG_FILE"):
    raw = values.get(key, "").strip()
    if raw:
        paths.append(resolved(Path(raw).expanduser().parent))

lines = []
for path in dict.fromkeys(paths):
    prefix = "" if Path(path).exists() else "-"
    lines.append(f"ReadWritePaths={prefix}{quoted(path)}")

out = Path(src).read_text().replace("@RW_PATHS@", "\n".join(lines))
Path(dst).write_text(out)
PY
}

# ------------------------------------------------------------------- service
mkdir -p "$UNIT_DIR"
render_unit "$HERE/systemd/$UNIT_NAME.in" "$UNIT_DIR/$UNIT_NAME"
info "unit generated to $UNIT_DIR/$UNIT_NAME"

# The watchdog timer restarts a hung-but-still-"active" bridge (crashes are
# already covered by Restart=always). Both files are idempotent to reinstall.
WATCHDOG=opencode-watchdog
render_unit "$HERE/systemd/$WATCHDOG.service.in" "$UNIT_DIR/$WATCHDOG.service"
cp "$HERE/systemd/$WATCHDOG.timer" "$UNIT_DIR/$WATCHDOG.timer"
info "watchdog units installed to $UNIT_DIR"

# Optional, but the server is where /sh and the agent's tools actually run.
# Installing its drop-in without roots would make every project read-only,
# so refuse rather than produce a bridge that cannot work.
SERVER_DROPIN=0
if [ "$HARDEN_SERVER" = 1 ]; then
    if grep -qE '^[[:space:]]*ALLOWED_ROOTS=.+' "$HERE/.env"; then
        SERVER_DROPIN_DIR="$UNIT_DIR/opencode-server.service.d"
        mkdir -p "$SERVER_DROPIN_DIR"
        render_unit \
            "$HERE/systemd/opencode-server-hardening.conf.in" \
            "$SERVER_DROPIN_DIR/50-hardening.conf"
        SERVER_DROPIN=1
        info "server hardening drop-in installed"
    else
        red "not hardening the server: ALLOWED_ROOTS is empty"
        info "set ALLOWED_ROOTS in .env, then re-run with --harden-server"
    fi
fi

# The bridge's own sandbox works with any roots: its writable universe is
# the repo directory and shared temp only, never the projects.
if [ "$HARDEN_BRIDGE" = 1 ]; then
    BRIDGE_DROPIN_DIR="$UNIT_DIR/$UNIT_NAME.service.d"
    mkdir -p "$BRIDGE_DROPIN_DIR"
    render_harden \
        "$HERE/systemd/opencode-telegram-bridge-hardening.conf.in" \
        "$BRIDGE_DROPIN_DIR/50-hardening.conf"
    info "bridge hardening drop-in installed"
fi

systemctl --user daemon-reload

# The watchdog note: enable the timer right with the service.
if systemctl --user is-active --quiet "$WATCHDOG.timer"; then
    info "watchdog timer already running"
else
    systemctl --user enable --now "$WATCHDOG.timer" >/dev/null
    info "watchdog timer enabled"
fi

if [ "$SERVER_DROPIN" = 1 ] && systemctl --user is-active --quiet opencode-server.service; then
    systemctl --user restart opencode-server.service
    info "opencode-server restarted with hardening"
fi

if systemctl --user is-active --quiet "$UNIT_NAME"; then
    systemctl --user restart "$UNIT_NAME"
    info "service restarted"
else
    systemctl --user enable "$UNIT_NAME" >/dev/null
    systemctl --user start "$UNIT_NAME"
    info "service enabled and started"
fi

sleep 3
if systemctl --user is-active --quiet "$UNIT_NAME"; then
    green "The bridge is running."
else
    red "The service did not start. Check the logs:"
    echo "  journalctl --user-unit=$UNIT_NAME -n 30 --no-pager"
    exit 1
fi

cat <<EOF

Next steps
  1. Send /doctor to your bot to confirm it can reach OpenCode.
  2. /help lists every command.
  3. Logs:  journalctl --user-unit=$UNIT_NAME -f --no-pager
           tail -f $HERE/bridge.log
  4. Sandbox both services as well:
           bash install.sh --harden-bridge --harden-server

To uninstall:  $HERE/uninstall.sh
EOF