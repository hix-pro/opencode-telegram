#!/usr/bin/env bash
# Remove the bridge service.
#
# Keeps your .env and state file by default - they hold your credentials,
# schedules and permission rules, and this script should not delete them
# because you asked for the service to go away. Pass --purge to delete
# everything, including backups of the OpenCode server data, and
# --purge-server (in any position) to also remove the OpenCode server
# service itself.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME=opencode-telegram-bridge.service
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE=0
PURGE_SERVER=0
red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --purge-server) PURGE_SERVER=1 ;;
        -h|--help)
            echo "Usage: bash uninstall.sh [--purge] [--purge-server]"
            echo
            echo "  --purge          also delete .env, state, logs and attachments"
            echo "  --purge-server   also remove the OpenCode server service"
            echo
            echo "Flags may be given in any order."
            exit 0
            ;;
        *)
            red "unknown option: $arg"
            echo "Try: bash uninstall.sh --help"
            exit 1
            ;;
    esac
done

echo "Uninstalling the OpenCode Telegram bridge"

if systemctl --user is-active --quiet "$UNIT_NAME"; then
    systemctl --user stop "$UNIT_NAME"
    info "service stopped"
fi

# The watchdog depends on nothing, but it would keep scanning for a bridge
# that no longer exists - take it with the service.
if systemctl --user is-active --quiet opencode-watchdog.timer 2>/dev/null; then
    systemctl --user disable --now opencode-watchdog.timer >/dev/null 2>&1 || true
    info "watchdog timer disabled"
fi
rm -f "$UNIT_DIR/opencode-watchdog.service" "$UNIT_DIR/opencode-watchdog.timer"

systemctl --user disable "$UNIT_NAME" >/dev/null 2>&1 || true
rm -f "$UNIT_DIR/$UNIT_NAME"
# Drop-ins rendered by `install.sh --harden-bridge` / `--harden-server`;
# the server itself stays.
rm -f "$UNIT_DIR/opencode-server.service.d/50-hardening.conf"
rmdir "$UNIT_DIR/opencode-server.service.d" 2>/dev/null || true
rm -f "$UNIT_DIR/opencode-telegram-bridge.service.d/50-hardening.conf"
rmdir "$UNIT_DIR/opencode-telegram-bridge.service.d" 2>/dev/null || true
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

# The source tree and the OpenCode server itself are left alone unless
# --purge-server was passed. Removing the server is a separate, deliberate
# act - you truly mean it.
if [ "$PURGE_SERVER" = "1" ]; then
    systemctl --user disable --now opencode-server.service >/dev/null 2>&1 || true
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/opencode-server.service"
    systemctl --user daemon-reload
    info "OpenCode server service also removed"
fi

if [ "$PURGE_SERVER" = "1" ]; then
    green "Done. Your sessions were left untouched."
else
    green "Done. The OpenCode server and your sessions were left untouched."
fi