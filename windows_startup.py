"""Per-user Windows sign-in auto-launch for IrisPD (no administrator rights).

The HKCU Run key is standard Windows functionality. We only touch our own
named value; settings do not alter the machine-wide HKLM Run registry key.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "IrisPD"


def startup_command(*, executable: Path, script: Path | None, frozen: bool) -> str:
    """Build a fully quoted Win32 command for either frozen EXE or Python source.

    Use pythonw.exe for source mode so no console window appears at sign-in.
    Script mode uses the running interpreter's environment (normally .venv).
    """
    interpreter = Path(executable).resolve()
    if frozen:
        argv = [str(interpreter)]
    else:
        if script is None:
            raise ValueError("A script path is required for the source version.")
        pythonw = interpreter.with_name("pythonw.exe")
        if pythonw.is_file():
            interpreter = pythonw
        argv = [str(interpreter), str(Path(script).resolve())]
    return subprocess.list2cmdline(argv)


def current_command() -> str:
    frozen = bool(getattr(sys, "frozen", False))
    return startup_command(
        executable=Path(sys.executable),
        script=None if frozen else Path(__file__).with_name("pd_app.py"),
        frozen=frozen,
    )


def set_auto_launch(enabled: bool, *, command: str | None = None, winreg_module=None) -> None:
    """Enable/disable IrisPD in the current user's sign-in Run key.

    Raising on failure lets the UI explain why the change couldn't be applied.
    Does not require admin privileges. Off is idempotent.
    """
    if winreg_module is None:
        if sys.platform != "win32":
            if enabled:
                raise OSError("Windows startup registration is available only on Windows.")
            return
        import winreg as winreg_module
    if enabled:
        command = command or current_command()
        with winreg_module.CreateKeyEx(
            winreg_module.HKEY_CURRENT_USER, RUN_KEY, 0, winreg_module.KEY_SET_VALUE
        ) as key:
            winreg_module.SetValueEx(key, VALUE_NAME, 0, winreg_module.REG_SZ, command)
    else:
        try:
            with winreg_module.OpenKey(
                winreg_module.HKEY_CURRENT_USER, RUN_KEY, 0, winreg_module.KEY_SET_VALUE
            ) as key:
                try:
                    winreg_module.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass


def registered_command(*, winreg_module=None) -> str | None:
    if winreg_module is None:
        if sys.platform != "win32":
            return None
        import winreg as winreg_module
    try:
        with winreg_module.OpenKey(
            winreg_module.HKEY_CURRENT_USER, RUN_KEY, 0, winreg_module.KEY_QUERY_VALUE
        ) as key:
            value, _ = winreg_module.QueryValueEx(key, VALUE_NAME)
            return value
    except FileNotFoundError:
        return None
