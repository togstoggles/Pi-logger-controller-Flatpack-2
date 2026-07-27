#!/usr/bin/env python3
"""Validated runtime decoder for the confirmed Flatpack2 0x05014004 stream."""
import logging
import struct
import time

from flatpack import FlatpackController as BaseController, STATES


class FlatpackController(BaseController):
    """Accept only the confirmed rectifier status frame and sanity-check values."""

    STATUS_ID = 0x05014004

    def __init__(self, path):
        super().__init__(path)
        self.frames_received = 0
        self.status_frames_received = 0
        self.rejected_status_frames = 0
        self.last_raw_frame = 0.0
        self._remove_invalid_history()

    def _remove_invalid_history(self):
        """Remove bad samples written by earlier broad-ID decoder builds."""
        conn = self._db()
        try:
            conn.execute(
                """DELETE FROM samples
                   WHERE voltage IS NOT NULL AND (
                       voltage < 35 OR voltage > 65 OR
                       current < 0 OR current > 100 OR
                       power < 0 OR power > 6500 OR
                       input_voltage < 80 OR input_voltage > 300 OR
                       temp_inlet < 0 OR temp_inlet > 120 OR
                       temp_outlet < 0 OR temp_outlet > 120
                   )"""
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _plausible(voltage, current, input_voltage, temp_inlet, temp_outlet):
        return (
            35.0 <= voltage <= 65.0 and
            0.0 <= current <= 100.0 and
            80 <= input_voltage <= 300 and
            0 <= temp_inlet <= 120 and
            0 <= temp_outlet <= 120
        )

    def decode(self, can_id, data):
        self.frames_received += 1
        self.last_raw_frame = time.time()

        normalized = int(can_id) & 0x1FFFFFFF
        payload = bytes(data)
        if normalized != self.STATUS_ID or len(payload) != 8:
            return False

        current = struct.unpack_from("<H", payload, 1)[0] / 10.0
        voltage = struct.unpack_from("<H", payload, 3)[0] / 100.0
        input_voltage = struct.unpack_from("<H", payload, 5)[0]
        temp_inlet = int(payload[0])
        temp_outlet = int(payload[7])

        if not self._plausible(voltage, current, input_voltage, temp_inlet, temp_outlet):
            self.rejected_status_frames += 1
            logging.warning(
                "Rejected implausible Flatpack frame %08X %s: %.2f V, %.1f A, %d VAC, %d/%d C",
                normalized,
                self._format_hex(payload),
                voltage,
                current,
                input_voltage,
                temp_inlet,
                temp_outlet,
            )
            return False

        now = time.time()
        power = voltage * current
        state_code = normalized & 0xFF

        with self.lock:
            if self.energy_t and self.state["power"] is not None:
                elapsed = min(max(now - self.energy_t, 0.0), 10.0)
                self.energy_wh += float(self.state["power"]) * elapsed / 3600.0
            self.energy_t = now
            self.last_frame = now
            self.status_count += 1
            self.status_frames_received += 1
            self.last_status_id = "0x%08X" % normalized
            self.last_error = None
            self.state.update(
                online=True,
                state=STATES.get(state_code, "Status 0x%02X" % state_code),
                state_code=state_code,
                voltage=round(voltage, 2),
                current=round(current, 1),
                power=round(power, 1),
                input_voltage=input_voltage,
                temp_inlet=temp_inlet,
                temp_outlet=temp_outlet,
                session_kwh=round(self.energy_wh / 1000.0, 4),
                last_seen=now,
                can_id="0x%08X" % normalized,
            )
        return True

    def snapshot(self):
        snapshot = super().snapshot()
        snapshot["diagnostics"].update({
            "frames_received": self.frames_received,
            "status_frames_received": self.status_frames_received,
            "rejected_status_frames": self.rejected_status_frames,
            "last_raw_frame": self.last_raw_frame or None,
            "expected_status_id": "0x%08X" % self.STATUS_ID,
            "decoder": "exact-id-plus-plausibility-filter",
        })
        return snapshot
