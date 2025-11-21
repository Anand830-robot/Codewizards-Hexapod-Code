# walk_nopoint.py
#
# Uses Freenove's walking logic (run_gait) but with ControlNoPoint,
# which ignores point.txt calibration and assumes servo.py neutral.

from control_nopoint import ControlNoPoint
import time

c = ControlNoPoint()

print("Standing in initial pose...")
time.sleep(1)

print("Walking forward with Freenove gait (no point.txt calibration)...")
for i in range(6):
    # Same command format as your myCode.py
    data = ['CMD_MOVE', '1', '0', '35', '10', '0']
    c.run_gait(data)

print("Done.")