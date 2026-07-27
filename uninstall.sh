#!/bin/bash
set -e

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
systemctl disable --now flatpack-dashboard flatpack-can 2>/dev/null || true
rm -f /etc/systemd/system/flatpack-dashboard.service /etc/systemd/system/flatpack-can.service
systemctl daemon-reload
echo "Services removed; data remains in /opt/flatpack-dashboard/data"
