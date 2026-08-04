#!/usr/bin/env bash
#
# Install mysql-monitor as a systemd service.
#
# Usage:
#   ./install_service.sh            # system-wide: /etc/systemd/system (needs sudo)
#   ./install_service.sh --user     # user scope: ~/.config/systemd/user (no sudo)
#
# To stop/disable later:
#   systemctl --user stop mysql-monitor && systemctl --user disable mysql-monitor
#   (or without --user if installed system-wide)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="$SCRIPT_DIR/mysql-monitor.service"
VENV_PY="$SCRIPT_DIR/.venv/bin/python3"

if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: virtualenv not found at $VENV_PY" >&2
  echo "Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ "${1:-}" == "--user" ]]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cp "$SERVICE" "$UNIT_DIR/mysql-monitor.service"
  systemctl --user daemon-reload
  systemctl --user enable mysql-monitor
  systemctl --user start mysql-monitor
  systemctl --user status mysql-monitor --no-pager | head -12
else
  sudo mkdir -p /etc/systemd/system
  sudo cp "$SERVICE" /etc/systemd/system/mysql-monitor.service
  sudo systemctl daemon-reload
  sudo systemctl enable mysql-monitor
  sudo systemctl start mysql-monitor
  sudo systemctl status mysql-monitor --no-pager | head -12
fi

echo
echo "Logs:  journalctl -u mysql-monitor -f"
