#!/usr/bin/env python3
"""Validated Flatpack2 runtime with calibrated generator constant-power control."""
import logging
import struct
import time

from flatpack import FlatpackController as BaseController, STATES


class FlatpackController(BaseController):
    """Decode guarded Flatpack status frames and provide adaptive control."""

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
        self.cfg.setdefault("generator_ramp_seconds", 30.0)
        self.cfg.setdefault("generator_start_current", 3.0)
        self.cfg.setdefault("generator_recovery_hold_seconds", 5.0)
        self.cfg.setdefault("generator_trip_voltage", 100.0)
        self.cfg.setdefault("generator_brownout_voltage", 215.0)
        self.cfg.setdefault("generator_recover_voltage", 225.0)
        self.cfg.setdefault("generator_trip_backoff", 0.85)
        # Recovery is intentionally faster than the original conservative
        # 60 s / 10 s scheme. After healthy AC returns, hold the learned cap
        # for 10 s, then probe upward by 1 A every 2 s. Any new brownout/trip
        # resets this timer immediately.
        self.cfg.setdefault("generator_cap_relax_delay_seconds", 10.0)
        self.cfg.setdefault("generator_cap_relax_interval_seconds", 2.0)
        self.cfg.setdefault("generator_cap_relax_step_amps", 1.0)
        self.frames_received = 0
        self.status_frames_received = 0
        self.rejected_status_frames = 0
        self.last_raw_frame = 0.0
        self.last_commanded_current = None

        self.generator_control_state = "WAITING AC"
        self.generator_ramp_started = 0.0
        self.generator_recover_since = 0.0
        self.generator_trip_count = 0
        self.generator_last_trip = 0.0
        self.generator_adaptive_current_cap = None
        self.generator_target_current = None
        self.generator_requested_current = None
        self.generator_limit_reason = None
        self.generator_ramp_progress = 0.0
        self.generator_had_good_ac = False
        self.generator_stable_since = 0.0
        self.generator_last_cap_relax = 0.0
        self.generator_alarm_with_ac_count = 0
        self.generator_last_alarm_with_ac = 0.0
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
        return (
            35.0 <= voltage <= 65.0 and
            0.0 <= current <= 100.0 and
            0 <= input_voltage <= 300 and
            0 <= temp_inlet <= 120 and
            0 <= temp_outlet <= 120
        )

    def _power_target_without_trip_cap(self):
        voltage = self.state.get("voltage") or float(self.cfg["target_voltage"])
        factor = max(1.0, float(self.cfg.get("generator_calibration_factor", 1.10)))
        generator_watts = float(self.cfg.get("generator_power_target", 1500.0))
        dc_watts = generator_watts / factor
        requested = dc_watts / max(float(voltage), 1.0)
        self.generator_requested_current = requested

        current = requested
        self.generator_limit_reason = "POWER TARGET"

        manual_limit = float(self.cfg["current_limit"])
        if current > manual_limit:
            current = manual_limit
            self.generator_limit_reason = "CURRENT LIMIT"

        system_max = float(self.cfg["max_current"])
        if current > system_max:
            current = system_max
            self.generator_limit_reason = "SYSTEM MAX"

        return max(float(self.cfg["min_current"]), current)

    def _constant_power_target_current(self):
        current = self._power_target_without_trip_cap()
        if self.generator_adaptive_current_cap is not None and current > float(self.generator_adaptive_current_cap):
            current = float(self.generator_adaptive_current_cap)
            self.generator_limit_reason = "LEARNED TRIP CAP"
        return current

    def _start_current(self, target):
        start = float(self.cfg.get("generator_start_current", 3.0))
        start = max(float(self.cfg["min_current"]), start)
        return min(start, target)

    def _telemetry_stale(self, now):
        if not self.last_frame:
            return True
        timeout = max(10.0, float(self.cfg.get("offline_after_seconds", 8.0)))
        return now - self.last_frame > timeout

    def _reset_generator_stability(self):
        self.generator_stable_since = 0.0
        self.generator_last_cap_relax = 0.0

    def _register_generator_trip(self, now, start_current):
        previous = float(self.last_commanded_current or start_current)
        if previous > start_current + 0.5 and now - self.generator_last_trip > 3.0:
            backoff = float(self.cfg.get("generator_trip_backoff", 0.85))
            learned = max(start_current, previous * backoff)
            if self.generator_adaptive_current_cap is None:
                self.generator_adaptive_current_cap = learned
            else:
                self.generator_adaptive_current_cap = min(self.generator_adaptive_current_cap, learned)
            self.generator_trip_count += 1
            self.generator_last_trip = now
        self._reset_generator_stability()

    def _relax_adaptive_cap(self, now):
        """Probe upward quickly after healthy AC without abandoning protection."""
        if self.generator_adaptive_current_cap is None:
            return False

        if not self.generator_stable_since:
            self.generator_stable_since = now
            return False

        delay = max(0.0, float(self.cfg.get("generator_cap_relax_delay_seconds", 10.0)))
        if now - self.generator_stable_since < delay:
            return False

        interval = max(2.0, float(self.cfg.get("generator_cap_relax_interval_seconds", 2.0)))
        if self.generator_last_cap_relax and now - self.generator_last_cap_relax < interval:
            return False

        uncapped_target = self._power_target_without_trip_cap()
        cap = float(self.generator_adaptive_current_cap)
        step = max(0.1, float(self.cfg.get("generator_cap_relax_step_amps", 1.0)))

        if cap >= uncapped_target - 0.05:
            self.generator_adaptive_current_cap = None
        else:
            new_cap = min(uncapped_target, cap + step)
            self.generator_adaptive_current_cap = None if new_cap >= uncapped_target - 0.05 else new_cap

        self.generator_last_cap_relax = now
        return True

    def _control_current(self):
        if self.cfg.get("control_mode") != "constant_power":
            self.generator_control_state = "MANUAL"
            self.generator_ramp_progress = 1.0
            self.generator_requested_current = float(self.cfg["current_limit"])
            self.generator_target_current = float(self.cfg["current_limit"])
            self.generator_limit_reason = "MANUAL"
            self._reset_generator_stability()
            return float(self.cfg["current_limit"])

        now = time.time()
        target = self._constant_power_target_current()
        self.generator_target_current = target
        start_current = self._start_current(target)
        vin = self.state.get("input_voltage")
        state_code = self.state.get("state_code")

        if self._telemetry_stale(now) or vin is None:
            self.generator_control_state = "WAITING AC"
            self.generator_ramp_started = 0.0
            self.generator_recover_since = 0.0
            self.generator_ramp_progress = 0.0
            self.generator_had_good_ac = False
            self._reset_generator_stability()
            self.last_commanded_current = start_current
            return start_current

        vin = float(vin)
        trip_voltage = float(self.cfg.get("generator_trip_voltage", 100.0))
        brownout_voltage = float(self.cfg.get("generator_brownout_voltage", 215.0))
        recover_voltage = float(self.cfg.get("generator_recover_voltage", 225.0))

        # Only learn a generator trip from actual AC collapse. The Flatpack can
        # briefly report its Alarm state while healthy mains is still present;
        # treating every 0x0C frame as a generator trip falsely ratchets the
        # adaptive current cap down (e.g. ~1500 W -> ~1000 W after two alarms).
        if vin < trip_voltage:
            self._register_generator_trip(now, start_current)
            self.generator_control_state = "AC TRIP"
            self.generator_ramp_started = 0.0
            self.generator_recover_since = 0.0
            self.generator_ramp_progress = 0.0
            self.generator_had_good_ac = False
            self.last_commanded_current = start_current
            return start_current

        if state_code == 0x0C:
            self.generator_alarm_with_ac_count += 1
            self.generator_last_alarm_with_ac = now

        if vin < brownout_voltage:
            previous = float(self.last_commanded_current or start_current)
            reduced = max(start_current, previous - max(1.0, previous * 0.10))
            self.generator_control_state = "AC LOW - BACKING OFF"
            self.generator_ramp_started = 0.0
            self.generator_recover_since = 0.0
            self.generator_ramp_progress = 0.0
            self._reset_generator_stability()
            self.last_commanded_current = reduced
            return reduced

        if vin < recover_voltage:
            hold = min(float(self.last_commanded_current or start_current), target)
            self.generator_control_state = "AC LOW - HOLD"
            self.generator_ramp_started = 0.0
            self.generator_recover_since = 0.0
            self.generator_ramp_progress = 0.0
            self._reset_generator_stability()
            self.last_commanded_current = max(start_current, hold)
            return self.last_commanded_current

        if not self.generator_had_good_ac:
            if not self.generator_recover_since:
                self.generator_recover_since = now
            hold_seconds = max(0.0, float(self.cfg.get("generator_recovery_hold_seconds", 5.0)))
            if now - self.generator_recover_since < hold_seconds:
                self.generator_control_state = "AC STABILISING"
                self.generator_ramp_progress = 0.0
                self._reset_generator_stability()
                self.last_commanded_current = start_current
                return start_current
            self.generator_had_good_ac = True
            self.generator_ramp_started = now
            self.generator_recover_since = 0.0
            self.generator_stable_since = now

        if not self.generator_stable_since:
            self.generator_stable_since = now

        self._relax_adaptive_cap(now)
        target = self._constant_power_target_current()
        self.generator_target_current = target
        start_current = self._start_current(target)

        if not self.generator_ramp_started:
            self.generator_ramp_started = now

        ramp_seconds = max(1.0, float(self.cfg.get("generator_ramp_seconds", 30.0)))
        progress = max(0.0, min(1.0, (now - self.generator_ramp_started) / ramp_seconds))
        current = start_current + (target - start_current) * progress
        self.generator_ramp_progress = progress

        if self.generator_adaptive_current_cap is not None:
            delay = max(0.0, float(self.cfg.get("generator_cap_relax_delay_seconds", 10.0)))
            if now - self.generator_stable_since < delay:
                self.generator_control_state = "CAP HOLD"
            else:
                self.generator_control_state = "CAP RECOVERY"
        elif progress < 1.0:
            self.generator_control_state = "RAMPING"
        else:
            self.generator_control_state = "STABLE"

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

    def update_settings(self, voltage, current, enabled, mode="manual", generator_power=None, calibration=None, ramp_seconds=None):
        voltage = float(voltage)
        current = float(current)
        mode = str(mode or "manual")
        if mode not in ("manual", "constant_power"):
            raise ValueError("Unknown control mode")
        self.validate(voltage, current)

        generator_power = float(generator_power if generator_power is not None else self.cfg["generator_power_target"])
        calibration = float(calibration if calibration is not None else self.cfg["generator_calibration_factor"])
        ramp_seconds = float(ramp_seconds if ramp_seconds is not None else self.cfg.get("generator_ramp_seconds", 30.0))
        if not 200.0 <= generator_power <= 2500.0:
            raise ValueError("Generator power target outside 200-2500 W")
        if not 1.0 <= calibration <= 1.5:
            raise ValueError("Calibration factor outside 1.00-1.50")
        if not 5.0 <= ramp_seconds <= 300.0:
            raise ValueError("Generator ramp time outside 5-300 seconds")

        with self.lock:
            self.cfg.update(
                target_voltage=voltage,
                current_limit=current,
                control_enabled=bool(enabled),
                control_mode=mode,
                generator_power_target=generator_power,
                generator_calibration_factor=calibration,
                generator_ramp_seconds=ramp_seconds,
            )
            self.last_commanded_current = None
            self.generator_ramp_started = 0.0
            self.generator_recover_since = 0.0
            self.generator_had_good_ac = False
            self.generator_adaptive_current_cap = None
            self.generator_trip_count = 0
            self.generator_control_state = "WAITING AC" if mode == "constant_power" else "MANUAL"
            self.generator_ramp_progress = 0.0
            self.generator_requested_current = None
            self.generator_target_current = None
            self.generator_limit_reason = None
            self.generator_alarm_with_ac_count = 0
            self.generator_last_alarm_with_ac = 0.0
            self._reset_generator_stability()
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
            self.generator_ramp_started = 0.0
            self.generator_recover_since = 0.0
            self.generator_had_good_ac = False
            self._reset_generator_stability()
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
        now = time.time()
        factor = float(self.cfg.get("generator_calibration_factor", 1.10))
        expected_ids = ["0x%08X" % can_id for can_id in sorted(self.STATUS_IDS)]
        stable_seconds = 0.0 if not self.generator_stable_since else max(0.0, now - self.generator_stable_since)
        relax_delay = max(0.0, float(self.cfg.get("generator_cap_relax_delay_seconds", 10.0)))
        snapshot["estimated_generator_power"] = round(float(snapshot.get("power") or 0.0) * factor, 0)
        snapshot["commanded_current"] = None if self.last_commanded_current is None else round(self.last_commanded_current, 1)
        snapshot["settings"].update({
            "control_mode": self.cfg.get("control_mode", "manual"),
            "generator_power_target": self.cfg.get("generator_power_target", 1500.0),
            "generator_calibration_factor": factor,
            "generator_ramp_seconds": self.cfg.get("generator_ramp_seconds", 30.0),
        })
        snapshot["generator_control"] = {
            "state": self.generator_control_state,
            "ramp_progress": round(self.generator_ramp_progress, 3),
            "trip_count": self.generator_trip_count,
            "requested_current": None if self.generator_requested_current is None else round(self.generator_requested_current, 1),
            "target_current": None if self.generator_target_current is None else round(self.generator_target_current, 1),
            "limit_reason": self.generator_limit_reason,
            "adaptive_current_cap": None if self.generator_adaptive_current_cap is None else round(self.generator_adaptive_current_cap, 1),
            "ramp_seconds": float(self.cfg.get("generator_ramp_seconds", 30.0)),
            "brownout_voltage": float(self.cfg.get("generator_brownout_voltage", 215.0)),
            "recover_voltage": float(self.cfg.get("generator_recover_voltage", 225.0)),
            "stable_seconds": round(stable_seconds, 1),
            "cap_relax_in_seconds": None if self.generator_adaptive_current_cap is None else round(max(0.0, relax_delay - stable_seconds), 1),
            "cap_relax_step_amps": float(self.cfg.get("generator_cap_relax_step_amps", 1.0)),
            "cap_relax_interval_seconds": float(self.cfg.get("generator_cap_relax_interval_seconds", 2.0)),
            "alarm_with_ac_count": self.generator_alarm_with_ac_count,
        }
        snapshot["diagnostics"].update({
            "frames_received": self.frames_received,
            "status_frames_received": self.status_frames_received,
            "rejected_status_frames": self.rejected_status_frames,
            "last_raw_frame": self.last_raw_frame or None,
            "expected_status_id": " | ".join(expected_ids),
            "expected_status_ids": expected_ids,
            "decoder": "flatpack-state-id-plus-plausibility-filter",
            "generator_control_state": self.generator_control_state,
            "generator_trip_count": self.generator_trip_count,
            "generator_limit_reason": self.generator_limit_reason,
            "generator_stable_seconds": round(stable_seconds, 1),
            "generator_alarm_with_ac_count": self.generator_alarm_with_ac_count,
            "generator_last_alarm_with_ac": self.generator_last_alarm_with_ac or None,
        })
        return snapshot
