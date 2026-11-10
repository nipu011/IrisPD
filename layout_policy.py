"""Deterministic responsive panel splits for IrisPD; no GUI imports.

Only window/client-area dimensions may affect split dimensions. Warning text,
measurements and status updates are intentionally absent from this function.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSplit:
    monitor_height: int
    camera_fraction: float
    pd_monitor_fraction: float


def panel_split_for_size(client_width: int, client_height: int) -> PanelSplit:
    """Stable two-row layout for FHD, 2.8K, 4K and intermediate window sizes.

    All sizes are in Tk display units (Windows may apply its own DPI scaling).
    The 1.05K x 760 minimum app window keeps monitoring controls available.
    """
    w, h = max(1, int(client_width)), max(1, int(client_height))
    # Keep more vertical space for the camera at large native resolutions,
    # while allocating enough height to both monitors' scaled controls.
    fraction = .305 if h < 1080 else .285 if h < 1770 else .255
    monitor_height = max(215, round(h * fraction))
    # V11 adds a short optional support banner above the workspace. When there
    # is enough vertical room, reserve extra height for the alignment controls
    # rather than letting its bottom buttons collapse on FHD screens.
    if h >= 650:
        monitor_height = max(280, monitor_height)
    # Never consume the camera/PD row when a user manually shrinks the window.
    monitor_height = min(monitor_height, max(215, h - 350))
    # Camera preview gets enough width for a useful image at FHD. On wider
    # 2.8K/4K displays, allocate additional space to numeric PD readouts.
    camera_fraction = .59 if w < 1550 else .57 if w < 2450 else .55
    pd_monitor_fraction = .54 if w < 1550 else .52 if w < 2450 else .50
    return PanelSplit(monitor_height, camera_fraction, pd_monitor_fraction)
