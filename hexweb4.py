#!/usr/bin/env python3
import os
import json
import threading
import time

from flask import Flask, request, jsonify, Response

from control_nopoint import ControlNoPoint   # your wrapper that ignores point.txt

# ---------- Hexapod height constants (your working values) ----------
TABLETOP_Z = 40   # fully raised tabletop pose
RESET_Z    = 15   # original walking pose
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

# Load servo offsets if present (for all ports, including pan/tilt)
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

# ---------- Global state ----------
app = Flask(__name__)
lock = threading.Lock()

class WebState:
    def __init__(self):
        self.ctrl = ControlNoPoint()   # has self.servo inside

        # Hexapod body height
        self.body_z = RESET_Z
        self.ctrl.move_position(0, 0, self.body_z)

        # Pan/Tilt state
        self.pan_angle  = 90
        self.tilt_angle = 90

        # Initialize pan/tilt servos
        self.ctrl.servo.set_servo_angle(PAN_PORT,  with_offset(PAN_PORT,  self.pan_angle))
        self.ctrl.servo.set_servo_angle(TILT_PORT, with_offset(TILT_PORT, self.tilt_angle))
        time.sleep(0.02)

        # Movement worker state
        self.current_cmd = None     # "fwd", "back", "left", etc.
        self.thread_running = True  # to shut down cleanly

state = WebState()

# ---------- STOP-ALL GATE ----------
STOP_ALL = False  # when True, ignore all servo angle commands

# Monkey-patch servo.set_servo_angle so STOP_ALL cancels future motion
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
                # Leave STOP_ALL as True until a new /cmd comes in
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
            # Only clear if no newer command was written on top
            if state.current_cmd == cmd:
                state.current_cmd = None

# Start worker thread
threading.Thread(target=movement_worker, daemon=True).start()

# ---------- Hexapod command mapping (keys -> logical commands) ----------
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
        return "raise", f"Raise body"
    elif key == "k":
        return "lower", f"Lower body"
    elif key == "t":
        return "tabletop", f"Tabletop pose"
    elif key == "r":
        return "reset", f"Reset pose"
    else:
        return None, "Unknown hexapod command"

# ---------- Pan/Tilt handler ----------
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
            # Relax ALL servos (pan/tilt + legs). Does not change STOP_ALL.
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

# ---------- Flask routes ----------

@app.route("/")
def index():
    html = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Hexapod Web Control + Pan/Tilt</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body{margin:0;background:#0e0f12;color:#eaeef7;font-family:system-ui,Segoe UI,Roboto,Arial}
  .wrap{max-width:960px;margin:18px auto;padding:12px}
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
  select{background:#0f1218;color:#eaeef7;border:1px solid #232736;border-radius:999px;padding:4px 8px}
  #hex_status,#pt_status{margin-top:8px;font-size:13px;color:#9aa4b2;min-height:1.4em}
  kbd{background:#333;border-radius:4px;padding:2px 6px;font-size:11px}
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
      Use buttons or keys:
      <kbd>Arrow keys</kbd> for pan/tilt ·
      <kbd>C</kbd> center ·
      <kbd>P</kbd> relax pan/tilt + legs
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
      <span class="pill">PAN <span id="pan_val">__PAN__</span>°</span>
      <span class="pill">TILT <span id="tilt_val">__TILT__</span>°</span>
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

</div>

<script>
function setHexStatus(msg){
  document.getElementById('hex_status').textContent = msg;
}
function setPTStatus(msg){
  document.getElementById('pt_status').textContent = msg;
}

// ---- Hexapod AJAX ----
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

// ---- Pan/Tilt AJAX ----
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
      setPTStatus(j.message || ('Pan/Tilt updated'));
    })
    .catch(err => setPTStatus('Error: ' + err));
}

// ---- Keyboard controls ----
window.addEventListener('keydown', function(ev){
  const k = ev.key.toLowerCase();

  // Hexapod keys (same as your terminal control)
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

    # Any hex command clears STOP_ALL so movement can resume
    with lock:
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
        STOP_ALL = True         # gate all future set_servo_angle calls
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

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
    finally:
        with lock:
            state.thread_running = False
            state.ctrl.servo.relax()