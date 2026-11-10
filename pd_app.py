"""IrisPD: a local-only, webcam-based pupillary distance estimate for Windows.

Requires Windows x64, Python 3.11, MediaPipe FaceMesh (0.10.21), OpenCV (via
MediaPipe), Pillow. Data are never sent to a server; images are not saved unless
user explicitly elects an external screenshot (not offered by this app).
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, fields, replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import csv
import json
import math
import os
import subprocess
import sys
import time
import webbrowser
import tkinter as tk
from tkinter import font as tkfont

import cv2
import mediapipe as mp
import numpy as np
from PIL import Image, ImageOps, ImageTk

from pd_core import (
    Reading, CAPTURE_FRAMES, apply_card_calibration,
    median_capture, reading_from_face_mesh, use_frame,
)
from monitor_core import AppendCSVLogger, MonitorConfig, MonitorEngine
from windows_startup import set_auto_launch
from camera_policy import CameraPowerSettings, HeadMotionGate
from layout_policy import panel_split_for_size
from coffee_policy import CoffeeSettings, DEFAULT_COFFEE_URL
from alignment_core import (
    AlignmentSettings, AlignmentEngine, AlignmentResult, EyeSample, AutoRecalibrationGate,
    sample_from_face_mesh, AppendAlignmentCSV, EVENT_HEADERS, SAMPLE_HEADERS,
)

BG = "#0b1220"
PANEL = "#111e30"
PANEL2 = "#17273b"
TEXT = "#eff7ff"
MUTED = "#aabbd0"
ACCENT = "#49d3c6"
GOLD = "#edc67d"
BAD = "#f49b9b"


def pd_monitor_visible(reading: Reading | None, eye: EyeSample | None) -> bool:
    """Hard presence/away gate independent of the optional PD quality bypasses.

    Reject absent/incomplete iris landmarks, near-closed eyes, substantial
    face yaw, or *both* pupils moving far toward the same side. A single eye
    wandering is intentionally not the same as looking away with both eyes.
    These are heuristic thresholds; not a clinical gaze estimator.
    """
    if reading is None or eye is None:
        return False
    if min(eye.left_width_px, eye.right_width_px) < 20:
        return False
    if min(eye.left_openness, eye.right_openness) < 0.085:
        return False
    if abs(eye.head_yaw_proxy) > 0.14 or abs(eye.head_roll_deg) > 18:
        return False
    if (eye.left_x < 0.25 and eye.right_x < 0.25) or (
            eye.left_x > 0.75 and eye.right_x > 0.75):
        return False
    return True


class IrisPDApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("IrisPD v14 | Pupil distance and eye-position monitoring")
        root.configure(bg=BG)
        root.geometry("1280x950")
        root.minsize(1050, 760)
        try:
            icon_file = Path(__file__).with_name("irispd_icon.png")
            if icon_file.exists():
                self._icon = tk.PhotoImage(file=str(icon_file))
                root.iconphoto(True, self._icon)
        except Exception:
            pass

        self.mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.65,
            min_tracking_confidence=0.55,
        )
        self.capture = None
        self.live = False
        self.after_job = None
        self.frame = None  # most recently processed BGR frame, NOT written to disk
        self.base_reading: Reading | None = None
        self.reading: Reading | None = None
        self.locked: Reading | None = None
        self.samples: deque[Reading] = deque(maxlen=CAPTURE_FRAMES)
        self.card_edge: list[tuple[float, float]] = []
        self.click_mode = None
        self._photo_image = None
        self._scale = 1.0
        self._offset = (0, 0)
        self._frame_count = 0
        self._preview_last_at = 0.0
        self._camera_fps = 10  # CPU-conscious target: inference and preview at <= 10 FPS
        self.layout_var = tk.StringVar(value=self._load_layout_mode())
        self.camera_power = self._load_camera_power()
        self._motion_gate = HeadMotionGate(self.camera_power.motion_hold_seconds)
        self.camera_paused = False
        self.camera_pause_until: float | None = None
        self.camera_pause_job = None
        self.camera_resting = False
        self._duty_job = None
        self._pause_restore = (False, False, False)
        self._hidden_preview_drawn = False
        self._head_motion_rejected = False
        # Session-only reference: never store an eye-calibration baseline on disk.
        # Camera duty cycling retains the active engine; timed/manual camera pauses
        # can reuse this reference until the user explicitly recalibrates.
        self._cached_alignment_reference = None
        self._alignment_resume_after = 0.0
        self._display_resize_pending = None
        self._initial_layout_stabilized = False
        self._startup_job = None
        self._last_display_size = None
        self._display_font_factor = 1.0
        self._font_base = {}
        self._full_display_fallback = False
        self._normal_window_geometry = None
        self.coffee_settings = self._load_coffee_settings()
        self._coffee_job = None
        self._coffee_deadline = None
        self._auto_recalibration_gate = AutoRecalibrationGate()

        self.status_var = tk.StringVar(value="Ready • All processing stays on your computer")
        self.pd_var = tk.StringVar(value="—")
        self.left_var = tk.StringVar(value="—")
        self.right_var = tk.StringVar(value="—")
        self.method_var = tk.StringVar(value="Start webcam or open a photo")
        self.quality_var = tk.StringVar(value="For adults; sit 40–60 cm from camera")
        self.progress_var = tk.StringVar(value="Waiting for webcam")
        self.camera_var = tk.StringVar(value="0")
        self.monitor_config = self._load_monitor_config()
        self.camera_var.set(str(self.monitor_config.camera_index))
        self.samples = deque(maxlen=self.monitor_config.frame_window)
        self.monitor_unavailable_reason = "No face detected yet"
        self.monitor_running = False
        self.monitor_job = None
        self.monitor_engine: MonitorEngine | None = None
        self.monitor_csv: AppendCSVLogger | None = None
        self.monitor_estimate: Reading | None = None
        self.monitor_last_good_at: float | None = None
        self.monitor_count = 0
        self.monitor_session = ""
        self.monitor_popup = None
        self.monitor_status_var = tk.StringVar(value="Not monitoring")
        self.monitor_summary_var = tk.StringVar(value="")
        self.alignment_settings = self._load_alignment_settings()
        self._auto_recalibration_gate.hold_seconds = self.alignment_settings.auto_recalibrate_delay_s
        self.alignment_running = False
        self.alignment_engine: AlignmentEngine | None = None
        self.alignment_sample: EyeSample | None = None
        self.alignment_result: AlignmentResult | None = None
        self.alignment_events_csv: AppendAlignmentCSV | None = None
        self.alignment_samples_csv: AppendAlignmentCSV | None = None
        self.alignment_last_csv_at: float | None = None
        self.alignment_session = ""
        self.alignment_popup = None
        self.alignment_status_var = tk.StringVar(value="Not monitoring — fixate one stationary target to calibrate")
        self.alignment_left_var = tk.StringVar(value="—")
        self.alignment_right_var = tk.StringVar(value="—")
        self.alignment_mismatch_var = tk.StringVar(value="Inter-eye change: —")
        self._build_gui()
        self._update_monitor_summary()
        self._show_coffee_banner()
        self._restore_pause_state()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._on_display_configure, add="+")
        self.root.bind("<F11>", self.toggle_full_display, add="+")
        self.root.bind("<Escape>", self.exit_full_display, add="+")
        self._display_resize_pending = self.root.after_idle(self._size_panels_for_display)
        if not self.camera_paused and (self.monitor_config.auto_start_monitor or self.alignment_settings.auto_start):
            # Both monitors can share the same camera and independent startup flags.
            self._startup_job = self.root.after(1500, self._start_saved_monitoring)

    def _label(self, parent, text, *, color=TEXT, size=11, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"),
                        anchor="w", **kwargs)

    def _btn(self, parent, text, command, *, primary=False, width=18):
        color = ACCENT if primary else PANEL2
        foreground = BG if primary else TEXT
        b = tk.Button(
            parent, text=text, command=command, bg=color, fg=foreground,
            activebackground="#71e6d9" if primary else "#264363",
            activeforeground=BG if primary else TEXT,
            font=("Segoe UI", 10, "bold"), padx=12, pady=10, width=width,
            relief="flat", borderwidth=0, cursor="hand2",
        )
        return b

    def _build_gui(self) -> None:
        # Fixed three-column header: logo | timed support | all menu controls.
        # Keep the center slot allocated even after the banner disappears, so
        # the logo, menus and workspace do not shift while monitoring.
        heading = tk.Frame(self.root, bg=BG)
        heading.pack(fill="x", padx=17, pady=(9, 8))
        heading.grid_columnconfigure(1, weight=1)
        brand = tk.Frame(heading, bg=BG)
        brand.grid(row=0, column=0, sticky="w", padx=(0, 9))
        self._label(brand, "◉  IrisPD", color=ACCENT, size=22,
                    bold=True).pack(anchor="w")
        self._label(brand, "Desktop pupil / iris tracking",
                    color=MUTED, size=9).pack(anchor="w", pady=(0, 3))

        # Middle of the header, not a separate strip below the menu.
        self.coffee_container = tk.Frame(heading, bg=BG, height=80, width=248)
        self.coffee_container.grid(row=0, column=1, sticky="nsew", padx=(4, 8))
        self.coffee_container.grid_propagate(False)
        self.coffee_row = tk.Frame(self.coffee_container, bg=PANEL2, padx=9, pady=5,
                                   highlightbackground="#2a4258", highlightthickness=1)
        self.coffee_label = tk.StringVar(value="")
        self.coffee_hint_var = tk.StringVar(value="")
        coffee_line = tk.Frame(self.coffee_row, bg=PANEL2)
        coffee_line.pack(anchor="center")
        self.coffee_button = self._btn(coffee_line, "Buy Me a Coffee",
                                        self._open_coffee_link, primary=False, width=14)
        self.coffee_button.pack(side="left", padx=(0, 5))
        tk.Label(coffee_line, textvariable=self.coffee_label, bg=PANEL2, fg=GOLD,
                 width=5, font=("Segoe UI", 9, "bold"), anchor="e").pack(side="left")
        tk.Label(self.coffee_row, textvariable=self.coffee_hint_var, bg=PANEL2,
                 fg=MUTED, font=("Segoe UI", 8)).pack(anchor="center", pady=(1, 0))

        # Two-row controls avoid crowding the logo on FHD desktop windows.
        layout_controls = tk.Frame(heading, bg=BG)
        layout_controls.grid(row=0, column=2, sticky="e", padx=(4, 0))
        self.camera_pause_btn = self._btn(layout_controls, "Pause camera", self.pause_camera, width=12)
        self.camera_pause_btn.grid(row=0, column=2, padx=2, pady=1)
        self._btn(layout_controls, "Pause for…", self.open_timed_pause, width=11).grid(
            row=0, column=1, padx=2, pady=1)
        self.camera_state_var = tk.StringVar(value="Camera ready")
        self.camera_state_label = tk.Label(layout_controls, textvariable=self.camera_state_var,
            width=18, anchor="e", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.camera_state_label.grid(row=0, column=0, sticky="e", padx=3)
        self.preview_btn = self._btn(layout_controls, "Hide preview", self.toggle_preview, width=12)
        self.preview_btn.grid(row=1, column=2, padx=2, pady=1)
        self._btn(layout_controls, "Camera power", self.open_camera_power_settings, width=11).grid(
            row=1, column=1, padx=2, pady=1)
        utility_row = tk.Frame(layout_controls, bg=BG)
        utility_row.grid(row=1, column=0, padx=2, pady=1)
        self._btn(utility_row, "Help PDF", self.open_help_pdf, width=8).pack(side="right", padx=2)
        self._btn(utility_row, "Reorient", self.open_layout_picker, width=9).pack(side="right", padx=2)
        self.preview_btn.configure(text="Hide preview" if self.camera_power.preview_enabled else "Show preview")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1)
        self.body = body
        self.monitor_row = tk.Frame(body, bg=BG)
        self.measurement_row = tk.Frame(body, bg=BG)
        for section in (self.monitor_row, self.measurement_row):
            section.grid_columnconfigure(0, weight=3)
            section.grid_columnconfigure(1, weight=2)
            section.grid_rowconfigure(0, weight=1)
        self._apply_layout()  # First call positions empty sections; final call places panels.

        left = tk.Frame(self.measurement_row, bg=PANEL, highlightbackground="#2a4258", highlightthickness=1)
        self.camera_panel = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        self._label(left, "CAMERA PREVIEW", color=MUTED, size=10, bold=True).grid(
            row=0, column=0, sticky="w", padx=18, pady=(16, 10))
        self.canvas = tk.Canvas(left, bg="#070b15", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 13))
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Configure>", lambda _evt: self.render())
        self.canvas.create_text(350, 220, text="Start your webcam\nor load a portrait photo",
                                fill=MUTED, font=("Segoe UI", 16), justify="center")

        controls = tk.Frame(left, bg=PANEL)
        controls.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 10))
        for i in range(3):
            controls.grid_columnconfigure(i, weight=1)
        self._btn(controls, "Start webcam", self.start_webcam, primary=True, width=16).grid(
            row=0, column=0, padx=3, pady=4, sticky="ew")
        self._btn(controls, "Freeze for card", self.freeze_frame, width=16).grid(
            row=0, column=1, padx=3, pady=4, sticky="ew")
        self._btn(controls, "Open photo", self.open_photo, width=16).grid(
            row=0, column=2, padx=3, pady=4, sticky="ew")
        self._btn(controls, "Mark card edges", self.mark_card, width=16).grid(
            row=1, column=0, padx=3, pady=4, sticky="ew")
        self._btn(controls, "Reset / measure again", self.reset, width=16).grid(
            row=1, column=1, padx=3, pady=4, sticky="ew")
        self._btn(controls, "Save result CSV", self.save_result, width=16).grid(
            row=1, column=2, padx=3, pady=4, sticky="ew")
        opts = tk.Frame(left, bg=PANEL)
        opts.grid(row=3, column=0, sticky="w", padx=19, pady=(2, 15))
        self._label(opts, "Camera index:", color=MUTED, size=9).pack(side="left")
        self.camera_box = ttk.Combobox(opts, textvariable=self.camera_var,
                                       width=4, values=("0", "1", "2", "3"),
                                       state="readonly")
        self.camera_box.pack(side="left", padx=(9, 16))
        self.camera_box.bind("<<ComboboxSelected>>", self._camera_selected)
        self._label(opts, "Card = 85.60 mm long edge, held at eye depth",
                    color=MUTED, size=9).pack(side="left")

        monitor_bar = tk.Frame(self.monitor_row, bg=PANEL2, padx=13, pady=10)
        self.pd_monitor_panel = monitor_bar
        monitor_bar.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        monitor_bar.grid_columnconfigure(0, weight=1)
        self._label(monitor_bar, "CONTINUOUS PD MONITOR", color=ACCENT,
                    size=10, bold=True).grid(row=0, column=0, sticky="w")
        tk.Label(monitor_bar, textvariable=self.monitor_summary_var,
                 bg=PANEL2, fg=MUTED, font=("Segoe UI", 9), anchor="w",
                 justify="left", wraplength=610, height=2).grid(
                     row=1, column=0, columnspan=3, sticky="ew", pady=(3, 3))
        self.monitor_status_label = tk.Label(monitor_bar, textvariable=self.monitor_status_var,
                 bg=PANEL2, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w",
                 justify="left", wraplength=520, height=2)
        self.monitor_status_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(3, 7))
        self.monitor_start_btn = self._btn(monitor_bar, "Start monitoring",
                                           self.start_monitor, primary=True, width=15)
        self.monitor_start_btn.grid(row=3, column=0, sticky="ew", padx=(0, 5))
        self.monitor_stop_btn = self._btn(monitor_bar, "Stop monitoring",
                                          self.stop_monitor, width=15)
        self.monitor_stop_btn.grid(row=3, column=1, sticky="ew", padx=(0, 5))
        self.monitor_stop_btn.config(state="disabled")
        self.monitor_settings_btn = self._btn(monitor_bar, "Limits / alerts / CSV",
                                              self.open_monitor_settings, width=18)
        self.monitor_settings_btn.grid(row=3, column=2, sticky="ew")

        right = tk.Frame(self.measurement_row, bg=PANEL, highlightbackground="#2a4258", highlightthickness=1)
        self.pd_panel = right
        right.grid(row=0, column=1, sticky="nsew")
        for c in range(2):
            right.grid_columnconfigure(c, weight=1)
        self._label(right, "PUPILLARY DISTANCE", color=MUTED,
                    size=10, bold=True).grid(row=0, column=0, columnspan=2,
                                               sticky="w", padx=18, pady=(17, 5))

        # A compact three-column measurement band keeps monocular estimates at
        # the two OUTER edges, with a smaller binocular PD value in the middle.
        # Fixed panel geometry prevents these numbers from shifting other panels.
        value_band = tk.Frame(right, bg=PANEL)
        value_band.grid(row=1, column=0, columnspan=2, sticky="ew",
                        padx=14, pady=(1, 12))
        for col, weight in enumerate((1, 2, 1)):
            value_band.grid_columnconfigure(col, weight=weight)

        mono_left = tk.Frame(value_band, bg=PANEL)
        mono_left.grid(row=0, column=0, sticky="w", padx=(3, 2))
        self._label(mono_left, "Screen-left", color=MUTED, size=8).pack(anchor="w")
        self.left_value_label = tk.Label(mono_left, textvariable=self.left_var,
            fg=TEXT, bg=PANEL, font=("Segoe UI", 17, "bold"), anchor="w")
        self.left_value_label.pack(anchor="w")

        total = tk.Frame(value_band, bg=PANEL)
        total.grid(row=0, column=1, sticky="n")
        tk.Label(total, text="TOTAL PD", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="center")
        total_line = tk.Frame(total, bg=PANEL)
        total_line.pack(anchor="center")
        self.pd_value_label = tk.Label(total_line, textvariable=self.pd_var,
            fg=ACCENT, bg=PANEL, font=("Segoe UI", 38, "bold"))
        self.pd_value_label.pack(side="left")
        self._label(total_line, "mm", color=MUTED, size=11).pack(side="left", padx=(3, 0), pady=(11, 0))

        mono_right = tk.Frame(value_band, bg=PANEL)
        mono_right.grid(row=0, column=2, sticky="e", padx=(2, 3))
        self._label(mono_right, "Screen-right", color=MUTED, size=8).pack(anchor="e")
        self.right_value_label = tk.Label(mono_right, textvariable=self.right_var,
            fg=TEXT, bg=PANEL, font=("Segoe UI", 17, "bold"), anchor="e")
        self.right_value_label.pack(anchor="e")

        tk.Frame(right, bg="#2a4258", height=1).grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=18)
        self._label(right, "Monocular estimates: approximate screen-left / screen-right split",
                    color=MUTED, size=8, wraplength=400).grid(
                        row=3, column=0, columnspan=2, sticky="w",
                        padx=18, pady=(8, 4))

        tk.Frame(right, bg="#2a4258", height=1).grid(row=5, column=0,
                                                        columnspan=2, sticky="ew",
                                                        padx=22, pady=(18, 0))
        self._label(right, "CAPTURE PROGRESS", color=MUTED,
                    size=10, bold=True).grid(row=6, column=0, columnspan=2,
                                              sticky="w", padx=22, pady=(17, 9))
        ttk.Style().configure("Iris.Horizontal.TProgressbar", troughcolor="#24394f",
                              background=ACCENT, thickness=10)
        self.progress = ttk.Progressbar(right, maximum=self.monitor_config.frame_window,
                                        style="Iris.Horizontal.TProgressbar")
        self.progress.grid(row=7, column=0, columnspan=2, sticky="ew", padx=22)
        tk.Label(right, textvariable=self.progress_var, bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 10), anchor="w", wraplength=360,
                 justify="left", height=2).grid(row=8, column=0, columnspan=2, sticky="ew",
                                      padx=22, pady=(8, 16))

        # Method (iris scale vs card calibration) is still retained in exported
        # CSV, but is no longer a displayed field. See the Help PDF.
        self._label(right, "ALIGNMENT / QUALITY", color=MUTED, size=10, bold=True).grid(
            row=9, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 6))
        # A scrollable fixed-height warning box: long/wrapped warnings cannot
        # change the height of the measurement panel or shift the camera preview.
        quality_box = tk.Frame(right, bg=PANEL2, height=100,
                               highlightbackground="#31485e", highlightthickness=1)
        quality_box.grid(row=10, column=0, columnspan=2, sticky="ew", padx=22)
        quality_box.grid_propagate(False)
        warning_scroll = tk.Scrollbar(quality_box, orient="vertical")
        warning_scroll.pack(side="right", fill="y")
        self.quality_text = tk.Text(
            quality_box, height=4, width=26, wrap="word", bg=PANEL2, fg=GOLD,
            font=("Segoe UI", 10), insertbackground=TEXT,
            padx=8, pady=7, relief="flat", highlightthickness=0,
            borderwidth=0, state="disabled", yscrollcommand=warning_scroll.set)
        self.quality_text.pack(side="left", fill="both", expand=True)
        warning_scroll.configure(command=self.quality_text.yview)
        self.quality_var.trace_add("write", lambda *_: self._refresh_quality_text())
        self._refresh_quality_text()
        self.quality_label = self.quality_text  # Preserve existing color updates.
        right.grid_rowconfigure(10, weight=0)

        alignment_panel = tk.Frame(self.monitor_row, bg=PANEL2, padx=12, pady=10)
        self.eye_monitor_panel = alignment_panel
        alignment_panel.grid(row=0, column=1, sticky="nsew")
        self._label(alignment_panel, "EYE ALIGNMENT • EXPERIMENTAL", color=ACCENT,
                    size=10, bold=True).pack(anchor="w")
        self.align_status_label = tk.Label(alignment_panel, textvariable=self.alignment_status_var,
            bg=PANEL2, fg=MUTED, font=("Segoe UI", 9), justify="left",
            anchor="w", wraplength=390, height=3)
        self.align_status_label.pack(fill="x", pady=(4, 5))
        self._label(alignment_panel, "Anatomical L (mirror-left) / R (mirror-right):",
                    color=MUTED, size=9).pack(anchor="w")
        self._label(alignment_panel, "L: Δx, Δy are normalized to eye width", color=MUTED,
                    size=8).pack(anchor="w")
        tk.Label(alignment_panel, textvariable=self.alignment_left_var, bg=PANEL2,
                 fg=TEXT, font=("Segoe UI", 9), anchor="w").pack(fill="x")
        tk.Label(alignment_panel, textvariable=self.alignment_right_var, bg=PANEL2,
                 fg=TEXT, font=("Segoe UI", 9), anchor="w").pack(fill="x")
        tk.Label(alignment_panel, textvariable=self.alignment_mismatch_var,
                 bg=PANEL2, fg=GOLD, font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
        align_controls = tk.Frame(alignment_panel, bg=PANEL2)
        align_controls.pack(fill="x", pady=(7, 0))
        self.align_start_btn = self._btn(align_controls, "Start alignment",
                                          self.start_alignment_monitor, primary=True, width=11)
        self.align_start_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.align_stop_btn = self._btn(align_controls, "Stop", self.stop_alignment_monitor, width=6)
        self.align_stop_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.align_stop_btn.config(state="disabled")
        self.align_calibrate_btn = self._btn(align_controls, "Recalibrate",
                                              self.recalibrate_alignment, width=8)
        self.align_calibrate_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.align_settings_btn = self._btn(align_controls, "Settings",
                                             self.open_alignment_settings, width=7)
        self.align_settings_btn.pack(side="left", expand=True, fill="x", padx=2)

        status_bar = tk.Label(right, textvariable=self.status_var, bg=PANEL2, fg=MUTED,
                              font=("Segoe UI", 9), anchor="w", wraplength=370,
                              justify="left", padx=14, pady=13, height=2)
        status_bar.grid(row=11, column=0, columnspan=2, sticky="sew", pady=(5, 0))

        self._label(self.root,
                    "ESTIMATE ONLY  •  Iris size differs between people; card placement also affects scale. "
                    "Confirm PD with an optician. Experimental eye-position flags are not a strabismus diagnosis.",
                    color=MUTED, size=9, wraplength=1130).pack(fill="x", padx=26, pady=(0, 10))
        self._apply_layout()  # Reposition populated panels using saved orientation.
        # Never let changing text, DPI labels or content force a different split.
        # The root-window size alone determines panel geometry.
        for fixed_panel in (self.camera_panel, self.pd_panel,
                            self.pd_monitor_panel, self.eye_monitor_panel,
                            self.measurement_row, self.monitor_row):
            fixed_panel.grid_propagate(False)

    def _on_display_configure(self, event) -> None:
        """Recalculate only when the *outer window* changes dimensions."""
        if event.widget is not self.root:
            return
        dims = (event.width, event.height)
        if dims == self._last_display_size:
            return
        self._last_display_size = dims
        if self._display_resize_pending is not None:
            self.root.after_cancel(self._display_resize_pending)
        # Debounce maximizing / Windows snap / FHD to 4K monitor transitions.
        self._display_resize_pending = self.root.after(65, self._size_panels_for_display)

    def _size_panels_for_display(self) -> None:
        self._display_resize_pending = None
        if not hasattr(self, "body") or not self.body.winfo_exists():
            return
        self.body.update_idletasks()
        width = self.body.winfo_width()
        height = self.body.winfo_height()
        if width < 100 or height < 100:
            return
        self._scale_fonts_for_display(height)
        self.body.update_idletasks()
        width, height = self.body.winfo_width(), self.body.winfo_height()
        sizes = panel_split_for_size(width, height)
        self._display_split = sizes
        monitor_row = 0 if self.layout_var.get().startswith("monitors_top") else 1
        measure_row = 1 - monitor_row
        # With geometry propagation disabled, Tk can retain the large old
        # requested height of a row and collapse the newly selected top row.
        # Explicitly allocate both row heights from the available client area.
        # This is essential when preset #2 (monitors on top) is the default.
        self.monitor_row.configure(height=sizes.monitor_height)
        self.measurement_row.configure(height=max(210, height-sizes.monitor_height-10))
        self.body.grid_rowconfigure(monitor_row, weight=0, minsize=sizes.monitor_height)
        self.body.grid_rowconfigure(measure_row, weight=1, minsize=0)
        self._apply_panel_column_weights(sizes)
        # First creation may inherit a stale height from a temporary empty row.
        # Make one additional idle pass, never a periodic resizing loop.
        if not self._initial_layout_stabilized:
            self._initial_layout_stabilized = True
            self.root.after_idle(self._size_panels_for_display)

    def _scale_fonts_for_display(self, body_height: int) -> None:
        """Readable FHD/2.8K/4K fonts, changing ONLY with display size.

        Windows DPI scaling remains authoritative: on a high-DPI monitor Tk
        typically sees smaller logical coordinates, avoiding double-scaling.
        """
        factor = 1.5 if body_height >= 1770 else 1.25 if body_height >= 1170 else 1.0
        if factor == self._display_font_factor:
            return
        self._display_font_factor = factor

        def visit(parent):
            for child in parent.winfo_children():
                if isinstance(child, tk.Toplevel):
                    continue
                if hasattr(child, "configure"):
                    try:
                        raw = child.cget("font")
                    except tk.TclError:
                        raw = None
                    if raw:
                        key = str(child)
                        if key not in self._font_base:
                            f = tkfont.Font(root=self.root, font=raw)
                            data = f.actual()
                            self._font_base[key] = (
                                data["family"], data["size"], data["weight"],
                                data["slant"], data["underline"], data["overstrike"])
                        family, size, weight, slant, under, strike = self._font_base[key]
                        pixels = max(7, round(abs(size) * factor))
                        new_size = -pixels if size < 0 else pixels
                        # Use a generated Font instance to preserve style and avoid
                        # assumptions about how tuple fonts encode underline/slant.
                        scaled = tkfont.Font(root=self.root, family=family, size=new_size,
                                             weight=weight, slant=slant,
                                             underline=under, overstrike=strike)
                        child.configure(font=scaled)
                        # Tk needs a persistent reference for dynamic Font objects.
                        child._irispd_scaled_font = scaled
                visit(child)
        visit(self.root)

    def _apply_panel_column_weights(self, sizes) -> None:
        mode = self.layout_var.get()
        camera_right = "camera_right" in mode or "both_reversed" in mode
        alignment_left = "alignment_left" in mode or "both_reversed" in mode
        camera_fraction = sizes.camera_fraction
        pd_monitor_fraction = sizes.pd_monitor_fraction
        self.measurement_row.grid_columnconfigure(0, weight=round(100 * (1-camera_fraction if camera_right else camera_fraction)), uniform="measure-columns")
        self.measurement_row.grid_columnconfigure(1, weight=round(100 * (camera_fraction if camera_right else 1-camera_fraction)), uniform="measure-columns")
        self.monitor_row.grid_columnconfigure(0, weight=round(100 * (1-pd_monitor_fraction if alignment_left else pd_monitor_fraction)), uniform="monitor-columns")
        self.monitor_row.grid_columnconfigure(1, weight=round(100 * (pd_monitor_fraction if alignment_left else 1-pd_monitor_fraction)), uniform="monitor-columns")

    def toggle_full_display(self, _event=None) -> None:
        """F11 opens a full-display workspace; ESC restores the normal window.

        Windows supports Tk's -fullscreen through its window manager. Xvfb
        without a window manager uses a borderless geometry fallback for tests.
        """
        active = self._full_display_fallback or bool(self.root.attributes("-fullscreen"))
        if active:
            if self._full_display_fallback:
                self.root.overrideredirect(False)
                self._full_display_fallback = False
            self.root.attributes("-fullscreen", False)
            if self._normal_window_geometry:
                self.root.geometry(self._normal_window_geometry)
        else:
            self._normal_window_geometry = self.root.geometry()
            self.root.attributes("-fullscreen", True)
            if sys.platform != "win32" and not bool(self.root.attributes("-fullscreen")):
                self.root.overrideredirect(True)
                self.root.geometry(f"{self.root.winfo_screenwidth()}x"
                                   f"{self.root.winfo_screenheight()}+0+0")
                self._full_display_fallback = True
        if self._display_resize_pending is not None:
            self.root.after_cancel(self._display_resize_pending)
        self._display_resize_pending = self.root.after_idle(self._size_panels_for_display)

    def exit_full_display(self, _event=None) -> None:
        if self._full_display_fallback or bool(self.root.attributes("-fullscreen")):
            self.toggle_full_display()

    def _refresh_quality_text(self) -> None:
        """Update the fixed-height, scrollable warning box without changing layout."""
        if not hasattr(self, "quality_text"):
            return
        latest = self.quality_var.get()
        if latest == getattr(self, "_quality_last_text", None):
            return  # Avoid unnecessary Text updates on repeated webcam frames.
        self._quality_last_text = latest
        widget = self.quality_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", latest)
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    @staticmethod
    def _layout_settings_file() -> Path:
        return IrisPDApp._settings_file().with_name("layout_settings.json")

    LAYOUT_OPTIONS = (
        ("measurements_top", "Measurements top | camera left | PD monitor left"),
        ("monitors_top", "Monitors top | camera left | PD monitor left"),
        ("measurements_top_camera_right", "Measurements top | camera right | PD monitor left"),
        ("monitors_top_camera_right", "Monitors top | camera right | PD monitor left"),
        ("measurements_top_alignment_left", "Measurements top | camera left | eye alignment left"),
        ("monitors_top_alignment_left", "Monitors top | camera left | eye alignment left"),
        ("measurements_top_both_reversed", "Measurements top | camera right | eye alignment left"),
        ("monitors_top_both_reversed", "Monitors top | camera right | eye alignment left"),
    )

    DEFAULT_LAYOUT = "monitors_top"  # Preset #2: monitors top, camera left, PD monitor left.

    def _load_layout_mode(self) -> str:
        # One-time upgrade to the new v12 default, then honor later choices.
        try:
            raw = json.loads(self._layout_settings_file().read_text(encoding="utf-8"))
            mode = raw.get("mode")
            if raw.get("v12_layout_selected") is True and mode in dict(self.LAYOUT_OPTIONS):
                return mode
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return self.DEFAULT_LAYOUT

    def _apply_layout(self) -> None:
        mode = self.layout_var.get()
        if mode not in dict(self.LAYOUT_OPTIONS):
            mode = self.DEFAULT_LAYOUT
        top_monitors = mode.startswith("monitors_top")
        camera_right = "camera_right" in mode or "both_reversed" in mode
        alignment_left = "alignment_left" in mode or "both_reversed" in mode
        self.monitor_row.grid(row=0 if top_monitors else 1, column=0,
                              sticky="nsew", pady=(0, 10) if top_monitors else (10, 0))
        self.measurement_row.grid(row=1 if top_monitors else 0, column=0,
                                  sticky="nsew")
        self.body.grid_rowconfigure(0, weight=0 if top_monitors else 1)
        self.body.grid_rowconfigure(1, weight=1 if top_monitors else 0)
        # Rearrange child panels in-place: never recreate a camera/monitor widget.
        if hasattr(self, "camera_panel") and hasattr(self, "pd_panel"):
            # Width allocations are resolved from the current display, not text.
            self.camera_panel.grid_configure(row=0, column=int(camera_right),
                padx=(12, 0) if camera_right else (0, 12), sticky="nsew")
            self.pd_panel.grid_configure(row=0, column=int(not camera_right),
                padx=(0, 12) if camera_right else (0, 0), sticky="nsew")
        if hasattr(self, "pd_monitor_panel") and hasattr(self, "eye_monitor_panel"):
            # Keep the selected column order while retaining fixed relative splits.
            self.pd_monitor_panel.grid_configure(row=0, column=int(alignment_left),
                padx=(12, 0) if alignment_left else (0, 12), sticky="nsew")
            self.eye_monitor_panel.grid_configure(row=0, column=int(not alignment_left),
                padx=(0, 12) if alignment_left else (0, 0), sticky="nsew")

    def set_layout(self, mode: str) -> None:
        """Select and persist one of eight orientations without restarting monitoring."""
        if mode not in dict(self.LAYOUT_OPTIONS):
            raise ValueError(f"Unsupported layout: {mode}")
        self.layout_var.set(mode)
        self._apply_layout()
        self._size_panels_for_display()
        try:
            path = self._layout_settings_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"mode": mode, "v12_layout_selected": True}, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            self.status_var.set(f"Layout changed but preference was not saved: {exc}")

    def toggle_layout(self) -> None:
        """Legacy two-layout shortcut retained for compatibility."""
        new_mode = "monitors_top" if self.layout_var.get() == "measurements_top" else "measurements_top"
        self.set_layout(new_mode)

    def open_layout_picker(self) -> None:
        """Show every orientation in a movable standard Windows dialog."""
        win = tk.Toplevel(self.root)
        win.title("IrisPD | Reorient layout")
        win.configure(bg=BG)
        win.geometry("690x655")
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text="Choose a workspace arrangement", fg=ACCENT, bg=BG,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=22, pady=(22, 6))
        tk.Label(win, text="Select which panels appear first and which panel occupies each column. "
                 "Monitoring and CSV recording will continue without interruption.",
                 fg=MUTED, bg=BG, font=("Segoe UI", 10), wraplength=505,
                 justify="left").pack(anchor="w", padx=22, pady=(0, 13))
        choice = tk.StringVar(value=self.layout_var.get())
        # Direct controls support orientation as three independent decisions.
        direct = tk.LabelFrame(win, text="Reorient individual panels", bg=PANEL,
                               fg=ACCENT, padx=12, pady=10)
        direct.pack(fill="x", padx=20, pady=(0, 8))
        original_mode = self.layout_var.get()
        row_var = tk.StringVar(value="Monitors on top" if original_mode.startswith("monitors_top")
                               else "Measurements on top")
        camera_var = tk.StringVar(value="right" if ("camera_right" in original_mode or
                           "both_reversed" in original_mode) else "left")
        eye_var = tk.StringVar(value="left" if ("alignment_left" in original_mode or
                         "both_reversed" in original_mode) else "right")
        selectors = (
            ("Top row", row_var, ("Measurements on top", "Monitors on top")),
            ("Camera side", camera_var, ("left", "right")),
            ("Eye alignment side", eye_var, ("right", "left")),
        )
        for col, (label, variable, choices) in enumerate(selectors):
            cell = tk.Frame(direct, bg=PANEL)
            cell.grid(row=0, column=col, sticky="ew", padx=6)
            self._label(cell, label, color=MUTED, size=9).pack(anchor="w")
            ttk.Combobox(cell, textvariable=variable, values=choices,
                         state="readonly", width=17).pack(anchor="w", pady=(4, 0))
            direct.grid_columnconfigure(col, weight=1)

        def mode_from_controls() -> str:
            mode = "monitors_top" if row_var.get() == "Monitors on top" else "measurements_top"
            if camera_var.get() == "right" and eye_var.get() == "left":
                return mode + "_both_reversed"
            if camera_var.get() == "right":
                return mode + "_camera_right"
            if eye_var.get() == "left":
                return mode + "_alignment_left"
            return mode

        syncing = {"active": False}
        def synchronize(*_):
            if syncing["active"]:
                return
            selected = mode_from_controls()
            if selected in dict(self.LAYOUT_OPTIONS) and selected != choice.get():
                syncing["active"] = True
                try:
                    choice.set(selected)
                finally:
                    syncing["active"] = False
        for var in (row_var, camera_var, eye_var):
            var.trace_add("write", synchronize)

        options_panel = tk.Frame(win, bg=PANEL, padx=14, pady=9)
        options_panel.pack(fill="both", expand=True, padx=20)
        for mode, name in self.LAYOUT_OPTIONS:
            tk.Radiobutton(options_panel, text=name, value=mode, variable=choice,
                           bg=PANEL, fg=TEXT, selectcolor=PANEL2, activebackground=PANEL,
                           activeforeground=TEXT, anchor="w", font=("Segoe UI", 9),
                           indicatoron=True).pack(fill="x", pady=2)
        # Direct selector changes keep the preset highlight synchronized; when
        # a preset radio is clicked, the selectors track that choice too.
        # Avoid a recursive trace cycle by updating only different values.
        def preset_to_controls(*_):
            if syncing["active"]:
                return
            mode = choice.get()
            if mode_from_controls() == mode:
                return
            syncing["active"] = True
            try:
                row = "Monitors on top" if mode.startswith("monitors_top") else "Measurements on top"
                cam = "right" if ("camera_right" in mode or "both_reversed" in mode) else "left"
                eye = "left" if ("alignment_left" in mode or "both_reversed" in mode) else "right"
                if row_var.get() != row: row_var.set(row)
                if camera_var.get() != cam: camera_var.set(cam)
                if eye_var.get() != eye: eye_var.set(eye)
            finally:
                syncing["active"] = False
        choice.trace_add("write", preset_to_controls)
        buttons = tk.Frame(win, bg=BG)
        buttons.pack(fill="x", padx=20, pady=(15, 19))
        self._btn(buttons, "Apply layout", lambda: (self.set_layout(choice.get()), win.destroy()),
                  primary=True, width=13).pack(side="right", padx=(8, 0))
        self._btn(buttons, "Cancel", win.destroy, width=9).pack(side="right")
        self._btn(buttons, "Full display (F11)", self.toggle_full_display,
                  width=17).pack(side="left")
        tk.Label(win, text="F11: toggle full display  |  Esc: leave full display  |  "
                 "Maximize also uses display-aware splits", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="bottom", pady=(0, 7))

    def open_help_pdf(self) -> None:
        manual = Path(__file__).with_name("IrisPD_Help.pdf")
        if not manual.exists():
            messagebox.showerror("Help PDF missing", "IrisPD_Help.pdf was not found beside the application.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(manual))
            else:
                subprocess.Popen(["xdg-open", str(manual)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Help PDF", str(exc))

    @staticmethod
    def _camera_power_file() -> Path:
        return IrisPDApp._settings_file().with_name("camera_power.json")

    @staticmethod
    def _camera_pause_file() -> Path:
        return IrisPDApp._settings_file().with_name("camera_pause.json")

    def _load_camera_power(self) -> CameraPowerSettings:
        try:
            raw = json.loads(self._camera_power_file().read_text(encoding="utf-8"))
            valid = {f.name for f in fields(CameraPowerSettings)}
            setting = CameraPowerSettings(**{k:v for k,v in raw.items() if k in valid})
            setting.validate()
            return setting
        except (OSError, ValueError, TypeError, AttributeError):
            return CameraPowerSettings()

    def _save_camera_power(self) -> None:
        path = self._camera_power_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self.camera_power), indent=2), encoding="utf-8")
        temp.replace(path)

    def open_camera_power_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("IrisPD | Camera cooling / power management")
        win.configure(bg=BG)
        win.geometry("525x400")
        win.transient(self.root)
        win.grab_set()
        panel = tk.Frame(win, bg=PANEL, padx=20, pady=17)
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        self._label(panel, "Camera duty cycle", color=ACCENT, size=16, bold=True).pack(anchor="w")
        self._label(panel, "The webcam is physically released during each rest period.\n"
                    "Eye-drift events cannot be captured while it is off.", color=GOLD,
                    size=9, justify="left").pack(anchor="w", pady=(5, 10))
        enabled = tk.BooleanVar(value=self.camera_power.duty_enabled)
        preview = tk.BooleanVar(value=self.camera_power.preview_enabled)
        on = tk.StringVar(value=f"{self.camera_power.on_seconds:g}")
        off = tk.StringVar(value=f"{self.camera_power.rest_seconds:g}")
        hold = tk.StringVar(value=f"{self.camera_power.motion_hold_seconds:g}")
        for title,var in (("Cycle camera ON / OFF while monitoring", enabled),
                          ("Display camera preview", preview)):
            tk.Checkbutton(panel, text=title, variable=var, bg=PANEL, fg=TEXT,
                selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT).pack(anchor="w", pady=3)
        for title,var in (("Camera ON (seconds; 10-3600)",on),
                          ("Camera OFF / cooling (seconds; 5-3600)",off),
                          ("After large head movement, wait (seconds; 0.3-5)",hold)):
            row=tk.Frame(panel,bg=PANEL)
            row.pack(fill="x",pady=5)
            self._label(row,title,color=MUTED,size=9).pack(side="left",expand=True,fill="x")
            tk.Entry(row, textvariable=var, width=8, bg=PANEL2, fg=TEXT,
                insertbackground=TEXT,relief="flat").pack(side="right",ipady=4)
        def save():
            try:
                setting = CameraPowerSettings(enabled.get(),float(on.get()),float(off.get()),
                                              preview.get(),float(hold.get()))
                setting.validate()
                self.camera_power=setting
                self._motion_gate.hold_seconds=setting.motion_hold_seconds
                self._save_camera_power()
            except (ValueError,OSError) as exc:
                messagebox.showerror("Invalid camera settings", str(exc), parent=win)
                return
            self._hidden_preview_drawn=False
            self.preview_btn.config(text="Hide preview" if setting.preview_enabled else "Show preview")
            self.render()
            self._cancel_duty_cycle()
            if self.camera_resting:
                self._end_camera_rest()
            else:
                self._schedule_duty_cycle()
            win.destroy()
        self._btn(panel, "Save camera settings", save, primary=True, width=19).pack(anchor="e",pady=(9,0))

    def toggle_preview(self) -> None:
        self.camera_power = replace(self.camera_power,
                                    preview_enabled=not self.camera_power.preview_enabled)
        try:
            self._save_camera_power()
        except OSError as exc:
            messagebox.showwarning("Could not save preview setting",str(exc),parent=self.root)
        self.preview_btn.configure(text="Hide preview" if self.camera_power.preview_enabled else "Show preview")
        self._hidden_preview_drawn=False
        if not self.camera_power.preview_enabled:
            self.frame=None
            self._photo_image=None
        self.render()

    def open_timed_pause(self) -> None:
        win=tk.Toplevel(self.root)
        win.title("IrisPD | Pause camera for hours")
        win.configure(bg=BG)
        win.geometry("390x190")
        win.transient(self.root)
        win.grab_set()
        panel=tk.Frame(win,bg=PANEL,padx=18,pady=15)
        panel.pack(fill="both",expand=True,padx=10,pady=10)
        self._label(panel,"Pause webcam access for how many hours?", color=TEXT, size=10).pack(anchor="w")
        hours=tk.StringVar(value="1")
        tk.Entry(panel,textvariable=hours,width=10,bg=PANEL2,fg=TEXT,
                 insertbackground=TEXT).pack(anchor="w",pady=10)
        self._label(panel,"Allowed: 0.01 to 168 hours (7 days)",color=MUTED,size=9).pack(anchor="w")
        def apply():
            try:
                h=float(hours.get())
                if not 0.01 <= h <= 168 or not math.isfinite(h):
                    raise ValueError("Enter a number between 0.01 and 168 hours")
            except ValueError as exc:
                messagebox.showerror("Invalid hours",str(exc),parent=win)
                return
            win.destroy()
            self.pause_camera(h)
        self._btn(panel,"Pause camera",apply,primary=True,width=12).pack(side="right",pady=(8,0))

    def _save_pause_state(self) -> None:
        path = self._camera_pause_file()
        if not self.camera_paused:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True,exist_ok=True)
        obj={"until":self.camera_pause_until,"restore_pd":self._pause_restore[0],
             "restore_align":self._pause_restore[1],"restore_webcam":self._pause_restore[2]}
        temp=path.with_suffix(".tmp")
        temp.write_text(json.dumps(obj),encoding="utf-8")
        temp.replace(path)

    def _restore_pause_state(self) -> None:
        try:
            obj=json.loads(self._camera_pause_file().read_text(encoding="utf-8"))
            expiry=obj.get("until")
            self._pause_restore=(bool(obj.get("restore_pd")),bool(obj.get("restore_align")),
                                  bool(obj.get("restore_webcam")))
            if expiry is not None and (not isinstance(expiry,(int,float)) or not math.isfinite(expiry)):
                return
            self.camera_pause_until=expiry
            self.camera_paused=True
            if expiry is not None and expiry <= time.time():
                self.root.after(300, self.resume_camera)
            else:
                self._update_pause_ui()
                self._schedule_pause_check()
        except (ValueError,OSError,TypeError):
            pass

    def _schedule_pause_check(self) -> None:
        if self.camera_pause_job is not None:
            self.root.after_cancel(self.camera_pause_job)
            self.camera_pause_job=None
        if self.camera_paused and self.camera_pause_until is not None:
            remaining=max(0,self.camera_pause_until-time.time())
            self.camera_pause_job=self.root.after(max(50,int(min(remaining,30)*1000)),self._pause_check)

    def _pause_check(self) -> None:
        self.camera_pause_job=None
        if self.camera_paused and self.camera_pause_until is not None and time.time() >= self.camera_pause_until:
            self.resume_camera()
        elif self.camera_paused:
            self._update_pause_ui()
            self._schedule_pause_check()

    def _update_pause_ui(self) -> None:
        if self.camera_paused:
            self.camera_pause_btn.config(text="Resume camera",command=self.resume_camera)
            if self.camera_pause_until is None:
                label="Paused manually"
            else:
                label=f"Paused {max(0,(self.camera_pause_until-time.time())/3600):.2f} h"
        else:
            self.camera_pause_btn.config(text="Pause camera",command=self.pause_camera)
            label="Cooling off" if self.camera_resting else "Camera active" if self.live else "Camera ready"
        self.camera_state_var.set(label)

    def pause_camera(self, hours: float | None = None) -> None:
        if self.camera_paused:
            # Replace an existing pause, preserving the pre-pause monitor intent.
            saved=self._pause_restore
        else:
            saved=(self.monitor_running,self.alignment_running,self.live)
        self._pause_restore=saved
        if self.alignment_engine is not None and self.alignment_engine.baseline is not None:
            self._cached_alignment_reference = self.alignment_engine.baseline
        self.camera_paused=True
        self.camera_pause_until=None if hours is None else time.time()+hours*3600
        self._cancel_duty_cycle()
        self._stop_camera()  # stops both monitors, releases hardware; no alerts
        self._clear_measurements()
        self.status_var.set("Camera released • available to other apps until Resume camera")
        self.monitor_status_var.set("PAUSED • no webcam readings or alerts")
        self.alignment_status_var.set("PAUSED • no eye-alignment analysis")
        self._update_pause_ui()
        self._save_pause_state()
        self._schedule_pause_check()
        self.render()

    def resume_camera(self) -> None:
        if not self.camera_paused:
            return
        self.camera_paused=False
        self.camera_pause_until=None
        if self.camera_pause_job is not None:
            self.root.after_cancel(self.camera_pause_job)
            self.camera_pause_job=None
        restore_pd,restore_align,restore_webcam=self._pause_restore
        self._pause_restore=(False,False,False)
        try:
            self._save_pause_state()
        except OSError:
            pass
        self._update_pause_ui()
        # Also honor auto-start after a restart if pause was set before app opened.
        if restore_pd:
            self.start_monitor()
        if restore_align:
            self.start_alignment_monitor()
            if self.alignment_running and self.alignment_engine is not None:
                if self._cached_alignment_reference is not None:
                    self.alignment_engine.baseline = self._cached_alignment_reference
                    self.alignment_status_var.set("Reference preserved from pause • fixate same target")
                self._alignment_resume_after = time.monotonic() + 0.5
        self._cached_alignment_reference = None
        if not (self.monitor_running or self.alignment_running) and restore_webcam:
            self.start_webcam()
        self._update_pause_ui()

    def _cancel_duty_cycle(self) -> None:
        if self._duty_job is not None:
            try:
                self.root.after_cancel(self._duty_job)
            except tk.TclError:
                pass
            self._duty_job=None

    def _schedule_duty_cycle(self) -> None:
        if (self._duty_job is None and not self.camera_paused and not self.camera_resting
                and self.live and (self.monitor_running or self.alignment_running)
                and self.camera_power.duty_enabled):
            self._duty_job=self.root.after(int(self.camera_power.on_seconds*1000),self._begin_camera_rest)

    def _suspend_camera_stream(self) -> None:
        self.live=False
        if self.after_job is not None:
            try:self.root.after_cancel(self.after_job)
            except tk.TclError:pass
            self.after_job=None
        if self.capture is not None:
            self.capture.release()
            self.capture=None
        self.frame=None
        self._photo_image=None
        self.base_reading=None
        self.reading=None
        self.alignment_sample=None
        self.monitor_estimate=None
        self.monitor_last_good_at=None
        self.samples.clear()
        self._motion_gate.reset()
        self._head_motion_rejected=False
        self._auto_recalibration_gate.clear_pending()
        self._hidden_preview_drawn=False
        self._display_reading(None)
        self.render()

    def _begin_camera_rest(self) -> None:
        self._duty_job=None
        if not (self.camera_power.duty_enabled and not self.camera_paused and self.live
                and (self.monitor_running or self.alignment_running)):
            return
        self.camera_resting=True
        self._suspend_camera_stream()
        self.monitor_unavailable_reason="Camera cooling: webcam hardware off"
        if self.monitor_engine is not None:
            self.monitor_engine.observe(None)
        if self.monitor_popup is not None:
            self.monitor_popup.destroy()
        if self.alignment_engine is not None:
            if self.alignment_engine.active_id is not None and self.alignment_result is not None:
                try:self._append_alignment_event("STOP", self.alignment_result,"Camera rest interrupted event")
                except (OSError,ValueError):pass
            self.alignment_engine.suspend_tracking()
        self.alignment_result = None
        if self.alignment_popup is not None:
            self.alignment_popup.destroy()
        self.alignment_left_var.set("Left: —")
        self.alignment_right_var.set("Right: —")
        self.alignment_mismatch_var.set("Inter-eye change: unavailable")
        reference_ready = bool(self.alignment_engine and self.alignment_engine.baseline is not None)
        self.alignment_status_var.set(
            "CAMERA REST • saved eye reference retained" if reference_ready else
            "CAMERA REST • initial reference still pending")
        self.monitor_status_var.set("CAMERA REST • no PD readings or alarms")
        self.status_var.set("Camera physically released for cooling")
        self._update_pause_ui()
        self._duty_job=self.root.after(int(self.camera_power.rest_seconds*1000),self._end_camera_rest)

    def _end_camera_rest(self) -> None:
        self._duty_job=None
        if not self.camera_resting:
            return
        self.camera_resting=False
        if self.camera_paused or not (self.monitor_running or self.alignment_running):
            self._update_pause_ui()
            return
        if not self._open_camera_stream():
            self._stop_camera()
            self.status_var.set("Webcam could not reopen after cooling; monitoring stopped")
            return
        self.monitor_unavailable_reason="Collecting fresh frames after camera rest"
        self._alignment_resume_after = time.monotonic() + 0.5
        if self.alignment_running:
            self.alignment_status_var.set(
                "REFERENCE PRESERVED • checking head position after cooling" if
                self.alignment_engine and self.alignment_engine.baseline is not None else
                "Initial fixation reference incomplete • complete once camera is steady")
        self._update_pause_ui()
        self._schedule_duty_cycle()

    @staticmethod
    def _settings_file() -> Path:
        if sys.platform.startswith("win"):
            base = Path(os.environ.get("APPDATA", str(Path.home())))
        else:
            base = Path.home() / ".config"
        return base / "IrisPD" / "monitor_settings.json"

    def _load_monitor_config(self) -> MonitorConfig:
        default = MonitorConfig(csv_path=str(Path.home() / "Documents" / "IrisPD" / "PD_measurements.csv"))
        try:
            raw = json.loads(self._settings_file().read_text(encoding="utf-8"))
            valid = {f.name for f in fields(MonitorConfig)}
            conf = MonitorConfig(**{k: v for k, v in raw.items() if k in valid})
            conf.validate()
            return conf
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return default

    def _persist_monitor_config(self, config: MonitorConfig) -> None:
        path = self._settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace atomically to avoid corrupting last-session preferences.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
        tmp.replace(path)

    def _camera_selected(self, _event=None) -> None:
        selected = int(self.camera_var.get())
        updated = replace(self.monitor_config, camera_index=selected)
        try:
            self._persist_monitor_config(updated)
        except OSError as exc:
            messagebox.showwarning("Camera setting not saved", str(exc), parent=self.root)
            return
        self.monitor_config = updated
        self.status_var.set("Camera index saved for the next webcam session")

    def _start_saved_monitoring(self) -> None:
        self._startup_job = None
        if self.monitor_config.auto_start_monitor and not self.monitor_running:
            self.start_monitor()
            if self.monitor_running:
                self.status_var.set("PD monitoring started using your saved settings")
        if self.alignment_settings.auto_start and not self.alignment_running:
            self.start_alignment_monitor()

    @staticmethod
    def _coffee_settings_file() -> Path:
        return IrisPDApp._settings_file().with_name("coffee_settings.json")

    def _load_coffee_settings(self) -> CoffeeSettings:
        try:
            data = json.loads(self._coffee_settings_file().read_text(encoding="utf-8"))
            # Intentionally ignore old or modified `url` keys from v11/v12.
            # The donation destination is a fixed application constant, never a preference.
            config = CoffeeSettings(enabled=data.get("enabled", True))
            config.validate()
            return config
        except (OSError, ValueError, TypeError, AttributeError):
            return CoffeeSettings()

    def _save_coffee_settings(self, config: CoffeeSettings) -> None:
        config.validate()
        path = self._coffee_settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        # No URL is ever stored in user-writable settings.
        temp.write_text(json.dumps({"enabled": config.enabled, "duration_seconds": 60},
                                   indent=2), encoding="utf-8")
        temp.replace(path)

    def _apply_coffee_settings(self, config: CoffeeSettings) -> None:
        if config == self.coffee_settings:
            return
        self._save_coffee_settings(config)
        self.coffee_settings = config
        self._show_coffee_banner()

    def _show_coffee_banner(self) -> None:
        if self._coffee_job is not None:
            try: self.root.after_cancel(self._coffee_job)
            except tk.TclError: pass
            self._coffee_job = None
        if not self.coffee_settings.enabled:
            self.coffee_row.pack_forget()
            self._coffee_deadline = None
            return
        self._coffee_deadline = time.monotonic() + 60.0
        if not self.coffee_row.winfo_manager():
            self.coffee_row.pack(anchor="center", pady=(7, 0))
        self.coffee_button.configure(text="Buy Me a Coffee")
        self.coffee_hint_var.set("Support IrisPD")
        self._coffee_banner_tick()

    def _coffee_banner_tick(self) -> None:
        self._coffee_job = None
        remaining = max(0, math.ceil((self._coffee_deadline or 0) - time.monotonic()))
        if remaining == 0 or not self.coffee_settings.enabled:
            self.coffee_row.pack_forget()
            self._coffee_deadline = None
            return
        self.coffee_label.set(f"{remaining:02d} s")
        self._coffee_job = self.root.after(250, self._coffee_banner_tick)

    def _open_coffee_link(self) -> None:
        """The support button always opens the fixed creator profile."""
        try:
            webbrowser.open(DEFAULT_COFFEE_URL, new=2)
        except OSError as exc:
            messagebox.showerror("Buy Me a Coffee link", str(exc), parent=self.root)

    def _coffee_settings_controls(self, parent, original: CoffeeSettings):
        block = tk.LabelFrame(parent, text="Buy Me a Coffee | top-center support banner", bg=PANEL,
                              fg=ACCENT, padx=12, pady=8)
        block.pack(fill="x", padx=12, pady=(8, 5))
        enabled = tk.BooleanVar(value=original.enabled)
        tk.Checkbutton(block, text="Show the header support link for 60 seconds on launch",
                       variable=enabled, bg=PANEL, fg=TEXT, selectcolor=PANEL2,
                       activebackground=PANEL, activeforeground=TEXT,
                       font=("Segoe UI", 9)).pack(anchor="w")
        self._label(block, "Support page: " + DEFAULT_COFFEE_URL,
                    color=ACCENT, size=9, wraplength=530).pack(anchor="center", pady=(8, 2))
        self._label(block, "The support button opens your browser. Camera images are not sent "
                    "to the support page. You can choose to show or hide the banner.",
                    color=MUTED, size=8, wraplength=530).pack(anchor="center")
        return enabled

    def _update_monitor_summary(self) -> None:
        cfg = self.monitor_config
        logging = " • append CSV ON" if cfg.auto_csv else " • append CSV OFF"
        self.monitor_summary_var.set(
            f"Limits {cfg.lower_mm:g}–{cfg.upper_mm:g} mm  •  every {cfg.interval_s:g} s  •  "
            f"confirm {cfg.confirm_checks}  •  {cfg.frame_window} frames{logging}\n"
            f"Quality bypass: alignment {'ON' if cfg.ignore_alignment else 'OFF'}, "
            f"instability {'ON' if cfg.ignore_instability else 'OFF'}  •  "
            f"Auto-start {'ON' if cfg.auto_start_monitor else 'OFF'}")

    def open_monitor_settings(self) -> None:
        if self.monitor_running:
            messagebox.showinfo("Monitoring active", "Stop monitoring to change limits or alerts.")
            return
        cfg = self.monitor_config
        window = tk.Toplevel(self.root)
        window.title("Continuous monitoring | Limits, alerts, CSV and startup")
        window.configure(bg=BG)
        window.geometry("690x780")
        window.minsize(560, 560)
        window.transient(self.root)
        window.grab_set()
        outer = tk.Frame(window, bg=BG)
        outer.pack(fill="both", expand=True, padx=13, pady=(13, 0))
        scroller = tk.Canvas(outer, bg=PANEL, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=scroller.yview)
        scrollbar.pack(side="right", fill="y")
        scroller.pack(side="left", fill="both", expand=True)
        scroller.configure(yscrollcommand=scrollbar.set)
        panel = tk.Frame(scroller, bg=PANEL, padx=22, pady=16)
        panel_id = scroller.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _e: scroller.configure(scrollregion=scroller.bbox("all")))
        scroller.bind("<Configure>", lambda e: scroller.itemconfigure(panel_id, width=e.width))
        self._label(panel, "Monitoring configuration", size=18, bold=True, color=ACCENT).pack(anchor="w", pady=(0, 9))
        self._label(panel, "A check uses the most recent N webcam frames (1–500). "
                    "The monitor continues until stopped, with 3 out-of-range checks "
                    "required for an alert by default.",
                    size=9, color=MUTED, wraplength=590).pack(anchor="w", pady=(0, 13))

        values = {
            "lower_mm": tk.StringVar(value=f"{cfg.lower_mm:g}"),
            "upper_mm": tk.StringVar(value=f"{cfg.upper_mm:g}"),
            "interval_s": tk.StringVar(value=f"{cfg.interval_s:g}"),
            "confirm_checks": tk.StringVar(value=str(cfg.confirm_checks)),
            "frame_window": tk.StringVar(value=str(cfg.frame_window)),
        }
        limit_frame = tk.LabelFrame(panel, text="Thresholds and timing", bg=PANEL,
                                    fg=TEXT, font=("Segoe UI", 10, "bold"), padx=9, pady=9)
        limit_frame.pack(fill="x")
        specs = [
            ("Alert BELOW (mm)", "lower_mm", 0, 0),
            ("Alert ABOVE (mm)", "upper_mm", 0, 1),
            ("Check every (seconds)", "interval_s", 1, 0),
            ("Consecutive checks to alert", "confirm_checks", 1, 1),
            ("Frames per reading (1–500)", "frame_window", 2, 0),
        ]
        for label, key, r, c in specs:
            f = tk.Frame(limit_frame, bg=PANEL)
            f.grid(row=r, column=c, sticky="ew", padx=7, pady=4)
            self._label(f, label, color=MUTED, size=9).pack(anchor="w")
            tk.Entry(f, textvariable=values[key], bg=PANEL2, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=("Segoe UI", 12),
                     width=17).pack(anchor="w", fill="x", ipady=4)
        limit_frame.grid_columnconfigure(0, weight=1)
        limit_frame.grid_columnconfigure(1, weight=1)

        quality_frame = tk.LabelFrame(panel, text="Optional quality-check bypasses", bg=PANEL,
                                      fg=TEXT, font=("Segoe UI", 10, "bold"), padx=10, pady=7)
        quality_frame.pack(fill="x", pady=(10, 0))
        ignore_alignment = tk.BooleanVar(value=cfg.ignore_alignment)
        ignore_instability = tk.BooleanVar(value=cfg.ignore_instability)
        tk.Checkbutton(
            quality_frame, text="Ignore poor alignment — use flagged webcam frames",
            variable=ignore_alignment, bg=PANEL, fg=TEXT,
            activebackground=PANEL, activeforeground=TEXT, selectcolor=PANEL2,
            font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=2)
        tk.Checkbutton(
            quality_frame, text="Ignore unstable rolling median — allow jittery results",
            variable=ignore_instability, bg=PANEL, fg=TEXT,
            activebackground=PANEL, activeforeground=TEXT, selectcolor=PANEL2,
            font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=2)
        self._label(quality_frame,
                    "Both are OFF by default. Bypassing alignment permits all flagged "
                    "geometry frames, even poor iris detail. Warnings remain visible "
                    "and are logged; no detected face still means no reading.",
                    color=GOLD, size=9, wraplength=575).pack(anchor="w", pady=(5, 1))

        # Keep the shared support setting in the MIDDLE of this dialog.
        coffee_enabled = self._coffee_settings_controls(panel, self.coffee_settings)

        alert_frame = tk.LabelFrame(panel, text="Alert types (choose any)", bg=PANEL,
                                    fg=TEXT, font=("Segoe UI", 10, "bold"), padx=10, pady=7)
        alert_frame.pack(fill="x", pady=(11, 0))
        opts = {name: tk.BooleanVar(value=getattr(cfg, name)) for name in (
            "visual", "sound", "popup", "voice", "repeat_notifications", "auto_csv")}
        choices = [
            ("Colored visual warning", "visual", 0, 0),
            ("Windows alert sound", "sound", 0, 1),
            ("Nonblocking popup", "popup", 1, 0),
            ("Spoken warning (Windows)", "voice", 1, 1),
            ("Repeat alerts every group of checks", "repeat_notifications", 2, 0),
        ]
        for label, key, r, c in choices:
            tk.Checkbutton(alert_frame, text=label, variable=opts[key],
                           bg=PANEL, fg=TEXT, activebackground=PANEL,
                           activeforeground=TEXT, selectcolor=PANEL2,
                           font=("Segoe UI", 9), anchor="w").grid(
                               row=r, column=c, columnspan=2 if r == 2 else 1,
                               sticky="w", padx=4, pady=3)

        csv_frame = tk.LabelFrame(panel, text="Automatic CSV logging", bg=PANEL,
                                  fg=TEXT, font=("Segoe UI", 10, "bold"), padx=10, pady=8)
        csv_frame.pack(fill="x", pady=(11, 0))
        tk.Checkbutton(csv_frame, text="Append each scheduled check to CSV as it measures",
                       variable=opts["auto_csv"], bg=PANEL, fg=TEXT,
                       activebackground=PANEL, activeforeground=TEXT,
                       selectcolor=PANEL2, font=("Segoe UI", 9)).pack(anchor="w")
        path_var = tk.StringVar(value=cfg.csv_path)
        path_line = tk.Frame(csv_frame, bg=PANEL)
        path_line.pack(fill="x", pady=(7, 3))
        entry = tk.Entry(path_line, textvariable=path_var, bg=PANEL2, fg=TEXT,
                         insertbackground=TEXT, relief="flat", font=("Segoe UI", 9))
        entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))

        def browse() -> None:
            selected = filedialog.asksaveasfilename(
                parent=window, title="Choose an append-only measurement CSV",
                defaultextension=".csv", initialfile="PD_measurements.csv",
                initialdir=str(Path(path_var.get()).expanduser().parent),
                filetypes=[("CSV files", "*.csv")])
            if selected:
                path_var.set(selected)
                opts["auto_csv"].set(True)

        self._btn(path_line, "Browse", browse, width=9).pack(side="right")
        self._label(csv_frame, "Existing compatible files are appended; no previous records are overwritten.",
                    color=MUTED, size=9, wraplength=540).pack(anchor="w", pady=(3, 0))

        startup_frame = tk.LabelFrame(panel, text="Windows startup and automatic monitoring",
                                      bg=PANEL, fg=TEXT,
                                      font=("Segoe UI", 10, "bold"), padx=10, pady=8)
        startup_frame.pack(fill="x", pady=(12, 0))
        auto_launch = tk.BooleanVar(value=cfg.auto_launch_windows)
        auto_monitor = tk.BooleanVar(value=cfg.auto_start_monitor)
        startup_checkbox = tk.Checkbutton(
            startup_frame, text="Automatically launch IrisPD when I sign in to Windows",
            variable=auto_launch, bg=PANEL, fg=TEXT, activebackground=PANEL,
            activeforeground=TEXT, selectcolor=PANEL2, font=("Segoe UI", 9),
            anchor="w")
        startup_checkbox.pack(anchor="w", pady=2)
        if sys.platform != "win32":
            startup_checkbox.config(state="disabled")
        tk.Checkbutton(
            startup_frame,
            text="Automatically start monitoring whenever IrisPD opens (last settings)",
            variable=auto_monitor, bg=PANEL, fg=TEXT, activebackground=PANEL,
            activeforeground=TEXT, selectcolor=PANEL2, font=("Segoe UI", 9),
            anchor="w").pack(anchor="w", pady=2)
        self._label(startup_frame,
                    "The first option runs at Windows sign-in for this user, without admin rights. "
                    "The second starts the camera and monitor after any app launch. "
                    "Your selected camera, limits, frame count, alerts and CSV path are reused. "
                    "Keep this app folder in the same location after enabling Windows startup.",
                    color=MUTED, size=9, wraplength=560).pack(anchor="w", pady=(5, 2))

        actions = tk.Frame(window, bg=PANEL, padx=17, pady=9)
        actions.pack(fill="x", padx=13, pady=(0, 13))

        def save_options() -> None:
            try:
                checks_text = values["confirm_checks"].get().strip()
                checks = int(checks_text)
                if str(checks) != checks_text:
                    raise ValueError("Consecutive checks must be a whole number.")
                window_text = values["frame_window"].get().strip()
                frame_window = int(window_text)
                if str(frame_window) != window_text:
                    raise ValueError("Frame window must be a whole number from 1 to 500.")
                updated = MonitorConfig(
                    lower_mm=float(values["lower_mm"].get()),
                    upper_mm=float(values["upper_mm"].get()),
                    interval_s=float(values["interval_s"].get()),
                    confirm_checks=checks,
                    frame_window=frame_window,
                    ignore_alignment=ignore_alignment.get(),
                    ignore_instability=ignore_instability.get(),
                    **{name: opts[name].get() for name in opts},
                    csv_path=path_var.get().strip(),
                    auto_launch_windows=auto_launch.get(),
                    auto_start_monitor=auto_monitor.get(),
                    camera_index=int(self.camera_var.get()),
                )
                updated.validate()
                coffee_new = CoffeeSettings(enabled=coffee_enabled.get())
                coffee_new.validate()
            except (TypeError, ValueError) as exc:
                messagebox.showerror("Invalid monitoring settings", str(exc), parent=window)
                return
            # Enable/disable the current-user Windows Run entry at save time.
            # The two startup switches remain independent of one another.
            try:
                self._persist_monitor_config(updated)
            except OSError as exc:
                messagebox.showerror("Cannot save settings", str(exc), parent=window)
                return
            if sys.platform == "win32":
                try:
                    set_auto_launch(updated.auto_launch_windows)
                except OSError as exc:
                    # No misleading checked setting after failed registration.
                    try:
                        self._persist_monitor_config(self.monitor_config)
                    except OSError:
                        pass
                    messagebox.showerror(
                        "Windows auto-launch could not be updated",
                        f"The startup entry could not be changed: {exc}\n"
                        "Your previous settings are still in use.", parent=window)
                    return
            previous_window = self.monitor_config.frame_window
            self.monitor_config = updated
            try:
                self._apply_coffee_settings(coffee_new)
            except (OSError, ValueError) as exc:
                messagebox.showwarning("Support setting not saved", str(exc), parent=window)
            self._update_monitor_summary()
            self.samples = deque(maxlen=updated.frame_window)
            self.progress["maximum"] = updated.frame_window
            self.progress["value"] = 0
            if self.live:
                self.locked = None  # Next single-capture uses new settings.
                self.progress_var.set(f"Waiting for {updated.frame_window} usable frames")
            if previous_window != updated.frame_window:
                self.status_var.set(f"Frame window updated to {updated.frame_window}; capture restarted")
            window.destroy()

        self._btn(actions, "Save settings", save_options, primary=True,
                  width=18).pack(side="right", padx=(9, 0))
        self._btn(actions, "Cancel", window.destroy, width=10).pack(side="right")

    def start_monitor(self) -> None:
        if self.camera_paused:
            self.monitor_status_var.set("Camera paused • click Resume camera first")
            return
        if self.monitor_running:
            return
        cfg = self.monitor_config
        try:
            cfg.validate()
        except ValueError as exc:
            messagebox.showerror("Check monitoring settings", str(exc))
            return
        if not self.live and not self.camera_resting:
            self.start_webcam()
            if not self.live:
                return
        logger = None
        if cfg.auto_csv:
            try:
                logger = AppendCSVLogger(cfg.csv_path)
            except (OSError, ValueError) as exc:
                messagebox.showerror("CSV logging error", f"Monitoring was not started: {exc}")
                return
        self.monitor_csv = logger
        self.monitor_engine = MonitorEngine(cfg)
        self.monitor_running = True
        self.monitor_count = 0
        self.monitor_session = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        self.monitor_estimate = None
        self.monitor_last_good_at = None
        self.monitor_unavailable_reason = "Collecting frames"
        self.samples = deque(maxlen=cfg.frame_window)
        self.progress["maximum"] = cfg.frame_window
        self.progress["value"] = 0
        self.locked = None
        self.monitor_start_btn.config(state="disabled")
        self.monitor_stop_btn.config(state="normal")
        self.monitor_settings_btn.config(state="disabled")
        self.monitor_status_var.set(f"Collecting {cfg.frame_window} frames • first check in "
                                    f"{cfg.interval_s:g} seconds")
        self.monitor_status_label.config(bg=PANEL2, fg=TEXT)
        self.status_var.set("Monitoring continuously • stop it with Stop monitoring")
        self.monitor_job = self.root.after(int(cfg.interval_s * 1000), self._monitor_sample_tick)
        self._schedule_duty_cycle()

    def stop_monitor(self, silent: bool = False) -> None:
        if self.monitor_job is not None:
            try:
                self.root.after_cancel(self.monitor_job)
            except tk.TclError:
                pass
            self.monitor_job = None
        was_running = self.monitor_running
        self.monitor_running = False
        self.monitor_estimate = None
        self.monitor_last_good_at = None
        if self.monitor_csv is not None:
            self.monitor_csv.close()
            self.monitor_csv = None
        if hasattr(self, "monitor_start_btn"):
            self.monitor_start_btn.config(state="normal")
            self.monitor_stop_btn.config(state="disabled")
            self.monitor_settings_btn.config(state="normal")
            self.monitor_status_label.config(bg=PANEL2, fg=TEXT)
        if self.monitor_popup is not None:
            try:
                self.monitor_popup.destroy()
            except tk.TclError:
                pass
            self.monitor_popup = None
        if was_running:
            self.samples.clear()
            self.locked = None
            self.monitor_status_var.set(f"Stopped • {self.monitor_count} checks in this session")
            if not silent:
                self.status_var.set("Monitoring stopped; CSV data already saved")
            if not self.alignment_running:
                self._release_if_idle()

    def _monitor_sample_tick(self) -> None:
        self.monitor_job = None
        if not self.monitor_running or self.monitor_engine is None:
            return
        cfg = self.monitor_config
        # Use a fresh N-frame median, subject to the user-selected quality filters.
        reading = self.monitor_estimate if (self.live and not self.camera_resting and not self.camera_paused) else None
        if (reading is None or self.monitor_last_good_at is None or
                (time.monotonic() - self.monitor_last_good_at > 1.5)):
            reading = None
        decision = self.monitor_engine.observe(reading.total_mm if reading else None)
        if decision.status == "NO_READING" and self.monitor_popup is not None:
            try:
                self.monitor_popup.destroy()
            except tk.TclError:
                pass
            self.monitor_popup = None
        self.monitor_count += 1
        check = ((self.monitor_count - 1) % cfg.confirm_checks) + 1
        set_number = ((self.monitor_count - 1) // cfg.confirm_checks) + 1
        pd_text = f"{reading.total_mm:.1f} mm" if reading else "no PD reading"
        if decision.status == "NO_READING":
            reason = self.monitor_unavailable_reason or "No recent webcam estimate"
            label = f"Check {self.monitor_count}: {reason} • no alert"
            color = MUTED
        elif decision.status == "IN_RANGE":
            label = f"IN RANGE • {pd_text} • check {self.monitor_count}"
            if decision.recovered:
                label = f"Back in range • {pd_text} • alert cleared"
            color = ACCENT
        else:
            bound = cfg.lower_mm if decision.status == "LOW" else cfg.upper_mm
            symbol = "<" if decision.status == "LOW" else ">"
            label = (f"{decision.status} • {pd_text} ({symbol} {bound:g} mm) • "
                     f"{min(decision.consecutive, cfg.confirm_checks)}/{cfg.confirm_checks} checks")
            if decision.confirmed:
                label = "ALERT: " + label
            color = BAD if decision.status == "HIGH" else GOLD
        if reading and reading.warnings:
            label += " • quality warning (bypass enabled)"
        self.monitor_status_var.set(label)
        if decision.confirmed and cfg.visual:
            self.monitor_status_label.config(fg=BG, bg=color)
        else:
            self.monitor_status_label.config(fg=color, bg=PANEL2)
        if decision.recovered and self.monitor_popup is not None:
            self.monitor_popup.destroy()
            self.monitor_popup = None

        if self.monitor_csv is not None:
            def mm(x):
                return f"{x:.2f}" if x is not None else ""
            notes = ("; ".join(reading.warnings) or "No quality warnings") if reading else (
                self.monitor_unavailable_reason or "No recent webcam estimate")
            try:
                self.monitor_csv.append([
                    datetime.now().isoformat(timespec="seconds"), self.monitor_session,
                    self.monitor_count, set_number, check,
                    mm(reading.total_mm if reading else None),
                    mm(reading.screen_left_mm if reading else None),
                    mm(reading.screen_right_mm if reading else None),
                    reading.method if reading else "", decision.status,
                    decision.consecutive, "yes" if decision.confirmed else "no",
                    "yes" if decision.notify else "no",
                    f"{cfg.lower_mm:g}", f"{cfg.upper_mm:g}", notes,
                ])
            except (OSError, ValueError) as exc:
                self.stop_monitor(silent=True)
                messagebox.showerror("CSV writing failed", f"Monitoring stopped to prevent "
                                     f"silent loss of measurements:\n{exc}")
                return
        if decision.notify:
            self._notify_monitor(decision.status, reading.total_mm)
        self.progress_var.set(
            f"Continuous • check {self.monitor_count}, set {set_number}, position {check}/"
            f"{cfg.confirm_checks}; next in {cfg.interval_s:g} seconds")
        self.monitor_job = self.root.after(int(cfg.interval_s * 1000), self._monitor_sample_tick)

    def _notify_monitor(self, status: str, measured_mm: float) -> None:
        cfg = self.monitor_config
        direction = "above" if status == "HIGH" else "below"
        limit = cfg.upper_mm if status == "HIGH" else cfg.lower_mm
        msg = f"Pupil distance {measured_mm:.1f} millimeters, {direction} {limit:g} millimeters."
        if cfg.sound:
            try:
                if sys.platform.startswith("win"):
                    import winsound
                    winsound.PlaySound("SystemHand" if status == "HIGH" else "SystemExclamation",
                                       winsound.SND_ALIAS | winsound.SND_ASYNC)
                else:
                    self.root.bell()
            except (RuntimeError, OSError, tk.TclError):
                pass
        if cfg.popup:
            self._show_timed_alert(
                "monitor_popup", "IrisPD | PD threshold alert",
                f"{status}: PD OUTSIDE LIMIT", msg,
                BAD if status == "HIGH" else GOLD, width=420, height=205)
        if cfg.voice and sys.platform.startswith("win"):
            # Speech is optional; PowerShell invokes Windows' built-in SAPI voice.
            speech = f"Warning. Pupil distance is {status.lower()}. {msg}"
            safe = speech.replace("'", "''")
            command = ("Add-Type -AssemblyName System.Speech; "
                       "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                       f"$s.Speak('{safe}')")
            try:
                subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                                  "-WindowStyle", "Hidden", "-Command", command],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.status_var.set("Spoken alert unavailable; other alerts still work")

    def _show_timed_alert(self, attr: str, title: str, headline: str,
                          message: str, highlight: str, *,
                          width: int = 450, height: int = 225) -> None:
        """Nonmodal alert: draggable by title bar or handle; self-dismisses after 4 s."""
        existing = getattr(self, attr, None)
        if existing is not None:
            try:
                existing.destroy()
            except tk.TclError:
                pass
        popup = tk.Toplevel(self.root)
        setattr(self, attr, popup)
        popup.title(title)
        popup.resizable(False, False)
        popup.configure(bg=PANEL2)
        x = max(0, self.root.winfo_rootx() + 50)
        y = max(0, self.root.winfo_rooty() + 50)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.attributes("-topmost", True)
        # Native Windows title bar is movable. This handle additionally allows
        # dragging the popup from inside, without modal grabs or focus trapping.
        handle = tk.Label(popup, text="⇕  Drag here to move    •    Dismisses after 4 seconds",
                          bg=PANEL, fg=MUTED, cursor="fleur", font=("Segoe UI", 9),
                          anchor="w", padx=12, pady=7)
        handle.pack(fill="x")
        def drag_begin(event):
            popup._drag_offset = (event.x_root - popup.winfo_x(),
                                  event.y_root - popup.winfo_y())
        def drag_motion(event):
            dx, dy = getattr(popup, "_drag_offset", (0, 0))
            popup.geometry(f"+{max(0, event.x_root - dx)}+{max(0, event.y_root - dy)}")
        handle.bind("<ButtonPress-1>", drag_begin)
        handle.bind("<B1-Motion>", drag_motion)
        tk.Label(popup, text=headline, bg=PANEL2, fg=highlight,
                 font=("Segoe UI", 13, "bold")).pack(pady=(12, 5))
        tk.Label(popup, text=message, bg=PANEL2, fg=TEXT, font=("Segoe UI", 10),
                 wraplength=width-35, justify="center").pack(padx=13)
        tk.Button(popup, text="Dismiss now", command=popup.destroy, bg=PANEL,
                  fg=TEXT, activebackground="#264363", activeforeground=TEXT,
                  relief="flat", padx=13, pady=4).pack(side="bottom", pady=(5, 10))
        popup._dismiss_job = popup.after(4000, popup.destroy)
        def cleanup(event):
            if event.widget is not popup:
                return
            try:
                popup.after_cancel(popup._dismiss_job)
            except tk.TclError:
                pass
            if getattr(self, attr, None) is popup:
                setattr(self, attr, None)
        popup.bind("<Destroy>", cleanup)

    @staticmethod
    def _alignment_settings_file() -> Path:
        return IrisPDApp._settings_file().with_name("alignment_settings.json")

    def _load_alignment_settings(self) -> AlignmentSettings:
        default = AlignmentSettings(csv_directory=str(Path.home() / "Documents" / "IrisPD"))
        try:
            raw = json.loads(self._alignment_settings_file().read_text(encoding="utf-8"))
            valid = {f.name for f in fields(AlignmentSettings)}
            config = AlignmentSettings(**{k: v for k, v in raw.items() if k in valid})
            config.validate()
            return config
        except (OSError, ValueError, TypeError, AttributeError):
            return default

    def _save_alignment_settings(self, config: AlignmentSettings) -> None:
        path = self._alignment_settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
        temp_path.replace(path)

    def open_alignment_settings(self) -> None:
        if self.alignment_running:
            messagebox.showinfo("Alignment monitoring active", "Stop eye-alignment monitoring to edit its settings.")
            return
        cfg = self.alignment_settings
        w = tk.Toplevel(self.root)
        w.title("Eye alignment • reference, drift detection, alerts and CSV")
        w.geometry("650x720")
        w.minsize(550, 600)
        w.configure(bg=BG)
        w.transient(self.root)
        w.grab_set()
        outer = tk.Frame(w, bg=BG)
        outer.pack(fill="both", expand=True, padx=14, pady=(14, 0))
        sc = tk.Canvas(outer, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(outer, command=sc.yview, orient="vertical")
        sb.pack(side="right", fill="y")
        sc.pack(side="left", fill="both", expand=True)
        sc.config(yscrollcommand=sb.set)
        content = tk.Frame(sc, bg=PANEL, padx=17, pady=16)
        content_id = sc.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>", lambda e: sc.itemconfigure(content_id, width=e.width))
        self._label(content, "Experimental eye-alignment settings", color=ACCENT,
                    size=17, bold=True).pack(anchor="w", pady=(0, 9))
        self._label(content, "Calibrate while looking steadily at a fixed target near the webcam. "
                    "The tool detects changes in relative iris position, not clinical strabismus or prism diopters.",
                    color=MUTED, size=9, wraplength=555).pack(anchor="w", pady=(0, 13))
        entries = {}
        options = (
            ("Reference frames (10–300)", "reference_frames", cfg.reference_frames),
            ("Inter-eye difference threshold (% eye width; 3–45)", "threshold_pct", cfg.threshold_pct),
            ("Continuous change required (seconds; 0.15–10)", "confirmation_s", cfg.confirmation_s),
            ("Return-to-reference duration (seconds; 0.15–10)", "recovery_s", cfg.recovery_s),
            ("Head-position tolerance (0.015–0.2)", "max_head_shift", cfg.max_head_shift),
            ("Hold before auto-reorient (seconds; 1–15)", "auto_recalibrate_delay_s",
             cfg.auto_recalibrate_delay_s),
            ("Sample CSV interval (seconds; 0.2–60)", "sample_interval_s", cfg.sample_interval_s),
        )
        for text, name, value in options:
            line = tk.Frame(content, bg=PANEL)
            line.pack(fill="x", pady=3)
            self._label(line, text, size=9, color=MUTED).pack(side="left", expand=True, fill="x")
            var = tk.StringVar(value=str(value))
            tk.Entry(line, textvariable=var, width=12, fg=TEXT, bg=PANEL2,
                     insertbackground=TEXT, relief="flat").pack(side="right", ipadx=3, ipady=6)
            entries[name] = var
        # Same shared support controls, centered among the eye settings.
        coffee_enabled = self._coffee_settings_controls(content, self.coffee_settings)

        self._label(content, "Alerts and exports", color=TEXT, size=12, bold=True).pack(anchor="w", pady=(13, 3))
        flags = {}
        flag_labels = (
            ("Visual warning", "visual"), ("Windows sound", "sound"),
            ("Popup", "popup"), ("Spoken warning (Windows)", "voice"),
            ("Append confirmed event transitions to CSV", "log_events"),
            ("Append one measurement row periodically to a separate CSV", "log_samples"),
            ("Auto-start alignment monitoring when IrisPD opens", "auto_start"),
            ("Auto-recalibrate after sustained head reorientation (optional)",
             "auto_recalibrate_on_head_move"),
        )
        for label, key in flag_labels:
            flag = tk.BooleanVar(value=getattr(cfg, key))
            flags[key] = flag
            tk.Checkbutton(content, text=label, variable=flag, bg=PANEL, fg=TEXT,
                selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT,
                anchor="w", font=("Segoe UI", 9)).pack(anchor="w", pady=2)
        self._label(content, "CSV data folder (no images or video saved)", color=MUTED, size=9).pack(anchor="w", pady=(8, 4))
        directory_var = tk.StringVar(value=cfg.csv_directory)
        row = tk.Frame(content, bg=PANEL)
        row.pack(fill="x", pady=(0, 7))
        tk.Entry(row, textvariable=directory_var, fg=TEXT, bg=PANEL2,
                 insertbackground=TEXT, relief="flat").pack(side="left", expand=True, fill="x", ipady=6)
        def browse() -> None:
            folder = filedialog.askdirectory(parent=w, title="Choose eye alignment CSV folder")
            if folder:
                directory_var.set(folder)
        self._btn(row, "Browse", browse, width=8).pack(side="right", padx=(6, 0))
        self._label(content, "By default, head movement pauses detection and retains the reference. "
                    "Optionally enable auto-reorientation only after the new head pose remains "
                    "steady for the selected interval (default 2.5 s); a 12-second cooldown "
                    "prevents repeated resets. Recalibrate always works manually.",
                    color=GOLD, size=9, wraplength=565).pack(anchor="w", pady=(3, 8))
        actions = tk.Frame(w, bg=PANEL, padx=17, pady=11)
        actions.pack(fill="x", padx=14, pady=(0, 14))
        def save() -> None:
            try:
                n = int(entries["reference_frames"].get())
                config = AlignmentSettings(
                    reference_frames=n,
                    threshold_pct=float(entries["threshold_pct"].get()),
                    confirmation_s=float(entries["confirmation_s"].get()),
                    recovery_s=float(entries["recovery_s"].get()),
                    max_head_shift=float(entries["max_head_shift"].get()),
                    sample_interval_s=float(entries["sample_interval_s"].get()),
                    auto_recalibrate_delay_s=float(entries["auto_recalibrate_delay_s"].get()),
                    csv_directory=directory_var.get().strip(),
                    **{key: var.get() for key, var in flags.items()},
                )
                config.validate()
                coffee_new = CoffeeSettings(enabled=coffee_enabled.get())
                coffee_new.validate()
                self._save_alignment_settings(config)
            except (ValueError, OSError, TypeError) as exc:
                messagebox.showerror("Invalid alignment configuration", str(exc), parent=w)
                return
            self.alignment_settings = config
            self._auto_recalibration_gate.hold_seconds = config.auto_recalibrate_delay_s
            self._auto_recalibration_gate.clear_pending()
            try:
                self._apply_coffee_settings(coffee_new)
            except (OSError, ValueError) as exc:
                messagebox.showwarning("Support setting not saved", str(exc), parent=w)
            self.alignment_status_var.set("Alignment settings saved; start to calibrate")
            w.destroy()
        self._btn(actions, "Save settings", save, primary=True, width=17).pack(side="right", padx=5)
        self._btn(actions, "Cancel", w.destroy, width=8).pack(side="right")

    def start_alignment_monitor(self) -> None:
        if self.camera_paused:
            self.alignment_status_var.set("Camera paused • click Resume camera first")
            return
        if self.alignment_running:
            return
        cfg = self.alignment_settings
        try:
            cfg.validate()
        except ValueError as exc:
            messagebox.showerror("Alignment settings", str(exc))
            return
        if not self.live and not self.camera_resting:
            self.start_webcam()
            if not self.live:
                return
        evt_logger = smp_logger = None
        try:
            if cfg.log_events:
                evt_logger = AppendAlignmentCSV(Path(cfg.csv_directory) / "EyeAlignment_events.csv", EVENT_HEADERS)
            if cfg.log_samples:
                smp_logger = AppendAlignmentCSV(Path(cfg.csv_directory) / "EyeAlignment_samples.csv", SAMPLE_HEADERS)
        except (OSError, ValueError) as exc:
            if evt_logger:
                evt_logger.close()
            messagebox.showerror("Alignment CSV error", f"Alignment did not start: {exc}")
            return
        self.alignment_engine = AlignmentEngine(cfg)
        self._auto_recalibration_gate.hold_seconds = cfg.auto_recalibrate_delay_s
        self._auto_recalibration_gate.clear_pending()
        if self._cached_alignment_reference is not None:
            # Only one reference per application session unless Recalibrate is pressed.
            self.alignment_engine.baseline = self._cached_alignment_reference
        self._alignment_resume_after = 0.0
        self.alignment_events_csv = evt_logger
        self.alignment_samples_csv = smp_logger
        self.alignment_last_csv_at = None
        self.alignment_session = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        self.alignment_result = None
        self.alignment_running = True
        self.align_start_btn.config(state="disabled")
        self.align_stop_btn.config(state="normal")
        self.align_settings_btn.config(state="disabled")
        if self.alignment_engine.baseline is None:
            self.alignment_status_var.set("Calibrating • look at a fixed target near webcam")
        else:
            self.alignment_status_var.set("Existing reference restored • monitoring without recalibration")
        self.align_status_label.config(fg=GOLD)
        self._schedule_duty_cycle()

    def _append_alignment_event(self, event: str, result: AlignmentResult,
                                note: str | None = None) -> None:
        if self.alignment_events_csv is None:
            return
        fmt = lambda v: "" if v is None else f"{v:.2f}"
        self.alignment_events_csv.append([
            datetime.now().isoformat(timespec="milliseconds"), self.alignment_session,
            result.event_id or "", event, result.likely_eye, result.axis,
            fmt(result.mismatch_pct), fmt(result.peak_pct), note or result.note,
        ])

    def stop_alignment_monitor(self, silent: bool = False) -> None:
        was_running = self.alignment_running
        self.alignment_running = False
        if self.alignment_engine and self.alignment_engine.active_id is not None:
            final_result = self.alignment_result or AlignmentResult("STOPPED", "Stopped")
            final_result = replace(final_result, event_id=self.alignment_engine.active_id,
                                   peak_pct=self.alignment_engine.peak,
                                   likely_eye=self.alignment_engine.active_eye,
                                   axis=self.alignment_engine.active_axis)
            try:
                self._append_alignment_event("STOP", final_result, "User stopped or camera closed")
            except (OSError, ValueError):
                pass
        if self.alignment_engine is not None and self.alignment_engine.baseline is not None:
            self._cached_alignment_reference = self.alignment_engine.baseline
        for log in (self.alignment_events_csv, self.alignment_samples_csv):
            if log is not None:
                log.close()
        self.alignment_events_csv = None
        self.alignment_samples_csv = None
        self.alignment_engine = None
        self.alignment_result = None
        if self.alignment_popup is not None:
            try:
                self.alignment_popup.destroy()
            except tk.TclError:
                pass
            self.alignment_popup = None
        if hasattr(self, "align_start_btn"):
            self.align_start_btn.config(state="normal")
            self.align_stop_btn.config(state="disabled")
            self.align_settings_btn.config(state="normal")
            self.align_status_label.config(fg=MUTED)
        if was_running and not silent:
            self.alignment_status_var.set("Eye alignment monitoring stopped")
        elif was_running:
            self.alignment_status_var.set("Camera closed — alignment monitoring stopped")
        if was_running and not self.monitor_running:
            self._release_if_idle()

    def recalibrate_alignment(self) -> None:
        if not self.alignment_running or self.alignment_engine is None:
            self._cached_alignment_reference = None
            self.start_alignment_monitor()
            return
        if self.alignment_engine.active_id is not None and self.alignment_result is not None:
            try:
                self._append_alignment_event("RESET", self.alignment_result, "Reference recalibration")
            except (OSError, ValueError):
                pass
        self.alignment_engine.reset_reference()
        self._auto_recalibration_gate.clear_pending()
        self._cached_alignment_reference = None
        self.alignment_result = None
        self.alignment_left_var.set("—")
        self.alignment_right_var.set("—")
        self.alignment_mismatch_var.set("Inter-eye change: —")
        self.alignment_status_var.set("Recalibrating • look at a stationary target")

    def _alignment_live_tick(self) -> None:
        if self.alignment_engine is None or not self.alignment_running:
            return
        now = time.monotonic()
        if now < self._alignment_resume_after:
            self.alignment_status_var.set("Camera settling • preserving existing reference")
            return
        result = self.alignment_engine.observe(self.alignment_sample, now)
        cfg = self.alignment_settings
        steady_new_pose = (cfg.auto_recalibrate_on_head_move and
                           self.alignment_sample is not None and
                           self.alignment_engine.baseline is not None and
                           result.status == "PAUSED" and
                           result.note.startswith("Head moved relative to reference"))
        if self._auto_recalibration_gate.observe(steady_new_pose, now):
            # Recalibration is intentional here. Never reset on a transient
            # head movement, blink, tracking loss or camera rest alone.
            if self.alignment_engine.active_id is not None and self.alignment_result is not None:
                self._append_alignment_event("RESET", self.alignment_result,
                                             "Stable head reorientation; auto calibration")
            self.alignment_engine.reset_reference()
            self._cached_alignment_reference = None
            result = AlignmentResult("CALIBRATING",
                     "Stable new head pose detected; reorienting fixation reference")
        self.alignment_result = result
        self.alignment_status_var.set(f"{result.status} • {result.note}")
        is_alert = result.status == "POSSIBLE EYE DRIFT"
        self.align_status_label.config(fg=BAD if is_alert else GOLD if result.status in
                                       ("PAUSED", "CALIBRATING", "CANDIDATE") else ACCENT)
        if result.left_dx_pct is not None:
            self.alignment_left_var.set(
                f"Left:  horizontal {result.left_dx_pct:+.1f}%, vertical {result.left_dy_pct:+.1f}%")
            self.alignment_right_var.set(
                f"Right: horizontal {result.right_dx_pct:+.1f}%, vertical {result.right_dy_pct:+.1f}%")
            self.alignment_mismatch_var.set(
                f"Inter-eye change: {result.mismatch_pct:.1f}% • {result.likely_eye} • {result.axis}")
        if result.status == "READY":
            self.alignment_left_var.set("Left: reference established")
            self.alignment_right_var.set("Right: reference established")
            self.alignment_mismatch_var.set("Inter-eye change: 0% at reference")
        elif result.status in ("PAUSED", "CALIBRATING"):
            # Never show stale last-valid offsets as though they are live.
            self.alignment_left_var.set("Left: —")
            self.alignment_right_var.set("Right: —")
            self.alignment_mismatch_var.set("Inter-eye change: unavailable")
        try:
            if result.started:
                self._append_alignment_event("START", result)
                self._notify_alignment(result)
            if result.ended:
                self._append_alignment_event("END", result)
                if self.alignment_popup is not None:
                    self.alignment_popup.destroy()
                    self.alignment_popup = None
            cfg = self.alignment_settings
            if self.alignment_samples_csv is not None and (
                    self.alignment_last_csv_at is None or
                    now - self.alignment_last_csv_at >= cfg.sample_interval_s):
                fmt = lambda v: "" if v is None else f"{v:.3f}"
                self.alignment_samples_csv.append([
                    datetime.now().isoformat(timespec="milliseconds"), self.alignment_session,
                    result.status, fmt(result.left_dx_pct), fmt(result.left_dy_pct),
                    fmt(result.right_dx_pct), fmt(result.right_dy_pct),
                    fmt(result.mismatch_pct), result.likely_eye, result.axis, result.note,
                ])
                self.alignment_last_csv_at = now
        except (OSError, ValueError) as exc:
            self.stop_alignment_monitor(silent=True)
            messagebox.showerror("Eye-alignment CSV failure",
                                 f"Alignment stopped to avoid silently losing logs:\n{exc}")

    def _notify_alignment(self, result: AlignmentResult) -> None:
        cfg = self.alignment_settings
        msg = (f"Possible eye position change: {result.likely_eye}. "
               f"Inter-eye difference {result.mismatch_pct:.1f}% of eye width. "
               "This is not a diagnosis of strabismus.")
        if cfg.sound:
            try:
                if sys.platform.startswith("win"):
                    import winsound
                    winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
                else:
                    self.root.bell()
            except (RuntimeError, OSError, tk.TclError):
                pass
        if cfg.popup:
            self._show_timed_alert(
                "alignment_popup", "IrisPD | Possible eye-position change",
                "POSSIBLE EYE-POSITION EVENT", msg, BAD,
                width=465, height=245)
        if cfg.voice and sys.platform.startswith("win"):
            safe = msg.replace("'", "''")
            command = ("Add-Type -AssemblyName System.Speech; "
                       "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                       f"$s.Speak('{safe}')")
            try:
                subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-WindowStyle", "Hidden", "-Command", command],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def _release_if_idle(self) -> None:
        """Release camera, queued callbacks and preview when neither monitor runs."""
        if self.monitor_running or self.alignment_running:
            return
        self._cancel_duty_cycle()
        self.camera_resting=False
        self.live = False
        if self.after_job is not None:
            try:
                self.root.after_cancel(self.after_job)
            except tk.TclError:
                pass
            self.after_job = None
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        self.frame = None
        self.base_reading = None
        self.reading = None
        self.alignment_sample = None
        self.monitor_estimate = None
        self.samples.clear()
        self._photo_image = None
        self._display_reading(None)
        self.render()

    def _stop_camera(self) -> None:
        self._cancel_duty_cycle()
        self.camera_resting=False
        if self.alignment_running:
            self.stop_alignment_monitor(silent=True)
        if self.monitor_running:
            self.stop_monitor(silent=True)
        self._suspend_camera_stream()
        self._update_pause_ui()

    def _open_camera_stream(self) -> bool:
        """Open just the camera without stopping either monitor (for duty-cycle resume)."""
        if self.camera_paused:
            return False
        if self.live and self.capture is not None:
            return True
        try:
            index=int(self.camera_var.get())
        except ValueError:
            index=0
        if sys.platform.startswith("win"):
            cap=cv2.VideoCapture(index,cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                cap=cv2.VideoCapture(index,cv2.CAP_MSMF)
        else:
            cap=cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            messagebox.showerror("Camera unavailable",
                "Could not open this camera. Select a different index and check Windows camera permissions.")
            self.status_var.set("Camera could not be opened")
            return False
        cap.set(cv2.CAP_PROP_BUFFERSIZE,1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,960)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT,540)
        cap.set(cv2.CAP_PROP_FPS,self._camera_fps)
        self.capture=cap
        self.live=True
        self._motion_gate.reset()
        self._head_motion_rejected=False
        self._hidden_preview_drawn=False
        self.progress["maximum"]=self.monitor_config.frame_window
        self.progress_var.set(f"Collecting {self.monitor_config.frame_window} usable frames")
        self.status_var.set("Camera on • keep your head steady")
        self._update_pause_ui()
        if not self.camera_power.preview_enabled:
            self.render()  # Draw one static hidden-preview message, not video frames.
        self._tick()
        return True

    def start_webcam(self) -> None:
        if self.camera_paused:
            self.status_var.set("Camera access paused • use Resume camera")
            return
        self._stop_camera()
        self._clear_measurements()
        self._open_camera_stream()

    def _detect(self, frame: np.ndarray) -> Reading | None:
        # Limit FaceMesh inference to 640 px for lower CPU load while keeping
        # original-size landmark coordinates (MediaPipe provides normalized x/y).
        h0, w0 = frame.shape[:2]
        if w0 > 640:
            infer_frame = cv2.resize(frame, (640, max(1, int(h0 * 640 / w0))),
                                     interpolation=cv2.INTER_AREA)
        else:
            infer_frame = frame
        rgb = cv2.cvtColor(infer_frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        response = self.mesh.process(rgb)
        self.alignment_sample = None
        if not response.multi_face_landmarks:
            return None
        h, w = frame.shape[:2]
        landmarks = response.multi_face_landmarks[0].landmark
        # Same FaceMesh inference serves both independently monitored features.
        try:
            self.alignment_sample = sample_from_face_mesh(landmarks, w, h)
        except (ValueError, IndexError, AttributeError, OverflowError):
            pass
        try:
            return reading_from_face_mesh(landmarks, w, h)
        except (ValueError, IndexError, AttributeError):
            return None

    def _tick(self) -> None:
        self.after_job = None
        if not self.live or self.capture is None:
            return
        ok, img = self.capture.read()
        if not ok:
            self.status_var.set("Cannot read camera frames. Restart webcam.")
            self._stop_camera()
            return
        img = cv2.flip(img, 1)  # familiar mirror-style webcam preview
        self.frame = img if self.camera_power.preview_enabled else None
        self.base_reading = self._detect(img)
        self.reading = self.base_reading
        head_stable = self._motion_gate.accept(self.reading,time.monotonic())
        raw_eye = self.alignment_sample
        if not head_stable:
            # Suppress suspicious eye-drift candidates during fast head motion.
            self.alignment_sample = None
            if (not self._head_motion_rejected and self.alignment_engine is not None):
                # IMPORTANT: head movement is NOT a reason to recalibrate.
                # Preserve the reference and discard interrupted candidates.
                # Record a STOP for an active event so the event CSV is not left
                # with an unmatched START when the user shifts their head.
                if self.alignment_engine.active_id is not None:
                    last = self.alignment_result or AlignmentResult("STOPPED", "Head moved")
                    final = replace(last, event_id=self.alignment_engine.active_id,
                                    peak_pct=self.alignment_engine.peak,
                                    likely_eye=self.alignment_engine.active_eye,
                                    axis=self.alignment_engine.active_axis)
                    try:
                        self._append_alignment_event("STOP", final, "Head movement interrupted event")
                    except (OSError, ValueError):
                        pass
                self.alignment_engine.suspend_tracking()
                if self.alignment_popup is not None:
                    self.alignment_popup.destroy()
            self._head_motion_rejected = True
        else:
            self._head_motion_rejected = False
        if self.alignment_running:
            self._alignment_live_tick()
            if (not head_stable and self.alignment_engine is not None
                    and self.alignment_engine.baseline is not None):
                self.alignment_status_var.set(
                    "PAUSED • head movement; calibrated reference retained. Resume when steady")
        cfg = self.monitor_config
        n = cfg.frame_window
        # Hard safety gate: missing eyes / looking away cannot be bypassed by
        # the optional *quality* filters. Ignore unstable samples as usual.
        attentive = pd_monitor_visible(self.reading, raw_eye)
        accepted = (attentive and head_stable and
                    use_frame(self.reading, ignore_alignment=cfg.ignore_alignment))

        if self.monitor_running:
            if not accepted:
                self.samples.clear()
                self.monitor_estimate = None
                self.monitor_last_good_at = None
                self.monitor_unavailable_reason = (
                    "No face / both irises not tracked" if self.reading is None
                    else "Large head movement • waiting to stabilize" if not head_stable
                    else "Looking away, blinking, or face turned" if not attentive
                    else "Poor alignment rejected: " + "; ".join(self.reading.warnings)
                )
                self._display_reading(None if not head_stable else self.reading)
                self.progress["value"] = 0
                self.progress_var.set("Monitoring • " + self.monitor_unavailable_reason)
            else:
                self.samples.append(self.reading)
                self.progress["value"] = len(self.samples)
                if len(self.samples) >= n:
                    try:
                        self.monitor_estimate = median_capture(
                            self.samples, frames=n,
                            reject_unstable=not cfg.ignore_instability)
                        self.monitor_last_good_at = time.monotonic()
                        self.monitor_unavailable_reason = ""
                        self._display_reading(self.monitor_estimate)
                        self.progress_var.set(
                            f"Monitoring • rolling median of {n} frames")
                    except ValueError:
                        self.monitor_estimate = None
                        self.monitor_last_good_at = None
                        self.monitor_unavailable_reason = "Unstable rolling median rejected"
                        self._display_reading(self.reading)
                        drop = max(1, n // 3)
                        self.samples = deque(list(self.samples)[drop:], maxlen=n)
                        self.progress["value"] = len(self.samples)
                        self.progress_var.set("Monitoring • unstable median rejected; recollecting")
                else:
                    self.monitor_estimate = None
                    self.monitor_last_good_at = None
                    self.monitor_unavailable_reason = (
                        f"Collecting usable frames: {len(self.samples)}/{n}")
                    self._display_reading(self.reading)
                    self.progress_var.set("Monitoring • " + self.monitor_unavailable_reason)
        elif self.locked is None:
            if self.reading is None:
                self.samples.clear()
                self._display_reading(None)
                self.progress["value"] = 0
                self.progress_var.set("Waiting for a face in the webcam")
                self.status_var.set("No face detected • Improve lighting and face the lens")
            elif not accepted:
                self.samples.clear()
                self.progress["value"] = 0
                self.progress_var.set(
                    "Align your head to begin the " + str(n) + "-frame capture")
                self._display_reading(None if not head_stable else self.reading)
                self.status_var.set("Head moving • wait until steady" if not head_stable else
                                    "Adjust alignment or enable the alignment bypass")
            else:
                self.samples.append(self.reading)
                self._display_reading(self.reading)
                self.progress["value"] = len(self.samples)
                self.progress_var.set(f"Usable frames: {len(self.samples)} / {n}")
                if len(self.samples) == n:
                    try:
                        self.locked = median_capture(
                            self.samples, frames=n,
                            reject_unstable=not cfg.ignore_instability)
                        self._display_reading(self.locked)
                        self.progress_var.set(f"Captured • median of {n} frames")
                        self.status_var.set(
                            "Measurement locked. Click Reset / measure again to repeat.")
                    except ValueError:
                        drop = max(1, n // 3)
                        self.samples = deque(list(self.samples)[drop:], maxlen=n)
                        self.progress["value"] = len(self.samples)
                        self.status_var.set(
                            "Unstable reading • hold still or enable instability bypass")
        now = time.monotonic()
        if self.camera_power.preview_enabled and now - self._preview_last_at >= 0.15:
            self.render()
            self._preview_last_at = now
        self.after_job = self.root.after(round(1000 / self._camera_fps), self._tick)

    def _clear_measurements(self) -> None:
        self.frame = None
        self.base_reading = None
        self.alignment_sample = None
        self.reading = None
        self.locked = None
        self.samples.clear()
        self.card_edge.clear()
        self.click_mode = None
        self._display_reading(None)
        self.progress["value"] = 0

    def reset(self) -> None:
        if self.monitor_running:
            self.stop_monitor(silent=True)
        if self.live:
            self._clear_measurements()
            self.status_var.set("Measurement restarted • Look directly at the lens")
            self.progress_var.set("Waiting for a well-aligned face")
        else:
            self.card_edge.clear()
            self.click_mode = None
            self.locked = None
            self.reading = self.base_reading
            self._display_reading(self.reading)
            self.progress_var.set("Still image • Optionally mark card edges")
            self.status_var.set("Calibration cleared")
            self.render()

    def freeze_frame(self) -> None:
        if self.frame is None or not self.live:
            messagebox.showinfo("Need visible camera", "Start the webcam and select Show preview before freezing a frame.")
            return
        self._stop_camera()
        self.samples.clear()
        self.locked = None
        self.card_edge.clear()
        self.click_mode = "card"
        self._display_reading(self.reading)
        self.progress_var.set("Frozen image • Click the two ends of ONE card long edge")
        self.status_var.set("Card must be flat, at eye depth and parallel to the camera")
        self.render()

    def open_photo(self) -> None:
        file = filedialog.askopenfilename(title="Open a frontal portrait",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"),
                                                     ("All files", "*.*")])
        if not file:
            return
        try:
            with Image.open(file) as src:
                pil = ImageOps.exif_transpose(src).convert("RGB")
                pil.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                img = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
            self._stop_camera()
            self._clear_measurements()
            self.frame = img
            self.base_reading = self._detect(img)
            self.reading = self.base_reading
            self._display_reading(self.reading)
            self.progress_var.set("Still photo • Optionally mark card edges for calibration")
            self.status_var.set("Photo stays on your computer • No automatic photo saving")
            self.render()
        except Exception as exc:
            messagebox.showerror("Photo error", f"Could not read this image:\n{exc}")

    def mark_card(self) -> None:
        if self.frame is None:
            messagebox.showinfo("Need an image", "First freeze the webcam or load a photo.")
            return
        if self.live:
            messagebox.showinfo("Freeze frame first",
                                "Click Freeze for card while holding the card at eye depth.")
            return
        self.card_edge.clear()
        self.locked = None
        self.click_mode = "card"
        self.reading = self.base_reading
        self._display_reading(self.reading)
        self.status_var.set("Click both endpoints of a card's 85.60 mm LONG edge")
        self.render()

    def on_canvas_click(self, event) -> None:
        if self.frame is None or self.live or self.click_mode != "card":
            return
        off_x, off_y = self._offset
        x = (event.x - off_x) / self._scale
        y = (event.y - off_y) / self._scale
        h, w = self.frame.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return
        if len(self.card_edge) == 2:
            self.card_edge.clear()
        self.card_edge.append((x, y))
        if len(self.card_edge) == 1:
            self.status_var.set("Card point 1 set • Click the other endpoint of the same LONG edge")
        else:
            if self.base_reading is None:
                self.status_var.set("Iris landmarks were not found in this image. Try another photo.")
            else:
                try:
                    self.reading = apply_card_calibration(
                        self.base_reading, self.card_edge[0], self.card_edge[1])
                    self.locked = self.reading
                    self._display_reading(self.reading)
                    self.progress_var.set("Card-calibrated • Frozen image")
                    self.status_var.set("Card calibration complete • Click edges again to re-mark")
                except ValueError as exc:
                    self.locked = None
                    self.reading = self.base_reading
                    self.status_var.set(str(exc))
        self.render()

    def _display_reading(self, reading: Reading | None) -> None:
        display = self.locked if (self.live and self.locked is not None) else reading
        if display is None:
            self.pd_var.set("—")
            self.left_var.set("—")
            self.right_var.set("—")
            self.method_var.set("No measurement yet")
            self.quality_var.set("Position face 40–60 cm from camera with even lighting")
            self.quality_label.config(fg=GOLD)
            self.progress["value"] = 0
            return
        self.pd_var.set(f"{display.total_mm:.1f}")
        self.left_var.set(f"{display.screen_left_mm:.1f} mm")
        self.right_var.set(f"{display.screen_right_mm:.1f} mm")
        self.method_var.set(display.method)
        if display.warnings:
            self.quality_var.set(" • ".join(display.warnings))
            self.quality_label.config(fg=GOLD)
        else:
            self.quality_var.set("Head alignment looks good. Stability is not a clinical accuracy score.")
            self.quality_label.config(fg=ACCENT)

    def render(self) -> None:
        if not hasattr(self, "canvas"):
            return
        if not self.camera_power.preview_enabled and self.live:
            if self._hidden_preview_drawn:
                return
            self.canvas.delete("all")
            self.canvas.create_text(max(200,self.canvas.winfo_width())//2,
                max(200,self.canvas.winfo_height())//2,
                text="CAMERA PREVIEW HIDDEN\nTracking remains active\nUse Show preview to see video",
                fill=MUTED,font=("Segoe UI",14),justify="center")
            self._hidden_preview_drawn=True
            return
        if self.frame is None:
            self.canvas.delete("all")
            w = max(200, self.canvas.winfo_width())
            h = max(200, self.canvas.winfo_height())
            message = ("CAMERA PAUSED\nClick Resume camera" if self.camera_paused else
                       "CAMERA COOLING\nWebcam temporarily released" if self.camera_resting else
                       "Start your webcam\nor load a portrait photo")
            self.canvas.create_text(w // 2, h // 2,
                                    text=message,
                                    fill=MUTED, font=("Segoe UI", 16), justify="center")
            return
        canvas_w = max(2, self.canvas.winfo_width())
        canvas_h = max(2, self.canvas.winfo_height())
        h, w = self.frame.shape[:2]
        scale = min(canvas_w / w, canvas_h / h)
        dw = max(1, round(w * scale))
        dh = max(1, round(h * scale))
        self._scale = scale
        ox = (canvas_w - dw) // 2
        oy = (canvas_h - dh) // 2
        self._offset = (ox, oy)

        frame = self.frame.copy()
        r = self.reading or self.base_reading
        if r is not None:
            a = tuple(round(v) for v in r.left_xy)
            b = tuple(round(v) for v in r.right_xy)
            center = tuple(round(v) for v in r.bridge_xy)
            for p in (a, b):
                cv2.circle(frame, p, 9, (195, 211, 73), 2, cv2.LINE_AA)
                cv2.circle(frame, p, 3, (195, 211, 73), -1, cv2.LINE_AA)
            cv2.line(frame, a, b, (198, 140, 81), 2, cv2.LINE_AA)
            cv2.circle(frame, center, 4, (255, 195, 111), -1, cv2.LINE_AA)
            label = f"Live {r.total_mm:.1f} mm" if self.live and self.locked else f"PD {r.total_mm:.1f} mm"
            center_x = int((a[0] + b[0]) / 2)
            top = max(28, min(a[1], b[1]) - 37)
            cv2.putText(frame, label, (max(5, center_x - 90), top),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2, cv2.LINE_AA)
        # Separate alignment overlay; PD visualizations remain unchanged.
        if self.alignment_running and self.alignment_sample is not None:
            es = self.alignment_sample
            label_color = (100, 130, 255) if (
                self.alignment_result is not None and
                self.alignment_result.status == "POSSIBLE EYE DRIFT") else (112, 224, 142)
            for point, tag in ((es.left_iris_xy, "L"), (es.right_iris_xy, "R")):
                xy = tuple(round(v) for v in point)
                cv2.circle(frame, xy, 14, label_color, 2, cv2.LINE_AA)
                cv2.putText(frame, tag, (xy[0] - 7, xy[1] - 21),
                            cv2.FONT_HERSHEY_SIMPLEX, .55, label_color, 2, cv2.LINE_AA)
        if self.card_edge:
            for i, p in enumerate(self.card_edge):
                xy = (round(p[0]), round(p[1]))
                cv2.circle(frame, xy, 9, (0, 213, 255), -1, cv2.LINE_AA)
                cv2.putText(frame, str(i+1), (xy[0]+12, xy[1]+6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 213, 255), 2, cv2.LINE_AA)
            if len(self.card_edge) == 2:
                a = tuple(round(v) for v in self.card_edge[0])
                b = tuple(round(v) for v in self.card_edge[1])
                cv2.line(frame, a, b, (0, 213, 255), 2, cv2.LINE_AA)
        if self.click_mode == "card" and not self.live:
            cv2.rectangle(frame, (0, h - 38), (w, h), (16, 29, 45), -1)
            cv2.putText(frame, "CLICK TWO ENDS OF CARD LONG EDGE (85.60 mm)",
                        (12, h-13), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (255, 225, 180), 2, cv2.LINE_AA)
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(
            (dw, dh), Image.Resampling.BILINEAR)
        self._photo_image = ImageTk.PhotoImage(pil)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, image=self._photo_image, anchor="nw")

    def save_result(self) -> None:
        reading = (self.monitor_estimate if self.monitor_running else
                   (self.locked if self.locked else (None if self.live else self.reading)))
        if reading is None:
            messagebox.showinfo("No final reading",
                                f"Finish the {self.monitor_config.frame_window}-frame capture, "
                                "or load a photo / mark a card before saving.")
            return
        filename = filedialog.asksaveasfilename(
            title="Save PD values (no camera image)", defaultextension=".csv",
            initialfile=f"IrisPD_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")])
        if not filename:
            return
        if Path(filename).exists():
            messagebox.showwarning("CSV already exists", "Choose a different filename to avoid "
                                   "overwriting any previous CSV measurements.")
            return
        try:
            with open(filename, "x", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp local", "Total PD (mm)",
                                 "Screen-left (mm)", "Screen-right (mm)",
                                 "Method", "Warnings", "Note"])
                writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    f"{reading.total_mm:.2f}", f"{reading.screen_left_mm:.2f}",
                    f"{reading.screen_right_mm:.2f}", reading.method,
                    "; ".join(reading.warnings),
                    "At-home estimate; screen left/right are NOT anatomical labels. "
                    "Monocular split uses nose-bridge landmark approximation. "
                    "Seek optician confirmation when precise fitting is needed.",
                ])
            self.status_var.set("CSV saved • No photo or video was saved")
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc))

    def close(self) -> None:
        for pending in (self._display_resize_pending, self._startup_job, self._coffee_job):
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except tk.TclError:
                    pass
        self._display_resize_pending = None
        self._startup_job = None
        self._coffee_job = None
        if self.camera_pause_job is not None:
            self.root.after_cancel(self.camera_pause_job)
            self.camera_pause_job=None
        self._cancel_duty_cycle()
        self._stop_camera()
        self.frame = None
        self._photo_image = None
        self.samples.clear()
        self.monitor_estimate = None
        self.alignment_sample = None
        self.mesh.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    IrisPDApp(root)
    root.mainloop()


if __name__ == "__main__":
    if "--verify-package" in sys.argv:
        # Safe startup check for a packaged build, without opening a camera or UI.
        from pd_core import CAPTURE_FRAMES
        from coffee_policy import DEFAULT_COFFEE_URL
        assert CAPTURE_FRAMES > 0
        assert DEFAULT_COFFEE_URL.startswith("https://")
        assert Path(__file__).with_name("IrisPD_Help.pdf").is_file()
        assert Path(__file__).with_name("irispd_icon.png").is_file()
    else:
        main()
