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

# ------------------------------------------------------------------- service
mkdir -p "$UNIT_DIR"
cp "$HERE/systemd/$UNIT_NAME" "$UNIT_DIR/$UNIT_NAME"
info "unit installed to $UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload

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