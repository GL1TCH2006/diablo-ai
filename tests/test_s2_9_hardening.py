"""S2.9 - Blender subprocess supervision and runtime hardening.

These tests are behavioural. Where a guarantee is about a real operating-system
process (termination, kill escalation, no orphan, bounded output) they use a real
child process and assert on the real OS state, because a mock cannot prove that
a process actually died. Where the guarantee is about classification, lifecycle
or inventory they use the real production code paths with only the outermost
subprocess boundary substituted.

The ratified contract this file enforces (docs/contracts/cross_layer_execution_contracts.md,
Contract 2) requires that cancellation stops the wait, terminates the child where
possible, escalates to a bounded kill, reports CANCELLED rather than failure,
leaves no orphan, and revalidates the session before reuse.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from blender import controller as blender_controller
from blender.capabilities import BlenderCapabilityLayer, session_file_is_valid
from blender.errors import (
    ERROR_CODES,
    BlenderCancelledError,
    BlenderClosedError,
    BlenderError,
    BlenderNotFoundError,
    BlenderPermissionError,
    BlenderProcessError,
    BlenderScriptError,
    BlenderSpawnError,
    BlenderTimeoutError,
)
from blender.operations import (
    LOCAL_OPERATIONS,
    OPERATION_NAMES,
    READ_ONLY_OPERATIONS,
    SESSION_MUTATING_OPERATIONS,
    SUBPROCESS_OPERATIONS,
    is_interruptible,
    requires_session_revalidation,
)
from blender.protocol import SUPPORTED_OPERATIONS, build_request
from blender.runner import MAX_STREAM_CHARS, BlenderRunner, _BoundedStream
from integration.contracts import (
    BlenderCapabilityAdapter,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityStatus,
)
from voice.cancellation import CancellationToken

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "blender" / "scripts"

TRUSTED = "S1-TRUSTED-REQ"


# --------------------------------------------------------------- utilities

def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` still names a live process, without extra dependencies."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: A real child process: records its pid, sleeps, then writes a valid result.
_CHILD_SOURCE = """
import json, os, sys, time
pid_file, req_file, out_file, seconds = sys.argv[1:5]
with open(pid_file, "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(float(seconds))
operation, request_id = "unknown", ""
try:
    with open(req_file) as fh:
        request = json.load(fh)
    operation = request.get("operation", "")
    request_id = request.get("request_id", "")
except Exception:
    pass
with open(out_file, "w") as fh:
    json.dump({"success": True, "operation": operation, "data": {"pid": os.getpid()},
               "error": None, "request_id": request_id}, fh)
"""


class SleepingRunner(BlenderRunner):
    """A real BlenderRunner whose command is a real, long-lived child process.

    Everything except the argv is production code: the spawn, the bounded
    readers, the supervision loop, the terminate/kill escalation and the reap.
    """

    def __init__(self, seconds: float = 60.0, pid_file: Path | None = None, **kwargs) -> None:
        super().__init__(exe=sys.executable, **kwargs)
        self.seconds = float(seconds)
        self.pid_file = Path(pid_file) if pid_file else None
        self.popen_calls: list[list[str]] = []

    def build_command(self, request, request_file, output_file, scene_file=None):
        assert self.pid_file is not None
        self.popen_calls.append([sys.executable, "-c", _CHILD_SOURCE])
        return [
            sys.executable, "-c", _CHILD_SOURCE,
            str(self.pid_file), str(request_file), str(output_file), str(self.seconds),
        ]


class CountingPopen:
    """Wraps subprocess.Popen so a test can see the process objects created."""

    def __init__(self) -> None:
        self.processes: list[subprocess.Popen] = []
        self._real = subprocess.Popen

    def install(self, monkeypatch) -> None:
        runner_module = sys.modules["blender.runner"]
        outer = self

        def factory(cmd, *args, **kwargs):
            proc = outer._real(cmd, *args, **kwargs)
            outer.processes.append(proc)
            return proc

        monkeypatch.setattr(runner_module.subprocess, "Popen", factory)

    @property
    def last(self):
        return self.processes[-1]


def _token_cancelled_after(delay: float) -> tuple[CancellationToken, threading.Thread]:
    token = CancellationToken("task-x")

    def fire():
        time.sleep(delay)
        token.request("test cancel", source="s2_9")

    thread = threading.Thread(target=fire, daemon=True)
    return token, thread


def _token_now() -> CancellationToken:
    token = CancellationToken("task-x")
    token.request("already cancelled", source="s2_9")
    return token


class _FakeProc:
    """A process double for the terminate/kill escalation ordering."""

    def __init__(self, exits_on_kill: bool = True) -> None:
        self.pid = 999999
        self.signals: list[str] = []
        self._alive = True
        self._exits_on_kill = exits_on_kill

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")
        if self._exits_on_kill:
            self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="fake-blender", timeout=timeout or 0)
        return 0


class ScriptedRunner:
    """Runner double that records the session state and raises on demand."""

    def __init__(self, error: BlenderError | None = None,
                 session_bytes: bytes | None = b"BLENDER-v304") -> None:
        self.error = error
        self.session_bytes = session_bytes
        self.calls: list[dict] = []
        self.session_existed_during_call: list[bool] = []
        self._save_file = ""

    def resolve_exe(self) -> str:
        return "<fake-blender>"

    def run(self, request, timeout=None, scene_file=None, cancel_token=None):
        self.calls.append({"request": dict(request), "cancel_token": cancel_token})
        save_file = str((request.get("_session") or {}).get("save_file") or "")
        session = Path(save_file) if save_file else None
        if session is not None:
            self.session_existed_during_call.append(session.is_file())
            if self.session_bytes is not None:
                session.parent.mkdir(parents=True, exist_ok=True)
                session.write_bytes(self.session_bytes)
        if self.error is not None:
            raise self.error
        return _ok_result(request)


def _ok_result(request):
    from blender.runner import RunnerResult

    return RunnerResult(
        success=True, operation=request.get("operation", ""),
        data={"ok": True}, request_id=request.get("request_id", ""),
    )


# ============================================================ 1. typed Popen

class TestTypedStartupFailures:
    """A failure to start the child is a typed S2 error, never a raw OSError."""

    def test_missing_executable_becomes_not_found(self, monkeypatch):
        def boom(cmd, *a, **kw):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(subprocess, "Popen", boom)
        runner = BlenderRunner(exe=__file__)
        with pytest.raises(BlenderNotFoundError) as excinfo:
            runner.run(build_request("inspect_scene"))
        assert excinfo.value.code == "blender_not_found"

    def test_permission_problem_becomes_permission_error(self, monkeypatch):
        def boom(cmd, *a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(subprocess, "Popen", boom)
        runner = BlenderRunner(exe=__file__)
        with pytest.raises(BlenderPermissionError) as excinfo:
            runner.run(build_request("inspect_scene"))
        assert excinfo.value.code == "blender_permission_denied"

    def test_generic_oserror_becomes_spawn_error(self, monkeypatch):
        def boom(cmd, *a, **kw):
            raise OSError(1011, "Resource temporarily unavailable")

        monkeypatch.setattr(subprocess, "Popen", boom)
        runner = BlenderRunner(exe=__file__)
        with pytest.raises(BlenderSpawnError) as excinfo:
            runner.run(build_request("inspect_scene"))
        assert excinfo.value.code == "blender_spawn_error"

    @pytest.mark.parametrize("raised", [FileNotFoundError, PermissionError, OSError])
    def test_no_raw_oserror_escapes_run(self, monkeypatch, raised):
        def boom(cmd, *a, **kw):
            raise raised("spawn failed")

        monkeypatch.setattr(subprocess, "Popen", boom)
        runner = BlenderRunner(exe=__file__)
        with pytest.raises(BlenderError):
            runner.run(build_request("render", {"path": "x.png"}))

    def test_startup_failure_is_attributed_to_the_operation(self, monkeypatch):
        def boom(cmd, *a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(subprocess, "Popen", boom)
        runner = BlenderRunner(exe=__file__)
        with pytest.raises(BlenderError) as excinfo:
            runner.run(build_request("render", {"path": "x.png"}, request_id=TRUSTED))
        assert excinfo.value.operation == "render"
        assert excinfo.value.request_id == TRUSTED

    def test_missing_configured_exe_still_raises_not_found_before_spawning(self, tmp_path):
        runner = BlenderRunner(exe=str(tmp_path / "gone" / "blender.exe"))
        with pytest.raises(BlenderNotFoundError):
            runner.run(build_request("inspect_scene"))


# ================================================ 2. cancellation, pre-exec

class TestCancellationBeforeExecution:
    """A request cancelled up front never starts a Blender process."""

    def test_cancelled_token_prevents_spawn(self, monkeypatch, tmp_path):
        counting = CountingPopen()
        counting.install(monkeypatch)
        runner = SleepingRunner(pid_file=tmp_path / "pid")
        with pytest.raises(BlenderCancelledError) as excinfo:
            runner.run(build_request("inspect_scene"), cancel_token=_token_now())
        assert counting.processes == []
        assert excinfo.value.code == "blender_cancelled"

    def test_adapter_reports_cancelled_without_dispatching(self, tmp_path):
        runner = ScriptedRunner()
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        request = CapabilityRequest(
            capability="blender", operation="inspect_scene", params={},
            task_id="t1", request_id=TRUSTED, cancellation_token=_token_now(),
        )
        result = registry.execute(request)
        assert result.status is CapabilityStatus.CANCELLED
        assert result.request_id == TRUSTED
        assert runner.calls == []

    def test_cancellation_never_becomes_invalid_request(self, tmp_path):
        layer = BlenderCapabilityLayer(
            runner=ScriptedRunner(), work_dir=str(tmp_path / "w2"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        request = CapabilityRequest(
            capability="blender", operation="inspect_scene", params={},
            task_id="t1", request_id=TRUSTED, cancellation_token=_token_now(),
        )
        assert registry.execute(request).status is not CapabilityStatus.INVALID_REQUEST


# ============================================ 3/4/5. supervision of a real child

class TestActiveSubprocessSupervision:
    """A cancellation that arrives mid-flight stops the wait and kills the child."""

    def test_cancellation_during_a_real_subprocess_terminates_it(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        runner = SleepingRunner(seconds=60.0, pid_file=pid_file,
                                terminate_grace=5.0, kill_grace=5.0)
        token, fire = _token_cancelled_after(0.4)
        fire.start()

        started = time.perf_counter()
        with pytest.raises(BlenderCancelledError):
            runner.run(build_request("render", {"path": "x.png"}, request_id=TRUSTED),
                       timeout=60.0, cancel_token=token)
        elapsed = time.perf_counter() - started

        assert pid_file.is_file(), "child never started"
        assert elapsed < 30, f"waited for the child instead of stopping ({elapsed:.1f}s)"

    def test_no_orphan_process_survives_cancellation(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        counting = CountingPopen()
        runner = SleepingRunner(seconds=60.0, pid_file=pid_file)
        token, fire = _token_cancelled_after(0.4)
        fire.start()

        with pytest.raises(BlenderCancelledError):
            runner.run(build_request("render", {"path": "x.png"}), timeout=60.0,
                       cancel_token=token)

        child_pid = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.05)
        assert not _pid_alive(child_pid), f"orphan blender process {child_pid} survived"
        # The runner reaped it, so the handle reports a real exit status.
        assert counting.processes == []  # Popen not monkeypatched here
        assert runner.last_result is None

    def test_terminate_is_attempted_before_kill(self):
        runner = BlenderRunner(exe=__file__, terminate_grace=0.01, kill_grace=0.01)
        proc = _FakeProc(exits_on_kill=True)
        runner._stop(proc, "render")
        assert proc.signals == ["terminate", "kill"]

    def test_kill_fallback_is_bounded_when_the_child_ignores_terminate(self):
        runner = BlenderRunner(exe=__file__, terminate_grace=0.01, kill_grace=0.01)
        proc = _FakeProc(exits_on_kill=False)
        started = time.perf_counter()
        runner._stop(proc, "render")
        elapsed = time.perf_counter() - started
        assert proc.signals == ["terminate", "kill"]
        assert elapsed < 5.0, f"kill fallback was not bounded ({elapsed:.1f}s)"

    def test_stop_is_a_noop_when_the_child_already_exited(self):
        runner = BlenderRunner(exe=__file__)
        proc = _FakeProc()
        proc._alive = False
        assert runner._stop(proc, "render") is True
        assert proc.signals == []

    def test_stop_reports_an_unterminated_child(self):
        """A child that survives kill must never be reported as terminated."""
        runner = BlenderRunner(exe=__file__, terminate_grace=0.01, kill_grace=0.01)
        proc = _FakeProc(exits_on_kill=False)
        assert runner._stop(proc, "render") is False

    def test_uncancellable_child_reports_the_ratified_pending_code(self, tmp_path, monkeypatch):
        """The ratified contract forbids claiming a termination that did not happen.

        The real kill still runs so this test leaves no orphan behind; only the
        *confirmation* is faked away, which is the case under test.
        """
        real_stop = BlenderRunner._stop

        def unconfirmed(self, proc, operation):
            real_stop(self, proc, operation)
            return False

        monkeypatch.setattr(BlenderRunner, "_stop", unconfirmed)
        pid_file = tmp_path / "child.pid"
        runner = SleepingRunner(seconds=60.0, pid_file=pid_file)
        token, fire = _token_cancelled_after(0.3)
        fire.start()
        with pytest.raises(BlenderCancelledError) as excinfo:
            runner.run(build_request("render", {"path": "x.png"}), timeout=60.0,
                       cancel_token=token)
        assert excinfo.value.code == "cancellation_pending"
        assert excinfo.value.terminated is False
        assert excinfo.value.diagnostics["cancellation_terminated"] is False
        # The test itself must not leave the child running.
        child_pid = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.05)
        assert not _pid_alive(child_pid), "the test leaked a child process"

    def test_pending_cancellation_surfaces_ratified_metadata(self, tmp_path):
        """status=cancelled + cancellation_pending + metadata, through the contract."""
        pending = BlenderCancelledError(
            operation="render", message="could not terminate", code="cancellation_pending",
            diagnostics={"cancellation_terminated": False, "interruptible": False},
        )
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(error=pending),
                                       work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        result = registry.execute(CapabilityRequest(
            capability="blender", operation="render",
            params={"path": str(tmp_path / "o.png")},
            task_id="t1", request_id=TRUSTED))
        assert result.status is CapabilityStatus.CANCELLED
        assert result.status is not CapabilityStatus.SUCCESS
        assert (result.error or {})["error"] == "cancellation_pending"
        assert result.metadata["cancellation_terminated"] is False
        assert result.metadata["interruptible"] is False

    def test_successful_termination_reports_terminated_metadata(self, tmp_path):
        layer = BlenderCapabilityLayer(
            runner=ScriptedRunner(error=BlenderCancelledError(
                operation="render", message="terminated")),
            work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        result = registry.execute(CapabilityRequest(
            capability="blender", operation="render",
            params={"path": str(tmp_path / "o.png")},
            task_id="t1", request_id=TRUSTED))
        assert result.status is CapabilityStatus.CANCELLED
        assert result.metadata["cancellation_terminated"] is False  # never assumed
        assert result.metadata["interruptible"] is False  # render mutates the session

    def test_read_only_cancellation_is_marked_interruptible(self, tmp_path):
        layer = BlenderCapabilityLayer(
            runner=ScriptedRunner(error=BlenderCancelledError(
                operation="inspect_scene", message="terminated")),
            work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        result = registry.execute(CapabilityRequest(
            capability="blender", operation="inspect_scene", params={},
            task_id="t1", request_id=TRUSTED))
        assert result.metadata["interruptible"] is True

    def test_cancellation_pending_code_maps_to_cancelled(self):
        error = BlenderError(operation="render", message="x", code="cancellation_pending")
        assert BlenderCapabilityAdapter._status_for_exception(error) is CapabilityStatus.CANCELLED
        assert "cancellation_pending" in ERROR_CODES

    def test_timeout_also_terminates_and_reaps_a_real_child(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        runner = SleepingRunner(seconds=60.0, pid_file=pid_file,
                                terminate_grace=5.0, kill_grace=5.0)
        with pytest.raises(BlenderTimeoutError) as excinfo:
            runner.run(build_request("render", {"path": "x.png"}), timeout=1.0)
        child_pid = int(pid_file.read_text().strip())
        assert excinfo.value.timeout == 1.0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.05)
        assert not _pid_alive(child_pid)

    def test_uninterrupted_run_still_succeeds_through_the_supervised_path(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        runner = SleepingRunner(seconds=0.1, pid_file=tmp_path / "child.pid")
        token = CancellationToken("t")
        result = runner.run(build_request("inspect_scene", request_id=TRUSTED),
                            timeout=30.0, cancel_token=token)
        assert result.success is True
        assert result.request_id == TRUSTED
        assert pid_file.is_file()

    def test_layer_reports_cancellation_through_the_capability_contract(self, tmp_path):
        """End to end: S1-style request -> adapter -> layer -> real child."""
        pid_file = tmp_path / "child.pid"
        runner = SleepingRunner(seconds=60.0, pid_file=pid_file)
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        token, fire = _token_cancelled_after(0.4)
        fire.start()
        request = CapabilityRequest(
            capability="blender", operation="render",
            params={"path": str(tmp_path / "out.png")},
            task_id="t1", request_id=TRUSTED, cancellation_token=token,
        )
        result = registry.execute(request)
        assert result.status is CapabilityStatus.CANCELLED, result.error
        assert result.request_id == TRUSTED
        assert (result.error or {}).get("error") == "blender_cancelled"

    def test_cancellation_does_not_leak_into_a_later_request(self, tmp_path):
        """A cancelled token must not cancel the next, unrelated call."""
        pid_file = tmp_path / "child.pid"
        runner = SleepingRunner(seconds=60.0, pid_file=pid_file)
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))

        token, fire = _token_cancelled_after(0.4)
        fire.start()
        cancelled = registry.execute(CapabilityRequest(
            capability="blender", operation="render",
            params={"path": str(tmp_path / "a.png")},
            task_id="t1", request_id="req-a", cancellation_token=token))
        assert cancelled.status is CapabilityStatus.CANCELLED, cancelled.error

        # A fresh request with no token must run to completion normally.
        runner.seconds = 0.1
        ok = registry.execute(CapabilityRequest(
            capability="blender", operation="inspect_scene",
            params={}, task_id="t2", request_id="req-b"))
        assert ok.status is CapabilityStatus.SUCCESS, ok.error
        assert ok.request_id == "req-b"


# ============================================ 6. session revalidation

class TestSessionRevalidation:
    """A session caught mid-write is never reused unproven."""

    def test_read_only_operations_are_interruptible(self):
        for operation in READ_ONLY_OPERATIONS:
            assert is_interruptible(operation) is True
            assert requires_session_revalidation(operation) is False

    def test_session_mutating_operations_require_revalidation(self):
        for operation in SESSION_MUTATING_OPERATIONS:
            assert requires_session_revalidation(operation) is True
            assert is_interruptible(operation) is False

    def test_local_operations_have_no_child_to_interrupt(self):
        assert LOCAL_OPERATIONS == ("inspect_render",)
        assert is_interruptible("inspect_render") is False

    def test_torn_session_is_discarded_after_an_interrupted_mutation(self, tmp_path):
        torn = b"\x00\x01\x02"  # neither BLENDER nor gzip magic
        runner = ScriptedRunner(
            error=BlenderCancelledError(operation="create_object", message="cancelled"),
            session_bytes=torn,
        )
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with pytest.raises(BlenderCancelledError) as excinfo:
            layer.create_object(name="Cube")
        # The child had written a torn session; it must not survive the call.
        assert excinfo.value.diagnostics["session_revalidated"] is True
        assert excinfo.value.diagnostics["session_discarded"] is True
        assert not layer.session_file.exists(), "torn session was left for reuse"
        assert not session_file_is_valid(layer.session_file)

    def test_intact_session_survives_an_interrupted_mutation(self, tmp_path):
        runner = ScriptedRunner(
            error=BlenderCancelledError(operation="create_object", message="cancelled"),
            session_bytes=b"BLENDER-v304",
        )
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with pytest.raises(BlenderCancelledError) as excinfo:
            layer.create_object(name="Cube")
        assert layer.session_file.is_file()
        assert excinfo.value.diagnostics["session_discarded"] is False

    def test_compressed_session_counts_as_intact(self, tmp_path):
        assert session_file_is_valid(
            _write(tmp_path / "s.blend", b"\x1f\x8b\x08\x00compressed-payload")) is True
        assert session_file_is_valid(_write(tmp_path / "p.blend", b"BLENDER-v304")) is True
        assert session_file_is_valid(_write(tmp_path / "e.blend", b"")) is False
        assert session_file_is_valid(tmp_path / "missing.blend") is False
        assert session_file_is_valid(_write(tmp_path / "j.blend", b"x" * 400)) is False
        assert session_file_is_valid(_write(tmp_path / "t.blend", b"\x00\x01" * 20)) is False

    def test_interrupted_read_only_operation_does_not_touch_the_session(self, tmp_path):
        runner = ScriptedRunner(
            error=BlenderCancelledError(operation="inspect_scene", message="cancelled"),
            session_bytes=None,  # a read-only op writes nothing
        )
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        layer.session_file.write_bytes(b"BLENDER-v304")
        with pytest.raises(BlenderCancelledError) as excinfo:
            layer.inspect_scene()
        assert excinfo.value.diagnostics.get("session_revalidated") is False
        assert layer.session_file.is_file(), "read-only cancellation must not discard state"

    def test_session_backups_are_removed_with_a_discarded_session(self, tmp_path):
        runner = ScriptedRunner(
            error=BlenderCancelledError(operation="save", message="cancelled"),
            session_bytes=b"garbage-not-a-blend",
        )
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with pytest.raises(BlenderCancelledError):
            layer.save(str(tmp_path / "out.blend"))
        (tmp_path / "w" / "session.blend1").write_bytes(b"backup")
        # A second interrupted save clears the backup too.
        with pytest.raises(BlenderCancelledError):
            layer.save(str(tmp_path / "out.blend"))
        assert not (tmp_path / "w" / "session.blend1").exists()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ================================================= 7. bounded output

class TestBoundedOutput:
    """Output capture is bounded, deterministic, and self-describing."""

    def test_boundary_exactly_at_the_limit_is_not_truncated(self):
        stream = _BoundedStream(limit=10)
        stream.feed("0123456789")
        assert stream.truncated is False
        assert stream.text() == "0123456789"

    def test_boundary_one_character_past_the_limit_is_truncated(self):
        stream = _BoundedStream(limit=10)
        stream.feed("0123456789A")
        assert stream.truncated is True
        assert stream.text().startswith("0123456789")
        assert "truncated: 1 more characters" in stream.text()

    def test_truncation_is_deterministic_for_the_same_input(self):
        payloads = ["0123456789" * 3, "x" * 7]
        first = _BoundedStream(limit=5)
        second = _BoundedStream(limit=5)
        for payload in payloads:
            first.feed(payload)
            second.feed(payload)
        assert first.text() == second.text()
        assert first.dropped_chars == second.dropped_chars

    def test_dropped_characters_are_counted_exactly(self):
        stream = _BoundedStream(limit=4)
        stream.feed("aaaa")
        stream.feed("bbbbbb")
        assert stream.text().startswith("aaaa")
        assert "truncated: 6 more characters" in stream.text()
        # Retention is exactly the limit; the rest is reported, never silently kept.
        assert stream.text().split("\n[truncated")[0] == "aaaa"

    def test_real_child_output_is_bounded_and_the_child_still_completes(self, tmp_path):
        """A chatty child must not be able to exhaust memory or deadlock."""
        noisy = (
            "import sys\n"
            "sys.stdout.write('o' * 200000)\n"
            "sys.stdout.write('!' * 200000)\n"
            "sys.stderr.write('e' * 200000)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "sys.argv[4] and open(sys.argv[4], 'w').write('x')\n"
        )
        out_file = tmp_path / "result.json"
        runner = BlenderRunner(exe=sys.executable, max_stream_chars=1000)
        # The child's extra write target must be an absolute path inside tmp_path.
        # A bare "0" is a non-empty (truthy) string, so the child would resolve it
        # against the CWD and drop a stray file named "0" in the repository root.
        child_side_effect = tmp_path / "child_side_effect.txt"
        cmd = [sys.executable, "-c", noisy, "pid", "req", str(out_file), str(child_side_effect)]
        out_file.write_text(json.dumps(
            {"success": True, "operation": "inspect_scene", "data": {},
             "error": None, "request_id": "r"}), encoding="utf-8")
        completed = runner._execute(cmd, timeout=60)
        assert completed.returncode == 0
        assert completed.stdout_truncated is True
        assert completed.stderr_truncated is True
        assert len(completed.stdout) < 2000
        assert "truncated:" in completed.stdout
        assert "truncated:" in completed.stderr

    def test_failure_preserves_truncation_diagnostics(self, tmp_path, monkeypatch):
        """A failing op keeps enough diagnostics to debug, and flags truncation."""
        exe = _write(tmp_path / "blender.exe", b"MZ")
        runner = BlenderRunner(exe=exe)
        stderr = "E" * 500 + "\n[truncated: 49500 more characters]"

        def failing(cmd, timeout):
            completed = subprocess.CompletedProcess(cmd, 3, "", stderr)
            completed.stderr_truncated = True
            return completed

        monkeypatch.setattr(runner, "_execute", failing)
        with pytest.raises(BlenderProcessError) as excinfo:
            runner.run(build_request("render", {"path": "x.png"}, request_id=TRUSTED))
        assert excinfo.value.return_code == 3
        assert excinfo.value.stderr.startswith("E" * 100)
        assert excinfo.value.diagnostics["stderr_truncated"] is True
        assert excinfo.value.request_id == TRUSTED
        # The retained diagnostic survives JSON serialization intact.
        assert json.dumps(excinfo.value.to_dict())

    def test_default_stream_limit_is_bounded(self):
        assert 0 < MAX_STREAM_CHARS <= 1024 * 1024


# ================================================= 8/9. temp cleanup + close

class TestTempCleanupAndClose:
    """Cleanup is complete, idempotent, failure-tolerant and race-safe."""

    def test_layer_created_work_dir_is_removed_on_close(self, tmp_path):
        layer = BlenderCapabilityLayer(runner=ScriptedRunner())
        work = layer._work_dir
        work.mkdir(parents=True, exist_ok=True)
        layer.session_file.write_bytes(b"BLENDER-v304")
        assert work.is_dir()
        layer.close()
        assert not work.exists()

    def test_caller_supplied_work_dir_is_preserved(self, tmp_path):
        work = tmp_path / "mine"
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(), work_dir=str(work))
        layer.create_object(name="C")
        keep = work / "keepme.txt"
        keep.write_text("caller data")
        layer.close()
        assert work.is_dir(), "a caller-owned directory must survive close"
        assert not layer.session_file.exists()
        assert keep.read_text() == "caller data"

    def test_close_is_idempotent(self, tmp_path):
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(), work_dir=str(tmp_path / "w"))
        layer.close()
        layer.close()
        layer.close()
        assert layer.closed is True

    def test_cleanup_failure_does_not_raise(self, tmp_path, monkeypatch):
        import blender.capabilities as capabilities_module

        layer = BlenderCapabilityLayer(runner=ScriptedRunner())
        layer.session_file.write_bytes(b"BLENDER-v304")

        def explode(path, **kwargs):
            raise OSError("file is locked by another process")

        monkeypatch.setattr(capabilities_module.shutil, "rmtree", explode)
        layer.close()  # must not raise
        assert layer.closed is True

    def test_unlink_failure_does_not_raise(self, tmp_path, monkeypatch):
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(), work_dir=str(tmp_path / "w"))
        layer.session_file.write_bytes(b"BLENDER-v304")

        def explode(self_path, *a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr(type(layer.session_file), "unlink", explode, raising=False)
        layer.close()
        assert layer.closed is True

    def test_repeated_create_and_close_cycles_leave_no_residue(self, tmp_path):
        created: list[Path] = []
        for _ in range(3):
            layer = BlenderCapabilityLayer(runner=ScriptedRunner())
            created.append(layer._work_dir)
            layer.create_object(name="C")
            assert layer.session_file.is_file()
            layer.close()
            assert not layer._work_dir.exists()
        for work in created:
            assert not work.exists()

    def test_operations_after_close_raise_a_typed_error(self, tmp_path):
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(), work_dir=str(tmp_path / "w"))
        layer.close()
        with pytest.raises(BlenderClosedError) as excinfo:
            layer.inspect_scene()
        assert excinfo.value.code == "blender_closed"

    def test_closed_layer_is_a_failure_not_invalid_input(self, tmp_path):
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(), work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        layer.close()
        result = registry.execute(CapabilityRequest(
            capability="blender", operation="inspect_scene", params={},
            task_id="t1", request_id=TRUSTED))
        assert result.status is CapabilityStatus.FAILURE
        assert result.status is not CapabilityStatus.INVALID_REQUEST
        assert (result.error or {}).get("error") == "blender_closed"

    def test_close_waits_for_an_in_flight_operation(self, tmp_path):
        """The session must not be unlinked while a child is still using it."""
        observed: list[bool] = []
        started = threading.Event()
        release = threading.Event()

        class SlowRunner(ScriptedRunner):
            def run(self, request, timeout=None, scene_file=None, cancel_token=None):
                save_file = str((request.get("_session") or {}).get("save_file") or "")
                session = Path(save_file)
                session.parent.mkdir(parents=True, exist_ok=True)
                session.write_bytes(b"BLENDER-v304")
                started.set()
                release.wait(timeout=30)
                observed.append(session.is_file())
                return _ok_result(request)

        layer = BlenderCapabilityLayer(runner=SlowRunner(), work_dir=str(tmp_path / "w"))
        worker = threading.Thread(target=lambda: layer.create_object(name="Slow"), daemon=True)
        worker.start()
        assert started.wait(timeout=10)

        closer = threading.Thread(target=layer.close, daemon=True)
        closer.start()
        time.sleep(0.3)
        assert closer.is_alive(), "close() did not wait for the in-flight operation"
        assert layer.session_file.is_file(), "session unlinked while a child was using it"

        release.set()
        worker.join(timeout=30)
        closer.join(timeout=30)
        assert observed == [True], "session vanished during the operation"
        assert not layer.session_file.exists()
        assert not closer.is_alive()

    def test_m210_serialization_is_preserved(self, tmp_path):
        """Four concurrent operations still never overlap (M2.10 regression)."""
        import threading as th

        class OverlapRunner(ScriptedRunner):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                self.overlaps: list[str] = []
                self._lock = th.Lock()

            def run(self, request, timeout=None, scene_file=None, cancel_token=None):
                operation = request.get("operation", "")
                with self._lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.active > 1:
                        self.overlaps.append(operation)
                try:
                    time.sleep(0.02)
                    return _ok_result(request)
                finally:
                    with self._lock:
                        self.active -= 1

        runner = OverlapRunner()
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        errors: list[Exception] = []

        def op(index: int):
            try:
                layer.create_object(name=f"C{index}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [th.Thread(target=op, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        assert runner.max_active == 1, runner.overlaps
        assert runner.overlaps == []


# ================================================= 10. detection caching

class TestDetectionCaching:
    """Detection is cached, and the cache cannot outlive the executable."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        blender_controller.invalidate_detection_cache()
        yield
        blender_controller.invalidate_detection_cache()

    @staticmethod
    def _install_fake_blender(root: Path, version: str = "4.2") -> Path:
        """Lay out a fake install in the shape ``_find_executable`` expects.

        That is ``<probe root>/Blender<version>/blender.exe``; anything else is
        invisible to detection.
        """
        exe = root / f"Blender {version}" / "blender.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ")
        return exe

    def test_successful_detection_is_cached(self, tmp_path, monkeypatch):
        exe = self._install_fake_blender(tmp_path / "Blender Foundation")
        calls: list[int] = []
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "Blender Foundation"])
        monkeypatch.setattr(blender_controller, "_query_version",
                            lambda path: (calls.append(1), "4.2.0")[1])

        first = blender_controller.detect_blender()
        second = blender_controller.detect_blender()
        third = blender_controller.detect_blender()

        assert first.installed and first.path == str(exe)
        assert second.path == third.path == first.path
        assert len(calls) == 1, f"re-spawned blender --version {len(calls)} times"

    def test_changing_the_probe_configuration_is_honoured(self, tmp_path, monkeypatch):
        self._install_fake_blender(tmp_path / "good")
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "good"])
        monkeypatch.setattr(blender_controller, "_query_version", lambda path: "4.2.0")
        assert blender_controller.detect_blender().installed is True

        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "absent"])
        assert blender_controller.detect_blender().installed is False

    def test_cache_is_invalidated_when_the_executable_disappears(self, tmp_path, monkeypatch):
        exe = self._install_fake_blender(tmp_path / "Blender Foundation")
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "Blender Foundation"])
        monkeypatch.setattr(blender_controller, "_query_version", lambda path: "4.2.0")
        assert blender_controller.detect_blender().installed is True

        exe.unlink()  # blender uninstalled / volume disconnected
        info = blender_controller.detect_blender()
        assert info.installed is False
        assert info.errors

    def test_explicit_invalidation_forces_a_fresh_probe(self, tmp_path, monkeypatch):
        self._install_fake_blender(tmp_path / "Blender Foundation")
        calls: list[int] = []
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "Blender Foundation"])
        monkeypatch.setattr(blender_controller, "_query_version",
                            lambda path: (calls.append(1), "4.2.0")[1])
        blender_controller.detect_blender()
        blender_controller.invalidate_detection_cache()
        blender_controller.detect_blender()
        assert len(calls) == 2

    def test_failed_detection_is_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "absent"])
        assert blender_controller.detect_blender().installed is False
        self._install_fake_blender(tmp_path / "Blender Foundation")
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "Blender Foundation"])
        monkeypatch.setattr(blender_controller, "_query_version", lambda path: "4.2.0")
        assert blender_controller.detect_blender().installed is True

    def test_caller_cannot_mutate_the_cached_value(self, tmp_path, monkeypatch):
        exe = self._install_fake_blender(tmp_path / "Blender Foundation")
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "Blender Foundation"])
        monkeypatch.setattr(blender_controller, "_query_version", lambda path: "4.2.0")
        first = blender_controller.detect_blender()
        first.path = "tampered"
        first.errors.append("tampered")
        assert blender_controller.detect_blender().path == str(exe)
        assert blender_controller.detect_blender().errors == []

    def test_runner_does_not_redetect_per_operation(self, tmp_path, monkeypatch):
        """Three operations must not spawn blender --version three times."""
        exe = self._install_fake_blender(tmp_path / "Blender Foundation")
        calls: list[int] = []
        monkeypatch.setattr(blender_controller, "_PROBE_PATHS", [tmp_path / "Blender Foundation"])
        monkeypatch.setattr(blender_controller, "_query_version",
                            lambda path: (calls.append(1), "4.2.0")[1])
        probe = BlenderRunner()
        for _ in range(3):
            assert probe.resolve_exe() == str(exe)
        assert len(calls) == 1, f"detection re-ran {len(calls)} times for 3 operations"


# ================================================= 11. diagnostic attribution

class TestDiagnosticAttribution:
    """Stale diagnostics are never presented as belonging to another request."""

    def test_last_result_is_cleared_when_a_later_run_fails(self, tmp_path, monkeypatch):
        exe = _write(tmp_path / "blender.exe", b"MZ")
        runner = BlenderRunner(exe=exe)
        out = tmp_path / "o.json"

        def good(cmd, timeout):
            path = Path(cmd[cmd.index("--") + 2])
            path.write_text(json.dumps(
                {"success": True, "operation": "inspect_scene", "data": {"n": 1},
                 "error": None, "request_id": "req-1"}), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(runner, "_execute", good)
        first = runner.run(build_request("inspect_scene", request_id="req-1"))
        assert first.success is True
        assert runner.last_result is first

        monkeypatch.setattr(runner, "_execute",
                            lambda cmd, timeout: subprocess.CompletedProcess(cmd, 9, "", "boom"))
        with pytest.raises(BlenderProcessError):
            runner.run(build_request("render", {"path": "x.png"}, request_id="req-2"))
        assert runner.last_result is None, "a failed run left the previous result visible"

    def test_error_dict_carries_the_trusted_request_id(self, tmp_path):
        runner = ScriptedRunner(error=BlenderScriptError(
            operation="create_object", message="induced", code="blender_script_error"))
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with layer.request_correlation(TRUSTED):
            with pytest.raises(BlenderScriptError):
                layer.create_object(name="C")
        error = layer.last_error()
        assert error is not None
        assert error["request_id"] == TRUSTED
        assert error["error"] == "blender_script_error"

    def test_a_later_success_clears_the_recorded_error(self, tmp_path):
        runner = ScriptedRunner(error=BlenderScriptError(operation="create_object", message="x"))
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with pytest.raises(BlenderScriptError):
            layer.create_object(name="C")
        assert layer.last_error() is not None
        runner.error = None
        layer.inspect_scene()
        assert layer.last_error() is None

    def test_errors_from_different_requests_stay_distinguishable(self, tmp_path):
        runner = ScriptedRunner(error=BlenderScriptError(operation="create_object", message="x"))
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with layer.request_correlation("req-A"):
            with pytest.raises(BlenderScriptError):
                layer.create_object(name="A")
        with layer.request_correlation("req-B"):
            with pytest.raises(BlenderScriptError):
                layer.create_object(name="B")
        assert layer.last_error()["request_id"] == "req-B"

    def test_diagnostics_are_json_safe(self, tmp_path):
        runner = ScriptedRunner(error=BlenderSpawnError(
            operation="render", message="nope", diagnostics={"terminated": True}))
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        with pytest.raises(BlenderSpawnError):
            layer.render(str(tmp_path / "o.png"))
        assert json.dumps(layer.last_error())

    def test_runner_result_reports_truncation_state(self, tmp_path, monkeypatch):
        exe = _write(tmp_path / "blender.exe", b"MZ")
        runner = BlenderRunner(exe=exe)

        def noisy(cmd, timeout):
            path = Path(cmd[cmd.index("--") + 2])
            path.write_text(json.dumps(
                {"success": True, "operation": "inspect_scene", "data": {},
                 "error": None, "request_id": "r"}), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "o" * 10, "")

        monkeypatch.setattr(runner, "_execute", noisy)
        result = runner.run(build_request("inspect_scene"))
        assert result.to_dict()["stdout_truncated"] is False
        assert result.to_dict()["return_code"] == 0


# ================================================= 12. error classification

class TestErrorClassification:
    """Status comes from stable codes, never from message wording."""

    @pytest.mark.parametrize("code,expected", [
        ("blender_cancelled", CapabilityStatus.CANCELLED),
        ("blender_not_found", CapabilityStatus.UNAVAILABLE),
        ("blender_spawn_error", CapabilityStatus.UNAVAILABLE),
        ("blender_permission_denied", CapabilityStatus.PERMISSION_REQUIRED),
        ("blender_timeout", CapabilityStatus.FAILURE),
        ("blender_process_error", CapabilityStatus.FAILURE),
        ("blender_script_error", CapabilityStatus.FAILURE),
        ("blender_protocol_error", CapabilityStatus.FAILURE),
        ("blender_closed", CapabilityStatus.FAILURE),
        ("blender_error", CapabilityStatus.FAILURE),
    ])
    def test_known_codes_map_to_stable_statuses(self, code, expected):
        error = BlenderError(operation="render", message="x", code=code)
        assert BlenderCapabilityAdapter._status_for_exception(error) is expected

    @pytest.mark.parametrize("code", sorted(ERROR_CODES))
    def test_no_stable_code_can_map_to_invalid_request(self, code):
        error = BlenderError(operation="render", message="x", code=code)
        assert BlenderCapabilityAdapter._status_for_exception(error) is not CapabilityStatus.INVALID_REQUEST

    @pytest.mark.parametrize("wording", [
        "invalid argument",
        "INVALID request internally",
        "invalid scene state",
        "permission-like wording in an unrelated failure",
        "not found in the user's asset library",
    ])
    def test_message_wording_never_changes_the_status(self, wording):
        error = BlenderError(operation="render", message=wording, code="blender_process_error")
        assert BlenderCapabilityAdapter._status_for_exception(error) is CapabilityStatus.FAILURE

    def test_unknown_code_is_a_plain_failure(self):
        error = BlenderError(operation="render", message="mystery", code="something_new")
        assert BlenderCapabilityAdapter._status_for_exception(error) is CapabilityStatus.FAILURE

    def test_error_codes_are_a_closed_documented_set(self):
        assert "blender_cancelled" in ERROR_CODES
        assert "blender_spawn_error" in ERROR_CODES
        assert "blender_permission_denied" in ERROR_CODES
        assert "blender_closed" in ERROR_CODES

    def test_error_classes_declare_stable_codes(self):
        assert BlenderCancelledError.code == "blender_cancelled"
        assert BlenderSpawnError.code == "blender_spawn_error"
        assert BlenderPermissionError.code == "blender_permission_denied"
        assert BlenderClosedError.code == "blender_closed"
        for cls in (BlenderCancelledError, BlenderSpawnError, BlenderPermissionError,
                    BlenderClosedError, BlenderTimeoutError, BlenderNotFoundError,
                    BlenderProcessError, BlenderScriptError):
            assert cls.code in ERROR_CODES

    def test_infrastructure_failure_through_the_registry_is_never_invalid(self, tmp_path):
        runner = ScriptedRunner(error=BlenderSpawnError(
            operation="render", message="invalid internal buffer"))
        layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
        registry = CapabilityRegistry()
        registry.register(BlenderCapabilityAdapter(layer))
        result = registry.execute(CapabilityRequest(
            capability="blender", operation="render", params={"path": "x.png"},
            task_id="t1", request_id=TRUSTED))
        assert result.status is not CapabilityStatus.INVALID_REQUEST
        assert result.status is CapabilityStatus.UNAVAILABLE
        assert result.request_id == TRUSTED


# ================================================= 13. operation inventory

class TestOperationInventory:
    """One inventory, no drift between the layers that spell operations out."""

    def test_adapter_operations_come_from_the_inventory(self):
        assert tuple(BlenderCapabilityAdapter._operations) == OPERATION_NAMES

    def test_protocol_operations_come_from_the_inventory(self):
        assert SUPPORTED_OPERATIONS == frozenset(SUBPROCESS_OPERATIONS)

    def test_inventory_has_no_duplicates(self):
        assert len(OPERATION_NAMES) == len(set(OPERATION_NAMES))

    def test_layer_exposes_exactly_the_inventory(self):
        layer = BlenderCapabilityLayer(runner=ScriptedRunner(), work_dir="")
        layer._work_dir.mkdir(parents=True, exist_ok=True)
        public = {
            name for name in dir(type(layer))
            if not name.startswith("_") and callable(getattr(type(layer), name, None))
            and name not in {"close", "detect", "last_error", "request_correlation",
                             "cancellation_scope"}
        }
        assert public <= set(OPERATION_NAMES), f"undeclared layer operations: {public - set(OPERATION_NAMES)}"
        assert set(OPERATION_NAMES) <= public, f"inventory names with no layer method: {set(OPERATION_NAMES) - public}"

    def test_every_subprocess_operation_has_a_blender_side_handler(self):
        source = (SCRIPTS_DIR / "diablo_blender_ops.py").read_text(encoding="utf-8")
        for operation in SUBPROCESS_OPERATIONS:
            assert f"def op_{operation}(" in source, f"no op_{operation} handler in blender"

    def test_blender_side_read_only_set_matches_the_inventory(self):
        source = (SCRIPTS_DIR / "runner.py").read_text(encoding="utf-8")
        assert 'operation in ("inspect_scene", "list_objects", "inspect_object")' in source, (
            "the Blender-side read-only set drifted from blender.operations.READ_ONLY_OPERATIONS"
        )
        assert set(READ_ONLY_OPERATIONS) == {"inspect_scene", "list_objects", "inspect_object"}

    def test_local_operation_is_not_in_the_subprocess_inventory(self):
        assert "inspect_render" in LOCAL_OPERATIONS
        assert "inspect_render" not in SUBPROCESS_OPERATIONS
        assert "inspect_render" in OPERATION_NAMES

    def test_session_mutation_classification_is_coherent(self):
        assert set(READ_ONLY_OPERATIONS) | set(SESSION_MUTATING_OPERATIONS) == set(SUBPROCESS_OPERATIONS)
        assert not (READ_ONLY_OPERATIONS & SESSION_MUTATING_OPERATIONS)
