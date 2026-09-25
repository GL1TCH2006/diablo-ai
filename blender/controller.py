from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_setup import get_logger

log = get_logger("blender")

_PROBE_PATHS = [
    Path("C:/Program Files/Blender Foundation"),
    Path("C:/Program Files (x86)/Blender Foundation"),
]

_VERSION_TIMEOUT = 20


@dataclass
class BlenderInfo:
    installed: bool
    path: str = ""
    version: str = ""
    found_by: str = ""
    errors: list[str] = field(default_factory=list)


def _find_executable() -> tuple[str, str] | None:
    for root in _PROBE_PATHS:
        if not root.is_dir():
            continue
        for entry in sorted(root.glob("Blender*"), reverse=True):
            candidate = entry / "blender.exe"
            if candidate.is_file():
                return str(candidate), entry.name
    return None


def _query_version(exe: str) -> str:
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=_VERSION_TIMEOUT, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Blender --version failed: %s", exc)
        return ""
    first = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not first:
        return ""
    match = re.search(r"Blender[ \t]+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", first[0], re.I)
    return match.group(1) if match else first[0][:60]


def detect_blender(**kwargs) -> BlenderInfo:
    found = _find_executable()
    if found is None:
        return BlenderInfo(installed=False, errors=["Blender executable not found in standard install locations."])
    exe, display = found
    version = _query_version(exe)
    info = BlenderInfo(installed=True, path=exe, version=version or "unknown", found_by=display)
    if not version:
        info.errors.append("Executable found but version could not be read.")
    return info


class BlenderController:
    """Blender integration entry point.

    Phase 1: reliable detection only. Scripting, scene management and
    rendering are Phase 4 and will be added behind this same controller.
    """

    def __init__(self, info: BlenderInfo | None = None):
        self.info = info or detect_blender()

    def describe(self) -> str:
        if not self.info.installed:
            errors = "; ".join(self.info.errors) or "not installed"
            return f"Blender: not detected ({errors})"
        return f"Blender: installed v{self.info.version} at {self.info.path}"