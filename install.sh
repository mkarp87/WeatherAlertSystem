#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/WeatherAlertSystem}"
INSTALL_DEPS=1
ADD_DVSWITCH_REPO=1
SERVICE_USER="${SERVICE_USER:-skyalert}"
DISABLE_HELPER_SERVICES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir)
      APP_DIR="$2"
      shift
      ;;
    --no-deps)
      INSTALL_DEPS=0
      ;;
    --add-dvswitch-repo)
      ADD_DVSWITCH_REPO=1
      ;;
    --no-add-dvswitch-repo)
      ADD_DVSWITCH_REPO=0
      ;;
    --user)
      SERVICE_USER="$2"
      shift
      ;;
    --disable-helper-services)
      DISABLE_HELPER_SERVICES=1
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# Do not rely on unzip preserving executable bits. Calling through bash also
# works if the source filesystem is mounted noexec.
chmod +x "$SRC_DIR"/*.sh "$SRC_DIR/scripts"/*.sh 2>/dev/null || true

if [ "$INSTALL_DEPS" -eq 1 ]; then
  if [ "$ADD_DVSWITCH_REPO" -eq 1 ]; then
    bash "$SRC_DIR/scripts/install_dependencies.sh" --add-dvswitch-repo
  else
    bash "$SRC_DIR/scripts/install_dependencies.sh" --no-add-dvswitch-repo
  fi
fi

if [ "$DISABLE_HELPER_SERVICES" -eq 1 ]; then
  for svc in analog_bridge analog-bridge mmdvm_bridge mmdvm-bridge md380-emu; do
    systemctl disable --now "$svc" >/dev/null 2>&1 || true
  done
fi

if [ "$SRC_DIR" != "$APP_DIR" ]; then
  mkdir -p "$APP_DIR"
  rsync -a --delete --exclude '.venv' --exclude 'state' "$SRC_DIR/" "$APP_DIR/"
fi

cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip setuptools wheel
pip install --no-build-isolation -e .

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$APP_DIR/state/audio" "$APP_DIR/state/bridges" "$APP_DIR/state/control"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/state"

if [ ! -f "$APP_DIR/config.yaml" ]; then
  cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
  chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/config.yaml"
fi

cat >/etc/systemd/system/weather-alert-system.service <<EOF_SERVICE
[Unit]
Description=Weather Alert System
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/weather-alert-system -c $APP_DIR/config.yaml run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF_SERVICE

systemctl disable --now skyalert-bridge >/dev/null 2>&1 || true
systemctl daemon-reload

echo "Installed Weather Alert System in $APP_DIR"
echo "Edit $APP_DIR/config.yaml, then run:"
echo "  $APP_DIR/.venv/bin/weather-alert-system -c $APP_DIR/config.yaml doctor"
echo "  systemctl enable --now weather-alert-system"
echo "With the service running, queue a test without restarting helpers:"
echo "  $APP_DIR/.venv/bin/weather-alert-system -c $APP_DIR/config.yaml queue-audio --group ARC125 --text 'Skywarn test announcement.'"
if [ "$DISABLE_HELPER_SERVICES" -eq 0 ]; then
  echo "If existing DVSwitch services are active on the same box, stop them or run install.sh with --disable-helper-services."
fi
