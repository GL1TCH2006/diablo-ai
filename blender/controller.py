from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field, replace
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


#: Cached successful detection plus the probe configuration it was produced
#: under. Guarded by ``_DETECTION_LOCK`` because the capability layer and the UI
#: can both ask for detection concurrently.
_DETECTION_CACHE: "tuple[tuple[str, ...], BlenderInfo] | None" = None
_DETECTION_LOCK = threading.Lock()


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


def _copy_info(info: BlenderInfo) -> BlenderInfo:
    """A defensive copy, including the mutable ``errors`` list.

    ``dataclasses.replace`` alone would share the list, so a caller appending to
    ``info.errors`` would corrupt the cached entry.
    """
    return replace(info, errors=list(info.errors))


def detect_blender(*, force_refresh: bool = False, **kwargs) -> BlenderInfo:
    """Detect Blender, caching a successful result.

    Detection shells out to ``blender --version``, so calling it per Blender
    operation doubled the process count for no benefit. A successful detection is
    cached and reused.

    The cache is keyed on the probe configuration, so changing where Blender is
    searched for is honoured immediately. It is also invalidated automatically
    when the cached executable stops being a file on disk (uninstalled, moved, or
    on a disconnected volume), so a stale positive can never outlive the binary
    it points at. Failed detections are deliberately *not* cached, so installing
    Blender takes effect immediately without an explicit invalidation.

    A fresh :class:`BlenderInfo` copy is returned each call so a caller cannot
    mutate the cached value.
    """
    global _DETECTION_CACHE
    if not force_refresh:
        cached = _cached_detection()
        if cached is not None:
            return _copy_info(cached)
    found = _find_executable()
    if found is None:
        _clear_detection_cache()
        return BlenderInfo(installed=False, errors=["Blender executable not found in standard install locations."])
    exe, display = found
    version = _query_version(exe)
    info = BlenderInfo(installed=True, path=exe, version=version or "unknown", found_by=display)
    if not version:
        info.errors.append("Executable found but version could not be read.")
    with _DETECTION_LOCK:
        _DETECTION_CACHE = (_probe_fingerprint(), info)
    return _copy_info(info)


def invalidate_detection_cache() -> None:
    """Drop the cached detection so the next call probes the filesystem again."""
    _clear_detection_cache()


def _probe_fingerprint() -> tuple[str, ...]:
    """Identity of the current probe configuration."""
    return tuple(str(path) for path in _PROBE_PATHS)


def _cached_detection() -> BlenderInfo | None:
    """The cached detection when it is still trustworthy, else ``None``."""
    entry = _DETECTION_CACHE
    if entry is None:
        return None
    fingerprint, info = entry
    if fingerprint != _probe_fingerprint():
        # Where we search changed; the cached answer no longer applies.
        _clear_detection_cache()
        return None
    if not info.installed or not info.path:
        return None
    if not Path(info.path).is_file():
        # The cached executable vanished; the positive is no longer valid.
        log.info("cached Blender detection invalidated: %s is gone", info.path)
        _clear_detection_cache()
        return None
    return info


def _clear_detection_cache() -> None:
    global _DETECTION_CACHE
    with _DETECTION_LOCK:
        _DETECTION_CACHE = None


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