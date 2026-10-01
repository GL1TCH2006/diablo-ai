from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from config.settings import Settings
from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("applications")

_CONFIRM_PROCESS_EXES = ("calc", "notepad", "chrome", "msedge", "code", "explorer", "blender", "mspaint", "cmd")
_DYNAMIC_LOOKUP_TIMEOUT = 15


def open_app(app: str, settings: Settings | None = None) -> ToolResult:
    if not app or not app.strip():
        return ToolResult(False, error="No application name given.")

    exe, display, source = _resolve_app(app, settings)
    if exe is None:
        return ToolResult(False, error=f"Application not found: '{app}'. (Checked known apps and the Start Menu.)")

    launch_path = exe
    if not os.path.isabs(launch_path):
        found = shutil.which(launch_path)
        launch_path = found if found else launch_path

    try:
        os.startfile(launch_path)
    except OSError as exc:
        return ToolResult(False, error=f"Failed to launch '{display}' ({source}): {exc}")

    process_ok = _confirm_process(display, launch_path)
    detail = "process confirmed running" if process_ok else "launch issued (process not confirmable)"
    return ToolResult(True, output=f"{display} ({source}) - {detail}")


def _resolve_app(app: str, settings: Settings | None) -> tuple[str | None, str, str]:
    key = app.strip().lower()
    known = settings.app_known_paths if settings else {}
    mapped = settings.app_whitelist if settings else {}

    for alias, path in known.items():
        if key == alias.lower():
            p = Path(path)
            if p.exists():
                return str(p), app.strip(), "known path"
            return None, app.strip(), "known path (missing)"

    for alias, token in mapped.items():
        if key == alias.lower():
            resolved = shutil.which(token) or token
            return resolved, app.strip(), "whitelist"

    start_app = _start_apps_lookup(key)
    if start_app:
        returns_exe, appid = start_app
        if returns_exe and appid.lower().endswith(".exe"):
            resolved = appid if os.path.isabs(appid) else (shutil.which(appid) or appid)
            return resolved, appid, "Start Menu lookup"
        return None, appid, "Start Menu lookup (UWP app, launching not yet supported)"

    return None, app.strip(), "unknown"


def _start_apps_lookup(key: str) -> tuple[bool, str] | None:
    """Native Start Menu discovery.

    M2.15 replaced ``Get-StartApps``. That cmdlet has no Win32 equivalent, but it
    is a PowerShell *call* whose data comes from the registry and the Start Menu
    directories, both of which can be read directly - see
    :mod:`core.windows.start_menu`.

    The PowerShell path is kept only as a fallback, because a user whose Start
    Menu layout defeats the native reader should still be able to launch things.
    """
    try:
        from core.windows.start_menu import start_menu_lookup

        found = start_menu_lookup(key)
        if found:
            return found
    except Exception as exc:  # noqa: BLE001 - never block a launch on discovery
        log.debug("native start menu lookup failed for %r: %s", key, exc)
    return _start_apps_lookup_powershell(key)


def _start_apps_lookup_powershell(key: str) -> tuple[bool, str] | None:
    """The previous ``Get-StartApps`` probe, kept as the fallback path."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True,
            text=True,
            timeout=_DYNAMIC_LOOKUP_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        log.warning("Get-StartApps failed: %s", proc.stderr.strip()[:200])
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    entries = data if isinstance(data, list) else ([] if not data else [data])
    tokens = key.split()
    best = None
    for entry in entries:
        name = str(entry.get("Name", "")).lower()
        appid = str(entry.get("AppID", ""))
        if all(t in name or t in appid.lower() for t in tokens):
            return True, appid
    return best


def _confirm_process(display: str, launch_path: str) -> bool:
    """Whether the launched process is actually running.

    M2.15: uses the native Windows process layer instead of polling PowerShell
    ``Get-Process`` six times. One native enumeration answers it, in milliseconds,
    with no subprocess per poll.

    Still a *confirmation*, not a guarantee: Windows reports a successful launch
    and the process can still exit immediately, which is why this reports what it
    saw rather than claiming the app is open.
    """
    exe_base = os.path.splitext(os.path.basename(launch_path or ""))[0].lower()
    wanted = {exe_base}
    try:
        from core.windows.processes import ProcessLayer

        layer = ProcessLayer()
    except Exception as exc:  # noqa: BLE001 - fall through to the PowerShell path
        log.debug("native process layer unavailable: %s", exc)
        return _confirm_process_powershell(exe_base, wanted)

    matches = [
        p for p in layer.find_processes(name=exe_base, limit=MAX_CONFIRM_MATCHES)
        if _name_matches(p.name, exe_base) or _name_matches(p.executable_path, exe_base)
    ]
    if matches:
        return True
    # Known apps whose name does not derive from the launched path.
    return _confirm_process_powershell(exe_base, wanted)


#: Confirmations only consult these known names; see the historical list.
_CONFIRM_PROCESS_EXES = frozenset({
    "calc", "notepad", "chrome", "msedge", "code", "explorer", "blender",
    "mspaint", "cmd",
})

_DYNAMIC_LOOKUP_TIMEOUT = 15

#: Upper bound on matches inspected while confirming. Bounded so a name that
#: matches many processes cannot turn a status line into a long loop.
MAX_CONFIRM_MATCHES = 20


def _name_matches(candidate: str, wanted: str) -> bool:
    text = str(candidate or "").strip().lower()
    if not text:
        return False
    base = os.path.basename(text.replace("\\", "/"))
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base == wanted


def _confirm_process_powershell(exe_base: str, wanted: set[str]) -> bool:
    """The previous PowerShell probe, kept as the fallback path."""
    for known in _CONFIRM_PROCESS_EXES:
        if exe_base == known or exe_base.startswith(known) or known.startswith(exe_base):
            for _ in range(6):
                matched = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"try {{ (Get-Process -Name '{exe_base}' -ErrorAction Stop).count }} catch {{ 0 }}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if matched.returncode == 0 and str(matched.stdout or "").strip().startswith(("1", "2", "3", "4", "5", "6", "7", "8", "9")):
                    return True
                time.sleep(0.4)
            return False
    return False