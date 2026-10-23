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
    python3 - "$1" "$2" "$UNIT_REPO" "$PYTHON_BIN" << 'PY'
import sys
src, dst, repo, python = sys.argv[1:5]
out = (
    open(src).read()
    .replace("@REPO@", repo)
    .replace("@PYTHON@", python)
)
with open(dst, "w") as fh:
    fh.write(out)
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

systemctl --user daemon-reload

# The watchdog note: enable the timer right with the service.
if systemctl --user is-active --quiet "$WATCHDOG.timer"; then
    info "watchdog timer already running"
else
    systemctl --user enable --now "$WATCHDOG.timer" >/dev/null
    info "watchdog timer enabled"
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

To uninstall:  $HERE/uninstall.sh
EOF