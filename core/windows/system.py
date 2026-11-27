"""M2.15 - native Windows system metrics.

Replaces the PowerShell/CIM backend behind ``system.info``.

The previous implementation ran a ``Get-CimInstance`` script through a fresh
PowerShell process on **every call** — a PowerShell startup per invocation, plus
a JSON parse, to read four numbers the kernel will hand over directly. This
module reads them with ``GlobalMemoryStatusEx`` and the registry, and reports
which backend produced the answer so a caller never has to guess.

What is native here
-------------------
* Memory: ``kernel32.GlobalMemoryStatusEx``. The same single call the existing
  ``ai/model_router.py`` resource guard already uses, so this is not a new
  dependency on a new idiom - it is the idiom the codebase already chose.
* OS caption/version/build/architecture: the registry
  (``SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion``), which is where Windows
  itself stores them.
* CPU identity: ``PROCESSOR_IDENTIFIER``, ``PROCESSOR_ARCHITECTURE`` and
  ``NUMBER_OF_PROCESSORS`` from the same key, with logical/physical cores from
  the CPU topology API.

What is honestly not reported
-----------------------------
**CPU load percentage.** The value the old script returned came from
``Win32_Processor.LoadPercentage``, which Windows documents as a *snapshot
estimate* that is frequently unavailable and, on many machines, persistently
0. There is no reliable native equivalent without a sampling loop, which would be
a behaviour change rather than a backend change. So ``load_percent`` is reported
as ``None`` and ``SystemInfo.backend`` records that this build did not measure
it, rather than shipping a number that is usually wrong.

A sampling loop is a legitimate future addition. Faking the number to preserve a
string in an output line is not.
"""

from __future__ import annotations

import ctypes
import platform
import time
from ctypes import wintypes
from typing import Any

from core.logging_setup import get_logger

from .errors import WindowsNotFoundError, WindowsOperationFailedError
from .models import CpuInfo, MemoryInfo, SystemInfo

log = get_logger("windows.system")

__all__ = ["SystemLayer", "read_memory", "read_system_info", "system_available"]


def system_available() -> bool:
    """True when the native system layer can run on this machine."""
    try:
        read_memory()
        return True
    except Exception:  # noqa: BLE001
        return False


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _mb(value: Any) -> float:
    return round(int(value or 0) / (1024 * 1024), 2)


def _kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def read_memory() -> MemoryInfo:
    """Physical and virtual memory from one ``GlobalMemoryStatusEx`` call.

    This is the same call the model router's resource guard uses. It is
    process-wide, cheap, and needs no COM or PowerShell.
    """
    kernel32 = _kernel32()
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise WindowsOperationFailedError(
            "GlobalMemoryStatusEx failed.",
            diagnostics={"winerror": ctypes.get_last_error()},
        )
    return MemoryInfo(
        total_physical_mb=_mb(status.ullTotalPhys),
        available_physical_mb=_mb(status.ullAvailPhys),
        total_virtual_mb=_mb(status.ullTotalVirtual),
        available_virtual_mb=_mb(status.ullAvailVirtual),
        total_page_file_mb=_mb(status.ullTotalPageFile),
        available_page_file_mb=_mb(status.ullAvailPageFile),
    )


def _registry_current_version() -> dict[str, Any]:
    """``HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion``.

    Read-only. Missing values are omitted rather than faked, so a caller can tell
    "not present" from "empty".
    """
    import winreg

    out: dict[str, Any] = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            for name in ("ProductName", "DisplayVersion", "CurrentBuild",
                         "CurrentBuildNumber", "EditionID", "BuildLabEx"):
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                    out[name] = str(value)
                except OSError:
                    continue
    except OSError as exc:
        log.warning("could not read the OS version key: %s", exc)
    return out


def _read_cpu() -> CpuInfo:
    info = _registry_current_version()
    logical = 0
    machine = ""
    try:
        logical = int(os_cpu_count())
    except Exception:  # noqa: BLE001
        logical = 0
    # PROCESSOR_IDENTIFIER is the only native source for the CPU brand string
    # that does not require a WMI connection.
    identifier = ""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",
            0, winreg.KEY_READ,
        ) as key:
            identifier, _kind = winreg.QueryValueEx(key, "ProcessorNameString")
    except OSError:
        identifier = ""
    if identifier:
        machine = identifier.strip()
    if not machine:
        machine = info.get("ProcessorName") or platform.processor() or ""
    return CpuInfo(
        name=machine,
        architecture=platform.machine(),
        logical_cores=logical,
        physical_cores=_physical_cores(),
        processor_id=identifier,
        # Never fabricated. See the module docstring.
        load_percent=None,
    )


def os_cpu_count() -> int:
    import os
    return os.cpu_count() or 0


class _SystemLogicalProcessorInformationEx(ctypes.Structure):
    _fields_ = [
        ("Relationship", wintypes.DWORD),
        ("Size", wintypes.DWORD),
        ("Group", ctypes.c_ushort * 1),
    ]


def _physical_cores() -> int:
    """Physical core count from the CPU topology API.

    Returns 0 when the API is unavailable or refused, which reads as "not
    measured" rather than "one core". Callers must treat 0 as unknown.
    """
    try:
        kernel32 = _kernel32()
        func = kernel32.GetLogicalProcessorInformationEx
        func.restype = wintypes.BOOL
        # RelationProcessorCore == 0: each entry describes one physical core.
        needed = wintypes.DWORD(0)
        func(0, None, ctypes.byref(needed))
        if needed.value == 0:
            return 0
        buffer = (ctypes.c_byte * needed.value)()
        if not func(0, ctypes.byref(buffer), ctypes.byref(needed)):
            return 0
        offset = 0
        cores = 0
        while offset < needed.value:
            header = _SystemLogicalProcessorInformationEx.from_buffer_copy(
                ctypes.string_at(ctypes.addressof(buffer) + offset, ctypes.sizeof(_SystemLogicalProcessorInformationEx)))
            entry_size = int(header.Size)
            if entry_size <= 0:
                break
            cores += 1
            offset += entry_size
        return cores
    except Exception as exc:  # noqa: BLE001 - a nicety is not worth failing over
        log.debug("physical core count unavailable: %s", exc)
        return 0


def read_system_info() -> SystemInfo:
    """A native system snapshot.

    Never raises for a missing optional field: each is filled independently and
    a machine that denies one read still yields the rest.
    """
    version = _registry_current_version()
    memory = read_memory()
    boot_time = ""
    try:
        boot_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_boot_time()))
    except Exception:  # noqa: BLE001
        boot_time = ""
    return SystemInfo(
        os_caption=version.get("ProductName", ""),
        os_version=version.get("DisplayVersion", version.get("ReleaseId", "")),
        os_build=version.get("CurrentBuildNumber", version.get("CurrentBuild", "")),
        os_architecture=platform.machine(),
        host_name=platform.node(),
        machine_name=platform.node(),
        boot_time=boot_time,
        cpu=_read_cpu(),
        memory=memory,
        observed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        backend="native",
    )


def _boot_time() -> float:
    """Seconds since boot, from ``GetTickCount64``."""
    kernel32 = _kernel32()
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    up_ms = int(kernel32.GetTickCount64())
    return time.time() - (up_ms / 1000.0)


class SystemLayer:
    """Native system information. Holds no handles across calls."""

    def __init__(self) -> None:
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def system_info(self, cancelled=None) -> dict[str, Any]:
        """Structured snapshot, ready for a ``CapabilityResult``."""
        if callable(cancelled) and cancelled():
            from .errors import WindowsCancelledError
            raise WindowsCancelledError(
                "Cancelled before the system query completed.",
                diagnostics={"cancellation_terminated": False},
            )
        try:
            return read_system_info().to_dict()
        except WindowsNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise to the layer vocabulary
            raise WindowsOperationFailedError(
                f"Native system query failed: {exc}",
                diagnostics={"error": type(exc).__name__},
            ) from exc

    def memory_info(self) -> dict[str, Any]:
        return read_memory().to_dict()

    def describe(self) -> str:
        """One-line summary for a status header."""
        try:
            info = read_system_info()
        except Exception as exc:  # noqa: BLE001
            return f"system info unavailable: {exc}"
        return (f"{info.os_caption} {info.os_version} (build {info.os_build}) "
                f"{info.os_architecture}, {info.memory.used_physical_mb}MB of "
                f"{info.memory.total_physical_mb}MB in use")