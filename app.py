#!/usr/bin/env python3
import csv
import io
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

from flatpack_runtime import FlatpackController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
ctl = FlatpackController(os.environ.get("FLATPACK_CONFIG", os.path.join(BASE, "config.json")))
APP_STARTED = time.time()


def controller_watchdog():
    """Restart the process if the background CAN/control loop ever stalls.

    The Flask server can remain responsive even if its controller thread dies,
    so systemd alone cannot detect that failure. This watchdog deliberately
    exits the whole process on a stale controller heartbeat; systemd then
    restarts it and CAN command transmission resumes automatically.
    """
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
    return jsonify(ctl.snapshot())


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
    if body.get("confirmation") != "WRITE DEFAULT":
        return jsonify(ok=False, error="Type WRITE DEFAULT"), 400
    try:
        ctl.login()
        time.sleep(0.1)
        if not ctl.default_voltage(body.get("voltage")):
            raise RuntimeError("CAN send failed")
        return jsonify(ok=True)
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
    })
    return jsonify(
        ok=True,
        can_online=snap["online"],
        diagnostics=diagnostics,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
