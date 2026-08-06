#!/bin/bash
set -u

CONFIG_FILE="${CAN_CONFIG_FILE:-/etc/default/flatpack-can}"
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"

CHANNEL="${CAN_CHANNEL:-can0}"
SPEED="${CAN_SPEED:-s4}"
POLL_SECONDS="${CAN_POLL_SECONDS:-2}"
START_TIMEOUT="${CAN_START_TIMEOUT:-10}"
STATE_FILE="${CAN_STATE_FILE:-/run/flatpack-can.state}"
CURRENT_DEVICE=""

log() {
  printf '%s flatpack-can: %s\n' "$(date '+%F %T')" "$*"
}

write_state() {
  local status="$1"
  local device="${2:-}"
  local tmp="${STATE_FILE}.tmp"
  {
    printf 'status=%s\n' "$status"
    printf 'channel=%s\n' "$CHANNEL"
    printf 'configured_device=%s\n' "${CAN_DEVICE:-auto}"
    printf 'resolved_device=%s\n' "$device"
    printf 'updated_at=%s\n' "$(date +%s)"
  } >"$tmp"
  mv "$tmp" "$STATE_FILE"
  chmod 0644 "$STATE_FILE"
}

udev_value() {
  local device="$1"
  local key="$2"
  udevadm info --query=property --name="$device" 2>/dev/null |
    sed -n "s/^${key}=//p" | head -n1
}

matches_usb_identity() {
  local device="$1"
  local vidpid="${CAN_USB_VIDPID:-}"
  local wanted_serial="${CAN_USB_SERIAL:-}"
  local vid pid serial

  [ -n "$vidpid" ] || return 1
  vid="$(udev_value "$device" ID_VENDOR_ID)"
  pid="$(udev_value "$device" ID_MODEL_ID)"
  serial="$(udev_value "$device" ID_SERIAL_SHORT)"
  [ "${vid}:${pid}" = "$vidpid" ] || return 1
  [ -z "$wanted_serial" ] || [ "$serial" = "$wanted_serial" ]
}

resolve_device() {
  local candidate target

  if [ -n "${CAN_DEVICE:-}" ] && [ "${CAN_DEVICE}" != "auto" ] && [ -e "${CAN_DEVICE}" ]; then
    readlink -f "${CAN_DEVICE}"
    return 0
  fi

  if [ -n "${CAN_DEVICE_ID:-}" ]; then
    candidate="/dev/serial/by-id/${CAN_DEVICE_ID}"
    if [ -e "$candidate" ]; then
      readlink -f "$candidate"
      return 0
    fi
  fi

  for candidate in /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$candidate" ] || continue
    if matches_usb_identity "$candidate"; then
      readlink -f "$candidate"
      return 0
    fi
  done

  for candidate in /dev/serial/by-id/*; do
    [ -L "$candidate" ] || continue
    target="$(readlink -f "$candidate" 2>/dev/null || true)"
    case "$target" in
      /dev/ttyACM*|/dev/ttyUSB*)
        printf '%s\n' "$target"
        return 0
        ;;
    esac
  done

  for candidate in /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$candidate" ] || continue
    readlink -f "$candidate"
    return 0
  done
  return 1
}

cleanup_transport() {
  pkill -TERM -f "slcand.*[[:space:]]${CHANNEL}([[:space:]]|$)" 2>/dev/null || true
  sleep 0.2
  if ip link show "$CHANNEL" >/dev/null 2>&1; then
    ip link set "$CHANNEL" down 2>/dev/null || true
    ip link delete "$CHANNEL" 2>/dev/null || true
  fi
  CURRENT_DEVICE=""
}

start_transport() {
  local device="$1"
  local speed_flag="-${SPEED#-}"
  local elapsed=0

  cleanup_transport
  log "starting ${CHANNEL} from ${device} at ${speed_flag} (125 kbit/s for s4)"
  if ! slcand -o -c "$speed_flag" "$device" "$CHANNEL"; then
    log "slcand failed for ${device}"
    write_state "start_failed" "$device"
    return 1
  fi

  while ! ip link show "$CHANNEL" >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge "$START_TIMEOUT" ]; then
      log "timed out waiting for ${CHANNEL}"
      write_state "start_timeout" "$device"
      cleanup_transport
      return 1
    fi
  done

  if ! ip link set "$CHANNEL" up; then
    log "could not bring ${CHANNEL} up"
    write_state "link_up_failed" "$device"
    cleanup_transport
    return 1
  fi

  CURRENT_DEVICE="$device"
  write_state "ready" "$device"
  log "${CHANNEL} is up using ${device}"
  return 0
}

shutdown() {
  log "stopping"
  write_state "stopping" "$CURRENT_DEVICE"
  exit 0
}
trap shutdown TERM INT
trap 'cleanup_transport; rm -f "$STATE_FILE"' EXIT

log "v2 supervisor started; configured device: ${CAN_DEVICE:-auto}"
while true; do
  DEVICE="$(resolve_device 2>/dev/null || true)"

  if [ -z "$DEVICE" ]; then
    if [ -n "$CURRENT_DEVICE" ] || ip link show "$CHANNEL" >/dev/null 2>&1; then
      log "CANable disconnected; cleaning up ${CHANNEL}"
      cleanup_transport
    fi
    write_state "waiting_for_usb" ""
    sleep "$POLL_SECONDS"
    continue
  fi

  if [ "$DEVICE" != "$CURRENT_DEVICE" ] || ! ip link show "$CHANNEL" >/dev/null 2>&1; then
    start_transport "$DEVICE" || true
    sleep "$POLL_SECONDS"
    continue
  fi

  # Repair a link that was administratively brought down without restarting USB.
  if [ "$(cat "/sys/class/net/${CHANNEL}/operstate" 2>/dev/null || echo down)" = "down" ]; then
    ip link set "$CHANNEL" up 2>/dev/null || true
  fi

  write_state "ready" "$DEVICE"
  sleep "$POLL_SECONDS"
done
