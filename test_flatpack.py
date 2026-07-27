#!/usr/bin/env python3
import json
import os
import tempfile
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
            "target_voltage": 53.5,
            "current_limit": 20.0,
            "min_voltage": 44.5,
            "max_voltage": 54.4,
            "min_current": 1.0,
            "max_current": 35.0,
            "ovp_voltage": 57.6,
            "database_path": os.path.join(temp_dir.name, "flatpack.db"),
        }
        with open(config_path, "w") as handle:
            json.dump(config, handle)
        return cls(config_path)

    def test_confirmed_status_frame_decodes(self):
        ctl = self.make_controller()
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

    def test_non_status_frame_is_ignored(self):
        ctl = self.make_controller()
        self.assertFalse(ctl.decode(0x05002034, bytes.fromhex("1B 11 51 71 10 20 34 4F")))
        self.assertFalse(ctl.snapshot()["online"])

    def test_compatibility_import_has_same_decode_signature(self):
        ctl = self.make_controller(RuntimeController)
        data = bytes.fromhex("16 7D 00 D5 14 F8 00 26")
        self.assertTrue(ctl.decode(0x05014004, data))


if __name__ == "__main__":
    unittest.main()
