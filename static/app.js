let hist = [];
let series = "voltage";
let loaded = false;
const $ = id => document.getElementById(id);
const f = (value, digits = 1) => value == null ? "—" : Number(value).toFixed(digits);

function updateControlVisibility() {
  $("power-controls").style.display = $("cm").value === "constant_power" ? "block" : "none";
}

function updateAwakeStatus(status) {
  const awake = $("awake");
  if (!status) {
    awake.textContent = "AWAKE UNKNOWN";
    awake.className = "pill warn";
    awake.title = "No keep-awake status received from Pi";
    return;
  }

  if (status.ok) {
    awake.textContent = "KEEP AWAKE ✓";
    awake.className = "pill on";
  } else if (status.sleep_locked || status.usb_power_locked) {
    awake.textContent = "AWAKE PARTIAL";
    awake.className = "pill warn";
  } else {
    awake.textContent = "SLEEP RISK";
    awake.className = "pill off";
  }

  awake.title = [
    "Pi sleep/hibernate lock: " + (status.sleep_locked ? "ON" : "OFF"),
    "CANable USB autosuspend lock: " + (status.usb_power_locked ? "ON" : "OFF"),
    "Controller watchdog: " + (status.watchdog_armed ? "ARMED" : "OFF")
  ].join("\n");
}

async function live() {
  try {
    const data = await (await fetch("/api/live", {cache: "no-store"})).json();
    $("v").textContent = f(data.voltage, 2);
    $("a").textContent = f(data.current);
    $("w").textContent = f(data.power, 0);
    $("gw").textContent = f(data.estimated_generator_power, 0);
    $("kwh").textContent = f(data.session_kwh, 3);
    $("vin").textContent = f(data.input_voltage, 0);
    $("temps").textContent = data.temp_inlet == null ? "—" : data.temp_inlet + " / " + data.temp_outlet;
    $("state").textContent = data.state;
    $("mode").textContent = data.state;
    $("cid").textContent = data.can_id || "—";
    $("seen").textContent = data.last_seen ? new Date(data.last_seen * 1000).toLocaleTimeString() : "—";
    $("status").textContent = data.online ? "ONLINE" : "OFFLINE";
    $("status").className = "pill " + (data.online ? "on" : "off");
    $("cca").textContent = f(data.commanded_current);
    updateAwakeStatus(data.keep_awake);

    if (!loaded) {
      const settings = data.settings;
      $("tv").value = settings.target_voltage;
      $("ci").value = settings.current_limit;
      $("cm").value = settings.control_mode || "manual";
      $("gp").value = settings.generator_power_target;
      $("gf").value = settings.generator_calibration_factor;
      $("en").checked = settings.control_enabled;
      $("dv").value = settings.persistent_fallback_voltage == null ? 43.7 : settings.persistent_fallback_voltage;
      $("vl").textContent = settings.min_voltage + "–" + settings.max_voltage + " V";
      $("cl").textContent = settings.min_current + "–" + settings.max_current + " A";
      updateControlVisibility();
      loaded = true;
    }

    $("frames").innerHTML = (data.raw_frames || []).map(frame =>
      `<tr><td>${new Date(frame.timestamp * 1000).toLocaleTimeString()}</td><td>${frame.id}</td><td>${frame.data}</td></tr>`
    ).join("");
  } catch (error) {
    $("status").textContent = "SERVER ERROR";
    $("status").className = "pill off";
    $("awake").textContent = "AWAKE UNKNOWN";
    $("awake").className = "pill warn";
  }
}

async function history() {
  hist = await (await fetch("/api/history?hours=24", {cache: "no-store"})).json();
  draw();
}

function draw() {
  const values = hist.map((row, index) => [index, row[series]]).filter(item => item[1] != null);
  if (values.length < 2) {
    $("line").setAttribute("points", "");
    $("mn").textContent = "No data";
    $("mx").textContent = "";
    return;
  }

  const numbers = values.map(item => Number(item[1]));
  let min = Math.min(...numbers);
  let max = Math.max(...numbers);
  if (max === min) {
    max += 1;
    min -= 1;
  }
  const padding = (max - min) * 0.08;
  min -= padding;
  max += padding;

  $("line").setAttribute("points", values.map((item, index) => {
    const x = index / (values.length - 1) * 1000;
    const y = 270 - (Number(item[1]) - min) / (max - min) * 250;
    return x + "," + y;
  }).join(" "));
  $("mn").textContent = min.toFixed(1);
  $("mx").textContent = max.toFixed(1);
}

document.querySelectorAll(".tabs button").forEach(button => {
  button.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(item => item.classList.remove("active"));
    button.classList.add("active");
    series = button.dataset.s;
    draw();
  };
});

$("cm").onchange = updateControlVisibility;

$("apply").onclick = async () => {
  const result = await (await fetch("/api/settings", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      target_voltage: Number($("tv").value),
      current_limit: Number($("ci").value),
      control_enabled: $("en").checked,
      control_mode: $("cm").value,
      generator_power_target: Number($("gp").value),
      generator_calibration_factor: Number($("gf").value)
    })
  })).json();
  $("res").textContent = result.ok ? "Settings applied." : "Blocked: " + result.error;
};

$("calibrate").onclick = async () => {
  const result = await (await fetch("/api/calibrate-generator", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({meter_watts: Number($("mw").value)})
  })).json();
  if (result.ok) {
    $("gf").value = result.calibration_factor;
    $("cres").textContent = "Calibration saved: " + Number(result.calibration_factor).toFixed(3);
  } else {
    $("cres").textContent = "Blocked: " + result.error;
  }
};

$("write").onclick = async () => {
  const result = await (await fetch("/api/default-voltage", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      voltage: Number($("dv").value),
      confirmation: $("confirm").value
    })
  })).json();
  if (result.ok) {
    $("dv").value = result.persistent_fallback_voltage;
    $("confirm").value = "";
    $("dres").textContent = "Persistent CAN-failure fallback written: " + Number(result.persistent_fallback_voltage).toFixed(1) + " V.";
  } else {
    $("dres").textContent = "Blocked: " + result.error;
  }
};

live();
history();
setInterval(live, 1000);
setInterval(history, 30000);
