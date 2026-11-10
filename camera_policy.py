"""Camera lifecycle settings and conservative motion rejection for IrisPD.

No camera, GUI or network dependencies. Motion gate uses successive relative
face geometry to avoid treating large head moves as pupil or eye-drift events.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from math import atan2, degrees, hypot, isfinite


@dataclass(frozen=True)
class CameraPowerSettings:
    # Real hardware rest requires VideoCapture.release(), not just skipping frames.
    duty_enabled: bool = True
    on_seconds: float = 30.0
    rest_seconds: float = 15.0
    preview_enabled: bool = True
    motion_hold_seconds: float = 1.0

    def validate(self) -> None:
        if type(self.duty_enabled) is not bool or type(self.preview_enabled) is not bool:
            raise ValueError('Camera switches must be true or false')
        for name, low, high in (
            ('on_seconds', 10, 3600), ('rest_seconds', 5, 3600),
            ('motion_hold_seconds', 0.3, 5.0),
        ):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or not isfinite(v) or not low <= v <= high:
                raise ValueError(f'{name} must be between {low} and {high} seconds')


class HeadMotionGate:
    """Reject large sudden head shifts and hold out for a quiet interval.

    Center displacement is measured in units of inter-iris separation. Scale
    and roll changes are also checked. This detects motion only, not head
    direction or true gaze. Rest/reopen should reset the geometry history.
    """
    def __init__(self, hold_seconds: float = 1.0):
        self.hold_seconds = hold_seconds
        self.previous = None
        self.history = deque(maxlen=6)
        self.block_until = 0.0

    def reset(self) -> None:
        self.previous = None
        self.history.clear()
        self.block_until = 0.0

    def accept(self, reading, now: float) -> bool:
        if reading is None:
            self.previous = None
            self.history.clear()
            self.block_until = max(self.block_until, now + .15)
            return False
        a, b = reading.left_xy, reading.right_xy
        span = hypot(b[0] - a[0], b[1] - a[1])
        if span < 35:
            self.previous = None
            self.history.clear()
            self.block_until = max(self.block_until, now + self.hold_seconds)
            return False
        center = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        roll = degrees(atan2(b[1] - a[1], b[0] - a[0]))
        current = (center, span, roll, now)
        prev = self.previous
        self.previous = current
        self.history.append(current)
        # Also reject gradual large movement over recent frames, even if no
        # individual 100 ms frame exceeds the instantaneous movement threshold.
        if len(self.history) >= 3:
            c0,s0,r0,t0 = self.history[0]
            if now-t0 <= 1.5:
                drift = hypot(center[0]-c0[0],center[1]-c0[1]) / max(35,(span+s0)/2)
                apparent_zoom = abs(span-s0) / max(35,s0)
                slow_roll = abs((roll-r0+180)%360-180)
                if drift > .18 or apparent_zoom > .13 or slow_roll > 9:
                    self.block_until=max(self.block_until,now+self.hold_seconds)
                    self.history.clear()
                    self.history.append(current)
            else:
                self.history.clear()
                self.history.append(current)
        if prev is not None:
            (pc, ps, pr, pt) = prev
            dt = now - pt
            if dt <= 0 or dt > 1.5:  # after long gaps require settling
                self.block_until = max(self.block_until, now + self.hold_seconds)
            else:
                # ~10fps webcam: large movement within one processed frame.
                shift = hypot(center[0]-pc[0], center[1]-pc[1]) / max(35, (span+ps)/2)
                zoom = abs(span-ps) / max(35, ps)
                roll_delta = abs((roll-pr+180) % 360 - 180)
                if shift > .13 or zoom > .10 or roll_delta > 8.0:
                    self.block_until = max(self.block_until, now + self.hold_seconds)
                    self.history.clear()
                    self.history.append(current)
        return now >= self.block_until
