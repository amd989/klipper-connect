#!/usr/bin/env bash
# Install KlipperConnect on the Klipper host (Raspberry Pi etc.).
# Run as the user that runs Klipper/Moonraker, from the repo folder.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DIR="$HOME/klipper-connect-env"
CONFIG="$HOME/printer_data/config/klipper_connect.conf"
SERVICE=/etc/systemd/system/klipper-connect.service

echo "Creating virtualenv in $ENV_DIR"
python3 -m venv "$ENV_DIR"
"$ENV_DIR/bin/pip" install --upgrade pip >/dev/null
# Editable install, so `git pull` (or Moonraker's updater) picks up changes
"$ENV_DIR/bin/pip" install -e "$REPO_DIR"

if [ ! -f "$CONFIG" ]; then
    cp "$REPO_DIR/klipper_connect.conf.example" "$CONFIG"
    echo "Wrote $CONFIG - set serial_number before registering"
fi

echo "Installing systemd service"
sed -e "s|%USER%|$USER|g" -e "s|%HOME%|$HOME|g" \
    "$REPO_DIR/systemd/klipper-connect.service" | sudo tee "$SERVICE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable klipper-connect

# Let Moonraker's update manager restart the service
ASVC="$HOME/printer_data/moonraker.asvc"
if [ -f "$ASVC" ] && ! grep -qx "klipper-connect" "$ASVC"; then
    echo "klipper-connect" >> "$ASVC"
fi

cat <<EOF

Done. Next steps:
  1. Edit $CONFIG (serial_number)
  2. $ENV_DIR/bin/klipper-connect register
  3. sudo systemctl start klipper-connect
EOF
