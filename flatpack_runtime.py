#!/usr/bin/env python3
"""Validated Flatpack2 runtime with calibrated generator constant-power control."""
import logging
import struct
import time

from flatpack import FlatpackController as BaseController, STATES


class FlatpackController(BaseController):
    """Decode guarded Flatpack status frames and provide adaptive control."""

    # The low byte is the operating-state code, not a fixed PSU address:
    # 0x04 constant voltage, 0x08 constant current, 0x0C alarm, 0x10 walk-in.
    STATUS_IDS = frozenset((
        0x05014004,
        0x05014008,
        0x0501400C,
        0x05014010,
    ))

    def __init__(self, path):
        super().__init__(path)
        self.cfg.setdefault("control_mode", "manual")
        self.cfg.setdefault("generator_power_target", 1500.0)
        self.cfg.setdefault("generator_calibration_factor", 1.10)
        self.frames_received = 0
        self.status_frames_received = 0
        self.rejected_status_frames = 0
        self.last_raw_frame = 0.0
        self.last_commanded_current = None
        self._remove_invalid_history()

    def _remove_invalid_history(self):
        conn = self._db()
        try:
            conn.execute(
                """DELETE FROM samples
                   WHERE voltage IS NOT NULL AND (
                       voltage < 35 OR voltage > 65 OR
                       current < 0 OR current > 100 OR
                       power < 0 OR power > 6500 OR
                       input_voltage < 0 OR input_voltage > 300 OR
                       temp_inlet < 0 OR temp_inlet > 120 OR
                       temp_outlet < 0 OR temp_outlet > 120
                   )"""
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _plausible(voltage, current, input_voltage, temp_inlet, temp_outlet):
        # AC input may legitimately be zero while the rectifier remains powered
        # from the battery/CAN side and reports the Alarm operating state.
        return (
            35.0 <= voltage <= 65.0 and
            0.0 <= current <= 100.0 and
            0 <= input_voltage <= 300 and
            0 <= temp_inlet <= 120 and
            0 <= temp_outlet <= 120
        )

    def _control_current(self):
        if self.cfg.get("control_mode") != "constant_power":
            return float(self.cfg["current_limit"])

        voltage = self.state.get("voltage") or float(self.cfg["target_voltage"])
        factor = max(1.0, float(self.cfg.get("generator_calibration_factor", 1.10)))
        generator_watts = float(self.cfg.get("generator_power_target", 1500.0))
        dc_watts = generator_watts / factor
        current = dc_watts / max(float(voltage), 1.0)
        current = max(float(self.cfg["min_current"]), min(current, float(self.cfg["max_current"])))

        # Limit each two-second adjustment to 1.5 A to avoid generator hunting.
        if self.last_commanded_current is not None:
            low = self.last_commanded_current - 1.5
            high = self.last_commanded_current + 1.5
            current = max(low, min(current, high))
        self.last_commanded_current = current
        return current

    def setpoints(self):
        if not self.cfg["control_enabled"]:
            return False
        voltage = float(self.cfg["target_voltage"])
        current = self._control_current()
        self.validate(voltage, current)
        payload = struct.pack(
            "<HHHH",
            round(current * 10),
            round(voltage * 100),
            round(voltage * 100),
            round(float(self.cfg["ovp_voltage"]) * 100),
        )
        return self.send(0x05FF4004, payload)

    def update_settings(self, voltage, current, enabled, mode="manual", generator_power=None, calibration=None):
        voltage = float(voltage)
        current = float(current)
        mode = str(mode or "manual")
        if mode not in ("manual", "constant_power"):
            raise ValueError("Unknown control mode")
        self.validate(voltage, current)

        generator_power = float(generator_power if generator_power is not None else self.cfg["generator_power_target"])
        calibration = float(calibration if calibration is not None else self.cfg["generator_calibration_factor"])
        if not 200.0 <= generator_power <= 2500.0:
            raise ValueError("Generator power target outside 200-2500 W")
        if not 1.0 <= calibration <= 1.5:
            raise ValueError("Calibration factor outside 1.00-1.50")

        with self.lock:
            self.cfg.update(
                target_voltage=voltage,
                current_limit=current,
                control_enabled=bool(enabled),
                control_mode=mode,
                generator_power_target=generator_power,
                generator_calibration_factor=calibration,
            )
            self.last_commanded_current = None
            self.save()
        if enabled:
            self.login()
            time.sleep(0.1)
            self.setpoints()

    def calibrate_generator(self, meter_watts):
        meter_watts = float(meter_watts)
        output_watts = float(self.state.get("power") or 0.0)
        if output_watts < 200.0:
            raise ValueError("Charger output is too low for calibration")
        if meter_watts < output_watts or meter_watts > 3000.0:
            raise ValueError("Meter reading is outside the valid range")
        factor = meter_watts / output_watts
        if not 1.0 <= factor <= 1.5:
            raise ValueError("Calculated calibration factor outside 1.00-1.50")
        with self.lock:
            self.cfg["generator_calibration_factor"] = round(factor, 4)
            self.last_commanded_current = None
            self.save()
        return self.cfg["generator_calibration_factor"]

    def decode(self, can_id, data):
        self.frames_received += 1
        self.last_raw_frame = time.time()
        normalized = int(can_id) & 0x1FFFFFFF
        payload = bytes(data)
        if normalized not in self.STATUS_IDS or len(payload) != 8:
            return False

        self.status_candidate_count += 1
        current = struct.unpack_from("<H", payload, 1)[0] / 10.0
        voltage = struct.unpack_from("<H", payload, 3)[0] / 100.0
        input_voltage = struct.unpack_from("<H", payload, 5)[0]
        temp_inlet = int(payload[0])
        temp_outlet = int(payload[7])
        if not self._plausible(voltage, current, input_voltage, temp_inlet, temp_outlet):
            self.rejected_status_frames += 1
            logging.warning("Rejected implausible Flatpack frame %08X %s", normalized, self._format_hex(payload))
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
        factor = float(self.cfg.get("generator_calibration_factor", 1.10))
        expected_ids = ["0x%08X" % can_id for can_id in sorted(self.STATUS_IDS)]
        snapshot["estimated_generator_power"] = round(float(snapshot.get("power") or 0.0) * factor, 0)
        snapshot["commanded_current"] = None if self.last_commanded_current is None else round(self.last_commanded_current, 1)
        snapshot["settings"].update({
            "control_mode": self.cfg.get("control_mode", "manual"),
            "generator_power_target": self.cfg.get("generator_power_target", 1500.0),
            "generator_calibration_factor": factor,
        })
        snapshot["diagnostics"].update({
            "frames_received": self.frames_received,
            "status_frames_received": self.status_frames_received,
            "rejected_status_frames": self.rejected_status_frames,
            "last_raw_frame": self.last_raw_frame or None,
            "expected_status_id": " | ".join(expected_ids),
            "expected_status_ids": expected_ids,
            "decoder": "flatpack-state-id-plus-plausibility-filter",
        })
        return snapshot
