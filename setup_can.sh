#!/bin/bash
set -eu

CH="${1:-can0}"
if ip link show "$CH" >/dev/null 2>&1; then
  ip link set "$CH" up || true
  exit 0
fi

PORT="$(ls /dev/ttyACM* 2>/dev/null | head -n1 || true)"
[ -n "$PORT" ] || { echo "CANable not found" >&2; exit 1; }

pkill slcand 2>/dev/null || true
sleep 1
slcand -o -c -s4 "$PORT" "$CH"
sleep 2
ip link set "$CH" up
ip -details link show "$CH"
