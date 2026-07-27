#!/bin/bash
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/flatpack-dashboard
U="${SUDO_USER:-$USER}"

[ "$(id -u)" -eq 0 ] || { echo "Run: sudo bash install.sh"; exit 1; }

apt-get update
apt-get install -y can-utils python3-venv unzip
mkdir -p "$DST/data"

# Stop the old process before replacing its source files.
systemctl stop flatpack-dashboard 2>/dev/null || true

cp -a "$SRC"/. "$DST"/
rm -rf "$DST/.venv" "$DST/__pycache__"
find "$DST" -name '*.pyc' -delete

python3 -m venv "$DST/.venv"
"$DST/.venv/bin/pip" install --upgrade "pip<24"
"$DST/.venv/bin/pip" install -r "$DST/requirements.txt"

# Refuse deployment when syntax or confirmed CAN-frame regression tests fail.
"$DST/.venv/bin/python" -m py_compile "$DST/app.py" "$DST/flatpack.py" "$DST/flatpack_runtime.py"
cd "$DST"
"$DST/.venv/bin/python" -m unittest -v test_flatpack.py

chmod +x "$DST/setup_can.sh" "$DST/install.sh" "$DST/uninstall.sh"
chown -R "$U:$U" "$DST/data"
chown "$U:$U" "$DST/config.json"

cat >/etc/systemd/system/flatpack-can.service <<EOF
[Unit]
Description=CANable SLCAN for Eltek Flatpack
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$DST/setup_can.sh can0
ExecStop=/sbin/ip link set can0 down
User=root

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/flatpack-dashboard.service <<EOF
[Unit]
Description=Eltek Flatpack Dashboard
After=flatpack-can.service network.target
Requires=flatpack-can.service

[Service]
Type=simple
User=$U
WorkingDirectory=$DST
Environment=PYTHONUNBUFFERED=1
ExecStart=$DST/.venv/bin/python $DST/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable flatpack-can flatpack-dashboard
systemctl restart flatpack-can
systemctl restart flatpack-dashboard

IP="$(hostname -I | awk '{print $1}')"
echo "Dashboard: http://${IP:-raspberrypi}:5000"
echo "Health:    http://${IP:-raspberrypi}:5000/health"
