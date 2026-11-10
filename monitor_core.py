"""Testable periodic-monitor decisions and crash-conscious append-only CSV logging.

Does not access webcam, network, or Tk; scheduled by the Tk event loop.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Optional


CSV_HEADERS = [
    "Timestamp (local)", "Session ID", "Reading number", "Set number",
    "Check in set", "Total PD (mm)", "Screen-left (mm)",
    "Screen-right (mm)", "Method", "Check status", "Consecutive checks",
    "Confirmed alert", "Notification sent", "Lower limit (mm)",
    "Upper limit (mm)", "Quality / detection notes",
]


@dataclass(frozen=True)
class MonitorConfig:
    lower_mm: float = 60.0
    upper_mm: float = 70.0
    interval_s: float = 3.0
    confirm_checks: int = 3
    frame_window: int = 30
    ignore_alignment: bool = False
    ignore_instability: bool = False
    visual: bool = True
    sound: bool = True
    popup: bool = False
    voice: bool = False
    repeat_notifications: bool = False
    auto_csv: bool = False
    csv_path: str = ""
    auto_launch_windows: bool = False
    auto_start_monitor: bool = False
    camera_index: int = 0

    def validate(self) -> None:
        if not (isfinite(self.lower_mm) and isfinite(self.upper_mm)
                and 0 < self.lower_mm < self.upper_mm <= 120):
            raise ValueError("Choose limits with 0 < lower < upper <= 120 mm.")
        if not isfinite(self.interval_s) or not 0.5 <= self.interval_s <= 3600:
            raise ValueError("Measurement interval must be 0.5–3600 seconds.")
        if not isinstance(self.confirm_checks, int) or not 1 <= self.confirm_checks <= 60:
            raise ValueError("Consecutive confirmation checks must be between 1 and 60.")
        if type(self.frame_window) is not int or not 1 <= self.frame_window <= 500:
            raise ValueError("Frame window must be a whole number from 1 to 500.")
        if not isinstance(self.ignore_alignment, bool) or not isinstance(self.ignore_instability, bool):
            raise ValueError("Quality filter settings must be true or false.")
        if not isinstance(self.auto_launch_windows, bool) or not isinstance(self.auto_start_monitor, bool):
            raise ValueError("Windows startup and auto-monitor settings must be true or false.")
        if type(self.camera_index) is not int or self.camera_index not in (0, 1, 2, 3):
            raise ValueError("Camera index must be 0, 1, 2 or 3.")
        if self.auto_csv and not self.csv_path.strip():
            raise ValueError("Choose a CSV file for automatic logging.")


@dataclass(frozen=True)
class MonitorDecision:
    status: str  # LOW, HIGH, IN_RANGE, or NO_READING
    consecutive: int
    confirmed: bool
    notify: bool
    recovered: bool


class MonitorEngine:
    """Confirm after consecutive same-direction excursions; no false alert on 1 jitter.

    A missing/poor-quality reading resets the candidate streak, but does not
    claim the user's PD is back to normal. A valid in-range reading clears it.
    """
    def __init__(self, config: MonitorConfig):
        config.validate()
        self.config = config
        self.candidate: Optional[str] = None
        self.consecutive = 0
        self.active_alert: Optional[str] = None

    def observe(self, pd_mm: Optional[float]) -> MonitorDecision:
        if pd_mm is None or not isfinite(pd_mm):
            self.candidate = None
            self.consecutive = 0
            # A new three-check confirmation is required when tracking resumes.
            self.active_alert = None
            return MonitorDecision("NO_READING", 0, False, False, False)

        if pd_mm < self.config.lower_mm:
            status = "LOW"
        elif pd_mm > self.config.upper_mm:
            status = "HIGH"
        else:
            status = "IN_RANGE"  # Limits are inclusive.

        if status == "IN_RANGE":
            recovered = self.active_alert is not None
            self.active_alert = None
            self.candidate = None
            self.consecutive = 0
            return MonitorDecision(status, 0, False, False, recovered)

        if status != self.candidate:
            self.candidate = status
            self.consecutive = 0
            # Do not keep an obsolete high alert while low checks accumulate.
            if self.active_alert != status:
                self.active_alert = None
        self.consecutive += 1
        confirmed = self.consecutive >= self.config.confirm_checks
        notify = False
        if confirmed:
            if self.active_alert != status:
                notify = True
                self.active_alert = status
            elif self.config.repeat_notifications and (
                (self.consecutive - self.config.confirm_checks)
                % self.config.confirm_checks == 0
                and self.consecutive > self.config.confirm_checks
            ):
                notify = True
        return MonitorDecision(status, self.consecutive, confirmed, notify, False)


class AppendCSVLogger:
    """Append-only: never overwrite a prior log and flush each scheduled check.

    Refuses to append into a CSV with unrelated column headers.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                existing = next(csv.reader(handle), [])
            if existing != CSV_HEADERS:
                raise ValueError("The selected CSV uses different columns. Choose a new file.")
        new = not self.path.exists() or self.path.stat().st_size == 0
        # No mid-file BOM when appending on a later session.
        self.handle = self.path.open("a", encoding="utf-8", newline="")
        self.writer = csv.writer(self.handle)
        if new:
            self.writer.writerow(CSV_HEADERS)
            self.handle.flush()

    def append(self, values: list[str | int | float]) -> None:
        if len(values) != len(CSV_HEADERS):
            raise ValueError("CSV row does not match the required column count")
        self.writer.writerow(values)
        self.handle.flush()

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()
