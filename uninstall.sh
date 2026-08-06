#!/bin/bash
set -e

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
systemctl disable --now flatpack-dashboard flatpack-can 2>/dev/null || true
pkill -TERM -f 'slcand.*[[:space:]]can0([[:space:]]|$)' 2>/dev/null || true
ip link set can0 down 2>/dev/null || true
ip link delete can0 2>/dev/null || true
rm -f /etc/systemd/system/flatpack-dashboard.service \
      /etc/systemd/system/flatpack-can.service \
      /etc/default/flatpack-can \
      /run/flatpack-can.state
systemctl daemon-reload
echo "Services and CANable pin removed; data remains in /opt/flatpack-dashboard/data"
