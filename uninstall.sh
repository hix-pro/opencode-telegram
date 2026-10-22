#!/usr/bin/env bash
# Remove the bridge service.
#
# Keeps your .env and state file by default - they hold your credentials,
# schedules and permission rules, and this script should not delete them
# because you asked for the service to go away. Pass --purge to delete
# everything, including backups of the OpenCode server data.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME=opencode-telegram-bridge.service
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }

echo "Uninstalling the OpenCode Telegram bridge"

if systemctl --user is-active --quiet "$UNIT_NAME"; then
    systemctl --user stop "$UNIT_NAME"
    info "service stopped"
fi

systemctl --user disable "$UNIT_NAME" >/dev/null 2>&1 || true
rm -f "$UNIT_DIR/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
info "service unit removed"

if [ "$PURGE" = "1" ]; then
    rm -f "$HERE/.env" "$HERE/.state.json" "$HERE"/bridge.log*
    rm -rf "$HERE/.attachments"
    info "credentials, state, logs and attachments deleted"
else
    info "kept: .env, .state.json, bridge.log"
fi

# The source tree and the OpenCode server itself are left alone. Removing the
# server is a separate, deliberate act - run --purge-server if you truly mean it.
if [ "${2:-}" = "--purge-server" ]; then
    systemctl --user disable --now opencode-server.service >/dev/null 2>&1 || true
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/opencode-server.service"
    systemctl --user daemon-reload
    info "OpenCode server service also removed"
fi

green "Done. The OpenCode server and your sessions were left untouched."