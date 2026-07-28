#!/usr/bin/env python3
import json
import logging
import os
import re
import sqlite3
import struct
import subprocess
import threading
import time
from collections import deque

STATES = {
    0x04: "Constant voltage",
    0x08: "Constant current",
    0x0C: "Alarm",
    0x10: "Walk-in",
}

CANDUMP_RE = re.compile(r"^\((?P<ts>[0-9.]+)\)\s+\S+\s+(?P<id>[0-9A-Fa-f]{8})#(?P<data>[0-9A-Fa-f]*)$")


class FlatpackController:
    def __init__(self, path):
        self.path = path
        with open(path, "r") as handle:
            self.cfg = json.load(handle)
        self.cfg.setdefault("control_mode", "manual")
        self.cfg.setdefault("generator_power_target", 1700.0)
        self.cfg.setdefault("generator_calibration_factor", 1.12)
        self.cfg.setdefault("control_interval_seconds", 2.0)
        self.cfg.setdefault("current_slew_limit", 1.5)

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.reader = None
        self.last_frame = 0.0
        self.last_login = 0.0
        self.last_control = 0.0
        self.last_sample = 0.0
        self.energy_wh = 0.0
        self.energy_t = 0.0
        self.raw = deque(maxlen=100)
        self.rx_count = 0
        self.status_count = 0
        self.status_candidate_count = 0
        self.last_error = None
        self.last_status_id = None
        self.last_rx_id = None
        self.commanded_current = float(self.cfg.get("current_limit", 20.0))
        self.state = {
            "online": False,
            "state": "Offline",
            "state_code": None,
            "voltage": None,
            "current": None,
            "power": None,
            "input_voltage": None,
            "temp_inlet": None,
            "temp_outlet": None,
            "session_kwh": 0.0,
            "last_seen": None,
            "can_id": None,
        }
        self._init_db()

    @staticmethod
    def _format_hex(data):
        return " ".join("%02X" % byte for byte in data)

    @staticmethod
    def _is_status_frame(can_id, data):
        if len(data) != 8:
            return False
        normalized = can_id & 0x1FFFFFFF
        if normalized == 0x05014004:
            return True
        return ((normalized >> 24) & 0xFF) == 0x05 and ((normalized >> 8) & 0xFF) == 0x40

    def _db(self):
        path = self.cfg["database_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._db()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS samples(
            timestamp REAL NOT NULL,
            voltage REAL,
            current REAL,
            power REAL,
            input_voltage REAL,
            temp_inlet REAL,
            temp_outlet REAL,
            state TEXT,
            online INTEGER NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_timestamp ON samples(timestamp)")
        conn.commit()
        conn.close()

    def save(self):
        temp_path = self.path + ".tmp"
        with open(temp_path, "w") as handle:
            json.dump(self.cfg, handle, indent=2)
        os.replace(temp_path, self.path)

    def send(self, arbitration_id, data):
        frame = "%08X#%s" % (arbitration_id, bytes(data).hex().upper())
        try:
            result = subprocess.run(
                ["cansend", self.cfg["can_channel"], frame],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2,
                universal_newlines=True,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "cansend failed")
            return True
        except Exception as exc:
            self.last_error = "CAN send failed: %s" % exc
            logging.warning(self.last_error)
            return False

    def login(self):
        serial = bytes.fromhex(self.cfg["serial_hex"])
        if len(serial) != 6:
            raise ValueError("serial_hex must be six bytes")
        arbitration_id = 0x05004800 | ((int(self.cfg["psu_id"]) * 4) & 0xFF)
        return self.send(arbitration_id, serial + b"\x00\x00")

    def validate(self, voltage, current):
        if not float(self.cfg["min_voltage"]) <= voltage <= float(self.cfg["max_voltage"]):
            raise ValueError("Voltage outside safety limits")
        if not float(self.cfg["min_current"]) <= current <= float(self.cfg["max_current"]):
            raise ValueError("Current outside safety limits")
        if float(self.cfg["ovp_voltage"]) <= voltage:
            raise ValueError("OVP must exceed target voltage")

    def _desired_current(self):
        mode = self.cfg.get("control_mode", "manual")
        if mode == "manual":
            return float(self.cfg["current_limit"])
        if mode != "constant_power":
            raise ValueError("Unknown control mode")
        voltage = self.state.get("voltage") or float(self.cfg["target_voltage"])
        factor = float(self.cfg.get("generator_calibration_factor", 1.12))
        if factor < 1.0 or factor > 2.0:
            raise ValueError("Calibration factor outside 1.00-2.00")
        dc_target = float(self.cfg["generator_power_target"]) / factor
        desired = dc_target / max(float(voltage), 1.0)
        return max(float(self.cfg["min_current"]), min(float(self.cfg["max_current"]), desired))

    def setpoints(self):
        if not self.cfg["control_enabled"]:
            return False
        voltage = float(self.cfg["target_voltage"])
        desired = self._desired_current()
        slew = max(0.1, float(self.cfg.get("current_slew_limit", 1.5)))
        delta = desired - self.commanded_current
        if delta > slew:
            current = self.commanded_current + slew
        elif delta < -slew:
            current = self.commanded_current - slew
        else:
            current = desired
        current = max(float(self.cfg["min_current"]), min(float(self.cfg["max_current"]), current))
        self.validate(voltage, current)
        payload = struct.pack(
            "<HHHH",
            round(current * 10),
            round(voltage * 100),
            round(voltage * 100),
            round(float(self.cfg["ovp_voltage"]) * 100),
        )
        sent = self.send(0x05FF4004, payload)
        if sent:
            self.commanded_current = current
        return sent

    def update_settings(self, voltage, current, enabled, mode="manual", generator_power=1700.0, calibration_factor=1.12):
        voltage = float(voltage)
        current = float(current)
        generator_power = float(generator_power)
        calibration_factor = float(calibration_factor)
        if mode not in ("manual", "constant_power"):
            raise ValueError("Invalid control mode")
        if not 100.0 <= generator_power <= 3000.0:
            raise ValueError("Generator power target outside 100-3000 W")
        if not 1.0 <= calibration_factor <= 2.0:
            raise ValueError("Calibration factor outside 1.00-2.00")
        self.validate(voltage, current)
        with self.lock:
            self.cfg.update(
                target_voltage=voltage,
                current_limit=current,
                control_enabled=bool(enabled),
                control_mode=mode,
                generator_power_target=generator_power,
                generator_calibration_factor=calibration_factor,
            )
            if mode == "manual":
                self.commanded_current = current
            self.save()
        if enabled:
            self.login()
            time.sleep(0.1)
            self.setpoints()

    def calibrate_generator(self, meter_watts):
        meter_watts = float(meter_watts)
        dc_watts = float(self.state.get("power") or 0.0)
        if meter_watts < 100.0 or meter_watts > 4000.0:
            raise ValueError("Meter reading outside 100-4000 W")
        if dc_watts < 100.0:
            raise ValueError("Live charger output is too low to calibrate")
        factor = meter_watts / dc_watts
        if not 1.0 <= factor <= 2.0:
            raise ValueError("Calculated factor outside 1.00-2.00; wait for a stable reading")
        with self.lock:
            self.cfg["generator_calibration_factor"] = round(factor, 4)
            self.save()
        return self.cfg["generator_calibration_factor"]

    def default_voltage(self, voltage):
        voltage = float(voltage)
        if not float(self.cfg["min_voltage"]) <= voltage <= float(self.cfg["max_voltage"]):
            raise ValueError("Default voltage outside safety limits")
        payload = b"\x29\x15\x00" + struct.pack("<H", round(voltage * 100))
        return self.send(0x05009C00, payload)

    def decode(self, can_id, data):
        normalized = can_id & 0x1FFFFFFF
        if not self._is_status_frame(normalized, data):
            return False
        self.status_candidate_count += 1
        now = time.time()
        try:
            current = struct.unpack("<H", data[1:3])[0] / 10.0
            voltage = struct.unpack("<H", data[3:5])[0] / 100.0
            input_voltage = struct.unpack("<H", data[5:7])[0]
            power = voltage * current
            state_code = normalized & 0xFF
            with self.lock:
                if self.energy_t and self.state["power"] is not None:
                    elapsed = min(max(now - self.energy_t, 0), 10)
                    self.energy_wh += float(self.state["power"]) * elapsed / 3600
                self.energy_t = now
                self.last_frame = now
                self.status_count += 1
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
                    temp_inlet=int(data[0]),
                    temp_outlet=int(data[7]),
                    session_kwh=round(self.energy_wh / 1000, 4),
                    last_seen=now,
                    can_id="0x%08X" % normalized,
                )
            return True
        except Exception as exc:
            self.last_error = "Status decode failed for 0x%08X: %s" % (normalized, exc)
            logging.exception(self.last_error)
            return False

    def _start_reader(self):
        if self.reader and self.reader.poll() is None:
            return
        self.reader = subprocess.Popen(
            ["candump", "-L", self.cfg["can_channel"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        logging.info("Started candump reader on %s", self.cfg["can_channel"])

    def _read_loop(self):
        while not self.stop_event.is_set():
            try:
                self._start_reader()
                line = self.reader.stdout.readline()
                if not line:
                    code = self.reader.poll()
                    self.last_error = "candump exited with code %s" % code
                    logging.warning(self.last_error)
                    time.sleep(1)
                    self.reader = None
                    continue
                line = line.strip()
                match = CANDUMP_RE.match(line)
                if not match:
                    continue
                can_id = int(match.group("id"), 16) & 0x1FFFFFFF
                data_hex = match.group("data")
                if len(data_hex) % 2:
                    continue
                data = bytes.fromhex(data_hex)
                self.rx_count += 1
                self.last_rx_id = "0x%08X" % can_id
                with self.lock:
                    self.raw.appendleft({
                        "timestamp": round(time.time(), 3),
                        "id": "0x%08X" % can_id,
                        "data": self._format_hex(data),
                        "dlc": len(data),
                    })
                if can_id == 0x05014004 or self._is_status_frame(can_id, data):
                    self.decode(can_id, data)
            except Exception as exc:
                self.last_error = "CAN reader error: %s" % exc
                logging.exception(self.last_error)
                if self.reader:
                    try:
                        self.reader.terminate()
                    except Exception:
                        pass
                self.reader = None
                time.sleep(1)

    def sample(self):
        with self.lock:
            state = dict(self.state)
        conn = self._db()
        conn.execute(
            "INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), state["voltage"], state["current"], state["power"],
             state["input_voltage"], state["temp_inlet"], state["temp_outlet"],
             state["state"], int(state["online"])),
        )
        conn.commit()
        conn.close()

    def history(self, hours=24):
        cutoff = time.time() - max(1, min(int(hours), 744)) * 3600
        conn = self._db()
        rows = conn.execute(
            "SELECT * FROM samples WHERE timestamp>=? ORDER BY timestamp", (cutoff,)
        ).fetchall()
        conn.close()
        keys = ["timestamp", "voltage", "current", "power", "input_voltage",
                "temp_inlet", "temp_outlet", "state", "online"]
        return [dict(zip(keys, row)) for row in rows]

    def snapshot(self):
        with self.lock:
            timeout = max(float(self.cfg.get("offline_after_seconds", 8)), 15.0)
            if self.last_frame and time.time() - self.last_frame > timeout:
                self.state["online"] = False
                self.state["state"] = "Offline"
            snapshot = dict(self.state)
            factor = float(self.cfg.get("generator_calibration_factor", 1.12))
            snapshot["commanded_current"] = round(self.commanded_current, 1)
            snapshot["estimated_generator_power"] = round(float(self.state.get("power") or 0.0) * factor)
            snapshot["settings"] = {
                key: self.cfg[key]
                for key in [
                    "control_enabled", "control_mode", "target_voltage", "current_limit",
                    "generator_power_target", "generator_calibration_factor",
                    "min_voltage", "max_voltage", "min_current", "max_current", "ovp_voltage",
                ]
            }
            snapshot["raw_frames"] = list(self.raw)[:25]
            snapshot["diagnostics"] = {
                "rx_count": self.rx_count,
                "status_candidate_count": self.status_candidate_count,
                "status_count": self.status_count,
                "last_rx_id": self.last_rx_id,
                "last_status_id": self.last_status_id,
                "last_error": self.last_error,
                "backend": "candump/cansend",
            }
            return snapshot

    def run(self):
        threading.Thread(target=self._read_loop, daemon=True).start()
        while not self.stop_event.is_set():
            now = time.time()
            if now - self.last_login >= float(self.cfg["login_interval_seconds"]):
                self.login()
                self.last_login = now
            interval = float(self.cfg.get("control_interval_seconds", 2.0))
            if self.cfg["control_enabled"] and now - self.last_control >= interval:
                try:
                    self.setpoints()
                except Exception as exc:
                    logging.error("Control blocked: %s", exc)
                self.last_control = now
            if now - self.last_sample >= float(self.cfg["sample_interval_seconds"]):
                self.sample()
                self.last_sample = now
            self.stop_event.wait(0.25)
