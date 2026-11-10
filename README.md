# IrisPD v14 - Windows EXE build and support information

**Download contents:** Updated source, Windows launcher, PDF Help, tests, a new `Build_Windows_Standalone_EXE.bat` builder for a **single-file `dist\IrisPD.exe`**, the existing folder-based EXE builder, and a GitHub Actions workflow to build on Windows. This ZIP does not contain a precompiled Windows executable; see [`STANDALONE_WINDOWS_EXE.md`](STANDALONE_WINDOWS_EXE.md).

**Buy Me a Coffee:** The header's support button opens [https://buymeacoffee.com/nipu011](https://buymeacoffee.com/nipu011) in your browser. The banner can be shown or hidden from either monitoring settings window and disappears after 60 seconds.

**Default Reorient preset:** #2, *Monitors top | camera left | PD monitor left*. All eight layouts, F11 fullscreen, cooling, pauses, card calibration, tracking, and alerts remain available.

**Help:** `IrisPD_Help.pdf` describes the full interface and 85.60-mm card procedure, plus standalone build steps.

**Testing:** Source-unit checks and code compilation are possible here; actual Windows build, webcam, and sign-in behavior must be verified on your Windows computer or a Windows CI runner. Neither PD estimation nor the experimental eye-alignment monitor is a diagnostic medical device.

---

## Earlier v11 features (retained)


**Install:** Extract the entire ZIP, then double-click `Run_IrisPD_Windows.bat` on Windows 10/11 (64-bit, Python 3.11). To build a standalone EXE, use `Build_Windows_Standalone_EXE.bat`. See `QUICK_START.txt` and the new eight-page `IrisPD_Help.pdf`.

### New in v11

- **Optional automatic reference reorientation:** In **Eye Alignment → Settings**, enable *Auto-recalibrate after sustained head reorientation*. This is **OFF by default**. When enabled, a steady new head pose must persist for an adjustable **1–15 seconds** (default **2.5 s**) before a new calibration begins, with a **12-second cooldown** to avoid repeated recalibration. Blinks, rapid head movement, camera rest, and missing landmarks do not themselves reset the reference. A new calibration can obscure a real eye-drift episode, so use this feature cautiously.
- **Reorient layout again:** The **Reorient** dialog now offers three independent choices — **top row**, **camera side**, and **eye-monitor side** — alongside the existing eight named presets. The selected configuration persists, and FHD/2.8K/4K geometry and F11 fullscreen remain supported. Monitor height was adjusted to accommodate the optional header banner.
- **Removed Method from the PD panel:** The scale method is still tracked internally and written in measurement CSV. The Help PDF explains the difference between the **iris-based 11.7-mm assumed-iris-size scale** and the **85.60-mm card-calibrated still-image method**. Card calibration does not carry forward into live monitoring.
- **Buy Me a Coffee support button:** A shared show/hide checkbox appears in the middle of **PD Monitoring → Limits / alerts / CSV** and **Eye Alignment → Settings**. The **top-center banner** appears for **60 seconds** when enabled, with a live countdown. Clicking the button opens https://buymeacoffee.com/nipu011 in your browser; no payments occur inside IrisPD. Disabling the banner in either settings window disables it globally.
- **Pause camera frees the device:** The top-right **Pause camera** control stops active monitors, cancels duty-cycle/access callbacks, releases `cv2.VideoCapture`, and clears the cached frame so other applications can access the webcam. Press **Resume camera** to reopen it. The Windows camera driver or other apps may briefly delay access. **Hide preview** is different: it retains camera access and continues tracking.
- **New Help PDF:** A fully consolidated eight-page manual explains each control and setting, includes an illustrated, step-by-step guide to using a standard **85.60-mm long-edge ID-1 card** at eye depth, and explains alerts, methods, privacy and limitations.

### Verification

**Core/policy regression tests included**, plus simulated desktop interface and camera-lifecycle checks, including pause/reopen, layout stability, 4-second alert popups, FHD/2.8K/4K, fullscreen and the new support banner. A *physical Windows webcam and Microsoft Windows sign-in have not been tested here*; test on your own hardware before relying on the readings. The eye alignment monitor is an experimental indicator, not a medical diagnostic test.

---

# IrisPD v10 — fixed eye-alignment reference and compact PD display

**Changes from v9**

- **No automatic recalibration from head movement.** The previous head-motion handler was incorrectly calling `reset_reference()`. It now calls `suspend_tracking()`, which cancels an interrupted candidate/event but **retains the existing baseline**. A large head movement results in a `PAUSED` status, not `CALIBRATING`. Tracking resumes only when the head is positioned within the baseline-based tolerance; click **Recalibrate** if you intentionally change the camera angle, seat position or fixation target.
- **One calibration per running app session by default.** The baseline is also retained when you stop and restart eye-alignment monitoring, temporarily pause camera access, or let camera cooling cycle. A fresh app launch acquires a new reference when alignment monitoring first begins. Pressing **Recalibrate** explicitly discards the old reference and records a new one. Reference data are not persisted to disk.
- **Smaller, better-arranged PD values.** The binocular PD font is reduced from 57 to 38 point units, and monocular estimate values from 28 to 17. Screen-left and screen-right estimates are now at opposite sides of the central binocular PD value. The measurement band is compact and the previous stable panel split/layout presets are retained on FHD, 2.8K and 4K displays.
- **Safety / interpretation:** Eye drift is an unvalidated relative iris-position estimate. Preserving a reference does *not* make measurements valid during a changed head pose; readings remain paused until normal positioning returns. No automatic calibration is performed to hide this mismatch. This is not a diagnostic tool for strabismus.

The bundled **IrisPD_Help.pdf** opens with the v10 corrections and retains the complete guide to the remaining controls. Any older wording in the original guide about collecting a new reference *at every monitoring start* is superseded by the above behavior.

---

# IrisPD v9 - preserved eye calibration and display-aware stable panels

**Updates from v8**

- **Calibration stays fixed through camera cooling.** The eye-alignment reference established when the monitor starts (or when **Recalibrate** is clicked) stays in memory during every 30s ON / 15s OFF cycle. Camera rest only clears interrupted candidate/event timing; it never calls `reset_reference`. Eye movement detection resumes after 0.5 seconds of warm-up and a valid head-position check. Partially completed first-time reference collection can continue after cooling. Manual/timed camera pauses also reuse the reference **within the same running app session**. In v9, manually restarting the alignment monitor would recalibrate; v10 now retains the reference in that case too. Nothing is persisted to disk, so an old reference cannot leak into a later day.
- **Fixed window splits:** Camera, measurements, and both monitor panels have geometry propagation disabled. Quality text, PD status, and alignment alerts never control their sizes. Only an actual outer-window resize, maximize, F11, or a saved orientation change recalculates the arrangement.
- **FHD, 2.8K and 4K display fitting:** The app computes deterministic row and column ratios from available logical pixels. It keeps the camera/PD region large, assigns sufficient height to the monitoring controls and modestly scales fonts on large native-pixel desktops. Windows Display Scale/DPI settings remain respected. **F11** toggles a full-display workspace; **Esc** exits; standard Windows maximize is supported.
- **Eight existing reorientation modes** remain saved. The new Reorient dialog also has a **Full display (F11)** button.
- **Resource behavior** is unchanged: processing at up to 10 FPS, bounded sample buffers, and physically released webcam between cooling intervals. Cooling gaps cannot monitor eye drift. All settings, CSV logs, pause controls, alerts and user-customized heading are preserved.

The included `IrisPD_Help.pdf` is updated for v9. This is not a validated strabismus detector or clinical PD measurement device.

---

# IrisPD v8 - camera pause, timed suspension, power-saving cycle, motion rejection and hidden preview

**New camera controls (top-right):** Pause camera, Pause for... (0.01-168 hours), Camera power, and Hide/Show preview.

- **Pause camera** physically closes the webcam and suspends both monitors. Resume camera reopens it and restores the monitor(s) that were running before pause. A manual pause persists across restarts; a timed pause resumes when due if the app is running, or when it is next launched.
- **Camera power** enables/disables a configurable hardware on/off cycle during monitoring. Default **30 s ON / 15 s OFF**. The webcam is actually released during the OFF interval. Choose 10-3600 s ON and 5-3600 s OFF. During cooling, no eye or PD readings or alerts can occur, and PD checks can be logged as `NO_READING`. **v9 correction:** eye alignment keeps its initial reference through camera rest and resumes without a new calibration.
- **Motion gate** rejects substantial head translation, apparent zoom or rotation, with an adjustable **1-second** settling period (0.3-5 seconds).
- **Hide preview** disables drawing live video and discards the frame buffer, but tracking, webcam access, and CSV logging continue. Use Pause camera to release camera hardware. Enable the preview again before freezing frames for card calibration.

**Tradeoff:** The camera cannot observe brief eye-wandering episodes during cooling/pauses. Disable hardware cycling under Camera power if uninterrupted eye-position observation matters more than reducing sensor use. Hardware duty cycling lowers continuous use but is not a guarantee that your camera will run cooler. The app and its timers must be open/running to resume at exactly the chosen time; otherwise it resumes after reopening.

All v7 features (fixed warning box, 8 rearrangeable layouts, movable four-second alert popups) plus existing PD/eye tracking, CSV and Windows startup settings remain. The included `IrisPD_Help.pdf` explains every control. Camera processing is local-only, without online upload. **This remains an unvalidated research-style tracking application, not a medical device or strabismus diagnostic instrument.**

---

# IrisPD v7 - fixed warning panel, eight layouts, movable timed alerts

## Changes from v6

- **Fixed-size quality warnings:** The **ALIGNMENT / QUALITY** panel is now a 100-pixel-high, scrollable, read-only text box. Long or short warning messages no longer alter the measurement panel height or displace the camera preview. Other dynamic status labels have fixed line heights. Repeated identical warnings do not trigger unnecessary text-widget updates.
- **Eight reorientation presets:** Click **Reorient layout** in the header. Choose whether the monitors or measurements go on top, whether the camera is on the left or right of the PD reading, and whether PD monitoring or eye alignment appears on the left. Apply the layout without stopping the webcam; the selected orientation persists across launches. Your previous v6 layout preference migrates automatically.
- **Movable 4-second popups:** PD-threshold and experimental eye-alignment alert windows are nonmodal. They **dismiss automatically 4 seconds after appearing**; click **Dismiss now** to close early. Move each alert by the Windows title bar or the **Drag here to move** handle within the popup. The 4-second timeout is not applied to settings dialogs, errors, or confirmation dialogs, which may require user action.
- **Help PDF updated:** The bundled `IrisPD_Help.pdf` now describes layout presets, quality box scrolling, and alert movement/auto-dismissal.

The application's **Desktop pupil / iris tracking** heading, continuous PD monitor, eye-alignment monitor, missing-face alarm suppression, quality-check bypasses, append-only CSV logs, auto-start options and lower-load camera pipeline remain.

# IrisPD v6 - low-overhead monitoring, safer PD alerts, rearrangeable interface

## Changes from v5

- **No PD alarm when the face or either iris is unavailable, the eyes are nearly closed, the face is substantially turned, or both pupils clearly look to one side.** These checks cannot be bypassed by the existing alignment/instability quality-bypass switches. The monitor records `NO_READING` with no threshold alert and starts a new confirmation sequence when tracking resumes. These are heuristic gates, not calibrated gaze detection; subtle looking-away behavior can still be missed.
- **Lower processing load:** requests a 960 x 540 camera stream at 10 FPS, runs FaceMesh inference on an image resized to at most 640 pixels wide, and updates the preview approximately 6-7 times per second. Actual camera driver FPS and CPU/RAM use depend on the PC. The camera is released when the last active monitor stops, and app exit clears preview buffers and closes MediaPipe. There is no guarantee of zero RAM use while the app remains open or after Python runtime allocations.
- **Reorient layout:** header button flips between camera+PD+monocular estimates at the top, monitors below, and the two monitors at the top, camera+PD+monocular estimates below. Layout preference survives restart.
- **Help PDF:** `IrisPD_Help.pdf` documents every GUI control and settings category. Open it from the new **Help PDF** header button. It is bundled into the PyInstaller build as well.
- Existing per-user Windows startup, PD CSV, eye-alignment CSV, alerts, threshold confirmation, 1-500-frame capture, and your **Desktop pupil / iris tracking** heading are preserved.

# IrisPD v5 — Pupil distance + experimental eye-alignment monitor

**IrisPD v5** preserves the previous PD measuring modes and two optional startup behaviors: **launch automatically at Windows sign-in** and **start monitoring automatically when IrisPD opens, using the last saved settings**. Both switches default to OFF and operate independently. The existing 1–500-frame window, quality-check bypasses, limits, alerts, and append-only CSV logging remain intact.

The app runs locally on Windows 10/11 (64-bit) with Python 3.11, OpenCV, MediaPipe FaceMesh and Pillow. Live camera frames are processed in memory. The program does not upload images or readings, and does not record photos or video.


## NEW in v5: Independent eye-alignment monitor

The **EYE ALIGNMENT • EXPERIMENTAL** panel on the right is a second, independent
monitor. Your customized application heading **"Desktop pupil / iris tracking"**
was preserved. Start it with **Start alignment**, keep your head still, and fixate
one stationary target near the webcam during reference calibration. The monitor
tracks **both eyes** on every processed webcam frame, even when PD monitoring is
stopped. PD monitoring can run at the same time, using the same webcam.

This new monitor uses the two refined iris centers (MediaPipe FaceMesh landmarks
468 and 473), eye-corner landmarks, eyelid-opening estimates, and head-position
proxies. It calculates horizontal and vertical iris-center changes relative to
**each eye's own width**, removing the shared eye movement (common mode) before
scoring a left–right difference. Output is in **percent of eye width**, not mm
of PD, gaze angle, or prism diopters. Eye labels refer to the mirrored preview:
**anatomical left appears screen-left** and **anatomical right appears screen-right**.
Sometimes the eye responsible for the difference cannot be distinguished:
the interface explicitly reports **"Either eye / uncertain"**.

An initial reference is collected at the first start within a running app (**45 frames by default**, adjustable
10–300). For credible measurements, calibrate while both eyes appear aligned,
with the face oriented toward the lens, good lighting, and a fixed gaze target.
The **Recalibrate** button re-establishes the reference after you move your chair,
camera, or fixation target. Calibration can fail until the reference becomes stable.
**No previous session's baseline is reused automatically** because posture,
distance, and camera placement change.

A **candidate** event is shown when the change in relative eye position passes
its adjustable threshold (default **12% of eye width**). Only a persistent change
(default **0.65 seconds AND at least three tracked frames**) becomes a
**POSSIBLE EYE DRIFT** event. It resets after the positions recover toward the
reference for the chosen time (default 0.60 seconds). Loss of face/landmarks,
blinks, large head rotation, or detectable head-position changes pause detection
instead of reporting an event. The default threshold and timing are experimental,
not clinically established cutoffs. A large excursion can be either eye, and a
single-camera view cannot reliably identify the deviating eye or prove fixation.

Open the alignment panel's **Settings** to change the reference window, position
threshold, confirmation/recovery times, head-movement tolerance, CSV sampling
frequency, and independent visual / sound / popup / spoken alerts. You can
independently enable **Auto-start alignment monitoring when IrisPD opens**.
The original Windows per-user auto-launch checkbox in **Limits / alerts / CSV**
is unchanged: enable both to auto-start the alignment monitor at sign-in.
The original PD auto-start option also remains independent.

**Separate append-only alignment CSV outputs**, in the configured local folder:

- `EyeAlignment_events.csv`: timestamps, session/event IDs, START/END/STOP/RESET
  transitions, candidate eye/axis, measured differential position and peak.
- `EyeAlignment_samples.csv`: optional periodic (default one-second) left/right
  normalized horizontal/vertical positions, differential change, detection status,
  and quality notes. Enable it in alignment settings if you want a time series.

PD logs remain in the original `PD_measurements.csv` and are not overwritten.
Neither monitor saves photos or videos, and nothing is uploaded.

**Crucial interpretation:** This is a research-style, unvalidated indication
of relative iris-position changes, **not a strabismus detector or diagnostic
measurement**. The estimated side can be wrong; an eye may drift with little
measurable inter-eye difference; false alerts can arise from changing gaze,
lighting, head pose, or face landmarks. For intermittent eye drifting, consider
clinical examination with cover testing by an ophthalmologist (ideally one
who evaluates adult strabismus). Sudden new double vision, especially with other
neurologic symptoms, warrants urgent assessment.

## Installation and upgrade

1. Install 64-bit Python 3.11 with the `py` launcher if it isn't installed already.
2. **Extract the entire `IrisPD_Windows_v5.zip`** into a new permanent folder, separate from the older version. Avoid moving the folder later if you enable Windows startup.
3. Double-click **`Run_IrisPD_Windows.bat`**. The first launch installs local packages; later launches reuse the environment.
4. Click **Limits / alerts / CSV** to choose the frame count, quality filters, alerts, logging, and **Windows startup and automatic monitoring** settings. Click **Save settings**.
5. Click **Start webcam** for the one-off capture, or **Start monitoring** for indefinite checks every 3 seconds. Use **Stop monitoring** when finished.

Settings saved by v2/v3 under `%APPDATA%\IrisPD\monitor_settings.json` remain compatible. Your last camera selection (index 0–3) is now saved too. New startup switches both default to OFF. The CSV layout remains compatible with v2/v3 automatic-logging CSV files; rows are appended instead of overwritten.

## New v4: Windows startup and automatic monitoring

In **Limits / alerts / CSV**, scroll down to **Windows startup and automatic monitoring**:

| Setting | Default | What it does |
| --- | --- | --- |
| **Automatically launch IrisPD when I sign in to Windows** | OFF | Adds/removes the **IrisPD** entry in `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Starts for the currently signed-in Windows user (not at computer power-on), with **no administrator rights needed**. |
| **Automatically start monitoring whenever IrisPD opens (last settings)** | OFF | Opens the previously selected camera and starts continuous monitoring about 1.5 seconds after the application starts, whether launched manually or via Windows sign-in. Reuses saved frame count, filters, interval, thresholds, alerts, and append-CSV selection/path. |

To start monitoring when you log in, **enable both options and click Save settings**. If only the first option is enabled, the app opens at sign-in but monitoring remains stopped. If only the second is enabled, monitoring starts each time you manually open IrisPD (but the app does not launch on Windows sign-in).

**Important:** Keep the extracted app folder and its `.venv` in the same location after registering Windows startup. The sign-in entry points to the current app executable (when built as EXE) or to the installed Python environment and `pd_app.py` (when run from source). For a different folder or EXE installation, open *that* copy of IrisPD and click **Save settings** again so the startup entry points to it. Uncheck Windows auto-launch and save to remove the entry. The entry is per-Windows-user, not system-wide. Windows may also disable startup apps in its own settings or by administrator policy.

The last selected **Camera index** is stored whenever you change it in the main window. At sign-in the webcam must be connected and authorized; if it is unavailable, the app shows an error instead of silently pretending to monitor. The app opens a normal window (not a hidden background service). Automatic CSV logging happens only if that option was previously enabled. Closing IrisPD stops monitoring; it will start again at a later launch if the auto-start option remains checked.

## Preserved v3 options

Open **Limits / alerts / CSV**. The settings window is scrollable, with Save/Cancel buttons fixed at the bottom.

| Setting | Default | Description |
| --- | --- | --- |
| **Frames per reading (1–500)** | 30 | Number of recent processed webcam frames used for the rolling median. Applies to both one-off webcam captures and continuous monitoring. |
| **Ignore poor alignment — use flagged webcam frames** | OFF | When checked, bypasses the rejection of face/iris geometry warnings (head tilt, side angle, limited iris detail, unusual geometry). Warnings remain visible and are kept in CSV notes. |
| **Ignore unstable rolling median — allow jittery results** | OFF | When checked, uses the median even when the typical spread or range fails the stability rule. This is **flagged as unstable** in the UI and CSV notes. |

The two bypasses are **independent**: you can activate either, neither, or both.

**No face detected cannot be bypassed**: without tracked iris landmarks, PD cannot be calculated. When alignment rejection is enabled and a frame fails, the current capture window is cleared. When the instability filter is enabled and a full rolling window fails, part of that window is discarded and additional frames are collected. Turning off those filters allows quicker but potentially less reliable measurements.

With a window of **1 frame**, the app can immediately measure from one usable webcam frame, but a single frame cannot establish temporal stability; jitter filtering is therefore not meaningful at that setting. Larger windows average more observations, but must collect that many usable frames before an estimate is available. The selected frame count also updates the progress bar and display messages.

## Preserved continuous monitoring and alerts

By default a check occurs **every 3 seconds**, indefinitely until you press **Stop monitoring**. The current rolling median is sampled at each check. Three consecutive valid HIGH (>70 mm) or LOW (<60 mm) checks in the same direction trigger an alert; thresholds and confirmation count are editable. Exactly 60.0 and 70.0 mm are in range under the default limits. The system can optionally repeat its alert every additional group of three out-of-range checks.

Alert types are independently selectable: **colored status warning, Windows system sound, always-on-top nonblocking popup, and Windows spoken warning**. Missing-face observations and other rejected/unavailable readings produce `NO_READING` checks, never fake zero readings or threshold alarms, and interrupt the confirmation sequence. When a bypass is enabled, flagged estimates can contribute to threshold alerts; interpret these with extra caution.

### Automatic CSV logging

In **Limits / alerts / CSV**, check **Append each scheduled check to CSV as it measures** and choose a path (suggested default: `Documents\IrisPD\PD_measurements.csv`). Every check—including HIGH, LOW, IN_RANGE, or NO_READING—is appended and flushed immediately. The file stores timestamps, session ID, reading/set/check numbers, PD and monocular estimates, method, threshold status, confirmation/notification flags, limits, and **quality / detection notes**. Accepted unstable or poorly aligned readings are explicitly flagged in the notes. No camera frames are saved.

Existing IrisPD v2/v3 CSV files with the same header are appended, with no duplicate header. An unrelated/incompatible CSV file is refused rather than overwritten. File-writing errors stop monitoring and report a problem. The separate **Save result CSV** button exports one reading to a new file and refuses to overwrite old files.

## Other preserved functions

- **Start webcam**: single webcam capture using your selected 1–500-frame window, then freezes the calculated median until reset.
- **Open photo**: measure one image using detected iris centers.
- **Freeze for card / Mark card edges**: optionally calibrate a *frozen* image against the long edge of an ID-1 card (85.60 mm), held at eye depth; calibration is not automatically carried into moving live frames.
- **Build_Windows_EXE.bat**: on Windows with Python 3.11, builds `dist\IrisPD\IrisPD.exe` using PyInstaller. The **whole** `dist\IrisPD` folder must accompany the executable. The ZIP ships Python source and launch/build scripts, **not** a precompiled EXE.

## Testing

Run `python -m pip install pytest` then `python -m pytest -q` from the extracted folder to run unit tests. Geometry, adjustable frame windows (1 and 500 included), quality bypasses, alert state transitions, append-only logging, startup command quoting, per-user registry toggling (mocked), and settings compatibility are covered. Tk settings were opened under a simulated display, and shared-camera simulation verified
that simultaneous PD and alignment monitoring work together. A total of 42 unit tests, plus simulated alignment, combined-camera and layout smoke tests, passed for v6; **live Windows webcam or sign-in launch has not been tested here**. Registry behavior is verified with a fake Windows registry and the startup controls/auto-monitor event with a simulated Tk window.

## Accuracy and use limitations

This is an **unvalidated camera-based estimate, not a medical device**. It cannot diagnose unusual pupil distance or detect disease. Normal interpupillary distance does not change rapidly from one webcam frame to the next; apparent changes usually reflect gaze, head pose, perspective, iris assumptions, tracking jitter, or calibration. The app estimates scale from a population-average iris diameter of **11.7 mm** unless calibrated manually on a still image. Screen-left/right monocular values are approximations, not anatomical labels. Bypassing either quality filter, especially with a 1-frame window, increases the risk of misleading readings and false threshold alarms. The 60–70 mm defaults are user-chosen demonstration limits, not clinical safety boundaries. Confirm prescription/fitting measurements with an optician.

Inspired by [PDMeasure](https://pdmeasure.com/); this is an independent implementation and is not affiliated with it.
