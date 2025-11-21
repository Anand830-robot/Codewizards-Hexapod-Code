#!/usr/bin/env python3
# hexweb8.py
#
# HexWeb 8 layout:
#  - Hexapod movement (same as before)
#  - Phone pan/tilt (ports 24/25)
#  - Built-in head pan/tilt (ports 6/7) + ultrasonic + camera placeholder
#  - System: battery, LED, buzzer
#
# Requires:
#   control_nopoint.py, servo.py, ultrasonic.py, adc.py, led.py, buzzer.py
#   optional: servo_offsets.json, pan_tilt_limits.json

import os
import json
import threading
import time

from flask import Flask, request, jsonify, Response

from control_nopoint import ControlNoPoint
from ultrasonic import Ultrasonic
from adc import ADC
from led import Led
from buzzer import Buzzer

# ---------- Hexapod height constants (your working values) ----------
TABLETOP_Z = 40   # fully raised tabletop pose
RESET_Z    = 15   # original walking pose
MAX_Z      = 45   # highest allowed
MIN_Z      = -30  # lowest allowed

# ---------- Pan/Tilt setup ----------
# Phone pan/tilt rig
PHONE_PAN_PORT  = 24
PHONE_TILT_PORT = 25

# Built-in Freenove head pan/tilt
HEAD_PAN_PORT   = 6
HEAD_TILT_PORT  = 7

# Limits (optional; can be overridden by pan_tilt_limits.json)
PAN_MIN, PAN_MAX, TILT_MIN, TILT_MAX = 0, 180, 0, 180
if os.path.exists("pan_tilt_limits.json"):
    try:
        _lims = json.load(open("pan_tilt_limits.json"))
        PAN_MIN  = int(_lims.get("PAN_MIN", PAN_MIN))
        PAN_MAX  = int(_lims.get("PAN_MAX", PAN_MAX))
        TILT_MIN = int(_lims.get("TILT_MIN", TILT_MIN))
        TILT_MAX = int(_lims.get("TILT_MAX", TILT_MAX))
    except Exception:
        pass

# Load servo offsets if present (for all ports)
OFFSETS = {}
if os.path.exists("servo_offsets.json"):
    try:
        OFFSETS = {int(k): int(v) for k, v in json.load(open("servo_offsets.json")).items()}
    except Exception:
        OFFSETS = {}

def with_offset(port, angle):
    off = OFFSETS.get(port, 0)
    a = int(angle + off)
    if a < 0:
        a = 0
    if a > 180:
        a = 180
    return a

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ---------- Flask + global state ----------
app = Flask(__name__)
lock = threading.Lock()

class WebState:
    def __init__(self):
        # Main hexapod control wrapper (has .servo inside)
        self.ctrl = ControlNoPoint()
        # Body height
        self.body_z = RESET_Z
        self.ctrl.move_position(0, 0, self.body_z)

        # Phone pan/tilt state (24/25)
        self.phone_pan_angle  = 90
        self.phone_tilt_angle = 90
        self.ctrl.servo.set_servo_angle(PHONE_PAN_PORT,  with_offset(PHONE_PAN_PORT,  self.phone_pan_angle))
        self.ctrl.servo.set_servo_angle(PHONE_TILT_PORT, with_offset(PHONE_TILT_PORT, self.phone_tilt_angle))
        time.sleep(0.02)

        # Head pan/tilt state (6/7)
        self.head_pan_angle  = 90
        self.head_tilt_angle = 90
        self.ctrl.servo.set_servo_angle(HEAD_PAN_PORT,  with_offset(HEAD_PAN_PORT,  self.head_pan_angle))
        self.ctrl.servo.set_servo_angle(HEAD_TILT_PORT, with_offset(HEAD_TILT_PORT, self.head_tilt_angle))
        time.sleep(0.02)

        # Sensors + peripherals
        self.ultra   = Ultrasonic()
        self.adc     = ADC()
        self.led     = Led()
        self.buzzer  = Buzzer()

        # For LED UI
        self.led_last_color = [0, 0, 0]

state = WebState()

# ---------- Hexapod command handler (same logic as your working hexcontrol) ----------
def handle_hex_command(key: str) -> str:
    """Map keys to Freenove run_gait/move_position like your working hexcontrol.py."""
    with lock:
        c = state.ctrl
        z = state.body_z

        if key == "w":
            # Forward
            c.run_gait(['CMD_MOVE', '1', '0', '35', '10', '0'])
            return "Forward"

        elif key == "s":
            # Backward
            c.run_gait(['CMD_MOVE', '2', '0', '-35', '10', '10'])
            return "Backward"

        elif key == "d":
            # Sidestep Right
            c.run_gait(['CMD_MOVE', '1', '35', '0', '10', '0'])
            return "Sidestep Right"

        elif key == "a":
            # Sidestep Left
            c.run_gait(['CMD_MOVE', '1', '-35', '0', '10', '0'])
            return "Sidestep Left"

        elif key == "j":
            # Turn Left
            c.run_gait(['CMD_MOVE', '1', '0', '0', '10', '20'])
            return "Turn Left"

        elif key == "l":
            # Turn Right
            c.run_gait(['CMD_MOVE', '1', '0', '0', '10', '-20'])
            return "Turn Right"

        elif key == "i":
            # Raise body
            z += 2
            z = clamp(z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Raise body to z={z}"

        elif key == "k":
            # Lower body
            z -= 2
            z = clamp(z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Lower body to z={z}"

        elif key == "t":
            # Tabletop pose
            z = clamp(TABLETOP_Z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Tabletop pose z={z}"

        elif key == "r":
            # Reset pose
            z = clamp(RESET_Z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Reset pose z={z}"

        elif key == "x":
            # Stop all / relax
            state.ctrl.servo.relax()
            return "All servos relaxed"

        else:
            return "Unknown hexapod command"

# ---------- Pan/Tilt handlers ----------
def handle_phone_pan_tilt(cmd: str, step: int):
    """Handle pan/tilt for the phone rig on ports 24/25."""
    with lock:
        pan  = state.phone_pan_angle
        tilt = state.phone_tilt_angle
        step = int(step) if step else 3

        def _cl(v): return clamp(int(v), PAN_MIN, PAN_MAX)
        def _clt(v): return clamp(int(v), TILT_MIN, TILT_MAX)

        if cmd == "center":
            pan, tilt = 90, 90
        elif cmd == "pan_left":
            pan = _cl(pan - step)
        elif cmd == "pan_right":
            pan = _cl(pan + step)
        elif cmd == "tilt_up":
            tilt = _clt(tilt - step)
        elif cmd == "tilt_down":
            tilt = _clt(tilt + step)
        elif cmd == "relax":
            state.ctrl.servo.relax()
            state.phone_pan_angle  = pan
            state.phone_tilt_angle = tilt
            return pan, tilt

        state.phone_pan_angle  = pan
        state.phone_tilt_angle = tilt

        state.ctrl.servo.set_servo_angle(PHONE_PAN_PORT,  with_offset(PHONE_PAN_PORT,  pan))
        state.ctrl.servo.set_servo_angle(PHONE_TILT_PORT, with_offset(PHONE_TILT_PORT, tilt))

        return pan, tilt

def handle_head_pan_tilt(cmd: str, step: int):
    """Handle pan/tilt for the built-in head on ports 6/7."""
    with lock:
        pan  = state.head_pan_angle
        tilt = state.head_tilt_angle
        step = int(step) if step else 3

        def _cl(v): return clamp(int(v), PAN_MIN, PAN_MAX)
        def _clt(v): return clamp(int(v), TILT_MIN, TILT_MAX)

        if cmd == "center":
            pan, tilt = 90, 90
        elif cmd == "pan_left":
            pan = _cl(pan - step)
        elif cmd == "pan_right":
            pan = _cl(pan + step)
        elif cmd == "tilt_up":
            tilt = _clt(tilt - step)
        elif cmd == "tilt_down":
            tilt = _clt(tilt + step)

        state.head_pan_angle  = pan
        state.head_tilt_angle = tilt

        state.ctrl.servo.set_servo_angle(HEAD_PAN_PORT,  with_offset(HEAD_PAN_PORT,  pan))
        state.ctrl.servo.set_servo_angle(HEAD_TILT_PORT, with_offset(HEAD_TILT_PORT, tilt))

        return pan, tilt

# ---------- Sensors & peripherals ----------
@app.get("/battery")
def battery_route():
    with lock:
        try:
            v = float(state.adc.read_battery_voltage())
        except Exception:
            v = 0.0
    status = "No reading"
    if v > 0:
        if v < 6.5:
            status = "LOW"
        elif v < 7.4:
            status = "OK"
        else:
            status = "FULL"
    return jsonify({"ok": True, "voltage": round(v, 2), "status": status})

@app.get("/ultra")
def ultra_route():
    with lock:
        try:
            d = float(state.ultra.get_distance())
        except Exception:
            d = -1.0
    if d <= 0:
        status = "No echo"
    elif d < 10:
        status = "VERY CLOSE"
    elif d < 25:
        status = "Close"
    else:
        status = "Clear"
    return jsonify({"ok": True, "distance_cm": round(d, 1), "status": status})

# ---------- LED control ----------
@app.post("/led")
def led_route():
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "solid").lower()
    r = int(data.get("r", 0))
    g = int(data.get("g", 0))
    b = int(data.get("b", 0))

    def apply_color(rgb):
        with lock:
            state.led.color_wipe(rgb)
            state.led_last_color = rgb[:]

    if mode == "off":
        apply_color([0, 0, 0])
        msg = "LEDs off"
    elif mode == "solid":
        r = clamp(r, 0, 255)
        g = clamp(g, 0, 255)
        b = clamp(b, 0, 255)
        apply_color([r, g, b])
        msg = f"LED solid color ({r},{g},{b})"
    elif mode == "red":
        apply_color([255, 0, 0])
        msg = "LED red"
    elif mode == "green":
        apply_color([0, 255, 0])
        msg = "LED green"
    elif mode == "blue":
        apply_color([0, 0, 255])
        msg = "LED blue"
    elif mode == "alert":
        # simple alert flash pattern in a background thread
        def _alert():
            for _ in range(3):
                apply_color([255, 0, 0])
                time.sleep(0.2)
                apply_color([0, 0, 0])
                time.sleep(0.2)
        threading.Thread(target=_alert, daemon=True).start()
        msg = "LED alert pattern"
    else:
        msg = "Unknown LED mode"

    return jsonify({"ok": True, "message": msg, "last_color": state.led_last_color})

# ---------- Buzzer control ----------
@app.post("/beep")
def beep_route():
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "short").lower()

    def beep_short():
        with lock:
            state.buzzer.set_state(True)
        time.sleep(0.15)
        with lock:
            state.buzzer.set_state(False)

    def beep_long():
        with lock:
            state.buzzer.set_state(True)
        time.sleep(0.5)
        with lock:
            state.buzzer.set_state(False)

    if mode == "short":
        threading.Thread(target=beep_short, daemon=True).start()
        msg = "Short beep"
    elif mode == "long":
        threading.Thread(target=beep_long, daemon=True).start()
        msg = "Long beep"
    elif mode == "triple":
        def triple():
            for _ in range(3):
                beep_short()
                time.sleep(0.15)
        threading.Thread(target=triple, daemon=True).start()
        msg = "Triple beep"
    else:
        msg = "Unknown beep mode"

    return jsonify({"ok": True, "message": msg})

# ---------- Routes: hexapod + pan/tilt ----------
@app.route("/cmd")
def cmd_route():
    key = (request.args.get("key") or "").lower()
    msg = handle_hex_command(key)
    return jsonify({"ok": True, "key": key, "message": msg, "body_z": state.body_z})

@app.route("/stopall")
def stopall_route():
    with lock:
        state.ctrl.servo.relax()
    return jsonify({"ok": True, "message": "All servos relaxed (legs + pan/tilt)"})

@app.post("/pt_phone")
def pt_phone_route():
    data = request.get_json(force=True, silent=True) or {}
    cmd  = (data.get("cmd") or "").lower()
    step = int(data.get("step", 3))
    pan, tilt = handle_phone_pan_tilt(cmd, step)
    return jsonify({"ok": True, "pan": pan, "tilt": tilt})

@app.post("/pt_head")
def pt_head_route():
    data = request.get_json(force=True, silent=True) or {}
    cmd  = (data.get("cmd") or "").lower()
    step = int(data.get("step", 3))
    pan, tilt = handle_head_pan_tilt(cmd, step)
    return jsonify({"ok": True, "pan": pan, "tilt": tilt})

# ---------- Main page ----------
@app.route("/")
def index():
    html = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>HexWeb 8 – Hexapod Control Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body{margin:0;background:#05070c;color:#eaeef7;font-family:system-ui,Segoe UI,Roboto,Arial}
  .wrap{max-width:1100px;margin:18px auto;padding:12px}
  .card{background:#13151d;border:1px solid #242837;border-radius:16px;padding:16px;margin-bottom:16px;box-shadow:0 14px 30px rgba(0,0,0,0.4)}
  h1,h2{margin:0 0 10px}
  h1{font-size:24px}
  h2{font-size:18px}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
  .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  button{padding:10px;border:none;border-radius:12px;background:#22273a;color:#eee;font-weight:600;cursor:pointer;font-size:13px}
  button:active{filter:brightness(1.25)}
  .move{background:#2a7fff}
  .turn{background:#29b36e}
  .height{background:#ff9800}
  .pose{background:#9c27b0}
  .danger{background:#e53935}
  .neutral{background:#22273a}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .pill{padding:6px 10px;border-radius:999px;background:#0e111a;border:1px solid #252a3b;display:inline-flex;align-items:center;gap:6px;font-size:12px}
  select,input[type=number]{background:#0e111a;color:#eaeef7;border:1px solid #252a3b;border-radius:999px;padding:4px 8px;font-size:12px}
  input[type=range]{width:100%}
  #hex_status,#pt_status,#head_status,#sys_status{margin-top:8px;font-size:13px;color:#9aa4b2;min-height:1.4em}
  kbd{background:#333b4f;border-radius:4px;padding:2px 6px;font-size:11px}
  .pill span.value{font-variant-numeric:tabular-nums}
  .mini-label{font-size:11px;text-transform:uppercase;letter-spacing:0.05em;color:#9aa4b2}
  .cam-box{margin-top:10px;border-radius:12px;border:1px dashed #394157;background:#090b12;padding:14px;font-size:13px;color:#9aa4b2;text-align:center}
  .big-number{font-size:26px;font-variant-numeric:tabular-nums}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px}
  .badge.ok{background:#1b5e20}
  .badge.low{background:#b71c1c}
  .badge.full{background:#283593}
  .ultra-indicator{padding:8px 10px;border-radius:10px;background:#0b1019;border:1px solid #25304a;display:flex;justify-content:space-between;align-items:center;margin-top:8px}
  .ultra-indicator.near{border-color:#ff9800;background:#26140b}
  .ultra-indicator.very-near{border-color:#e53935;background:#2b0e11}
</style>
</head>
<body>
<div class="wrap">
  <h1>HexWeb 8 – Hexapod Control Console</h1>

  <!-- Hexapod Movement -->
  <div class="card">
    <h2>Hexapod Movement</h2>
    <p class="mini-label">
      Keyboard:
      <kbd>W</kbd>/<kbd>S</kbd>/<kbd>A</kbd>/<kbd>D</kbd> move ·
      <kbd>J</kbd>/<kbd>L</kbd> turn ·
      <kbd>I</kbd>/<kbd>K</kbd> raise/lower body ·
      <kbd>T</kbd> tabletop ·
      <kbd>R</kbd> reset ·
      <kbd>X</kbd> stop all
    </p>

    <div class="row" style="margin-bottom:8px">
      <span class="pill">Body height z = <span id="body_z" class="value">__BODY_Z__</span></span>
    </div>

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
      <button class="danger" onclick="stopAll()">X<br>Stop All</button>
      <span></span>
    </div>

    <div id="hex_status"></div>
  </div>

  <!-- Phone Pan/Tilt -->
  <div class="card">
    <h2>Phone Pan/Tilt Rig (ports 24/25)</h2>
    <p class="mini-label">
      Keyboard:
      <kbd>Arrow Keys</kbd> to aim ·
      <kbd>C</kbd> center ·
      <kbd>P</kbd> relax all
    </p>

    <div class="row">
      <span class="pill">
        Step:
        <select id="pt_phone_step">
          <option value="1">1°</option>
          <option value="2">2°</option>
          <option value="3" selected>3°</option>
          <option value="5">5°</option>
          <option value="8">8°</option>
        </select>
      </span>
      <span class="pill">PAN <span id="phone_pan_val" class="value">__PHONE_PAN__</span>°</span>
      <span class="pill">TILT <span id="phone_tilt_val" class="value">__PHONE_TILT__</span>°</span>
    </div>

    <div class="grid3" style="max-width:260px;margin:12px auto 0">
      <span></span>
      <button onclick="sendPhonePT('tilt_up')">▲</button>
      <span></span>

      <button onclick="sendPhonePT('pan_left')">◀</button>
      <button onclick="sendPhonePT('center')">Center</button>
      <button onclick="sendPhonePT('pan_right')">▶</button>

      <span></span>
      <button onclick="sendPhonePT('tilt_down')">▼</button>
      <span></span>
    </div>

    <div class="row" style="margin-top:10px">
      <button onclick="sendPhonePT('relax')" class="danger">Relax (PWM off)</button>
    </div>

    <div id="pt_status"></div>
  </div>

  <!-- Head control + Ultrasonic + Camera -->
  <div class="card">
    <h2>Head Module – Pan/Tilt + Ultrasonic + Camera</h2>
    <p class="mini-label">Built-in Freenove head servos on ports 6/7</p>

    <div class="row">
      <span class="pill">
        Step:
        <select id="pt_head_step">
          <option value="1">1°</option>
          <option value="2">2°</option>
          <option value="3" selected>3°</option>
          <option value="5">5°</option>
          <option value="8">8°</option>
        </select>
      </span>
      <span class="pill">PAN <span id="head_pan_val" class="value">__HEAD_PAN__</span>°</span>
      <span class="pill">TILT <span id="head_tilt_val" class="value">__HEAD_TILT__</span>°</span>
    </div>

    <div class="grid3" style="max-width:260px;margin:12px auto 0">
      <span></span>
      <button onclick="sendHeadPT('tilt_up')">▲</button>
      <span></span>

      <button onclick="sendHeadPT('pan_left')">◀</button>
      <button onclick="sendHeadPT('center')">Center</button>
      <button onclick="sendHeadPT('pan_right')">▶</button>

      <span></span>
      <button onclick="sendHeadPT('tilt_down')">▼</button>
      <span></span>
    </div>

    <div class="row" style="margin-top:12px">
      <div style="flex:1;min-width:220px">
        <div class="mini-label">Ultrasonic Distance</div>
        <div id="ultra_box" class="ultra-indicator">
          <div>
            <div class="big-number" id="ultra_dist">--.-</div>
            <div style="font-size:12px;color:#9aa4b2">cm</div>
          </div>
          <div>
            <span id="ultra_status" class="badge">--</span><br>
            <button class="neutral" style="margin-top:6px;font-size:12px" onclick="refreshUltra()">Ping</button>
          </div>
        </div>
      </div>
      <div style="flex:1;min-width:220px">
        <div class="mini-label">Camera</div>
        <div class="cam-box">
          <strong>Camera Placeholder</strong><br>
          Live video will appear here once the Pi camera is working
          and a /stream endpoint is added.
        </div>
      </div>
    </div>

    <div id="head_status"></div>
  </div>

  <!-- System: Battery + LED + Buzzer -->
  <div class="card">
    <h2>System – Battery, LED, Buzzer</h2>

    <div class="row" style="margin-bottom:10px;flex-wrap:wrap">
      <div style="min-width:200px">
        <div class="mini-label">Battery</div>
        <div class="row" style="margin-top:4px">
          <span class="pill">Voltage: <span id="bat_voltage" class="value">--.--</span> V</span>
          <span class="pill">Status: <span id="bat_status_txt" class="value">--</span></span>
        </div>
        <button class="neutral" style="margin-top:6px" onclick="refreshBattery()">Refresh Battery</button>
      </div>

      <div style="min-width:200px">
        <div class="mini-label">LED</div>
        <div class="row" style="margin-top:4px">
          <button onclick="sendLED('red')" class="neutral">Red</button>
          <button onclick="sendLED('green')" class="neutral">Green</button>
          <button onclick="sendLED('blue')" class="neutral">Blue</button>
          <button onclick="sendLED('alert')" class="danger">Alert</button>
          <button onclick="sendLED('off')" class="neutral">Off</button>
        </div>
        <div class="row" style="margin-top:6px">
          <span class="pill">
            Custom RGB:
            R <input type="number" id="led_r" min="0" max="255" value="255" style="width:60px">
            G <input type="number" id="led_g" min="0" max="255" value="255" style="width:60px">
            B <input type="number" id="led_b" min="0" max="255" value="255" style="width:60px">
          </span>
          <button onclick="sendLED('solid')" class="neutral">Apply</button>
        </div>
      </div>

      <div style="min-width:200px">
        <div class="mini-label">Buzzer</div>
        <div class="row" style="margin-top:4px">
          <button onclick="sendBeep('short')" class="neutral">Short Beep</button>
          <button onclick="sendBeep('long')" class="neutral">Long Beep</button>
          <button onclick="sendBeep('triple')" class="neutral">Triple Beep</button>
        </div>
      </div>
    </div>

    <div id="sys_status"></div>
  </div>

</div>

<script>
function setHexStatus(msg){
  document.getElementById('hex_status').textContent = msg;
}
function setPTStatus(msg){
  document.getElementById('pt_status').textContent = msg;
}
function setHeadStatus(msg){
  document.getElementById('head_status').textContent = msg;
}
function setSysStatus(msg){
  document.getElementById('sys_status').textContent = msg;
}

// ---- Hexapod AJAX ----
function sendHex(key){
  fetch('/cmd?key=' + encodeURIComponent(key))
    .then(r => r.json())
    .then(j => {
      if('body_z' in j){
        document.getElementById('body_z').textContent = j.body_z;
      }
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

// ---- Phone Pan/Tilt ----
function phoneStep(){
  const v = document.getElementById('pt_phone_step').value;
  return parseInt(v) || 3;
}

function sendPhonePT(cmd){
  fetch('/pt_phone', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd:cmd, step: phoneStep()})
  })
    .then(r => r.json())
    .then(j => {
      if('pan' in j)  document.getElementById('phone_pan_val').textContent  = j.pan;
      if('tilt' in j) document.getElementById('phone_tilt_val').textContent = j.tilt;
      setPTStatus(j.message || 'Phone pan/tilt updated');
    })
    .catch(err => setPTStatus('Error: ' + err));
}

// ---- Head Pan/Tilt ----
function headStep(){
  const v = document.getElementById('pt_head_step').value;
  return parseInt(v) || 3;
}

function sendHeadPT(cmd){
  fetch('/pt_head', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd:cmd, step: headStep()})
  })
    .then(r => r.json())
    .then(j => {
      if('pan' in j)  document.getElementById('head_pan_val').textContent  = j.pan;
      if('tilt' in j) document.getElementById('head_tilt_val').textContent = j.tilt;
      setHeadStatus('Head pan/tilt updated');
    })
    .catch(err => setHeadStatus('Error: ' + err));
}

// ---- Ultrasonic ----
function refreshUltra(){
  fetch('/ultra')
    .then(r => r.json())
    .then(j => {
      const dEl = document.getElementById('ultra_dist');
      const sEl = document.getElementById('ultra_status');
      const box = document.getElementById('ultra_box');
      if('distance_cm' in j) dEl.textContent = j.distance_cm.toFixed ? j.distance_cm.toFixed(1) : j.distance_cm;
      if('status' in j) sEl.textContent = j.status;

      box.className = 'ultra-indicator';
      if(j.distance_cm > 0){
        if(j.distance_cm < 10){
          box.className += ' very-near';
        }else if(j.distance_cm < 25){
          box.className += ' near';
        }
      }
    })
    .catch(err => {
      setHeadStatus('Ultrasonic error: ' + err);
    });
}

// Auto-refresh ultrasonic every 1.5s
setInterval(refreshUltra, 1500);

// ---- Battery ----
function refreshBattery(){
  fetch('/battery')
    .then(r => r.json())
    .then(j => {
      if('voltage' in j) document.getElementById('bat_voltage').textContent = j.voltage.toFixed ? j.voltage.toFixed(2) : j.voltage;
      if('status' in j){
        document.getElementById('bat_status_txt').textContent = j.status;
      }
      setSysStatus('Battery updated');
    })
    .catch(err => setSysStatus('Battery error: ' + err));
}

// ---- LED ----
function sendLED(mode){
  const body = {mode:mode};
  if(mode === 'solid'){
    body.r = parseInt(document.getElementById('led_r').value) || 0;
    body.g = parseInt(document.getElementById('led_g').value) || 0;
    body.b = parseInt(document.getElementById('led_b').value) || 0;
  }
  fetch('/led', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  })
    .then(r => r.json())
    .then(j => {
      setSysStatus(j.message || 'LED updated');
    })
    .catch(err => setSysStatus('LED error: ' + err));
}

// ---- Buzzer ----
function sendBeep(mode){
  fetch('/beep', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode:mode})
  })
    .then(r => r.json())
    .then(j => {
      setSysStatus(j.message || 'Beep sent');
    })
    .catch(err => setSysStatus('Beep error: ' + err));
}

// ---- Keyboard controls ----
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

  // Phone pan/tilt with arrows + C/P
  if(ev.key === 'ArrowUp'){
    ev.preventDefault();
    sendPhonePT('tilt_up');
  } else if(ev.key === 'ArrowDown'){
    ev.preventDefault();
    sendPhonePT('tilt_down');
  } else if(ev.key === 'ArrowLeft'){
    ev.preventDefault();
    sendPhonePT('pan_left');
  } else if(ev.key === 'ArrowRight'){
    ev.preventDefault();
    sendPhonePT('pan_right');
  } else if(k === 'c'){
    ev.preventDefault();
    sendPhonePT('center');
  } else if(k === 'p'){
    ev.preventDefault();
    sendPhonePT('relax');
  }
});

// Initial battery + ultrasonic refresh
refreshBattery();
refreshUltra();
</script>
</body>
</html>
"""
    html = (html
            .replace("__BODY_Z__", str(state.body_z))
            .replace("__PHONE_PAN__", str(state.phone_pan_angle))
            .replace("__PHONE_TILT__", str(state.phone_tilt_angle))
            .replace("__HEAD_PAN__", str(state.head_pan_angle))
            .replace("__HEAD_TILT__", str(state.head_tilt_angle)))
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        with lock:
            state.ctrl.servo.relax()