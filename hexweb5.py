#!/usr/bin/env python3
import os
import json
import threading
import time

from flask import Flask, request, jsonify, Response

# Your existing wrapper that ignores point.txt but uses servo.py calibration
from control_nopoint import ControlNoPoint

# New hardware helpers from the test file you showed
from led import Led
from ultrasonic import Ultrasonic
from adc import ADC
from buzzer import Buzzer

# ---------- Hexapod height constants (your working values) ----------
TABLETOP_Z = 40   # fully raised tabletop pose
RESET_Z    = 15   # normal walking pose
MAX_Z      = 45   # highest allowed
MIN_Z      = -30  # lowest allowed

# ---------- Pan/Tilt setup ----------
PAN_PORT  = 24
TILT_PORT = 25

PAN_MIN, PAN_MAX, TILT_MIN, TILT_MAX = 0, 180, 0, 180
if os.path.exists("pan_tilt_limits.json"):
    try:
        _lims = json.load(open("pan_tilt_limits.json"))
        PAN_MIN  = int(_lims.get("PAN_MIN", 0))
        PAN_MAX  = int(_lims.get("PAN_MAX", 180))
        TILT_MIN = int(_lims.get("TILT_MIN", 0))
        TILT_MAX = int(_lims.get("TILT_MAX", 180))
    except Exception:
        pass

# ---------- Servo Offsets ----------
OFFSETS = {}
if os.path.exists("servo_offsets.json"):
    try:
        OFFSETS = {int(k): int(v) for k, v in json.load(open("servo_offsets.json")).items()}
    except Exception:
        OFFSETS = {}

def with_offset(port, angle):
    off = OFFSETS.get(port, 0)
    a = int(angle + off)
    if a < 0:   a = 0
    if a > 180: a = 180
    return a

# ---------- Global state ----------
app = Flask(__name__)
lock = threading.Lock()

class WebState:
    def __init__(self):
        # Main controller (hexapod motion)
        self.ctrl = ControlNoPoint()   # has self.servo inside

        # Hexapod body height
        self.body_z = RESET_Z
        self.ctrl.move_position(0, 0, self.body_z)

        # Pan/Tilt state
        self.pan_angle  = 90
        self.tilt_angle = 90
        self.ctrl.servo.set_servo_angle(PAN_PORT,  with_offset(PAN_PORT,  self.pan_angle))
        self.ctrl.servo.set_servo_angle(TILT_PORT, with_offset(TILT_PORT, self.tilt_angle))
        time.sleep(0.02)

        # Movement worker state
        self.current_cmd = None     # "fwd", "back", etc.
        self.thread_running = True  # to shut down cleanly

        # Extra hardware
        self.led = Led()
        self.ultrasonic = Ultrasonic()
        self.adc = ADC()
        self.buzzer = Buzzer()

state = WebState()

# ---------- STOP-ALL gate ----------
STOP_ALL = False  # when True, ignore servo commands (soft emergency stop)

_orig_set_servo_angle = state.ctrl.servo.set_servo_angle

def guarded_set_servo_angle(channel, angle):
    if STOP_ALL:
        return
    _orig_set_servo_angle(channel, angle)

state.ctrl.servo.set_servo_angle = guarded_set_servo_angle

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ---------- Movement worker (no queuing, latest command wins) ----------
def movement_worker():
    """
    Runs in the background:
      - Reads state.current_cmd
      - Executes ONE gait/pose step
      - Clears current_cmd
      - If STOP_ALL is set, relax servos and ignore moves
    """
    global STOP_ALL
    while state.thread_running:
        time.sleep(0.01)

        with lock:
            if STOP_ALL:
                # Relax everything and clear any queued command
                state.ctrl.servo.relax()
                state.current_cmd = None
                # keep STOP_ALL true until a new /cmd arrives
                continue

            cmd = state.current_cmd
            z = state.body_z

        if cmd is None:
            continue

        # ---- Execute exactly ONE movement action ----
        if cmd == "fwd":
            state.ctrl.run_gait(['CMD_MOVE', '1', '0', '35', '10', '0'])

        elif cmd == "back":
            state.ctrl.run_gait(['CMD_MOVE', '2', '0', '-35', '10', '10'])

        elif cmd == "right":
            state.ctrl.run_gait(['CMD_MOVE', '1', '35', '0', '10', '0'])

        elif cmd == "left":
            state.ctrl.run_gait(['CMD_MOVE', '1', '-35', '0', '10', '0'])

        elif cmd == "turn_left":
            state.ctrl.run_gait(['CMD_MOVE', '1', '0', '0', '10', '20'])

        elif cmd == "turn_right":
            state.ctrl.run_gait(['CMD_MOVE', '1', '0', '0', '10', '-20'])

        elif cmd == "raise":
            with lock:
                z = clamp(state.body_z + 2, MIN_Z, MAX_Z)
                state.body_z = z
            state.ctrl.move_position(0, 0, z)

        elif cmd == "lower":
            with lock:
                z = clamp(state.body_z - 2, MIN_Z, MAX_Z)
                state.body_z = z
            state.ctrl.move_position(0, 0, z)

        elif cmd == "tabletop":
            with lock:
                state.body_z = clamp(TABLETOP_Z, MIN_Z, MAX_Z)
                z = state.body_z
            state.ctrl.move_position(0, 0, z)

        elif cmd == "reset":
            with lock:
                state.body_z = clamp(RESET_Z, MIN_Z, MAX_Z)
                z = state.body_z
            state.ctrl.move_position(0, 0, z)

        # After finishing one action, clear command (no queue)
        with lock:
            if state.current_cmd == cmd:
                state.current_cmd = None

# Start worker thread
threading.Thread(target=movement_worker, daemon=True).start()

# ---------- Hexapod key mapping ----------
def map_key_to_cmd(key: str):
    if key == "w":
        return "fwd", "Forward"
    elif key == "s":
        return "back", "Backward"
    elif key == "d":
        return "right", "Right"
    elif key == "a":
        return "left", "Left"
    elif key == "j":
        return "turn_left", "Turn Left"
    elif key == "l":
        return "turn_right", "Turn Right"
    elif key == "i":
        return "raise", "Raise body"
    elif key == "k":
        return "lower", "Lower body"
    elif key == "t":
        return "tabletop", "Tabletop pose"
    elif key == "r":
        return "reset", "Reset pose"
    else:
        return None, "Unknown hexapod command"

# ---------- Pan/Tilt ----------
def handle_pan_tilt(cmd: str, step: int):
    with lock:
        pan = state.pan_angle
        tilt = state.tilt_angle
        c = state.ctrl.servo

        def _clamp(v, lo, hi):
            return max(lo, min(hi, int(v)))

        if cmd == "center":
            pan = 90
            tilt = 90

        elif cmd == "relax":
            c.relax()
            state.pan_angle = pan
            state.tilt_angle = tilt
            return pan, tilt

        elif cmd == "pan_left":
            pan = _clamp(pan - step, PAN_MIN, PAN_MAX)

        elif cmd == "pan_right":
            pan = _clamp(pan + step, PAN_MIN, PAN_MAX)

        elif cmd == "tilt_up":
            tilt = _clamp(tilt - step, TILT_MIN, TILT_MAX)

        elif cmd == "tilt_down":
            tilt = _clamp(tilt + step, TILT_MIN, TILT_MAX)

        state.pan_angle = pan
        state.tilt_angle = tilt

        c.set_servo_angle(PAN_PORT,  with_offset(PAN_PORT,  pan))
        c.set_servo_angle(TILT_PORT, with_offset(TILT_PORT, tilt))

        return pan, tilt

# ---------- Buzzer helper ----------
def buzzer_pulse(duration=0.2):
    # run in a small thread so we don't block Flask
    def _run():
        with lock:
            state.buzzer.set_state(True)
        time.sleep(duration)
        with lock:
            state.buzzer.set_state(False)
    threading.Thread(target=_run, daemon=True).start()

# ---------- LED helper ----------
def led_set(mode: str, r: int, g: int, b: int):
    """
    mode: 'solid', 'off', 'blink'
    """
    rgb = [r, g, b]
    if mode == "off":
        with lock:
            state.led.color_wipe([0, 0, 0])
    elif mode == "solid":
        with lock:
            state.led.color_wipe(rgb)
    elif mode == "blink":
        # quick triple blink in a thread
        def _blink():
            for _ in range(3):
                with lock:
                    state.led.color_wipe(rgb)
                time.sleep(0.2)
                with lock:
                    state.led.color_wipe([0, 0, 0])
                time.sleep(0.15)
        threading.Thread(target=_blink, daemon=True).start()

# ---------- Flask routes ----------

@app.route("/")
def index():
    html = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Hexapod Web Control + Sensors</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body{margin:0;background:#0e0f12;color:#eaeef7;font-family:system-ui,Segoe UI,Roboto,Arial}
  .wrap{max-width:980px;margin:18px auto;padding:12px}
  .card{background:#17191f;border:1px solid #20232b;border-radius:16px;padding:16px;margin-bottom:14px}
  h1,h2{margin:0 0 10px}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
  .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  button{padding:10px;border:none;border-radius:12px;background:#222;color:#eee;font-weight:600;cursor:pointer}
  button:active{filter:brightness(1.3)}
  .move{background:#1e88e5}
  .turn{background:#43a047}
  .height{background:#fb8c00}
  .pose{background:#8e24aa}
  .danger{background:#c62828}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .pill{padding:6px 10px;border-radius:999px;background:#0f1218;border:1px solid #232736;display:inline-flex;align-items:center;gap:6px}
  select,input[type=color]{background:#0f1218;color:#eaeef7;border:1px solid #232736;border-radius:999px;padding:4px 8px}
  #hex_status,#pt_status,#sensor_status{margin-top:8px;font-size:13px;color:#9aa4b2;min-height:1.4em}
  kbd{background:#333;border-radius:4px;padding:2px 6px;font-size:11px}
  .stat{font-variant-numeric:tabular-nums}
</style>
</head>
<body>
<div class="wrap">
  <h1>Hexapod Web Control</h1>

  <!-- Hexapod card -->
  <div class="card">
    <h2>Hexapod Movement</h2>
    <p>
      Keys: 
      <kbd>W</kbd>/<kbd>S</kbd>/<kbd>A</kbd>/<kbd>D</kbd> move ·
      <kbd>J</kbd>/<kbd>L</kbd> turn ·
      <kbd>I</kbd>/<kbd>K</kbd> body up/down ·
      <kbd>T</kbd> tabletop ·
      <kbd>R</kbd> reset ·
      <kbd>X</kbd> stop all
    </p>

    <div class="grid3" style="max-width:320px;margin:auto">
      <span></span>
      <button class="move" onclick="sendHex('w')">W<br>Forward</button>
      <span></span>

      <button class="move" onclick="sendHex('a')">A<br>Left</button>
      <span></span>
      <button class="move" onclick="sendHex('d')">D<br>Right</button>

      <span></span>
      <button class="move" onclick="sendHex('s')">S<br>Back</button>
      <span></span>
    </div>

    <div class="grid4" style="margin-top:12px">
      <button class="turn" onclick="sendHex('j')">J<br>Turn Left</button>
      <button class="turn" onclick="sendHex('l')">L<br>Turn Right</button>
      <button class="height" onclick="sendHex('i')">I<br>Raise</button>
      <button class="height" onclick="sendHex('k')">K<br>Lower</button>
      <button class="pose" onclick="sendHex('t')">T<br>Tabletop</button>
      <button class="pose" onclick="sendHex('r')">R<br>Reset</button>
      <button class="danger" onclick="stopAll()">Stop All</button>
      <span></span>
    </div>

    <div id="hex_status"></div>
  </div>

  <!-- Pan/Tilt card -->
  <div class="card">
    <h2>Pan / Tilt</h2>
    <p>
      Keys: <kbd>Arrow keys</kbd> pan/tilt · <kbd>C</kbd> center · <kbd>P</kbd> relax pan/tilt + legs
    </p>

    <div class="row">
      <span class="pill">
        Step:
        <select id="pt_step">
          <option value="1">1°</option>
          <option value="2">2°</option>
          <option value="3" selected>3°</option>
          <option value="5">5°</option>
          <option value="8">8°</option>
        </select>
      </span>
      <span class="pill">PAN <span id="pan_val" class="stat">__PAN__</span>°</span>
      <span class="pill">TILT <span id="tilt_val" class="stat">__TILT__</span>°</span>
    </div>

    <div class="grid3" style="max-width:260px;margin:12px auto 0">
      <span></span>
      <button onclick="sendPT('tilt_up')">▲</button>
      <span></span>

      <button onclick="sendPT('pan_left')">◀</button>
      <button onclick="sendPT('center')">Center</button>
      <button onclick="sendPT('pan_right')">▶</button>

      <span></span>
      <button onclick="sendPT('tilt_down')">▼</button>
      <span></span>
    </div>

    <div class="row" style="margin-top:10px">
      <button onclick="sendPT('relax')" class="danger">Relax (PWM off)</button>
    </div>

    <div id="pt_status"></div>
  </div>

  <!-- Sensors & LED/Buzzer card -->
  <div class="card">
    <h2>Sensors & Effects</h2>
    <div class="row">
      <span class="pill">Battery: <span id="bat_val" class="stat">--.-</span> V</span>
      <span class="pill">Ultrasonic: <span id="dist_val" class="stat">--</span> cm</span>
      <button onclick="refreshSensors()">Refresh Now</button>
      <button onclick="beepOnce()">Beep</button>
    </div>

    <h3 style="margin-top:14px;margin-bottom:6px">LED</h3>
    <div class="row">
      <span class="pill">
        Color:
        <input type="color" id="led_color" value="#00ffff">
      </span>
      <span class="pill">
        Mode:
        <select id="led_mode">
          <option value="solid">Solid</option>
          <option value="blink">Blink 3x</option>
          <option value="off">Off</option>
        </select>
      </span>
      <button onclick="applyLed()">Apply LED</button>
    </div>

    <div id="sensor_status"></div>
  </div>

</div>

<script>
// ---------- Status helpers ----------
function setHexStatus(msg){
  document.getElementById('hex_status').textContent = msg;
}
function setPTStatus(msg){
  document.getElementById('pt_status').textContent = msg;
}
function setSensorStatus(msg){
  document.getElementById('sensor_status').textContent = msg;
}

// ---------- Hexapod AJAX ----------
function sendHex(key){
  fetch('/cmd?key=' + encodeURIComponent(key))
    .then(r => r.json())
    .then(j => {
      setHexStatus(j.message || ('OK: ' + key));
    })
    .catch(err => setHexStatus('Error: ' + err));
}

function stopAll(){
  fetch('/stopall')
    .then(r => r.json())
    .then(j => setHexStatus(j.message || 'All servos relaxed'))
    .catch(err => setHexStatus('Error: ' + err));
}

// ---------- Pan/Tilt AJAX ----------
function ptStep(){
  const v = document.getElementById('pt_step').value;
  return parseInt(v) || 3;
}

function sendPT(cmd){
  fetch('/pt', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd:cmd, step: ptStep()})
  })
    .then(r => r.json())
    .then(j => {
      if('pan' in j) document.getElementById('pan_val').textContent = j.pan;
      if('tilt' in j) document.getElementById('tilt_val').textContent = j.tilt;
      setPTStatus(j.message || 'Pan/Tilt updated');
    })
    .catch(err => setPTStatus('Error: ' + err));
}

// ---------- Sensors ----------
function refreshSensors(){
  fetch('/sensors')
    .then(r => r.json())
    .then(j => {
      if('battery' in j) document.getElementById('bat_val').textContent = j.battery.toFixed(2);
      if('distance' in j) document.getElementById('dist_val').textContent = j.distance.toFixed(1);
      setSensorStatus('Sensors updated');
    })
    .catch(err => setSensorStatus('Error: ' + err));
}

// Auto-refresh sensors every 2.5s
setInterval(refreshSensors, 2500);

// ---------- Buzzer ----------
function beepOnce(){
  fetch('/buzzer', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode:'pulse'})
  })
    .then(r => r.json())
    .then(j => setSensorStatus(j.message || 'Beep'))
    .catch(err => setSensorStatus('Error: ' + err));
}

// ---------- LED ----------
function hexToRgb(hex){
  // "#rrggbb"
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if(!m) return {r:0,g:0,b:0};
  return {
    r: parseInt(m[1],16),
    g: parseInt(m[2],16),
    b: parseInt(m[3],16)
  };
}

function applyLed(){
  const hex = document.getElementById('led_color').value || '#ffffff';
  const mode = document.getElementById('led_mode').value || 'solid';
  const rgb = hexToRgb(hex);
  fetch('/led', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      mode: mode,
      r: rgb.r,
      g: rgb.g,
      b: rgb.b
    })
  })
    .then(r => r.json())
    .then(j => setSensorStatus(j.message || 'LED updated'))
    .catch(err => setSensorStatus('Error: ' + err));
}

// ---------- Keyboard controls ----------
window.addEventListener('keydown', function(ev){
  const k = ev.key.toLowerCase();

  // Hexapod keys
  const hexKeys = ['w','a','s','d','j','l','i','k','t','r','x'];
  if(hexKeys.includes(k)){
    ev.preventDefault();
    if(k === 'x') stopAll();
    else sendHex(k);
    return;
  }

  // Pan/Tilt keys
  if(ev.key === 'ArrowUp'){
    ev.preventDefault();
    sendPT('tilt_up');
  } else if(ev.key === 'ArrowDown'){
    ev.preventDefault();
    sendPT('tilt_down');
  } else if(ev.key === 'ArrowLeft'){
    ev.preventDefault();
    sendPT('pan_left');
  } else if(ev.key === 'ArrowRight'){
    ev.preventDefault();
    sendPT('pan_right');
  } else if(k === 'c'){
    ev.preventDefault();
    sendPT('center');
  } else if(k === 'p'){
    ev.preventDefault();
    sendPT('relax');
  }
});
</script>
</body>
</html>
"""
    html = html.replace("__PAN__", str(state.pan_angle)).replace("__TILT__", str(state.tilt_angle))
    return Response(html, mimetype="text/html")

@app.route("/cmd")
def cmd_route():
    global STOP_ALL
    key = (request.args.get("key") or "").lower()

    with lock:
        # any movement command clears STOP_ALL so it can walk again
        STOP_ALL = False
        cmd, msg = map_key_to_cmd(key)
        if cmd is not None:
            state.current_cmd = cmd
        else:
            msg = "Unknown hexapod command"

    return jsonify({"ok": True, "key": key, "message": msg})

@app.route("/stopall")
def stopall_route():
    global STOP_ALL
    with lock:
        STOP_ALL = True
        state.ctrl.servo.relax()
        state.current_cmd = None
    return jsonify({"ok": True, "message": "All servos relaxed (legs + pan/tilt)"})

@app.post("/pt")
def pt_route():
    data = request.get_json(force=True, silent=True) or {}
    cmd = (data.get("cmd") or "").lower()
    step = int(data.get("step", 3))
    pan, tilt = handle_pan_tilt(cmd, step)
    return jsonify({"ok": True, "pan": pan, "tilt": tilt})

@app.get("/sensors")
def sensors_route():
    """Return battery voltage and ultrasonic distance."""
    try:
        with lock:
            bat = state.adc.read_battery_voltage()
            dist = state.ultrasonic.get_distance()
        return jsonify({"ok": True, "battery": float(bat), "distance": float(dist)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/buzzer")
def buzzer_route():
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "pulse").lower()
    if mode == "pulse":
        buzzer_pulse()
        msg = "Beep pulse triggered"
    elif mode == "on":
        with lock:
            state.buzzer.set_state(True)
        msg = "Buzzer on"
    elif mode == "off":
        with lock:
            state.buzzer.set_state(False)
        msg = "Buzzer off"
    else:
        msg = "Unknown buzzer mode"
    return jsonify({"ok": True, "message": msg})

@app.post("/led")
def led_route():
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "solid").lower()
    r = int(data.get("r", 0))
    g = int(data.get("g", 0))
    b = int(data.get("b", 0))
    led_set(mode, r, g, b)
    return jsonify({"ok": True, "message": f"LED {mode} ({r},{g},{b})"})

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
    finally:
        with lock:
            state.thread_running = False
            state.ctrl.servo.relax()
            # ensure buzzer off and LED off on shutdown
            try:
                state.buzzer.set_state(False)
            except Exception:
                pass
            try:
                state.led.color_wipe([0, 0, 0])
            except Exception:
                pass