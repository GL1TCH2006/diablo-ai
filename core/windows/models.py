"""M2.15 - typed result models for the native Windows layer.

Every operation returns a frozen dataclass with a ``to_dict``. There are no bare
tuples, no ``dict[str, Any]`` escapes and no native handles anywhere in these
types: a process identity on the wire is a PID plus a creation timestamp, never a
``PyHANDLE``. That is the boundary the capability layer enforces so the model can
never be handed an operating-system object.

Process identity is a pair, not a PID
--------------------------------------
A PID alone is not an identity. Windows reuses PIDs, so a PID read a moment ago
can name a different process by the time an action is taken. Every
:class:`ProcessInfo` therefore carries ``pid`` **and** ``created_at``, and
:mod:`core.windows.processes` re-checks the creation timestamp after any state
change it did not cause. :func:`process_identity` makes that pair explicit, and
:meth:`ProcessInfo.same_process` is the only supported way to compare two
observations.

That matters most for termination. Requesting a kill by PID and then confirming
"it is gone" by polling that PID can confirm the death of a *different* process
if the PID was recycled. The termination path checks identity, not just liveness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ProcessInfo",
    "TerminationResult",
    "SystemInfo",
    "MemoryInfo",
    "CpuInfo",
    "process_identity",
]


def _mb(value: int | float | None) -> float:
    """Bytes to megabytes, rounded, with ``None`` preserved."""
    if value is None:
        return 0.0
    return round(int(value) / (1024 * 1024), 2)


def process_identity(pid: int, created_at: str = "") -> tuple[int, str]:
    """The canonical process identity: ``(pid, created_at)``.

    Empty ``created_at`` means "identity is PID only", which is what a caller
    that could not read the creation time gets. Comparing an identified pair
    against an unidentified one is refused rather than assumed equal.
    """
    return (int(pid), str(created_at or ""))


@dataclass(frozen=True)
class ProcessInfo:
    """One observed process. Immutable, and safe to serialise."""

    pid: int
    name: str = ""
    parent_pid: int | None = None
    executable_path: str = ""
    command_line: str = ""
    owner: str = ""
    created_at: str = ""
    working_set_mb: float = 0.0
    peak_working_set_mb: float = 0.0
    private_mb: float = 0.0
    thread_count: int = 0
    handle_count: int = 0
    session_id: int = 0
    #: True when the OS refused details. The process is real; only the metadata
    #: is unavailable. Never silently blanks a field to fake completeness.
    protected: bool = False
    unavailable_fields: tuple[str, ...] = field(default=())

    @property
    def identity(self) -> tuple[int, str]:
        return process_identity(self.pid, self.created_at)

    def same_process(self, other: "ProcessInfo | None") -> bool:
        """Whether two observations describe the same process.

        Requires both creation timestamps. If either is missing the answer is
        ``False``, not ``True``: an unverifiable identity match must not be
        treated as a match, or PID reuse defeats every check built on it.
        """
        if other is None:
            return False
        if not self.created_at or not other.created_at:
            return False
        return (self.pid, self.created_at) == (other.pid, other.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "parent_pid": self.parent_pid,
            "executable_path": self.executable_path,
            "command_line": self.command_line,
            "owner": self.owner,
            "created_at": self.created_at,
            "working_set_mb": self.working_set_mb,
            "peak_working_set_mb": self.peak_working_set_mb,
            "private_mb": self.private_mb,
            "thread_count": self.thread_count,
            "handle_count": self.handle_count,
            "session_id": self.session_id,
            "protected": self.protected,
            "unavailable_fields": list(self.unavailable_fields),
        }


@dataclass(frozen=True)
class TerminationResult:
    """The outcome of a controlled termination.

    ``requested`` and ``confirmed`` are separate facts and are reported
    separately. A process whose termination was requested but which did not exit
    within the bounded wait is ``confirmed=False`` with the reason, never a
    success.
    """

    pid: int
    identity: tuple[int, str]
    requested: bool
    confirmed: bool
    exit_code: int | None = None
    waited_seconds: float = 0.0
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """True only when termination was requested *and* the exit was observed.

        Deliberately not ``confirmed`` alone: a caller that never got to request
        termination must not read as a success.
        """
        return bool(self.requested and self.confirmed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "identity_pid": self.identity[0],
            "identity_created_at": self.identity[1],
            "requested": self.requested,
            "confirmed": self.confirmed,
            "succeeded": self.succeeded,
            "exit_code": self.exit_code,
            "waited_seconds": round(self.waited_seconds, 3),
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class MemoryInfo:
    """Physical memory, from ``GlobalMemoryStatusEx``."""

    total_physical_mb: float = 0.0
    available_physical_mb: float = 0.0
    total_virtual_mb: float = 0.0
    available_virtual_mb: float = 0.0
    total_page_file_mb: float = 0.0
    available_page_file_mb: float = 0.0

    @property
    def used_physical_mb(self) -> float:
        return round(self.total_physical_mb - self.available_physical_mb, 2)

    @property
    def used_percent(self) -> float:
        if self.total_physical_mb <= 0:
            return 0.0
        return round(self.used_physical_mb / self.total_physical_mb * 100.0, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_physical_mb": self.total_physical_mb,
            "available_physical_mb": self.available_physical_mb,
            "used_physical_mb": self.used_physical_mb,
            "used_percent": self.used_percent,
            "total_virtual_mb": self.total_virtual_mb,
            "available_virtual_mb": self.available_virtual_mb,
            "total_page_file_mb": self.total_page_file_mb,
            "available_page_file_mb": self.available_page_file_mb,
        }


@dataclass(frozen=True)
class CpuInfo:
    """CPU identity and load."""

    name: str = ""
    architecture: str = ""
    logical_cores: int = 0
    physical_cores: int = 0
    processor_id: str = ""
    load_percent: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "architecture": self.architecture,
            "logical_cores": self.logical_cores,
            "physical_cores": self.physical_cores,
            "processor_id": self.processor_id,
            "load_percent": self.load_percent,
        }


@dataclass(frozen=True)
class SystemInfo:
    """A native system snapshot.

    Carries the identity of the machine it came from, so a caller can tell two
    snapshots apart and so a stale cached snapshot is detectable.
    """

    os_caption: str = ""
    os_version: str = ""
    os_build: str = ""
    os_architecture: str = ""
    host_name: str = ""
    machine_name: str = ""
    boot_time: str = ""
    cpu: CpuInfo = field(default_factory=CpuInfo)
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    observed_at: str = ""
    #: The backend that produced this. "native" or "powershell_fallback". Never
    #: claim native when the fallback ran.
    backend: str = "native"

    def to_dict(self) -> dict[str, Any]:
        return {
            "os_caption": self.os_caption,
            "os_version": self.os_version,
            "os_build": self.os_build,
            "os_architecture": self.os_architecture,
            "host_name": self.host_name,
            "machine_name": self.machine_name,
            "boot_time": self.boot_time,
            "cpu": self.cpu.to_dict(),
            "memory": self.memory.to_dict(),
            "observed_at": self.observed_at,
            "backend": self.backend,
        }
