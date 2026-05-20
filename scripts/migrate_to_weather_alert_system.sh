#!/usr/bin/env bash
set -euo pipefail
OLD_DIR="${OLD_DIR:-/opt/skyalert_bridge_app}"
NEW_DIR="${NEW_DIR:-/opt/WeatherAlertSystem}"
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash scripts/migrate_to_weather_alert_system.sh" >&2
  exit 1
fi
systemctl stop skyalert-bridge >/dev/null 2>&1 || true
systemctl disable skyalert-bridge >/dev/null 2>&1 || true
systemctl stop weather-alert-system >/dev/null 2>&1 || true
if [ -d "$OLD_DIR" ] && [ ! -d "$NEW_DIR" ]; then
  mv "$OLD_DIR" "$NEW_DIR"
fi
cd "$NEW_DIR"
bash ./install.sh --disable-helper-services
systemctl enable --now weather-alert-system
echo "Migrated to Weather Alert System at $NEW_DIR"
echo "Check logs with: journalctl -u weather-alert-system -f"
