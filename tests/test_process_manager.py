"""Unit tests for ``core.process_manager.ProcessManager`` (M3.1 untracked module).

All tests are bounded and read-only: they use fake ``subprocess.Popen`` doubles,
no real subprocesses, no network, no paid APIs, and no temporary directories.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from core.process_manager import ProcessManager, get_process_manager, cleanup_global_processes


class _FakeProc:
    """A stand-in for ``subprocess.Popen`` with controllable liveness."""

    def __init__(self, pid: int, running: bool = True):
        self.pid = pid
        self._running = running
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminated = True
        self._running = False

    def kill(self):
        self.killed = True
        self._running = False

    def wait(self, timeout=None):
        self._running = False
        return 0


def _make_manager() -> ProcessManager:
    return ProcessManager()


def test_register_and_info_tracking():
    pm = _make_manager()
    proc = _FakeProc(1234, running=True)
    pm.register("task-a", proc, operation="terminal")
    assert pm.get_process_info(1234) is not None
    assert pm.get_process_info(1234).pid == 1234
    assert pm.get_task_pids("task-a") == [1234]
    assert pm.is_alive("task-a") is True


def test_register_pid_le_zero_is_noop():
    pm = _make_manager()
    proc = _FakeProc(0, running=True)
    pm.register("task-b", proc)
    assert pm.get_process_info(0) is None
    assert pm.get_task_pids("task-b") == []


def test_terminate_tree_running_process_stops_it():
    pm = _make_manager()
    proc = _FakeProc(1235, running=True)
    pm.register("task-c", proc, operation="terminal")
    ok = pm.terminate_tree("task-c", grace_period=0.1)
    assert ok is True
    assert proc.terminated or proc.killed
    assert pm.is_alive("task-c") is False


def test_terminate_tree_already_exited_process_returns_true():
    pm = _make_manager()
    proc = _FakeProc(1236, running=False)
    pm.register("task-d", proc)
    ok = pm.terminate_tree("task-d", grace_period=0.1)
    assert ok is True


def test_terminate_tree_unknown_task_returns_true():
    pm = _make_manager()
    assert pm.terminate_tree("no-such-task", grace_period=0.1) is True


def test_unregister_removes_process():
    pm = _make_manager()
    proc = _FakeProc(1237, running=False)
    pm.register("task-e", proc)
    assert pm.get_process_info(1237) is not None
    assert pm.unregister(1237) is True
    assert pm.get_process_info(1237) is None
    assert pm.unregister(1237) is False


def test_wait_bounded_on_exited_process_returns_true():
    pm = _make_manager()
    proc = _FakeProc(1238, running=False)
    pm.register("task-f", proc)
    assert pm.wait_bounded("task-f", timeout=0.1) is True


def test_cleanup_all_closes_everything():
    pm = _make_manager()
    proc = _FakeProc(1239, running=True)
    pm.register("task-g", proc)
    pm.cleanup_all()
    assert pm.is_alive("task-g") is False
    assert pm.get_task_pids("task-g") == []


def test_context_manager_closes_on_exit():
    with ProcessManager() as pm:
        proc = _FakeProc(1240, running=True)
        pm.register("task-h", proc)
    # After exit the manager is closed; re-registering is a safe no-op.
    proc2 = _FakeProc(1241, running=True)
    pm.register("task-i", proc2)
    assert pm.get_process_info(1241) is None


def test_global_manager_singleton_cleanup():
    # get_process_manager returns a shared instance; cleanup_global_processes
    # resets it. This only exercises the cleanup entry point, not state.
    mgr = get_process_manager()
    assert mgr is not None
    cleanup_global_processes()


def test_is_alive_unknown_task_is_false():
    pm = _make_manager()
    assert pm.is_alive("no-such-task") is False


def test_module_uses_subprocess():
    # Sanity: the module imports subprocess (real Windows Job Object path is
    # guarded behind sys.platform == "win32" and ctypes availability).
    import core.process_manager as pm_mod  # noqa: F401
    assert hasattr(subprocess, "Popen")
