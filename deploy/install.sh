#!/usr/bin/env bash
# Installs the systemd service and timer that run the snapshot agent every 5 minutes.
# Run after ./setup.sh, from any directory:
#   sudo deploy/install.sh [--user NAME] [--name UNIT] [--read-all] [--no-start]
# Preview without touching the system:   deploy/install.sh --render-only DIR
#
# --user NAME   run as this user (default: the user who ran sudo). root is allowed.
# --read-all    let a non-root user read files it does not own (CAP_DAC_READ_SEARCH);
#               needed to back up system directories or other users' homes.
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$APP_DIR"

SERVICE_USER=${SUDO_USER:-}
NAME=jabs-snapshot-agent
START=1
READ_ALL=0
RENDER_ONLY=""

usage() {
    echo "Usage: sudo $0 [--user NAME] [--name UNIT] [--read-all] [--no-start]   |   $0 --render-only DIR" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --user) [[ $# -ge 2 ]] || usage; SERVICE_USER=$2; shift 2 ;;
        --name) [[ $# -ge 2 ]] || usage; NAME=$2; shift 2 ;;
        --read-all) READ_ALL=1; shift ;;
        --no-start) START=0; shift ;;
        --render-only) [[ $# -ge 2 ]] || usage; RENDER_ONLY=$2; shift 2 ;;
        *) usage ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[[ $NAME =~ ^[a-z][a-z0-9-]*$ ]] || die "unit name must be lowercase letters, digits and dashes"
[[ $APP_DIR =~ ^[A-Za-z0-9_./-]+$ ]] || die "app directory path has unsupported characters: $APP_DIR"
if [[ -z $RENDER_ONLY && -z $SERVICE_USER ]]; then
    die "cannot tell which user should run the agent; pass --user NAME"
fi
SERVICE_USER=${SERVICE_USER:-nobody}
[[ $SERVICE_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unusual user name: $SERVICE_USER"
if [[ $SERVICE_USER == root ]]; then
    echo "warning: the agent will run as root; backups can read everything and restic runs with full privileges." >&2
    READ_ALL=0   # root needs no extra capability
fi

CAPS=""
if ((READ_ALL)); then
    CAPS='AmbientCapabilities=CAP_DAC_READ_SEARCH\nCapabilityBoundingSet=CAP_DAC_READ_SEARCH'
fi

# Directories the agent writes to, as the service user sees them (data dir, local repo, restic cache).
rw_paths() {
    local listed out="" path
    if [[ -n $RENDER_ONLY ]]; then
        listed=$(JABS_HOME="$APP_DIR" "$APP_DIR/.venv/bin/python" -m app paths) \
            || die "could not work out the writable paths (is config/config.yaml valid?)"
    else
        listed=$(runuser -u "$SERVICE_USER" -- "$APP_DIR/.venv/bin/python" -m app paths) \
            || die "could not work out the writable paths (is config/config.yaml valid?)"
    fi
    while IFS= read -r path; do
        [[ -n $path ]] || continue
        [[ $path =~ ^/[A-Za-z0-9_./-]*$ ]] || die "writable path has unsupported characters: $path"
        if [[ $path == "$APP_DIR/data" ]]; then out+=" $path"; else out+=" -$path"; fi
    done <<< "$listed"
    printf '%s' "${out# }"
}

render() {  # render SRC DEST
    sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@NAME@|$NAME|g" \
        -e "s|@RW_PATHS@|$RW|g" -e "s|@CAPS@|$CAPS|g" "$1" > "$2"
}

UNITS=("$NAME.service:agent.service" "$NAME.timer:agent.timer")

if [[ -n $RENDER_ONLY ]]; then
    [[ -x $APP_DIR/.venv/bin/python ]] || die "no .venv found; run ./setup.sh first"
    RW=$(rw_paths)
    mkdir -p "$RENDER_ONLY"
    for pair in "${UNITS[@]}"; do
        render "$APP_DIR/deploy/${pair#*:}" "$RENDER_ONLY/${pair%%:*}"
    done
    echo "Rendered units into $RENDER_ONLY"
    exit 0
fi

[[ $EUID -eq 0 ]] || die "run with sudo"
id "$SERVICE_USER" >/dev/null 2>&1 || die "user $SERVICE_USER does not exist"
[[ -x $APP_DIR/.venv/bin/python ]] || die "no .venv found; run ./setup.sh first"
[[ -f $APP_DIR/config/config.yaml ]] || die "config/config.yaml missing; run ./setup.sh first"

# Files created under sudo end up root-owned and unreadable by the service user.
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER": "$APP_DIR/data"
for entry in .env config/config.yaml config/jobs; do
    [[ -e $APP_DIR/$entry ]] && chown -R "$SERVICE_USER": "$APP_DIR/$entry"
done
runuser -u "$SERVICE_USER" -- "$APP_DIR/.venv/bin/python" -m app config \
    || die "config check failed as $SERVICE_USER (fix config or file ownership)"

RW=$(rw_paths)
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
for pair in "${UNITS[@]}"; do
    render "$APP_DIR/deploy/${pair#*:}" "$tmp"
    install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/${pair%%:*}"
    echo "Installed /etc/systemd/system/${pair%%:*}"
done
systemctl daemon-reload

if ((START)); then
    systemctl enable --now "$NAME.timer"
else
    systemctl enable "$NAME.timer"
fi

# The old scheduler ran from cron; leaving that entry in place would run jobs twice.
for who in "$SERVICE_USER" root; do
    if crontab -l -u "$who" 2>/dev/null | grep -q "scheduler.py"; then
        echo "warning: $who's crontab still has a scheduler.py entry; remove it (crontab -e -u $who) now that the timer runs the agent." >&2
    fi
done

cat <<EOF

Done.
  Status:  systemctl status $NAME.timer
  Logs:    journalctl -u $NAME -f
  Timer:   systemctl list-timers $NAME.timer
  Run now: sudo systemctl start $NAME
  Jobs:    sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m app jobs
  Re-run this script after changing repo_path or restic cache settings (it sets what the agent may write).
EOF
