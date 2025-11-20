from flask import Flask, request, jsonify, Response
from control_nopoint import ControlNoPoint
import threading
import time

app = Flask(__name__)

# === HEIGHT CONSTANTS (your working values) ===
TABLETOP_Z = 40   # fully raised tabletop pose
RESET_Z    = 15   # original walking pose
MAX_Z      = 45   # highest allowed
MIN_Z      = -30  # lowest allowed

lock = threading.Lock()

class HexState:
    def __init__(self):
        self.ctrl = ControlNoPoint()
        self.body_z = RESET_Z
        # start in reset pose
        self.ctrl.move_position(0, 0, self.body_z)

state = HexState()

def clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))

def handle_command(key: str) -> str:
    """Same mapping as your working hexcontrol.py, but callable from web."""
    with lock:
        c = state.ctrl
        z = state.body_z

        if key == "w":
            c.run_gait(['CMD_MOVE', '1', '0', '35', '10', '0'])
            return "Forward"

        elif key == "s":
            c.run_gait(['CMD_MOVE', '2', '0', '-35', '10', '10'])
            return "Backward"

        elif key == "d":
            c.run_gait(['CMD_MOVE', '1', '35', '0', '10', '0'])
            return "Right"

        elif key == "a":
            c.run_gait(['CMD_MOVE', '1', '-35', '0', '10', '0'])
            return "Left"

        elif key == "j":
            c.run_gait(['CMD_MOVE', '1', '0', '0', '10', '20'])
            return "Turn Left"

        elif key == "l":
            c.run_gait(['CMD_MOVE', '1', '0', '0', '10', '-20'])
            return "Turn Right"

        elif key == "i":
            z += 2
            z = clamp(z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Raise body to z={z}"

        elif key == "k":
            z -= 2
            z = clamp(z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Lower body to z={z}"

        elif key == "t":
            z = clamp(TABLETOP_Z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Tabletop pose z={z}"

        elif key == "r":
            z = clamp(RESET_Z, MIN_Z, MAX_Z)
            state.body_z = z
            c.move_position(0, 0, z)
            return f"Reset pose z={z}"

        elif key == "q":
            # No “quit” action on robot from web, just acknowledge
            return "Quit (no action)"

        else:
            return "Unknown command"


@app.route("/")
def index():
    # Serve a simple HTML control page
    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Hexapod Web Control</title>
<style>
  body { font-family: sans-serif; background:#111; color:#eee; text-align:center; }
  .btn-grid { display:inline-grid; grid-template-columns: repeat(3, 90px); grid-gap:10px; margin-top:20px;}
  button { padding:10px; font-size:16px; border-radius:8px; border:none; cursor:pointer; }
  .move { background:#1e88e5; color:white; }
  .turn { background:#43a047; color:white; }
  .height { background:#fb8c00; color:white; }
  .pose { background:#8e24aa; color:white; grid-column: span 3; }
  #status { margin-top:20px; font-size:14px; min-height:1.5em; }
  kbd { background:#333; border-radius:4px; padding:2px 6px; }
</style>
</head>
<body>
<h1>Hexapod Web Control</h1>
<p>Use the buttons or keyboard keys:</p>
<p>
  <kbd>W</kbd>/<kbd>S</kbd>/<kbd>A</kbd>/<kbd>D</kbd> – move ·
  <kbd>J</kbd>/<kbd>L</kbd> – turn ·
  <kbd>I</kbd>/<kbd>K</kbd> – body up/down ·
  <kbd>T</kbd> tabletop · <kbd>R</kbd> reset
</p>

<div class="btn-grid">
  <span></span>
  <button class="move" onclick="sendCmd('w')">W<br>Forward</button>
  <span></span>

  <button class="move" onclick="sendCmd('a')">A<br>Left</button>
  <span></span>
  <button class="move" onclick="sendCmd('d')">D<br>Right</button>

  <span></span>
  <button class="move" onclick="sendCmd('s')">S<br>Back</button>
  <span></span>

  <button class="turn" onclick="sendCmd('j')">J<br>Turn Left</button>
  <span></span>
  <button class="turn" onclick="sendCmd('l')">L<br>Turn Right</button>

  <button class="height" onclick="sendCmd('i')">I<br>Raise</button>
  <span></span>
  <button class="height" onclick="sendCmd('k')">K<br>Lower</button>

  <button class="pose" onclick="sendCmd('t')">T – Tabletop Pose</button>
  <button class="pose" onclick="sendCmd('r')">R – Reset Pose</button>
</div>

<div id="status"></div>

<script>
function sendCmd(key) {
  fetch('/cmd?key=' + encodeURIComponent(key))
    .then(r => r.json())
    .then(data => {
      document.getElementById('status').textContent = data.message;
    })
    .catch(err => {
      document.getElementById('status').textContent = 'Error: ' + err;
    });
}

// Keyboard support: same as your terminal version
window.addEventListener('keydown', function(ev) {
  const k = ev.key.toLowerCase();
  const allowed = ['w','a','s','d','j','l','i','k','t','r'];
  if (allowed.includes(k)) {
    ev.preventDefault();
    sendCmd(k);
  }
});
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.route("/cmd")
def cmd():
    key = request.args.get("key", "").lower()
    msg = handle_command(key)
    return jsonify({"ok": True, "key": key, "message": msg})


if __name__ == "__main__":
    # Run on all interfaces so you can reach it from another device on your network
    app.run(host="0.0.0.0", port=5000)