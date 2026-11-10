"""Independent, experimental eye-position monitoring for IrisPD.

Tracks each iris relative to its own eye corners in face-local coordinates.
Outputs are normalized eye-width percentages, NOT prism diopters, gaze angles,
or an ophthalmic diagnosis. Offline geometry; no camera or GUI dependencies.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import atan2, degrees, hypot, isfinite
from statistics import median
from pathlib import Path
import csv
from typing import Sequence


@dataclass(frozen=True)
class AlignmentSettings:
    reference_frames: int = 45
    threshold_pct: float = 12.0
    confirmation_s: float = 0.65
    recovery_s: float = 0.60
    max_head_shift: float = 0.055
    sample_interval_s: float = 1.0
    visual: bool = True
    sound: bool = True
    popup: bool = False
    voice: bool = False
    log_events: bool = True
    log_samples: bool = False
    csv_directory: str = ""
    auto_start: bool = False
    auto_recalibrate_on_head_move: bool = False
    auto_recalibrate_delay_s: float = 2.5

    def validate(self) -> None:
        if type(self.reference_frames) is not int or not 10 <= self.reference_frames <= 300:
            raise ValueError("Reference calibration frames must be 10–300")
        if not isfinite(self.threshold_pct) or not 3 <= self.threshold_pct <= 45:
            raise ValueError("Eye-position threshold must be 3–45% of eye width")
        if not isfinite(self.confirmation_s) or not 0.15 <= self.confirmation_s <= 10:
            raise ValueError("Confirmation duration must be 0.15–10 seconds")
        if not isfinite(self.recovery_s) or not 0.15 <= self.recovery_s <= 10:
            raise ValueError("Recovery duration must be 0.15–10 seconds")
        if not isfinite(self.max_head_shift) or not 0.015 <= self.max_head_shift <= 0.2:
            raise ValueError("Head-motion tolerance must be 0.015–0.2")
        if not isfinite(self.sample_interval_s) or not 0.2 <= self.sample_interval_s <= 60:
            raise ValueError("CSV sample interval must be 0.2–60 seconds")
        if not isfinite(self.auto_recalibrate_delay_s) or not 1.0 <= self.auto_recalibrate_delay_s <= 15.0:
            raise ValueError("Auto-reorientation wait must be 1-15 seconds")
        if any(type(getattr(self, k)) is not bool for k in
               ("visual", "sound", "popup", "voice", "log_events", "log_samples", "auto_start",
                "auto_recalibrate_on_head_move")):
            raise ValueError("Alignment switches must be true or false")
        if (self.log_events or self.log_samples) and not self.csv_directory.strip():
            raise ValueError("Choose a folder to save alignment CSV data")


class AutoRecalibrationGate:
    """Debounces a changed but stable head pose. No reset during movement itself.

    The gate must be told whether the head is *steadily displaced* from its
    original reference; invalid frames clear the hold. A cooldown prevents a
    second recalibration if a user shifts position immediately afterward.
    """
    def __init__(self, hold_seconds: float = 2.5, cooldown_seconds: float = 12.0):
        self.hold_seconds = hold_seconds
        self.cooldown_seconds = cooldown_seconds
        self.displaced_since: float | None = None
        self.last_triggered_at: float | None = None

    def clear_pending(self) -> None:
        self.displaced_since = None

    def observe(self, steadily_displaced: bool, now: float) -> bool:
        if not steadily_displaced:
            self.clear_pending()
            return False
        if self.last_triggered_at is not None and now - self.last_triggered_at < self.cooldown_seconds:
            self.clear_pending()
            return False
        if self.displaced_since is None:
            self.displaced_since = now
            return False
        if now - self.displaced_since < self.hold_seconds:
            return False
        self.last_triggered_at = now
        self.displaced_since = None
        return True


@dataclass(frozen=True)
class EyeSample:
    # x increases toward screen right; y increases toward screen bottom,
    # each relative to local eye corners and scaled to that eye's width.
    left_x: float
    left_y: float
    right_x: float
    right_y: float
    left_openness: float
    right_openness: float
    head_yaw_proxy: float
    head_roll_deg: float
    left_iris_xy: tuple[float, float]
    right_iris_xy: tuple[float, float]
    left_width_px: float
    right_width_px: float


def _pt(lm: Sequence, index: int, w: int, h: int) -> tuple[float, float]:
    return (float(lm[index].x) * w, float(lm[index].y) * h)


def _eye_position(lm: Sequence, w: int, h: int, corner_a: int, corner_b: int,
                  upper: int, lower: int, iris: int):
    a = _pt(lm, corner_a, w, h)
    b = _pt(lm, corner_b, w, h)
    p = _pt(lm, iris, w, h)
    dx, dy = b[0] - a[0], b[1] - a[1]
    width = hypot(dx, dy)
    if width < 20 or dx < 8:
        raise ValueError("Eye too small or landmarks swapped")
    ux, uy = dx / width, dy / width
    cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    nx, ny = -uy, ux
    x = .5 + ((p[0] - cx) * ux + (p[1] - cy) * uy) / width
    y = ((p[0] - cx) * nx + (p[1] - cy) * ny) / width
    eyelid_gap = hypot(*tuple(a - b for a, b in zip(_pt(lm, upper, w, h),
                                                       _pt(lm, lower, w, h))))
    return x, y, eyelid_gap / width, p, width, (cx, cy)


def sample_from_face_mesh(landmarks: Sequence, width: int, height: int) -> EyeSample:
    """MediaPipe FaceMesh, refine_landmarks=True (478 landmarks).

    Anatomical LEFT: iris 473, eye corners 362 -> 263 (screen-right).
    Anatomical RIGHT: iris 468, eye corners 33 -> 133 (screen-left).
    These indexes apply to the MIRRORED frame processed by the app; anatomical
    labels must be swapped after cv2.flip(image, 1) to remain correct. Since
    MediaPipe is run *after* the flip, its anatomical interpretation is of the
    mirrored image; we therefore store labels by displayed side explicitly.
    """
    if len(landmarks) < 478:
        raise ValueError("Refined iris landmarks unavailable")
    # Screen right and screen left labels are objectively unambiguous in the
    # mirrored preview. For anatomical identity we map screen-right to the
    # subject's anatomical LEFT when the original camera feed is mirrored.
    # FaceMesh landmark 473 lives at the screen-right side of an unmirrored
    # frontal face but can be on screen-left after mirroring. Don't assume:
    # choose the center by its measured x pixel position.
    a = _eye_position(landmarks, width, height, 33, 133, 159, 145, 468)
    b = _eye_position(landmarks, width, height, 362, 263, 386, 374, 473)
    # These eye corner indices and iris centers have matching handedness on
    # a normal MediaPipe output. If the indices no longer increase in x,
    # sample creation fails safely rather than guessing a side.
    eye_screen_left, eye_screen_right = sorted((a, b), key=lambda z: z[5][0])
    # The application mirrors its preview and *also* runs FaceMesh on that
    # mirror, so screen-left corresponds to anatomical left of the original
    # person. Use anatomical labels in the mirrored interface accordingly.
    L, R = eye_screen_left, eye_screen_right
    center_l = L[5]
    center_r = R[5]
    between = hypot(center_r[0] - center_l[0], center_r[1] - center_l[1])
    nose = _pt(landmarks, 168, width, height)
    if between < 60:
        raise ValueError("Face too small for eye monitoring")
    mid_x = (center_l[0] + center_r[0]) / 2
    head_yaw = (nose[0] - mid_x) / between
    roll = degrees(atan2(center_r[1] - center_l[1], center_r[0] - center_l[0]))
    vals = [*L[:3], *R[:3], head_yaw, roll]
    if not all(isfinite(v) for v in vals):
        raise ValueError("Non-finite eye landmarks")
    return EyeSample(
        left_x=L[0], left_y=L[1], right_x=R[0], right_y=R[1],
        left_openness=L[2], right_openness=R[2],
        head_yaw_proxy=head_yaw, head_roll_deg=roll,
        left_iris_xy=L[3], right_iris_xy=R[3],
        left_width_px=L[4], right_width_px=R[4],
    )


@dataclass(frozen=True)
class AlignmentResult:
    status: str
    note: str
    calibrated: bool = False
    left_dx_pct: float | None = None
    left_dy_pct: float | None = None
    right_dx_pct: float | None = None
    right_dy_pct: float | None = None
    mismatch_pct: float | None = None
    likely_eye: str = "Undetermined"
    axis: str = "Undetermined"
    started: bool = False
    ended: bool = False
    event_id: int | None = None
    peak_pct: float | None = None


class AlignmentEngine:
    """Hold-fixed-target reference, differential iris-position and dwell alerts.

    A differential eye position *may* represent eye drift, but single camera
    images cannot recover visual axes or provide prism-diopter measurements.
    """
    def __init__(self, settings: AlignmentSettings):
        settings.validate()
        self.settings = settings
        self.reference_samples: deque[EyeSample] = deque(maxlen=settings.reference_frames)
        self.baseline: tuple[float, ...] | None = None
        self.candidate_at: float | None = None
        self.candidate_frames = 0
        self.recovery_at: float | None = None
        self.active_id: int | None = None
        self.active_eye = "Undetermined"
        self.active_axis = "Undetermined"
        self.peak = 0.0
        self.event_counter = 0

    def reset_reference(self) -> None:
        self.reference_samples.clear()
        self.baseline = None
        self.candidate_at = None
        self.candidate_frames = 0
        self.recovery_at = None
        self.active_id = None
        self.peak = 0

    def suspend_tracking(self) -> None:
        """End an interrupted candidate/episode without discarding calibration.

        Camera cooling is not a new session. Keep the eye-width normalized
        baseline and any incomplete first-time reference frames; discard only
        transient dwell timers and an active event that cannot be observed.
        """
        self.candidate_at = None
        self.candidate_frames = 0
        self.recovery_at = None
        self.active_id = None
        self.active_eye = "Undetermined"
        self.active_axis = "Undetermined"
        self.peak = 0.0

    @staticmethod
    def _valid(sample: EyeSample | None) -> str:
        if sample is None:
            return "No face or refined iris landmarks"
        if min(sample.left_width_px, sample.right_width_px) < 23:
            return "Move closer to camera"
        if min(sample.left_openness, sample.right_openness) < 0.11:
            return "Blink or partly closed eyelid"
        if abs(sample.head_roll_deg) > 12:
            return "Head tilted too far"
        if abs(sample.head_yaw_proxy) > .18:
            return "Face angled away from camera"
        if not (0.12 < sample.left_x < .88 and 0.12 < sample.right_x < .88):
            return "Iris landmark outside eye; check lighting"
        return ""

    def observe(self, sample: EyeSample | None, time_s: float) -> AlignmentResult:
        problem = self._valid(sample)
        if problem:
            self.candidate_at = None
            self.candidate_frames = 0
            self.recovery_at = None
            return AlignmentResult("PAUSED", problem, self.baseline is not None,
                                   event_id=self.active_id)
        assert sample is not None
        if self.baseline is None:
            self.reference_samples.append(sample)
            n = len(self.reference_samples)
            if n < self.settings.reference_frames:
                return AlignmentResult("CALIBRATING", f"Fixate a stationary target: {n}/{self.settings.reference_frames} reference frames")
            fields = ("left_x", "left_y", "right_x", "right_y", "head_yaw_proxy", "head_roll_deg")
            self.baseline = tuple(median(getattr(s, key) for s in self.reference_samples)
                                  for key in fields)
            max_mad = max(median(abs(getattr(s, k) - self.baseline[i])
                                 for s in self.reference_samples)
                          for i, k in enumerate(fields[:4]))
            if max_mad > .045:
                self.baseline = None
                return AlignmentResult("CALIBRATING", "Reference unstable. Keep eyes on one stationary target")
            return AlignmentResult("READY", "Reference calibrated. Keep looking at the same target", True)

        bx_l, by_l, bx_r, by_r, yaw0, roll0 = self.baseline
        if (abs(sample.head_yaw_proxy - yaw0) > self.settings.max_head_shift
                or abs(sample.head_roll_deg - roll0) > 7.5):
            self.candidate_at = None
            self.candidate_frames = 0
            self.recovery_at = None
            return AlignmentResult("PAUSED", "Head moved relative to reference; face camera squarely", True,
                                   event_id=self.active_id)

        # Subtract the common eye movement so an ordinary conjugate gaze shift
        # doesn't become a unilateral event. Score is inter-eye displacement.
        ldx, ldy = sample.left_x - bx_l, sample.left_y - by_l
        rdx, rdy = sample.right_x - bx_r, sample.right_y - by_r
        diffx, diffy = ldx - rdx, ldy - rdy
        magnitude = 100 * hypot(diffx, diffy)
        lm, rm = hypot(ldx, ldy), hypot(rdx, rdy)
        if lm > rm * 1.65 and lm > self.settings.threshold_pct / 125:
            eye = "LEFT (screen-left)"
        elif rm > lm * 1.65 and rm > self.settings.threshold_pct / 125:
            eye = "RIGHT (screen-right)"
        else:
            eye = "Either eye / uncertain"
        axis = "horizontal" if abs(diffx) >= abs(diffy) else "vertical"
        above = magnitude >= self.settings.threshold_pct
        started = ended = False
        event_id = self.active_id
        peak = self.peak if self.active_id is not None else magnitude
        status = "IN REFERENCE RANGE"
        note = "No sustained differential eye-position change"

        if self.active_id is not None:
            # Preserve the side/axis assigned at the episode onset for its END row.
            eye, axis = self.active_eye, self.active_axis
            if magnitude > self.peak:
                self.peak = magnitude
            peak = self.peak
            status = "POSSIBLE EYE DRIFT"
            note = "Sustained differential position change (not a diagnosis)"
            if magnitude < self.settings.threshold_pct * .6:
                if self.recovery_at is None:
                    self.recovery_at = time_s
                elif time_s - self.recovery_at >= self.settings.recovery_s:
                    ended = True
                    event_id = self.active_id
                    self.active_id = None
                    self.candidate_at = None
                    self.candidate_frames = 0
                    self.recovery_at = None
                    status = "RECOVERED"
                    note = "Position returned toward reference"
            else:
                self.recovery_at = None
        elif above:
            status = "CANDIDATE"
            note = "Potential change; checking whether it persists"
            if self.candidate_at is None:
                self.candidate_at = time_s
                self.candidate_frames = 1
            else:
                self.candidate_frames += 1
            if (time_s - self.candidate_at >= self.settings.confirmation_s
                    and self.candidate_frames >= 3):
                self.event_counter += 1
                self.active_id = self.event_counter
                self.active_eye = eye
                self.active_axis = axis
                self.peak = magnitude
                peak = magnitude
                started = True
                event_id = self.active_id
                status = "POSSIBLE EYE DRIFT"
                note = "Sustained change; review video/clinical examination"
        else:
            self.candidate_at = None
            self.candidate_frames = 0

        return AlignmentResult(
            status, note, True, 100 * ldx, 100 * ldy, 100 * rdx, 100 * rdy,
            magnitude, eye, axis, started, ended, event_id, peak,
        )


EVENT_HEADERS = ["Timestamp (local)", "Session ID", "Event ID", "Transition",
                 "Possible eye", "Dominant axis", "Mismatch (% eye width)",
                 "Peak mismatch (% eye width)", "Notes"]
SAMPLE_HEADERS = ["Timestamp (local)", "Session ID", "Status", "Left Δx (% width)",
                  "Left Δy (% width)", "Right Δx (% width)", "Right Δy (% width)",
                  "Inter-eye mismatch (% width)", "Possible eye", "Dominant axis", "Notes"]


class AppendAlignmentCSV:
    """Append-only CSV with schema checking and flush on each row."""
    def __init__(self, path: str | Path, headers: list[str]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.headers = headers
        nonempty = self.path.exists() and self.path.stat().st_size > 0
        if nonempty:
            with self.path.open("r", newline="", encoding="utf-8-sig") as f:
                if next(csv.reader(f), []) != headers:
                    raise ValueError(f"CSV columns differ at {self.path}; select a new folder")
        self.handle = self.path.open("a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.handle)
        if not nonempty:
            self.writer.writerow(headers)
            self.handle.flush()

    def append(self, row: list) -> None:
        if len(row) != len(self.headers):
            raise ValueError("Alignment CSV column count mismatch")
        self.writer.writerow(row)
        self.handle.flush()

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()
