# control_nopoint.py
#
# Wrapper around Freenove's Control class that DISABLES point.txt calibration
# and just uses the mechanical neutral you set with servo.py (90° == straight).

from control import Control

class ControlNoPoint(Control):
    def calibrate(self):
        """
        Override Freenove's calibration so we DON'T use point.txt at all.
        We just assume your mechanical neutral (servo.py, 90°) is correct,
        so all calibration angles are zero.
        """
        # Freenove expects these to exist, so keep them but zero out the offsets.
        self.leg_positions = [[140, 0, 0] for _ in range(6)]
        self.calibration_angles = [[0, 0, 0] for _ in range(6)]

        # Recompute current_angles based purely on the default leg_positions.
        for i in range(6):
            a, b, c = self.coordinate_to_angle(
                -self.leg_positions[i][2],
                self.leg_positions[i][0],
                self.leg_positions[i][1]
            )
            self.current_angles[i][0] = a
            self.current_angles[i][1] = b
            self.current_angles[i][2] = c
        # No extra offsets applied. Your servo.py 90° neutral is the truth.