#!/usr/bin/env python3
import json
import os
import tempfile
import time
import unittest

from flatpack import FlatpackController
from flatpack_runtime import FlatpackController as RuntimeController


class FlatpackDecoderTests(unittest.TestCase):
    def make_controller(self, cls=FlatpackController):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config_path = os.path.join(temp_dir.name, "config.json")
        config = {
            "can_channel": "can0",
            "serial_hex": "115171102034",
            "psu_id": 1,
            "login_interval_seconds": 5,
            "sample_interval_seconds": 10,
            "offline_after_seconds": 15,
            "control_enabled": False,
            "control_mode": "manual",
            "target_voltage": 53.5,
            "current_limit": 20.0,
            "generator_power_target": 1700.0,
            "generator_calibration_factor": 1.10,
            "min_voltage": 44.5,
            "max_voltage": 55.6,
            "min_current": 1.0,
            "max_current": 38.0,
            "ovp_voltage": 57.6,
            "database_path": os.path.join(temp_dir.name, "flatpack.db"),
        }
        with open(config_path, "w") as handle:
            json.dump(config, handle)
        return cls(config_path)

    def test_confirmed_status_frame_decodes(self):
        ctl = self.make_controller(RuntimeController)
        data = bytes.fromhex("16 7D 00 D5 14 F8 00 26")
        self.assertTrue(ctl.decode(0x05014004, data))
        snap = ctl.snapshot()
        self.assertTrue(snap["online"])
        self.assertEqual(snap["voltage"], 53.33)
        self.assertEqual(snap["current"], 12.5)
        self.assertEqual(snap["input_voltage"], 248)
        self.assertEqual(snap["temp_inlet"], 22)
        self.assertEqual(snap["temp_outlet"], 38)
        self.assertAlmostEqual(snap["power"], 666.6, places=1)
        self.assertEqual(snap["can_id"], "0x05014004")

    def test_all_operating_state_ids_decode(self):
        expected = {
            0x05014004: "Constant voltage",
            0x05014008: "Constant current",
            0x0501400C: "Alarm",
            0x05014010: "Walk-in",
        }
        data = bytes.fromhex("16 7D 00 D5 14 F8 00 26")
        for can_id, state in expected.items():
            ctl = self.make_controller(RuntimeController)
            self.assertTrue(ctl.decode(can_id, data), hex(can_id))
            self.assertEqual(ctl.snapshot()["state"], state)

    def test_alarm_frame_with_zero_ac_input_is_valid(self):
        ctl = self.make_controller(RuntimeController)
        data = bytes.fromhex("16 00 00 18 15 00 00 26")
        self.assertTrue(ctl.decode(0x0501400C, data))
        snap = ctl.snapshot()
        self.assertTrue(snap["online"])
        self.assertEqual(snap["state"], "Alarm")
        self.assertEqual(snap["voltage"], 54.0)
        self.assertEqual(snap["current"], 0.0)
        self.assertEqual(snap["input_voltage"], 0)

    def test_non_status_frame_is_ignored(self):
        ctl = self.make_controller(RuntimeController)
        self.assertFalse(ctl.decode(0x05002034, bytes.fromhex("1B 11 51 71 10 20 34 4F")))
        self.assertFalse(ctl.snapshot()["online"])

    def test_similar_but_unrelated_status_id_is_ignored(self):
        ctl = self.make_controller(RuntimeController)
        data = bytes.fromhex("16 7D 00 D5 14 F8 00 26")
        self.assertFalse(ctl.decode(0x05024004, data))
        self.assertFalse(ctl.snapshot()["online"])

    def test_setpoint_frame_is_not_mistaken_for_status(self):
        ctl = self.make_controller(RuntimeController)
        payload = bytes.fromhex("C8 00 E6 14 E6 14 80 16")
        self.assertFalse(ctl.decode(0x05FF4004, payload))
        self.assertFalse(ctl.snapshot()["online"])

    def test_implausible_same_id_frame_is_rejected_without_overwriting_good_data(self):
        ctl = self.make_controller(RuntimeController)
        good = bytes.fromhex("16 F4 00 2A 15 F9 00 26")
        bad = bytes.fromhex("5E 01 40 15 40 15 80 16")
        self.assertTrue(ctl.decode(0x05014004, good))
        before = ctl.snapshot()
        self.assertFalse(ctl.decode(0x05014004, bad))
        after = ctl.snapshot()
        self.assertEqual(after["voltage"], before["voltage"])
        self.assertEqual(after["current"], before["current"])
        self.assertEqual(after["input_voltage"], before["input_voltage"])
        self.assertEqual(after["diagnostics"]["rejected_status_frames"], 1)

    def test_base_and_runtime_decode_signatures_accept_can_id_and_data(self):
        base = self.make_controller(FlatpackController)
        runtime = self.make_controller(RuntimeController)
        data = bytes.fromhex("16 7D 00 D5 14 F8 00 26")
        self.assertTrue(base.decode(0x05014004, data))
        self.assertTrue(runtime.decode(0x05014004, data))

    def test_constant_power_target_uses_live_voltage_and_calibration(self):
        ctl = self.make_controller(RuntimeController)
        ctl.cfg["control_mode"] = "constant_power"
        ctl.cfg["current_limit"] = 38.0
        ctl.state["voltage"] = 55.0
        current = ctl._constant_power_target_current()
        self.assertAlmostEqual(current, (1700.0 / 1.10) / 55.0, places=3)

    def test_manual_current_is_hard_ceiling_in_constant_power_mode(self):
        ctl = self.make_controller(RuntimeController)
        ctl.cfg.update(control_mode="constant_power", current_limit=20.0, generator_recovery_hold_seconds=0.0)
        ctl.state.update(voltage=55.0, input_voltage=235, state_code=0x08)
        ctl.last_frame = time.time()
        ctl.generator_had_good_ac = True
        ctl.generator_ramp_started = time.time() - 31.0
        current = ctl._control_current()
        self.assertAlmostEqual(current, 20.0, places=2)

    def test_generator_soft_start_ramps_over_thirty_seconds(self):
        ctl = self.make_controller(RuntimeController)
        ctl.cfg.update(
            control_mode="constant_power",
            current_limit=38.0,
            generator_recovery_hold_seconds=0.0,
            generator_ramp_seconds=30.0,
            generator_start_current=3.0,
        )
        ctl.state.update(voltage=55.0, input_voltage=235, state_code=0x08)
        ctl.last_frame = time.time()
        start = ctl._control_current()
        self.assertAlmostEqual(start, 3.0, places=1)
        ctl.generator_ramp_started = time.time() - 15.0
        halfway = ctl._control_current()
        target = ctl._constant_power_target_current()
        self.assertAlmostEqual(halfway, 3.0 + (target - 3.0) * 0.5, delta=0.3)
        self.assertEqual(ctl.generator_control_state, "RAMPING")

    def test_brownout_feedback_backs_current_off(self):
        ctl = self.make_controller(RuntimeController)
        ctl.cfg.update(control_mode="constant_power", current_limit=38.0)
        ctl.state.update(voltage=55.0, input_voltage=205, state_code=0x08)
        ctl.last_frame = time.time()
        ctl.last_commanded_current = 20.0
        current = ctl._control_current()
        self.assertAlmostEqual(current, 18.0, places=1)
        self.assertEqual(ctl.generator_control_state, "AC LOW - BACKING OFF")

    def test_hard_ac_trip_resets_to_start_current_and_learns_lower_cap(self):
        ctl = self.make_controller(RuntimeController)
        ctl.cfg.update(control_mode="constant_power", current_limit=38.0, generator_start_current=3.0)
        ctl.state.update(voltage=55.0, input_voltage=0, state_code=0x0C)
        ctl.last_frame = time.time()
        ctl.last_commanded_current = 20.0
        ctl.generator_had_good_ac = True
        current = ctl._control_current()
        self.assertAlmostEqual(current, 3.0, places=1)
        self.assertEqual(ctl.generator_control_state, "AC TRIP")
        self.assertEqual(ctl.generator_trip_count, 1)
        self.assertAlmostEqual(ctl.generator_adaptive_current_cap, 17.0, places=1)

    def test_alarm_state_with_healthy_ac_does_not_learn_trip_cap(self):
        ctl = self.make_controller(RuntimeController)
        now = time.time()
        ctl.cfg.update(
            control_mode="constant_power",
            current_limit=38.0,
            generator_recovery_hold_seconds=0.0,
            generator_ramp_seconds=5.0,
        )
        ctl.state.update(voltage=52.5, input_voltage=239, state_code=0x0C)
        ctl.last_frame = now
        ctl.last_commanded_current = 27.0
        ctl.generator_had_good_ac = True
        ctl.generator_ramp_started = now - 10.0
        ctl.generator_stable_since = now - 10.0

        current = ctl._control_current()
        expected = (1700.0 / 1.10) / 52.5
        self.assertAlmostEqual(current, expected, delta=0.2)
        self.assertIsNone(ctl.generator_adaptive_current_cap)
        self.assertEqual(ctl.generator_trip_count, 0)
        self.assertEqual(ctl.generator_alarm_with_ac_count, 1)

    def test_learned_trip_cap_relaxes_after_stable_ac(self):
        ctl = self.make_controller(RuntimeController)
        now = time.time()
        ctl.cfg.update(
            control_mode="constant_power",
            current_limit=38.0,
            generator_recovery_hold_seconds=0.0,
            generator_ramp_seconds=5.0,
            generator_cap_relax_delay_seconds=60.0,
            generator_cap_relax_interval_seconds=10.0,
            generator_cap_relax_step_amps=1.0,
        )
        ctl.state.update(voltage=55.0, input_voltage=240, state_code=0x08)
        ctl.last_frame = now
        ctl.generator_had_good_ac = True
        ctl.generator_ramp_started = now - 120.0
        ctl.generator_stable_since = now - 61.0
        ctl.generator_adaptive_current_cap = 5.0
        ctl.last_commanded_current = 5.0

        current = ctl._control_current()
        self.assertAlmostEqual(ctl.generator_adaptive_current_cap, 6.0, delta=0.1)
        self.assertAlmostEqual(current, 6.0, delta=0.2)
        self.assertEqual(ctl.generator_control_state, "CAP RECOVERY")

    def test_brownout_resets_cap_recovery_timer(self):
        ctl = self.make_controller(RuntimeController)
        now = time.time()
        ctl.cfg.update(control_mode="constant_power", current_limit=38.0)
        ctl.state.update(voltage=55.0, input_voltage=205, state_code=0x08)
        ctl.last_frame = now
        ctl.generator_stable_since = now - 100.0
        ctl.generator_adaptive_current_cap = 8.0
        ctl.last_commanded_current = 8.0
        ctl._control_current()
        self.assertEqual(ctl.generator_stable_since, 0.0)

    def test_calibration_uses_meter_watts_over_dc_watts(self):
        ctl = self.make_controller(RuntimeController)
        ctl.state["power"] = 1500.0
        factor = ctl.calibrate_generator(1700.0)
        self.assertAlmostEqual(factor, 1.1333, places=4)
        self.assertEqual(ctl.cfg["generator_calibration_factor"], factor)


if __name__ == "__main__":
    unittest.main()
