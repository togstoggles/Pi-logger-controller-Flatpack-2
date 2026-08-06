#!/bin/bash
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/flatpack-dashboard
ENV_FILE=/etc/default/flatpack-can
U="${SUDO_USER:-$USER}"

[ "$(id -u)" -eq 0 ] || { echo "Run: sudo bash install.sh"; exit 1; }

find_current_slcand_tty() {
  ps -eo args= | awk '
    /[s]lcand/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^\/dev\/tty(ACM|USB)[0-9]+$/) { print $i; exit }
      }
    }'
}

find_by_id_for_tty() {
  local tty="$1"
  local link
  [ -n "$tty" ] || return 1
  for link in /dev/serial/by-id/*; do
    [ -L "$link" ] || continue
    if [ "$(readlink -f "$link" 2>/dev/null || true)" = "$(readlink -f "$tty" 2>/dev/null || true)" ]; then
      printf '%s\n' "$link"
      return 0
    fi
  done
  return 1
}

udev_value() {
  local device="$1"
  local key="$2"
  udevadm info --query=property --name="$device" 2>/dev/null |
    sed -n "s/^${key}=//p" | head -n1
}

# Capture the already-working adapter before stopping the old service. This is
# the safest way to distinguish the CANable from other USB serial devices.
CURRENT_TTY="$(find_current_slcand_tty || true)"
PINNED_DEVICE=""
CAN_DEVICE_ID=""
CAN_USB_VIDPID=""
CAN_USB_SERIAL=""

if [ -r "$ENV_FILE" ]; then
  # Preserve an existing v2 pin, even when the CANable is temporarily unplugged.
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  if [ -n "${CAN_DEVICE:-}" ] && [ "${CAN_DEVICE}" != "auto" ]; then
    PINNED_DEVICE="$CAN_DEVICE"
  fi
fi

if [ -z "$PINNED_DEVICE" ] && [ -n "$CURRENT_TTY" ]; then
  PINNED_DEVICE="$(find_by_id_for_tty "$CURRENT_TTY" || true)"
  [ -n "$PINNED_DEVICE" ] || PINNED_DEVICE="$CURRENT_TTY"
fi

if [ -z "$PINNED_DEVICE" ]; then
  for link in /dev/serial/by-id/*; do
    [ -L "$link" ] || continue
    case "$(readlink -f "$link" 2>/dev/null || true)" in
      /dev/ttyACM*|/dev/ttyUSB*) PINNED_DEVICE="$link"; break ;;
    esac
  done
fi

IDENTITY_TTY="$CURRENT_TTY"
if [ -z "$IDENTITY_TTY" ] && [ -n "$PINNED_DEVICE" ] && [ -e "$PINNED_DEVICE" ]; then
  IDENTITY_TTY="$(readlink -f "$PINNED_DEVICE")"
fi
if [ -n "$IDENTITY_TTY" ] && [ -e "$IDENTITY_TTY" ]; then
  VID="$(udev_value "$IDENTITY_TTY" ID_VENDOR_ID || true)"
  PID="$(udev_value "$IDENTITY_TTY" ID_MODEL_ID || true)"
  CAN_USB_SERIAL="$(udev_value "$IDENTITY_TTY" ID_SERIAL_SHORT || true)"
  [ -z "$VID" ] || [ -z "$PID" ] || CAN_USB_VIDPID="${VID}:${PID}"
fi
if [[ "$PINNED_DEVICE" == /dev/serial/by-id/* ]]; then
  CAN_DEVICE_ID="${PINNED_DEVICE##*/}"
fi

apt-get update
apt-get install -y can-utils python3-venv unzip udev
mkdir -p "$DST/data" /etc/default

# Preserve live settings and meter calibration across software updates.
SAVED_CONFIG="$(mktemp)"
if [ -f "$DST/config.json" ]; then
  cp "$DST/config.json" "$SAVED_CONFIG"
else
  : >"$SAVED_CONFIG"
fi

systemctl stop flatpack-dashboard flatpack-can 2>/dev/null || true
cp -a "$SRC"/. "$DST"/
if [ -s "$SAVED_CONFIG" ]; then
  cp "$SAVED_CONFIG" "$DST/config.json"
fi
rm -f "$SAVED_CONFIG"
rm -rf "$DST/.venv" "$DST/__pycache__"
find "$DST" -name '*.pyc' -delete

python3 -m venv "$DST/.venv"
"$DST/.venv/bin/pip" install --upgrade "pip<24"
"$DST/.venv/bin/pip" install -r "$DST/requirements.txt"

# Refuse deployment when source syntax or confirmed CAN/control tests fail.
bash -n "$DST/install.sh" "$DST/can_supervisor.sh" "$DST/setup_can.sh" "$DST/uninstall.sh"
"$DST/.venv/bin/python" -m py_compile "$DST/app.py" "$DST/flatpack.py" "$DST/flatpack_runtime.py"
cd "$DST"
"$DST/.venv/bin/python" -m unittest -v test_flatpack.py

chmod +x "$DST/can_supervisor.sh" "$DST/setup_can.sh" "$DST/install.sh" "$DST/uninstall.sh"
chown -R "$U:$U" "$DST/data"
chown "$U:$U" "$DST/config.json"

{
  echo 'CAN_CHANNEL=can0'
  echo 'CAN_SPEED=s4'
  if [ -n "$PINNED_DEVICE" ]; then
    printf 'CAN_DEVICE="%s"\n' "$PINNED_DEVICE"
  else
    echo 'CAN_DEVICE=auto'
  fi
  [ -z "$CAN_DEVICE_ID" ] || printf 'CAN_DEVICE_ID="%s"\n' "$CAN_DEVICE_ID"
  [ -z "$CAN_USB_VIDPID" ] || printf 'CAN_USB_VIDPID="%s"\n' "$CAN_USB_VIDPID"
  [ -z "$CAN_USB_SERIAL" ] || printf 'CAN_USB_SERIAL="%s"\n' "$CAN_USB_SERIAL"
  echo 'CAN_POLL_SECONDS=2'
  echo 'CAN_START_TIMEOUT=10'
} >"$ENV_FILE"
chmod 0644 "$ENV_FILE"

cat >/etc/systemd/system/flatpack-can.service <<EOF_SERVICE
[Unit]
Description=Flatpack Controller v2 persistent CANable transport
After=systemd-udevd.service
Wants=systemd-udevd.service
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=-$ENV_FILE
ExecStart=$DST/can_supervisor.sh
Restart=always
RestartSec=2
KillMode=control-group
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF_SERVICE

cat >/etc/systemd/system/flatpack-dashboard.service <<EOF_SERVICE
[Unit]
Description=Eltek Flatpack Controller v2 Dashboard
After=network.target flatpack-can.service
Wants=flatpack-can.service

[Service]
Type=simple
User=$U
WorkingDirectory=$DST
Environment=PYTHONUNBUFFERED=1
Environment=FLATPACK_VERSION=2.0.0
ExecStart=$DST/.venv/bin/python $DST/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF_SERVICE

systemctl daemon-reload
systemctl enable flatpack-can flatpack-dashboard
systemctl restart flatpack-can
systemctl restart flatpack-dashboard
sleep 2

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Flatpack Controller v2 installed"
echo "Pinned CANable: ${PINNED_DEVICE:-automatic discovery}"
echo "Dashboard: http://${IP:-raspberrypi}:5000"
echo "Health:    http://${IP:-raspberrypi}:5000/health"
echo "CAN state: $(cat /run/flatpack-can.state 2>/dev/null | tr '\n' ' ' || echo initialising)"
