"""M3.1 — Unified subprocess lifecycle management with Windows Job Objects."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("process_manager")


@dataclass
class ProcessInfo:
    """Information about a tracked subprocess."""
    pid: int
    proc: subprocess.Popen
    task_id: str
    operation: str
    start_time: float
    job_handle: Any = None  # Windows Job Object handle
    creationflags: int = 0


class ProcessManager:
    """Unified subprocess lifecycle management with Windows Job Objects.

    Tracks all child processes by task_id, provides bounded termination with
    escalation (terminate -> kill), and uses Windows Job Objects for reliable
    process tree termination on Windows.
    """

    def __init__(self):
        self._processes: dict[int, ProcessInfo] = {}
        self._task_processes: dict[str, list[int]] = defaultdict(list)
        self._lock = threading.RLock()
        self._closed = False

        # Windows Job Object API
        self._job_objects: dict[int, int] = {}  # task_id -> job_handle
        self._win32_available = self._check_win32()

    def _check_win32(self) -> bool:
        """Check if Windows Job Object API is available."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True)
            return True
        except Exception:
            return False

    def _create_job_object(self) -> int | None:
        """Create a Windows Job Object for process tree management."""
        if not self._win32_available:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", ctypes.c_ulonglong * 3),  # simplified
                    ("IoInfo", ctypes.c_ulonglong * 4),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation[0] = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

            # SetInformationJobObject
            kernel32.SetInformationJobObject(
                job,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info)
            )
            return job
        except Exception as exc:
            log.debug("Failed to create Job Object: %s", exc)
            return None

    def _assign_process_to_job(self, job_handle: int, pid: int) -> bool:
        """Assign a process to a Job Object."""
        if not self._win32_available or job_handle == 0:
            return False
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h_process = kernel32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS
            if not h_process:
                return False
            try:
                result = kernel32.AssignProcessToJobObject(job_handle, h_process)
                return bool(result)
            finally:
                kernel32.CloseHandle(h_process)
        except Exception as exc:
            log.debug("Failed to assign process %d to job: %s", pid, exc)
            return False

    def register(
        self,
        task_id: str,
        proc: subprocess.Popen,
        operation: str = "",
        creationflags: int = 0,
    ) -> None:
        """Register a subprocess for tracking and managed cleanup."""
        if self._closed:
            log.warning("ProcessManager closed, refusing to register new process")
            return

        pid = proc.pid
        if pid <= 0:
            return

        with self._lock:
            # Create Job Object for this task if on Windows
            job_handle = 0
            if sys.platform == "win32" and self._win32_available:
                job_handle = self._create_job_object() or 0
                if job_handle:
                    self._assign_process_to_job(job_handle, proc.pid)

            info = ProcessInfo(
                pid=pid,
                proc=proc,
                task_id=task_id,
                operation=operation,
                start_time=time.monotonic(),
                job_handle=job_handle,
                creationflags=creationflags,
            )

            self._processes[pid] = info
            self._task_processes[task_id].append(pid)
            if job_handle:
                self._job_objects[task_id] = job_handle

            log.debug("Registered process pid=%d for task_id=%s operation=%s", pid, task_id, operation)

    def unregister(self, pid: int) -> bool:
        """Unregister a process (e.g., after it exits cleanly)."""
        with self._lock:
            info = self._processes.pop(pid, None)
            if not info:
                return False
            self._task_processes[info.task_id].remove(pid)
            if not self._task_processes[info.task_id]:
                del self._task_processes[info.task_id]
            if info.task_id in self._job_objects:
                del self._job_objects[info.task_id]
            return True

    def terminate_tree(self, task_id: str, grace_period: float = 5.0) -> bool:
        """Terminate all processes for a task_id, using Job Objects on Windows.

        Args:
            task_id: Task identifier
            grace_period: Seconds to wait for graceful termination before force kill

        Returns:
            True if all processes confirmed terminated, False otherwise
        """
        with self._lock:
            pids = self._task_processes.get(task_id, []).copy()

        if not pids:
            return True

        log.warning("Terminating process tree for task_id=%s (pids=%s)", task_id, pids)

        # Phase 1: Try Job Object termination (Windows) - kills entire tree atomically
        job_handle = self._job_objects.get(task_id, 0)
        if job_handle and sys.platform == "win32":
            try:
                import ctypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                result = kernel32.TerminateJobObject(job_handle, 1)  # exit code 1
                if result:
                    log.debug("Terminated job object for task_id=%s", task_id)
                    # Wait for processes to exit
                    deadline = time.monotonic() + grace_period
                    while time.monotonic() < deadline:
                        all_dead = True
                        for pid in pids:
                            info = self._processes.get(pid)
                            if info and info.proc.poll() is None:
                                all_dead = False
                                break
                        if all_dead:
                            log.info("All processes in job terminated for task_id=%s", task_id)
                            return True
                        time.sleep(0.1)
            except Exception as exc:
                log.debug("Job Object termination failed for task_id=%s: %s", task_id, exc)

        # Phase 2: Standard terminate/kill fallback per process
        return self._terminate_processes_fallback(pids, task_id, grace_period)

    def _terminate_processes_fallback(self, pids: list[int], task_id: str, grace_period: float) -> bool:
        """Fallback termination using standard terminate/kill per process."""
        deadline = time.monotonic() + grace_period
        all_dead = False

        for pid in pids:
            info = self._processes.get(pid)
            if not info or info.proc.poll() is not None:
                continue

            proc = info.proc
            log.warning("Terminating process pid=%d for %s", pid, task_id)

            # Phase 1: Graceful terminate
            try:
                proc.terminate()
            except (OSError, ProcessLookupError, AttributeError):
                pass

            # Wait for graceful termination
            try:
                proc.wait(timeout=min(2.0, grace_period / max(1, len(pids))))
                continue
            except subprocess.TimeoutExpired:
                pass

            # Phase 2: Force kill
            try:
                proc.kill()
            except (OSError, ProcessLookupError, AttributeError):
                pass

            # Wait for kill
            try:
                proc.wait(timeout=min(2.0, grace_period / max(1, len(pids))))
            except subprocess.TimeoutExpired:
                pass

        # Final reap - block until all confirmed dead or timeout
        final_deadline = time.monotonic() + 2.0
        while time.monotonic() < final_deadline:
            all_dead = True
            for pid in pids:
                info = self._processes.get(pid)
                if info and info.proc.poll() is None:
                    all_dead = False
                    break
            if all_dead:
                break
            time.sleep(0.1)

        # Verify all dead
        all_confirmed = True
        for pid in pids:
            info = self._processes.get(pid)
            if info and info.proc.poll() is None:
                all_confirmed = False
                log.error("Process pid=%d for task_id=%s survived termination", pid, task_id)

        return all_confirmed

    def wait_bounded(self, task_id: str, timeout: float) -> bool:
        """Wait for all processes in a task to complete, with timeout."""
        deadline = time.monotonic() + timeout
        pids = self._task_processes.get(task_id, [])

        while time.monotonic() < deadline:
            all_done = True
            for pid in pids:
                info = self._processes.get(pid)
                if info and info.proc.poll() is None:
                    all_done = False
                    break
            if all_done:
                return True
            time.sleep(0.1)

        # Check final state
        for pid in pids:
            info = self._processes.get(pid)
            if info and info.proc.poll() is None:
                return False
        return True

    def cleanup_all(self) -> None:
        """Terminate all tracked processes and clean up."""
        if self._closed:
            return
        self._closed = True

        with self._lock:
            all_pids = list(self._processes.keys())
            for pid in all_pids:
                info = self._processes.get(pid)
                if info and info.proc.poll() is None:
                    try:
                        info.proc.terminate()
                    except Exception:
                        pass

            # Wait briefly for graceful termination
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                alive = [p for p in all_pids if self._processes.get(p) and self._processes[p].proc.poll() is None]
                if not alive:
                    break
                time.sleep(0.1)

            # Force kill remaining
            for pid in all_pids:
                info = self._processes.get(pid)
                if info and info.proc.poll() is None:
                    try:
                        info.proc.kill()
                    except Exception:
                        pass

            # Final reap
            for pid in all_pids:
                info = self._processes.get(pid)
                if info:
                    try:
                        info.proc.wait(timeout=1.0)
                    except Exception:
                        pass

            # Clean up Job Objects
            for job_handle in self._job_objects.values():
                if job_handle:
                    try:
                        import ctypes
                        ctypes.windll.kernel32.CloseHandle(job_handle)
                    except Exception:
                        pass

            self._processes.clear()
            self._task_processes.clear()
            self._job_objects.clear()

    def get_task_pids(self, task_id: str) -> list[int]:
        """Get all PIDs associated with a task_id."""
        with self._lock:
            return self._task_processes.get(task_id, []).copy()

    def get_process_info(self, pid: int) -> Optional[ProcessInfo]:
        """Get information about a specific process."""
        with self._lock:
            return self._processes.get(pid)

    def is_alive(self, task_id: str) -> bool:
        """Check if any process for a task is still running."""
        with self._lock:
            for pid in self._task_processes.get(task_id, []):
                info = self._processes.get(pid)
                if info and info.proc.poll() is None:
                    return True
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup_all()
        return False


# Global instance for convenience
_default_manager: ProcessManager | None = None


def get_process_manager() -> ProcessManager:
    """Get the global process manager instance."""
    global _default_manager
    if _default_manager is None:
        _default_manager = ProcessManager()
    return _default_manager


def cleanup_global_processes() -> None:
    """Clean up all globally tracked processes."""
    global _default_manager
    if _default_manager is not None:
        _default_manager.cleanup_all()
        _default_manager = None


# Register cleanup at exit
import atexit
atexit.register(cleanup_global_processes)


__all__ = [
    "ProcessManager",
    "ProcessInfo",
    "get_process_manager",
    "cleanup_global_processes",
]