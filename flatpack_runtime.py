#!/usr/bin/env python3
"""Runtime fixes for the confirmed 0x05014004 Flatpack2 status stream."""
import logging
import struct
import time

from flatpack import FlatpackController as BaseController, STATES


class FlatpackController(BaseController):
    """Use a direct, documented status-ID match and expose diagnostics."""

    def __init__(self, path):
        super().__init__(path)
        self.frames_received = 0
        self.status_frames_received = 0
        self.last_raw_frame = 0.0

    @staticmethod
    def is_status_id(can_id):
        # Confirmed unit sends 0x05014004. Ignore the assigned PSU byte (0x01).
        return (can_id & 0xFF00FFFF) == 0x05004004

    def decode(self, message):
        self.frames_received += 1
        self.last_raw_frame = time.time()

        can_id = int(message.arbitration_id)
        data = bytes(message.data)
        if not self.is_status_id(can_id) or len(data) != 8:
            return False

        now = time.time()
        current = struct.unpack_from("<H", data, 1)[0] / 10.0
        voltage = struct.unpack_from("<H", data, 3)[0] / 100.0
        input_voltage = struct.unpack_from("<H", data, 5)[0]
        power = voltage * current
        state_code = can_id & 0xFF

        # Reject corrupt frames without hiding valid zero-current standby data.
        if not (0.0 <= current <= 100.0 and 0.0 <= voltage <= 65.0 and 0 <= input_voltage <= 300):
            logging.warning("Rejected implausible status frame %08X %s", can_id, data.hex())
            return False

        with self.lock:
            if self.energy_t and self.state["power"] is not None:
                elapsed = min(max(now - self.energy_t, 0.0), 10.0)
                self.energy_wh += float(self.state["power"]) * elapsed / 3600.0
            self.energy_t = now
            self.last_frame = now
            self.status_frames_received += 1
            self.state.update(
                online=True,
                state=STATES.get(state_code, "Status 0x%02X" % state_code),
                state_code=state_code,
                voltage=round(voltage, 2),
                current=round(current, 1),
                power=round(power, 1),
                input_voltage=input_voltage,
                temp_inlet=int(data[0]),
                temp_outlet=int(data[7]),
                session_kwh=round(self.energy_wh / 1000.0, 4),
                last_seen=now,
                can_id="0x%08X" % can_id,
            )
        return True

    def snapshot(self):
        snapshot = super().snapshot()
        snapshot["diagnostics"] = {
            "frames_received": self.frames_received,
            "status_frames_received": self.status_frames_received,
            "last_raw_frame": self.last_raw_frame or None,
            "expected_status_id": "0x05014004",
        }
        return snapshot
