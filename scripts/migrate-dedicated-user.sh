#!/usr/bin/env bash
# Migrate the bridge, the watchdog and the OpenCode server to a dedicated
# OS user.
#
# The systemd sandbox caps what a compromised process can *write*. It
# cannot stop a process running as you from *reading* what you can read:
# SSH keys, browser data, e-mail, other projects' tokens. Those surfaces
# disappear when the units stop running as your user.
#
# The script copies the deployment under the new user's home, enables
# linger, re-renders and starts the units there, verifies health, and only
# then stops your original units. They stay defined; rollback is a start
# command, documented in docs/HARDENING.md.
#
# Usage:
#   sudo bash scripts/migrate-dedicated-user.sh --check   # show the plan
#   sudo bash scripts/migrate-dedicated-user.sh --apply    # do the work
#
# Environment overrides (all optional):
#   NEW_USER   target OS user                  (default: bridge)
#   APP_DIR    where the runtime lands         (default: /home/<user>/app)
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Run this script with sudo (it creates a user and copies homes)." >&2
    exit 1
fi

if [ ! -f "systemd/opencode-telegram-bridge.service.in" ]; then
    echo "Run it from the checkout:" >&2
    echo "  sudo bash scripts/migrate-dedicated-user.sh --check" >&2
    exit 1
fi

SOURCE=$(pwd)
NEW_USER="${NEW_USER:-bridge}"
APP_DIR="${APP_DIR:-/home/$NEW_USER/app}"

OLD_OWNER=$(stat -c '%U' "$SOURCE")
OLD_UID=$(id -u "$OLD_OWNER")
OLD_HOME=$(getent passwd "$OLD_OWNER" | cut -d: -f6)

command -v rsync >/dev/null || { echo "rsync is required"; exit 1; }
command -v runuser >/dev/null || { echo "runuser is required"; exit 1; }

say() { printf '\n== %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*"; }

# ---------------------------------------------------------------- preflight
if id "$NEW_USER" >/dev/null 2>&1; then
    NEW_HOME=$(getent passwd "$NEW_USER" | cut -d: -f6)
    warn "user $NEW_USER already exists (home: $NEW_HOME)"
else
    NEW_HOME="/home/$NEW_USER"
fi
case "$APP_DIR" in
    "$NEW_HOME" | "$NEW_HOME"/*) ;;
    *) warn "APP_DIR ($APP_DIR) is outside $NEW_HOME - keep it inside the new user's home" ;;
esac
if [ "$OLD_OWNER" = "root" ]; then
    warn "SOURCE is owned by root - is this the right checkout?"
fi

OPENCODE_BIN=$(command -v opencode || true)
if [ -z "$OPENCODE_BIN" ]; then
    warn "opencode is not on this user's PATH; install it system-wide first:"
    warn "  sudo npm install --global opencode"
fi

# The server state lives in the OLD user's home: sessions, auth, config.
# /home/ROOT-style sudo surprises make \$HOME unreliable here, so the real
# owner of the checkout defines which home the trees come from.
copy_trees=(
    "$OLD_HOME/.config/opencode"
    "$OLD_HOME/.local/share/opencode"
    "$OLD_HOME/.local/state/opencode"
)

say "Plan"
echo "  new user       : $NEW_USER (home: $NEW_HOME)"
echo "  runtime target : $APP_DIR"
echo "  copied from    : $SOURCE (owner $OLD_OWNER)"
echo "  server files   : ${copy_trees[0]}"
echo "                   ${copy_trees[1]}"
echo "                   ${copy_trees[2]}"
echo "  server binary  : ${OPENCODE_BIN:-<not found; system-wide install required>}"
echo "  last step      : stop $OLD_OWNER's units only after the new ones are healthy"

if [ "${1:-}" = "--check" ]; then
    say "Dry run - nothing was changed"
    echo "  Re-run with --apply to perform the migration."
    exit 0
fi
if [ "${1:-}" != "--apply" ]; then
    echo "Pass --check or --apply." >&2
    exit 1
fi

read -r -p "Migrate now? Type 'yes' to proceed: " REPLY
[ "$REPLY" = "yes" ] || { echo "aborted"; exit 1; }

# ------------------------------------------------------------------- apply
say "Creating the user"
if ! id "$NEW_USER" >/dev/null 2>&1; then
    useradd -m -s /usr/sbin/nologin "$NEW_USER"
    echo "  created $NEW_USER (home $NEW_HOME)"
else
    echo "  user exists - reusing"
fi

say "Copying the checkout into $APP_DIR"
mkdir -p "$(dirname "$APP_DIR")"
rsync -a --exclude '.env.bak-*' "$SOURCE"/ "$APP_DIR"/
chown -R "$NEW_USER:$NEW_USER" "$APP_DIR"
chmod -R go-rwx "$APP_DIR"
[ -f "$APP_DIR/.env" ] && chmod 600 "$APP_DIR/.env"
echo "  rsync complete, ownership set on $APP_DIR"

say "Copying the OpenCode server's data trees"
for tree in "${copy_trees[@]}"; do
    if [ -d "$tree" ]; then
        target="$NEW_HOME/${tree#"$OLD_HOME/"}"
        mkdir -p "$(dirname "$target")"
        rsync -a "$tree"/ "$target"/
        chown -R "$NEW_USER:$NEW_USER" "$target"
        echo "  $tree -> $target"
    fi
done

say "Enabling linger for $NEW_USER"
loginctl enable-linger "$NEW_USER"
echo "  the user manager will run without a login session"

say "Rendering and starting the units as $NEW_USER"
RUNAS_XDG="/run/user/$(id -u "$NEW_USER")"
runuser -u "$NEW_USER" -- env XDG_RUNTIME_DIR="$RUNAS_XDG" HOME="$NEW_HOME" \
    bash "$APP_DIR/install.sh" --harden-bridge --harden-server

say "Verifying"
sleep 3
FAILED=0
for unit in opencode-server.service opencode-telegram-bridge.service; do
    if runuser -u "$NEW_USER" -- env XDG_RUNTIME_DIR="$RUNAS_XDG" \
        systemctl --user is-active --quiet "$unit"; then
        echo "  $unit active"
    else
        warn "$unit did not come up - the original units were left untouched"
        echo "  diagnostics:"
        echo "    runuser -u $NEW_USER -- env XDG_RUNTIME_DIR=$RUNAS_XDG \\"
        echo "      systemctl --user status $unit"
        FAILED=1
    fi
done
[ "$FAILED" -eq 0 ] || exit 1

say "Retiring the originals (defined, not deleted - rollback is a start)"
# Retire via the OLD owner's manager, not root's.
for unit in opencode-telegram-bridge.service opencode-watchdog.timer opencode-server.service; do
    runuser -u "$OLD_OWNER" -- env XDG_RUNTIME_DIR="/run/user/$OLD_UID" \
        systemctl --user stop "$unit" >/dev/null 2>&1 || true
    runuser -u "$OLD_OWNER" -- env XDG_RUNTIME_DIR="/run/user/$OLD_UID" \
        systemctl --user disable "$unit" >/dev/null 2>&1 || true
    echo "  $OLD_OWNER: $unit stopped and disabled"
done

say "Migration complete"
NEW_XDG="$RUNAS_XDG"
cat <<EOF

  The bot now runs as $NEW_USER.

  Post-migration checklist (docs/HARDENING.md carries the full notes):
    logs      : runuser -u $NEW_USER -- env XDG_RUNTIME_DIR=$NEW_XDG \\
                  journalctl --user-unit=opencode-telegram-bridge -f
    upgrades  : sudo -u $NEW_USER git -C $APP_DIR pull
                then re-run install.sh --harden-bridge --harden-server as $NEW_USER
    backups   : point opencode-backup.service at $APP_DIR/backup-essential.sh
                and remove the old ~/opencode-bridge alias afterwards

  Rollback (nothing was deleted; the copies stay in place):
    runuser -u $NEW_USER -- env XDG_RUNTIME_DIR=$NEW_XDG \\
        systemctl --user stop opencode-server.service opencode-telegram-bridge.service opencode-watchdog.timer
    sudo -u $OLD_OWNER env XDG_RUNTIME_DIR=/run/user/$OLD_UID \\
        systemctl --user start opencode-server.service opencode-telegram-bridge.service opencode-watchdog.timer
EOF
