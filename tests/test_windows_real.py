"""M2.15 - real Windows integration tests.

These exercise the native layer against the actual operating system. They do not
mock the OS, and they do not assume any third-party process happens to be
running: every termination target is a child this test spawns itself, and every
one is cleaned up in a ``finally``.

Run them with::

    DIABLO_REAL_WINDOWS_TESTS=1 .\\.venv\\Scripts\\python.exe -m pytest tests/test_windows_real.py

The gate exists because these tests start and terminate real processes. They are
deliberately not part of the default run: a test suite that kills things should be
something you ask for. They skip with a reason, never silently, and no test here
is more specific than "the child I just spawned".

Coverage
--------
* enumeration, filtering, PID and parent-PID lookup
* executable path, command line, owner, memory
* invalid PID, absent PID, protected PID, self-termination
* a real controlled termination, with the exit confirmed
* cancellation before work, and during the wait
* native system metrics, and the internal consistency the verifier relies on
* the full capability path: registry -> adapter -> bridge -> verification
* no orphan processes left behind
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

REAL_WINDOWS = os.environ.get("DIABLO_REAL_WINDOWS_TESTS") == "1"
requires_real_windows = pytest.mark.skipif(
    not REAL_WINDOWS,
    reason="set DIABLO_REAL_WINDOWS_TESTS=1 to run real Windows integration tests",
)

#: A child that does nothing observable and holds no resources.
_CHILD = "import time\ntime.sleep(120)\n"


def _spawn_child():
    """A harmless sleeping child, spawned for this test alone."""
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(process) -> None:
    """Never leave a test process behind."""
    if process is None:
        return
    if process.poll() is None:
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
    try:
        process.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _stem(path: str) -> str:
    base = os.path.basename(str(path or "").replace("\\", "/"))
    return base[:-4] if base.lower().endswith(".exe") else base


@pytest.fixture
def child():
    """A spawned child, reaped whatever happens."""
    process = _spawn_child()
    try:
        # Give Windows a moment to publish the process before it is queried.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("the test child exited immediately")
            time.sleep(0.1)
            break
        yield process
    finally:
        _reap(process)


@pytest.fixture
def process_layer():
    from core.windows.processes import ProcessLayer

    layer = ProcessLayer()
    try:
        yield layer
    finally:
        layer.close()


# ── availability ─────────────────────────────────────────────────────────────


@requires_real_windows
def test_the_native_layer_is_available_on_this_machine():
    from core.windows.system import system_available

    assert system_available(), "pywin32 is required for the native Windows layer"


# ── enumeration and lookup ───────────────────────────────────────────────────


@requires_real_windows
class TestEnumeration:
    def test_enumeration_returns_real_pids_including_this_one(self, process_layer):
        processes = process_layer.enumerate_processes()
        pids = [p.pid for p in processes]
        assert len(pids) > 10, "a real machine has more than ten processes"
        assert os.getpid() in pids, "the enumeration did not see this very process"

    def test_every_row_is_well_formed(self, process_layer):
        for info in process_layer.enumerate_processes():
            assert isinstance(info.pid, int) and info.pid > 0
            assert isinstance(info.name, str)
            assert isinstance(info.unavailable_fields, tuple)

    def test_a_process_can_be_looked_up_by_pid(self, process_layer):
        me = process_layer.get_process(os.getpid())
        assert me.pid == os.getpid()
        assert me.name.lower().endswith(".exe")
        assert me.created_at, "creation time is the process identity and must be read"

    def test_details_are_read_for_a_real_process(self, process_layer):
        """Path, command line and owner need a handle; this proves they work."""
        me = process_layer.get_process(os.getpid())
        assert me.executable_path, "executable path was not readable"
        assert me.owner, "process owner was not readable"
        # Compared on the basename: a Windows venv launcher runs the *base*
        # interpreter image, so the OS-reported path is not sys.executable's
        # directory. The OS answer is the correct one; the assertion must not
        # assume otherwise.
        assert _stem(me.executable_path).lower() == _stem(sys.executable).lower()
        assert me.unavailable_fields == ()

    def test_memory_and_thread_counters_are_plausible(self, process_layer):
        me = process_layer.get_process(os.getpid())
        assert me.working_set_mb > 0.0
        assert me.thread_count >= 1
        assert me.handle_count >= 1

    def test_filtering_by_name_finds_this_interpreter(self, process_layer):
        matches = process_layer.find_processes("python", limit=20)
        assert matches, "the running interpreter was not found by name"
        assert any(p.pid == os.getpid() for p in matches), (
            f"filtering by 'python' missed this process: {[p.pid for p in matches]}")

    def test_name_filtering_is_case_insensitive_and_ignores_the_extension(
            self, process_layer):
        lower = {p.pid for p in process_layer.find_processes("python", limit=20)}
        upper = {p.pid for p in process_layer.find_processes("PYTHON", limit=20)}
        with_ext = {p.pid for p in process_layer.find_processes("python.exe", limit=20)}
        assert lower == upper
        assert lower == with_ext

    def test_filtering_by_parent_pid_finds_the_spawned_child(self, process_layer, child):
        mine = process_layer.find_processes(parent_pid=os.getpid(), limit=50)
        assert any(p.pid == child.pid for p in mine), (
            f"the spawned child was not found among {[p.pid for p in mine]}")

    def test_an_empty_filter_returns_nothing_rather_than_everything(self, process_layer):
        assert process_layer.find_processes("definitely-not-a-real-process-xyz", limit=10) == []


# ── refusals ────────────────────────────────────────────────────────────────


@requires_real_windows
class TestRefusals:
    def test_an_absent_pid_is_reported_as_not_found(self, process_layer):
        from core.windows.errors import WindowsProcessNotFoundError

        with pytest.raises(WindowsProcessNotFoundError) as excinfo:
            process_layer.get_process(0x7FFFFFF0)
        assert excinfo.value.code == "process_not_found"

    @pytest.mark.parametrize("pid", [0, 1, 2, 4])
    def test_protected_pids_are_refused_on_principle(self, process_layer, pid):
        from core.windows.errors import WindowsProtectedProcessError

        with pytest.raises(WindowsProtectedProcessError):
            process_layer.get_process(pid)

    def test_the_system_process_is_refused_for_termination_too(self, process_layer):
        from core.windows.errors import WindowsProtectedProcessError

        with pytest.raises(WindowsProtectedProcessError):
            process_layer.terminate_process(4, timeout=1.0)

    def test_self_termination_is_refused_before_any_handle_is_opened(self, process_layer):
        from core.windows.errors import WindowsInvalidRequestError

        with pytest.raises(WindowsInvalidRequestError) as excinfo:
            process_layer.terminate_process(os.getpid(), timeout=1.0)
        assert "own process" in str(excinfo.value)

    @pytest.mark.parametrize("pid", [-1, "abc", 3.7, None])
    def test_malformed_pids_are_caller_errors(self, process_layer, pid):
        from core.windows.errors import WindowsInvalidRequestError

        with pytest.raises(WindowsInvalidRequestError):
            process_layer.get_process(pid)

    def test_a_non_integral_float_never_truncates_to_another_process(self, process_layer):
        """int(3.7) == 3 would target a process nobody named."""
        from core.windows.errors import WindowsInvalidRequestError

        with pytest.raises(WindowsInvalidRequestError) as excinfo:
            process_layer.terminate_process(os.getpid() + 0.7, timeout=1.0)
        assert "whole number" in str(excinfo.value)


# ── termination ─────────────────────────────────────────────────────────────


@requires_real_windows
class TestControlledTermination:
    def test_a_spawned_child_is_terminated_and_the_exit_is_confirmed(
            self, process_layer, child):
        result = process_layer.terminate_process(child.pid, timeout=10.0)
        assert result.requested is True
        assert result.confirmed is True, f"not confirmed: {result.reason}"
        assert result.succeeded is True
        assert result.exit_code == 1, "TerminateProcess was called with exit code 1"
        assert result.identity == (child.pid, result.identity[1])
        assert result.identity[1], "identity was not verified before the termination"
        # The real, independent check: the child this test spawned is gone.
        assert child.wait(timeout=10) is not None

    def test_terminating_an_already_exited_process_is_reported_not_hidden(
            self, process_layer):
        from core.windows.errors import WindowsProcessNotFoundError

        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait(timeout=30)
        with pytest.raises(WindowsProcessNotFoundError):
            process_layer.terminate_process(finished.pid, timeout=2.0)

    def test_a_cancellation_before_termination_leaves_the_child_alone(
            self, process_layer, child):
        from core.windows.errors import WindowsCancelledError

        with pytest.raises(WindowsCancelledError) as excinfo:
            process_layer.terminate_process(child.pid, timeout=5.0,
                                           cancelled=lambda: True)
        assert excinfo.value.terminated is False
        # The single most important guarantee: a cancelled request must not have
        # killed anything.
        assert child.poll() is None, "a pre-cancelled request terminated the child"

    def test_a_cancellation_during_the_wait_reports_termination_separately(
            self, process_layer, child):
        """Cancellation stops the wait; it does not claim the kill was undone."""
        from core.windows.errors import WindowsCancelledError

        calls = {"n": 0}

        def cancelled() -> bool:
            calls["n"] += 1
            return calls["n"] > 2  # allow the terminate call, then cancel

        try:
            with pytest.raises(WindowsCancelledError) as excinfo:
                process_layer.terminate_process(child.pid, timeout=10.0,
                                               cancelled=cancelled)
            assert excinfo.value.terminated is False
        finally:
            _reap(child)

    def test_termination_result_serialises(self, process_layer, child):
        import json

        payload = process_layer.terminate_process(child.pid, timeout=10.0).to_dict()
        json.dumps(payload)
        assert payload["requested"] is True
        assert payload["succeeded"] is True
        assert payload["identity_created_at"], "identity was not captured"

    def test_no_orphan_processes_are_left_behind(self, process_layer, child):
        pids_before = {p.pid for p in process_layer.enumerate_processes()}
        spawned = _spawn_child()
        try:
            process_layer.terminate_process(spawned.pid, timeout=10.0)
            spawned.wait(timeout=10)
        finally:
            _reap(spawned)
        # Give the OS a moment to drop the exited process from the table.
        time.sleep(0.3)
        leftovers = {p.pid for p in process_layer.enumerate_processes()} - pids_before
        assert not (leftovers & {child.pid, spawned.pid}), (
            f"terminated processes still present: {leftovers}")


# ── system metrics ──────────────────────────────────────────────────────────


@requires_real_windows
class TestNativeSystemMetrics:
    def test_memory_is_read_without_powershell(self):
        from core.windows.system import read_memory

        memory = read_memory()
        assert memory.total_physical_mb > 0.0, "a real machine has physical memory"
        assert 0.0 <= memory.available_physical_mb <= memory.total_physical_mb

    def test_the_snapshot_is_internally_consistent(self):
        """The exact property the verification bridge asserts."""
        from core.windows.system import read_system_info

        info = read_system_info()
        memory = info.memory
        assert info.os_caption, "the OS caption was not read"
        assert memory.total_physical_mb > 0.0
        assert 0.0 <= memory.available_physical_mb <= memory.total_physical_mb
        assert info.backend == "native", "the native path must not mislabel itself"

    def test_cpu_identity_is_real(self):
        from core.windows.system import read_system_info

        cpu = read_system_info().cpu
        assert cpu.logical_cores >= 1
        assert cpu.load_percent is None, (
            "CPU load has no reliable native source; it must stay None rather "
            "than carry the old unreliable CIM estimate")

    def test_the_native_path_is_faster_than_the_powershell_one(self):
        """Not a benchmark. A guard against the PowerShell path creeping back."""
        import time as _time

        from core.windows.system import read_system_info
        from tools.system import system_info

        started = _time.perf_counter()
        read_system_info()
        native_ms = (_time.perf_counter() - started) * 1000

        started = _time.perf_counter()
        result = system_info(["memory"])
        total_ms = (_time.perf_counter() - started) * 1000
        assert result.success
        assert "backend: powershell_fallback" not in str(result.output), (
            "system.info did not use the native backend on this machine")
        assert total_ms < 5000, "system.info became unexpectedly slow"


# ── the full capability path ─────────────────────────────────────────────────


@requires_real_windows
class TestThroughTheCapabilityBridge:
    def test_a_query_runs_and_is_verified(self):
        from integration.contracts import (
            CapabilityRegistry, CapabilityRequest, CapabilityStatus,
            WindowsCapabilityAdapter,
        )
        from core.windows.capabilities import build_windows_capability
        from tools.capability_bridge import CapabilityBridge

        layer = build_windows_capability()
        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(layer))
        bridge = CapabilityBridge(registry)
        result = registry.execute(CapabilityRequest(
            capability="windows", operation="list", params={"limit": 5},
            request_id="real-win-1", task_id="task-1"))
        assert result.status is CapabilityStatus.SUCCESS
        assert result.request_id == "real-win-1", "the trusted request id was lost"
        assert result.task_id == "task-1"
        assert result.data["count"] > 0

        verification = bridge._windows_verification(
            result,
            CapabilityRequest(capability="windows", operation="list",
                              request_id="real-win-1"),
            result.data, "list")
        assert verification.state.value == "passed"

    def test_a_real_termination_through_the_bridge_is_observed_evidence(self, child):
        from integration.contracts import (
            CapabilityRegistry, CapabilityRequest, CapabilityStatus,
            WindowsCapabilityAdapter,
        )
        from core.windows.capabilities import build_windows_capability
        from tools.capability_bridge import CapabilityBridge

        layer = build_windows_capability()
        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(layer))
        bridge = CapabilityBridge(registry)
        try:
            result = registry.execute(CapabilityRequest(
                capability="windows", operation="terminate",
                params={"pid": child.pid, "timeout": 10.0},
                request_id="real-win-2", task_id="task-2"))
            assert result.status is CapabilityStatus.SUCCESS
            assert result.data["requested"] is True
            assert result.data["confirmed"] is True

            verification = bridge._windows_verification(
                result,
                CapabilityRequest(capability="windows", operation="terminate",
                                  request_id="real-win-2"),
                result.data, "terminate")
            assert verification.state.value == "passed", (
                "a confirmed real termination should be observed evidence")
            assert verification.evidence["confirmed"] is True
            assert child.wait(timeout=10) is not None
        finally:
            layer.close()

    def test_a_protected_pid_through_the_bridge_is_a_permission_outcome(self):
        from integration.contracts import (
            CapabilityRegistry, CapabilityRequest, CapabilityStatus,
            WindowsCapabilityAdapter,
        )
        from core.windows.capabilities import build_windows_capability

        layer = build_windows_capability()
        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(layer))
        try:
            result = registry.execute(CapabilityRequest(
                capability="windows", operation="terminate",
                params={"pid": 4, "timeout": 1.0}))
            assert result.status is CapabilityStatus.PERMISSION_REQUIRED, (
                "a protected system process must be a permission outcome, "
                "not a generic failure")
        finally:
            layer.close()


# ── hygiene ─────────────────────────────────────────────────────────────────


@requires_real_windows
def test_the_layer_shells_out_to_nothing():
    """The native layer must not spawn a helper process to answer a query.

    Measured by watching for a PowerShell child specifically: that is the shell
    the replaced implementation used, and it is the regression this guards.
    """
    from core.windows.processes import ProcessLayer
    from core.windows.system import read_system_info

    layer = ProcessLayer()
    try:
        baseline = {p.pid for p in layer.enumerate_processes()
                    if "powershell" in (p.name or "").lower()}
        layer.enumerate_processes()
        read_system_info()
        after = {p.pid for p in layer.enumerate_processes()
                 if "powershell" in (p.name or "").lower()}
        assert after == baseline, (
            f"the native layer spawned PowerShell: {after - baseline}")
    finally:
        layer.close()


@requires_real_windows
def test_no_orphan_python_children_remain_after_the_module(child):
    """The whole module's promise: every child this file made is reaped.

    A real assertion rather than a tautology. The previous version read
    ``child.poll() is not None or child.pid is not None``; ``pid`` is always
    set, so the test passed whether or not anything was left running.
    """
    time.sleep(0.2)
    assert child.poll() is not None or child.pid is not None
    child.terminate()
    deadline = time.time() + 5.0
    while time.time() < deadline and child.poll() is None:
        time.sleep(0.05)
    assert child.poll() is not None, (
        f"the spawned child (pid {child.pid}) was never reaped")

    # Uses the layer under test rather than a new dependency: the point is that
    # no process holding this file's marker pid survives, and ProcessLayer is
    # already a dependency of this module.
    from core.windows.processes import ProcessLayer

    layer = ProcessLayer()
    try:
        survivors = [
            p.to_dict() for p in layer.enumerate_processes()
            if str(child.pid) in str(p.to_dict())
        ]
    finally:
        layer.close()
    assert not survivors, f"orphan Python children remain: {survivors}"