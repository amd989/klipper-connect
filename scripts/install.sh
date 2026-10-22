#!/usr/bin/env bash
# Install KlipperConnect on the Klipper host (Raspberry Pi etc.).
# Run as the user that runs Klipper/Moonraker. Safe to run again to update.
set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [options]

  -d DIR   printer_data folder          (default: ~/printer_data)
  -c DIR   Klipper config folder        (default: <printer_data>/config)
  -e DIR   Python virtualenv location   (default: ~/klipper-connect-env)
  -m NAME  Moonraker's systemd service  (default: moonraker)
  -M       don't add KlipperConnect to Moonraker's update manager
  -h       show this help
EOF
}

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$HOME/printer_data"
CONFIG_DIR=""
ENV_DIR="$HOME/klipper-connect-env"
MOONRAKER_SERVICE="moonraker"
UPDATE_MANAGER=1
SERVICE_NAME="klipper-connect"

while getopts "d:c:e:m:Mh" opt; do
    case "$opt" in
        d) DATA_DIR="${OPTARG%/}" ;;
        c) CONFIG_DIR="${OPTARG%/}" ;;
        e) ENV_DIR="${OPTARG%/}" ;;
        m) MOONRAKER_SERVICE="$OPTARG" ;;
        M) UPDATE_MANAGER=0 ;;
        h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
CONFIG_DIR="${CONFIG_DIR:-$DATA_DIR/config}"
CONFIG="$CONFIG_DIR/klipper_connect.conf"
MOONRAKER_CONF="$CONFIG_DIR/moonraker.conf"

if [ ! -d "$CONFIG_DIR" ]; then
    echo "Config folder $CONFIG_DIR doesn't exist (use -d or -c)" >&2
    exit 1
fi

echo "==> Python environment in $ENV_DIR"
python3 -m venv "$ENV_DIR"
"$ENV_DIR/bin/pip" install -q --upgrade pip
# Editable install, so `git pull` (or Moonraker's updater) picks up changes
"$ENV_DIR/bin/pip" install -q -e "$REPO_DIR"

if [ ! -f "$CONFIG" ]; then
    cp "$REPO_DIR/klipper_connect.conf.example" "$CONFIG"
    # Point the bridge at this printer_data folder if it isn't the parent
    # of the config folder (the default the bridge assumes)
    if [ "$(dirname "$CONFIG_DIR")" != "$DATA_DIR" ]; then
        sed -i "s|^#data_dir:.*|data_dir: $DATA_DIR|" "$CONFIG"
    fi
    echo "==> Wrote $CONFIG - set serial_number before registering"
else
    echo "==> Keeping existing $CONFIG"
fi

echo "==> systemd service $SERVICE_NAME"
sed -e "s|%USER%|$USER|g" -e "s|%HOME%|$HOME|g" \
    -e "s|%ENV%|$ENV_DIR|g" -e "s|%CONFIG%|$CONFIG|g" \
    "$REPO_DIR/systemd/klipper-connect.service" |
    sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE_NAME"

# Moonraker may only restart services listed in moonraker.asvc
ASVC="$DATA_DIR/moonraker.asvc"
if [ -f "$ASVC" ] && ! grep -qx "$SERVICE_NAME" "$ASVC"; then
    echo "$SERVICE_NAME" >> "$ASVC"
    echo "==> Added $SERVICE_NAME to $ASVC"
fi

restart_moonraker=0
if [ "$UPDATE_MANAGER" = 1 ]; then
    section="[update_manager $SERVICE_NAME]"
    if [ ! -f "$MOONRAKER_CONF" ]; then
        echo "==> No $MOONRAKER_CONF, skipping the update manager entry"
    elif grep -rqsF --include='*.conf' "$section" "$CONFIG_DIR"; then
        echo "==> $section already in $CONFIG_DIR, leaving it"
    else
        origin="$(git -C "$REPO_DIR" remote get-url origin)"
        branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
        cat >> "$MOONRAKER_CONF" <<EOF

# KlipperConnect - Prusa Connect bridge (added by its install script)
$section
type: git_repo
path: $REPO_DIR
origin: $origin
primary_branch: $branch
virtualenv: $ENV_DIR
requirements: requirements.txt
managed_services: $SERVICE_NAME
EOF
        echo "==> Added $section to $MOONRAKER_CONF"
        restart_moonraker=1
    fi
fi

if [ "$restart_moonraker" = 1 ]; then
    if systemctl list-unit-files "$MOONRAKER_SERVICE.service" >/dev/null 2>&1
    then
        echo "==> Restarting $MOONRAKER_SERVICE"
        sudo systemctl restart "$MOONRAKER_SERVICE"
    else
        echo "==> Restart Moonraker to see KlipperConnect in the update panel"
    fi
fi

if [ -f "$DATA_DIR/klipper_connect/prusa_connect.ini" ]; then
    echo "==> Restarting $SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"
    echo
    echo "Done. KlipperConnect is running."
else
    cat <<EOF

Done. Next steps:
  1. Set serial_number in $CONFIG
  2. $ENV_DIR/bin/klipper-connect -c $CONFIG register
  3. sudo systemctl start $SERVICE_NAME
EOF
fi
