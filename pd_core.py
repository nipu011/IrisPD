"""Pure geometry for IrisPD. No camera, GUI, network, or model dependencies."""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import atan2, degrees, hypot, isfinite
from statistics import median
from typing import Sequence

IRIS_DIAMETER_MM = 11.7  # population estimate, NOT a person-specific calibration
ID1_CARD_WIDTH_MM = 85.60
CAPTURE_FRAMES = 30


@dataclass(frozen=True)
class Reading:
    total_mm: float
    screen_left_mm: float
    screen_right_mm: float
    left_xy: tuple[float, float]
    right_xy: tuple[float, float]
    bridge_xy: tuple[float, float]
    iris_diameters_px: tuple[float, float]
    pixels_per_mm: float
    eye_spacing_px: float
    roll_deg: float
    nose_offset_fraction: float
    method: str
    warnings: tuple[str, ...] = ()

    @property
    def acceptable(self) -> bool:
        return not self.warnings


def _point(landmarks: Sequence, idx: int, width: int, height: int) -> tuple[float, float]:
    lm = landmarks[idx]
    return (float(lm.x) * width, float(lm.y) * height)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def _iris(landmarks: Sequence, start: int, width: int, height: int):
    """One center landmark + four ring landmarks; opposing pairs yield diameter."""
    center = _point(landmarks, start, width, height)
    ring = [_point(landmarks, i, width, height) for i in range(start + 1, start + 5)]
    diameter = (_distance(ring[0], ring[2]) + _distance(ring[1], ring[3])) / 2.0
    return center, diameter


def geometry_from_points(
    screen_left: tuple[float, float],
    screen_right: tuple[float, float],
    bridge: tuple[float, float],
    iris_diameters_px: tuple[float, float],
    *,
    pixels_per_mm: float | None = None,
    method: str = "Iris (11.7 mm assumption)",
) -> Reading:
    """Project nose bridge onto the iris-center line so L+R always equals total.

    The monocular split is *approximate* because landmark 168 is an anatomical
    proxy, not a measured spectacle-frame/visual-axis centration position.
    """
    if not all(isfinite(v) for p in (screen_left, screen_right, bridge)
               for v in p):
        raise ValueError("Non-finite landmark coordinates")
    if screen_left[0] > screen_right[0]:
        screen_left, screen_right = screen_right, screen_left
        iris_diameters_px = iris_diameters_px[::-1]
    dist_px = _distance(screen_left, screen_right)
    if dist_px < 1:
        raise ValueError("Eye centers are too close")
    d0, d1 = iris_diameters_px
    if min(d0, d1) <= 0:
        raise ValueError("Iris diameter could not be estimated")
    if pixels_per_mm is None:
        pixels_per_mm = ((d0 + d1) / 2.0) / IRIS_DIAMETER_MM
    if not isfinite(pixels_per_mm) or pixels_per_mm <= 0:
        raise ValueError("Invalid calibration scale")

    dx = screen_right[0] - screen_left[0]
    dy = screen_right[1] - screen_left[1]
    ux, uy = dx / dist_px, dy / dist_px
    projected_bridge_px = ((bridge[0] - screen_left[0]) * ux
                           + (bridge[1] - screen_left[1]) * uy)
    # A clamped bridge prevents negative monocular outputs if the model is poor.
    projected_bridge_px = max(0, min(dist_px, projected_bridge_px))
    roll = abs(degrees(atan2(dy, dx)))
    nose_offset = abs((projected_bridge_px / dist_px) - 0.5)
    disparity = abs(d0 - d1) / ((d0 + d1) / 2.0)
    total_mm = dist_px / pixels_per_mm
    warnings = []
    if dist_px < 65:
        warnings.append("Move closer: eyes are too small in the image")
    if min(d0, d1) < 7:
        warnings.append("Move closer or improve light: iris detail is limited")
    if roll > 6:
        warnings.append("Keep your head level")
    if nose_offset > 0.135:
        warnings.append("Face the camera squarely")
    if disparity > 0.24:
        warnings.append("Iris sizes look unequal; avoid side angles")
    if not (48 <= total_mm <= 82):
        warnings.append("Unusual PD: recheck calibration and face alignment")

    return Reading(
        total_mm=total_mm,
        screen_left_mm=projected_bridge_px / pixels_per_mm,
        screen_right_mm=(dist_px - projected_bridge_px) / pixels_per_mm,
        left_xy=screen_left,
        right_xy=screen_right,
        bridge_xy=bridge,
        iris_diameters_px=(d0, d1),
        pixels_per_mm=pixels_per_mm,
        eye_spacing_px=dist_px,
        roll_deg=roll,
        nose_offset_fraction=nose_offset,
        method=method,
        warnings=tuple(warnings),
    )


def reading_from_face_mesh(landmarks: Sequence, width: int, height: int) -> Reading:
    """MediaPipe refined FaceMesh: centers 468 & 473, bridge landmark 168."""
    if len(landmarks) < 478:
        raise ValueError("MediaPipe iris landmarks are unavailable")
    a, da = _iris(landmarks, 468, width, height)
    b, db = _iris(landmarks, 473, width, height)
    bridge = _point(landmarks, 168, width, height)
    return geometry_from_points(a, b, bridge, (da, db))


def apply_card_calibration(
    reading: Reading,
    first: tuple[float, float],
    second: tuple[float, float],
) -> Reading:
    """Card edge endpoints must be marked on the *same, stationary image*.

    Card must be 85.60 mm across its LONG edge, held in the same depth plane
    as the eyes and fronto-parallel to the camera.
    """
    edge_px = _distance(first, second)
    if edge_px < 35:
        raise ValueError("Card points are too close. Choose its long-edge ends.")
    result = geometry_from_points(
        reading.left_xy, reading.right_xy, reading.bridge_xy,
        reading.iris_diameters_px,
        pixels_per_mm=edge_px / ID1_CARD_WIDTH_MM,
        method="Card (85.60 mm, manual endpoints)",
    )
    return result


def use_frame(reading: Reading | None, *, ignore_alignment: bool = False) -> bool:
    """Missing-face frames can never produce a PD; alignment filtering is optional."""
    return reading is not None and (ignore_alignment or not reading.warnings)


def median_capture(
    readings: Sequence[Reading],
    *,
    frames: int = CAPTURE_FRAMES,
    reject_unstable: bool = True,
) -> Reading:
    """Rolling-frame median, with optional (explicitly labeled) quality bypass.

    A bypass DOES NOT make a noisy result accurate; the result carries warnings.
    """
    if type(frames) is not int or not (1 <= frames <= 500):
        raise ValueError("Frame window must be a whole number from 1 to 500")
    if len(readings) < frames:
        raise ValueError(f"Need {frames} usable frames")
    recent = list(readings)[-frames:]
    latest = recent[-1]
    totals = [r.total_mm for r in recent]
    center = median(totals)
    mad = median(abs(v - center) for v in totals)
    span = max(totals) - min(totals)
    unstable = mad > 0.85 or span > 4.0
    if unstable and reject_unstable:
        raise ValueError("Unstable rolling median: hold still or disable instability rejection")
    left = median(r.screen_left_mm for r in recent)
    right = median(r.screen_right_mm for r in recent)
    fraction = left / (left + right) if left + right else 0.5
    warnings = list(dict.fromkeys(w for r in recent for w in r.warnings))
    if unstable:
        warnings.append(f"Unstable rolling median accepted (MAD {mad:.2f} mm; range {span:.2f} mm)")
    unit = "frame" if frames == 1 else "frames"
    return replace(
        latest, total_mm=center,
        screen_left_mm=center * fraction,
        screen_right_mm=center * (1 - fraction),
        method=f"Iris median ({frames} {unit})",
        warnings=tuple(warnings),
    )
