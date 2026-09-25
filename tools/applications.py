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
    exe_base = os.path.splitext(os.path.basename(launch_path or ""))[0].lower()
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