#!/usr/bin/env python3
import csv
import io
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

from flatpack import FlatpackController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
ctl = FlatpackController(os.environ.get("FLATPACK_CONFIG", os.path.join(BASE, "config.json")))
threading.Thread(target=ctl.run, name="flatpack-can", daemon=True).start()
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
        )
        return jsonify(ok=True, settings=ctl.snapshot()["settings"])
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
    return jsonify(
        ok=True,
        can_online=snap["online"],
        diagnostics=snap.get("diagnostics", {}),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
