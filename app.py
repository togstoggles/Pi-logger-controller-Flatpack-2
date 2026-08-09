#!/usr/bin/env python3
import csv
import io
import logging
import os
import struct
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

from flatpack_runtime import FlatpackController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
ctl = FlatpackController(os.environ.get("FLATPACK_CONFIG", os.path.join(BASE, "config.json")))
ctl.cfg.setdefault("persistent_fallback_voltage", 43.7)
APP_STARTED = time.time()
SLEEP_TARGETS = ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target")
USB_POWER_RULE = "/etc/udev/rules.d/99-flatpack-canable-power.rules"


def _target_masked(name):
    path = os.path.join("/etc/systemd/system", name)
    return os.path.islink(path) and os.path.realpath(path) == "/dev/null"


def keep_awake_status():
    sleep_locked = all(_target_masked(name) for name in SLEEP_TARGETS)
    usb_power_locked = os.path.exists(USB_POWER_RULE)
    return {
        "ok": bool(sleep_locked and usb_power_locked),
        "sleep_locked": bool(sleep_locked),
        "usb_power_locked": bool(usb_power_locked),
        "watchdog_armed": True,
    }


def controller_watchdog():
    """Restart the process if the background CAN/control loop ever stalls."""
    while True:
        time.sleep(5)
        now = time.time()
        if now - APP_STARTED < 30:
            continue

        login_interval = max(1.0, float(ctl.cfg.get("login_interval_seconds", 5.0)))
        login_stale_after = max(30.0, login_interval * 6.0)
        last_login = float(getattr(ctl, "last_login", 0.0) or 0.0)
        if not last_login or now - last_login > login_stale_after:
            logging.critical(
                "Controller watchdog: login/control loop stale for %.1f s; forcing systemd restart",
                now - last_login if last_login else now - APP_STARTED,
            )
            os._exit(70)

        if ctl.cfg.get("control_enabled"):
            control_interval = max(0.5, float(ctl.cfg.get("control_interval_seconds", 2.0)))
            control_stale_after = max(15.0, control_interval * 6.0)
            last_control = float(getattr(ctl, "last_control", 0.0) or 0.0)
            if not last_control or now - last_control > control_stale_after:
                logging.critical(
                    "Controller watchdog: setpoint loop stale for %.1f s while armed; forcing restart",
                    now - last_control if last_control else now - APP_STARTED,
                )
                os._exit(71)


threading.Thread(target=ctl.run, name="flatpack-can", daemon=True).start()
threading.Thread(target=controller_watchdog, name="flatpack-watchdog", daemon=True).start()
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live")
def live():
    snap = ctl.snapshot()
    snap.setdefault("settings", {})["persistent_fallback_voltage"] = float(
        ctl.cfg.get("persistent_fallback_voltage", 43.7)
    )
    snap["keep_awake"] = keep_awake_status()
    return jsonify(snap)


@app.route("/api/history")
def history():
    return jsonify(ctl.history(request.args.get("hours", "24")))


@app.route("/api/settings", methods=["POST"])
def settings():
    body = request.get_json(force=True)
    try:
        ctl.update_settings(
            body.get("target_voltage"),
            body.get("current_limit"),
            body.get("control_enabled", False),
            body.get("control_mode", "manual"),
            body.get("generator_power_target"),
            body.get("generator_calibration_factor"),
        )
        return jsonify(ok=True, settings=ctl.snapshot()["settings"])
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route("/api/calibrate-generator", methods=["POST"])
def calibrate_generator():
    body = request.get_json(force=True)
    try:
        factor = ctl.calibrate_generator(body.get("meter_watts"))
        return jsonify(ok=True, calibration_factor=factor, settings=ctl.snapshot()["settings"])
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route("/api/default-voltage", methods=["POST"])
def default_voltage():
    body = request.get_json(force=True)
    if body.get("confirmation") not in ("WRITE FALLBACK", "WRITE DEFAULT"):
        return jsonify(ok=False, error="Type WRITE FALLBACK"), 400
    try:
        voltage = float(body.get("voltage"))
        minimum = 40.0
        maximum = float(ctl.cfg.get("max_voltage", 55.6))
        if not minimum <= voltage <= maximum:
            raise ValueError("Fallback voltage outside %.1f-%.1f V" % (minimum, maximum))

        ctl.login()
        time.sleep(0.1)
        payload = b"\x29\x15\x00" + struct.pack("<H", round(voltage * 100))
        if not ctl.send(0x05009C00, payload):
            raise RuntimeError("CAN send failed")

        with ctl.lock:
            ctl.cfg["persistent_fallback_voltage"] = round(voltage, 2)
            ctl.save()
        return jsonify(ok=True, persistent_fallback_voltage=round(voltage, 2))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route("/api/export.csv")
def export_csv():
    rows = ctl.history(request.args.get("hours", "168"))
    output = io.StringIO()
    keys = [
        "timestamp", "voltage", "current", "power", "input_voltage",
        "temp_inlet", "temp_outlet", "state", "online",
    ]
    writer = csv.DictWriter(output, fieldnames=keys)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=flatpack-history.csv"},
    )


@app.route("/health")
def health():
    snap = ctl.snapshot()
    now = time.time()
    diagnostics = snap.get("diagnostics", {})
    diagnostics.update({
        "app_uptime_seconds": round(now - APP_STARTED, 1),
        "last_login_age_seconds": None if not ctl.last_login else round(now - ctl.last_login, 1),
        "last_control_age_seconds": None if not ctl.last_control else round(now - ctl.last_control, 1),
        "watchdog": "armed",
        "keep_awake": keep_awake_status(),
        "persistent_fallback_voltage": float(ctl.cfg.get("persistent_fallback_voltage", 43.7)),
    })
    return jsonify(
        ok=True,
        can_online=snap["online"],
        diagnostics=diagnostics,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
