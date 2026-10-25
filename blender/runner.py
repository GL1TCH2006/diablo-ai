from __future__ import annotations

"""Subprocess Blender runner.

Runs Blender in background mode with a generated/embedded Python dispatch
script, feeding it a JSON request file and collecting a JSON result file.

Execution model::

    blender.exe [scene.blend] --background --factory-startup
        --python blender/scripts/runner.py -- <request.json> <result.json>

Each invocation is an independent Blender process. To build a scene across
multiple operations the capability layer passes a persistent session
``.blend`` file which the dispatch runner opens (if present) and re-saves after
mutating operations.

Runtime hardening (S2.9)
------------------------
* **Typed startup failures.** A failure to create the child becomes a
  :class:`~blender.errors.BlenderError` subclass instead of a raw ``OSError``.
* **Supervised cancellation.** The child is started with :class:`subprocess.Popen`
  so the runner keeps a handle. On cancellation the runner stops waiting,
  terminates the child, escalates to a bounded kill, reaps it so no orphan is
  left behind, and reports a dedicated cancellation error.
* **Bounded output.** stdout/stderr are drained fully (so the child can never
  block on a full pipe) but only a bounded prefix is retained, with the number of
  dropped characters reported.
* **Detection caching.** Resolving the executable does not re-probe and re-run
  ``blender --version`` on every operation.

The runner is kept subprocess-clean so it can be tested without Blender being
installed: :meth:`BlenderRunner._execute` is the only place a real child process
is spawned, and tests override it.
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.logging_setup import get_logger

from .errors import (
    C_CANCELLATION_PENDING,
    BlenderCancelledError,
    BlenderError,
    BlenderNotFoundError,
    BlenderPermissionError,
    BlenderProcessError,
    BlenderProtocolError,
    BlenderSpawnError,
    BlenderTimeoutError,
)
from .protocol import parse_result

log = get_logger("blender.runner")

#: Blender-side dispatch script shipped with this package.
DEFAULT_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
DISPATCH_SCRIPT = "runner.py"

#: Maximum characters retained per captured stream. Output beyond this is
#: counted and discarded. The child is still drained to EOF, so a chatty Blender
#: can never deadlock on a full pipe buffer.
MAX_STREAM_CHARS = 64 * 1024

#: How often the supervision loop re-checks cancellation and the deadline.
POLL_INTERVAL_S = 0.05

#: Bounded wait for a graceful terminate before escalating to kill.
TERMINATE_GRACE_S = 5.0

#: Bounded wait for the kill to take effect and the child to be reaped.
KILL_GRACE_S = 5.0

#: Size of a single read from the child's pipe.
_READ_CHUNK = 65536


class _BoundedStream:
    """Retains a bounded prefix of a stream while draining it to EOF.

    Reading stops mattering, writing does not: the reader thread keeps pulling
    from the pipe until EOF so the child never blocks, but only the first
    ``limit`` characters are kept. The count of discarded characters is
    reported so a truncated diagnostic is never mistaken for a complete one.
    """

    def __init__(self, limit: int = MAX_STREAM_CHARS) -> None:
        self._limit = max(0, int(limit))
        self._parts: list[str] = []
        self._kept = 0
        self.dropped_chars = 0
        self._lock = threading.Lock()

    def feed(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            room = self._limit - self._kept
            if room <= 0:
                self.dropped_chars += len(text)
                return
            if len(text) <= room:
                self._parts.append(text)
                self._kept += len(text)
                return
            self._parts.append(text[:room])
            self._kept += room
            self.dropped_chars += len(text) - room

    @property
    def truncated(self) -> bool:
        return self.dropped_chars > 0

    def text(self) -> str:
        with self._lock:
            body = "".join(self._parts)
        if self.dropped_chars:
            # Deterministic, self-describing truncation marker.
            body += f"\n[truncated: {self.dropped_chars} more characters]"
        return body


def _drain(stream, sink: _BoundedStream) -> None:
    """Read ``stream`` to EOF, retaining only a bounded prefix."""
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            sink.feed(chunk)
    except (ValueError, OSError):
        # Pipe closed underneath us during termination; nothing left to drain.
        pass
    finally:
        try:
            stream.close()
        except (ValueError, OSError):
            pass


def _spawn_error(exc: OSError, operation: str, cmd: list[str]) -> BlenderError:
    """Map a spawn-time ``OSError`` onto the right typed Blender error.

    A missing executable, a permission problem and a generic spawn failure are
    distinct outcomes with distinct codes; none of them may escape as a raw
    ``OSError``.
    """
    stderr = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, FileNotFoundError):
        return BlenderNotFoundError(
            operation=operation,
            message=f"blender executable could not be started: {exc}",
            command=cmd, stderr=stderr,
        )
    if isinstance(exc, PermissionError):
        return BlenderPermissionError(
            operation=operation,
            message=f"blender executable is not executable: {exc}",
            command=cmd, stderr=stderr,
        )
    return BlenderSpawnError(
        operation=operation,
        message=f"blender process could not be started: {exc}",
        command=cmd, stderr=stderr,
    )


@dataclass
class RunnerResult:
    """Structured outcome of one Blender operation after subprocess handling."""

    success: bool
    operation: str
    data: dict | None = None
    error_code: str = ""
    error_message: str = ""
    stderr: str = ""
    stdout: str = ""
    return_code: int = 0
    request_id: str = ""
    duration_s: float = 0.0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def to_dict(self) -> dict:
        """JSON-safe view of the outcome, including truncation state."""
        return {
            "success": self.success,
            "operation": self.operation,
            "data": self.data,
            "error": self.error_code,
            "message": self.error_message,
            "return_code": self.return_code,
            "request_id": self.request_id,
            "duration_s": self.duration_s,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }


class BlenderRunner:
    """Executes one JSON request in a background Blender subprocess."""

    def __init__(
        self,
        exe: str | Path | None = None,
        scripts_dir: Path | None = None,
        timeout: float = 120.0,
        max_stream_chars: int = MAX_STREAM_CHARS,
        terminate_grace: float = TERMINATE_GRACE_S,
        kill_grace: float = KILL_GRACE_S,
    ) -> None:
        self.exe = str(exe) if exe is not None else None
        self.scripts_dir = Path(scripts_dir) if scripts_dir else DEFAULT_SCRIPTS_DIR
        self.dispatch_script = self.scripts_dir / DISPATCH_SCRIPT
        self.timeout = timeout
        self.max_stream_chars = int(max_stream_chars)
        self.terminate_grace = float(terminate_grace)
        self.kill_grace = float(kill_grace)
        self._last_result: RunnerResult | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ exe

    def resolve_exe(self) -> str:
        """Return a usable Blender executable path or raise BlenderNotFoundError.

        Detection is cached (see :func:`blender.controller.detect_blender`), so
        this does not re-probe the filesystem or re-run ``blender --version``
        for every operation. The cached answer is re-validated cheaply: if the
        executable is no longer a file on disk the cache is invalidated and
        detection runs again.
        """
        if self.exe:
            if Path(self.exe).is_file():
                return self.exe
            raise BlenderNotFoundError(
                message=f"blender executable not found at configured path: {self.exe}",
            )
        from .controller import detect_blender

        info = detect_blender()
        if not info.installed or not info.path:
            detail = "; ".join(info.errors) or "not installed"
            raise BlenderNotFoundError(
                message=f"blender not detected on this machine ({detail})",
            )
        return info.path

    # ------------------------------------------------------------- commands

    def build_command(
        self,
        request: dict,
        request_file: Path | str,
        output_file: Path | str,
        scene_file: Path | str | None = None,
    ) -> list[str]:
        """Construct the exact command vector for one Blender invocation."""
        cmd = [self.resolve_exe()]
        if scene_file and Path(scene_file).is_file():
            cmd.append(str(Path(scene_file).resolve()))
        cmd += [
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(self.dispatch_script.resolve()),
            "--",
            str(Path(request_file).resolve()),
            str(Path(output_file).resolve()),
        ]
        return cmd

    # ------------------------------------------------------------- execution

    def _execute(self, cmd: list[str], timeout: float):
        """Spawn and reap one Blender process. Override point for tests.

        Returns a :class:`subprocess.CompletedProcess`-compatible object. The
        real implementation is the same bounded, supervised machinery used for
        cancellable work; with no cancellation token it simply waits for exit.
        """
        return self._spawn_and_collect(cmd, timeout=timeout, cancel_token=None)

    def _execute_supervised(self, cmd: list[str], timeout: float, cancel_token):
        """Spawn and supervise one Blender process, honouring cancellation.

        Raises :class:`~blender.errors.BlenderCancelledError` when the request is
        cancelled, after terminating and reaping the child.
        """
        return self._spawn_and_collect(cmd, timeout=timeout, cancel_token=cancel_token)

    def _spawn_and_collect(self, cmd: list[str], timeout: float, cancel_token, operation: str = ""):
        """Start the child, supervise it, and return its completed outcome.

        Raises a typed :class:`~blender.errors.BlenderError` for every failure
        mode: spawn, cancellation, and timeout.
        """
        if _cancelled(cancel_token):
            raise BlenderCancelledError(
                operation=operation,
                message="request cancelled before blender started",
                command=cmd,
            )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise _spawn_error(exc, operation, cmd) from exc

        out = _BoundedStream(self.max_stream_chars)
        err = _BoundedStream(self.max_stream_chars)
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + max(0.0, float(timeout))
        cancelled = False
        timed_out = False
        reaped = True
        try:
            while True:
                if _cancelled(cancel_token):
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                try:
                    proc.wait(timeout=POLL_INTERVAL_S)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if cancelled or timed_out:
                reaped = self._stop(proc, operation)
        finally:
            # Reap: never leave a child running, whatever happened above.
            for reader in readers:
                reader.join(timeout=self.kill_grace + 1.0)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (ValueError, OSError):
                        pass

        stdout, stderr = out.text(), err.text()
        if cancelled:
            # Never claim a termination that was not confirmed. The ratified
            # contract requires the distinct `cancellation_pending` code and
            # cancellation_terminated=false when the child survives.
            raise BlenderCancelledError(
                operation=operation,
                message=(
                    "blender terminated because the request was cancelled"
                    if reaped else
                    "request cancelled but the blender process could not be terminated"
                ),
                command=cmd,
                return_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                code=BlenderCancelledError.code if reaped else C_CANCELLATION_PENDING,
                diagnostics={
                    "terminated": reaped,
                    "cancellation_terminated": reaped,
                    "stdout_truncated": out.truncated,
                    "stderr_truncated": err.truncated,
                },
            )
        if timed_out:
            raise BlenderTimeoutError(
                operation=operation,
                message=f"blender did not finish within {timeout}s",
                command=cmd,
                return_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                diagnostics={
                    "terminated": reaped,
                    "stdout_truncated": out.truncated,
                    "stderr_truncated": err.truncated,
                },
            )
        completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        # Truncation state travels with the outcome so run() never has to infer it.
        completed.stdout_truncated = out.truncated
        completed.stderr_truncated = err.truncated
        return completed

    def _stop(self, proc, operation: str) -> bool:
        """Terminate then kill the child process tree, both bounded, and reap it.

        Returns ``True`` only when the child is *confirmed* dead. A graceful
        terminate is attempted first so Blender can shut down; if it has not
        exited within the grace window it is killed. Either way the child is
        waited on so no orphan is left silently behind.

        On Windows, uses ``taskkill /T /F /PID`` to terminate the entire
        process tree, which is necessary because Blender may spawn child
        processes (e.g. via ``execute_script``).
        """
        if proc.poll() is not None:
            return True
        log.warning("terminating blender (pid=%s) for op %s", proc.pid, operation)

        # First try: use taskkill to kill the entire process tree (Windows)
        if sys.platform == "win32":
            try:
                log.debug("using taskkill /T /PID %d", proc.pid)
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=self.kill_grace,
                )
                # Wait a moment for reap
                try:
                    proc.wait(timeout=1.0)
                    return True
                except subprocess.TimeoutExpired:
                    pass
            except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
                pass

        # Fallback: standard terminate/kill (works on Unix, limited on Windows)
        log.warning("falling back to standard terminate/kill for blender operation (pid=%s)", proc.pid)
        for action, grace in (("terminate", self.terminate_grace), ("kill", self.kill_grace)):
            try:
                getattr(proc, action)()
            except (OSError, ValueError, ProcessLookupError, AttributeError):
                pass
            try:
                proc.wait(timeout=grace)
                return True
            except subprocess.TimeoutExpired:
                continue
        # Last resort: block on reap so we never return with a live child.
        try:
            proc.wait(timeout=self.kill_grace)
            return True
        except subprocess.TimeoutExpired:
            log.error("blender (pid=%s) survived kill for op %s", proc.pid, operation)
            return False

    def run(
        self,
        request: dict,
        timeout: float | None = None,
        scene_file: Path | str | None = None,
        cancel_token=None,
    ) -> RunnerResult:
        """Run one request; raises BlenderError subclasses for infra failures."""
        operation = request.get("operation", "")
        request_id = request.get("request_id", "")
        timeout = timeout or self.timeout
        started = time.perf_counter()
        # Never let a previous request's result masquerade as this one's.
        with self._lock:
            self._last_result = None

        with tempfile.TemporaryDirectory(prefix="diablo_blender_") as tmp:
            tmp_path = Path(tmp)
            req_file = tmp_path / "request.json"
            out_file = tmp_path / "result.json"
            req_file.write_text(
                json.dumps(request), encoding="utf-8",
            )
            cmd = self.build_command(request, req_file, out_file, scene_file)

            try:
                if cancel_token is not None:
                    proc = self._execute_supervised(cmd, timeout=timeout, cancel_token=cancel_token)
                else:
                    proc = self._execute(cmd, timeout=timeout)
            except BlenderError as exc:
                # The spawn/cancel path does not know the operation name; the
                # request does. Attribute the failure to *this* request before it
                # propagates, so a reused exception cannot claim another id.
                exc.operation = exc.operation or operation
                exc.request_id = request_id
                raise
            except subprocess.TimeoutExpired as exc:
                log.warning("blender timed out after %ss: %s", timeout, operation)
                raise BlenderTimeoutError(
                    operation=operation,
                    message=f"blender did not finish within {timeout}s",
                    command=cmd,
                    stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                    stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                    timeout=timeout,
                    request_id=request_id,
                ) from exc

            duration_s = time.perf_counter() - started
            stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
            stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
            rc = getattr(proc, "returncode", 0)
            stdout_truncated = bool(getattr(proc, "stdout_truncated", False))
            stderr_truncated = bool(getattr(proc, "stderr_truncated", False))

            if rc != 0:
                log.warning("blender exited rc=%s for op %s", rc, operation)
                raise BlenderProcessError(
                    operation=operation,
                    message=f"blender exited with code {rc}",
                    command=cmd,
                    return_code=rc,
                    stdout=stdout,
                    stderr=stderr,
                    request_id=request_id,
                    diagnostics={"stdout_truncated": stdout_truncated,
                                "stderr_truncated": stderr_truncated},
                )

            if not out_file.is_file():
                raise BlenderProtocolError(
                    operation=operation,
                    message="blender produced no result file",
                    command=cmd,
                    stdout=stdout,
                    stderr=stderr,
                    request_id=request_id,
                )

            try:
                payload = parse_result(out_file.read_text(encoding="utf-8"), operation)
            except ValueError as exc:
                raise BlenderProtocolError(
                    operation=operation,
                    message=str(exc),
                    command=cmd,
                    stdout=stdout,
                    stderr=stderr,
                    request_id=request_id,
                ) from exc

            result = RunnerResult(
                success=bool(payload["success"]),
                operation=payload.get("operation", operation),
                data=payload.get("data") if payload.get("success") else None,
                error_code="",
                error_message="",
                stdout=stdout,
                stderr=stderr,
                return_code=rc,
                request_id=payload.get("request_id") or request_id,
                duration_s=duration_s,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
            if not result.success:
                error = payload.get("error") or {}
                result.error_code = error.get("error") or error.get("code") or ""
                result.error_message = error.get("message") or "blender script failed"
            with self._lock:
                self._last_result = result
            return result

    @property
    def last_result(self) -> RunnerResult | None:
        """The most recent result, or ``None`` if the last run did not produce one.

        Cleared at the start of every run, so a caller can never read a previous
        request's result as if it belonged to the current one.
        """
        return self._last_result


def _cancelled(token) -> bool:
    """Whether a cancellation token reports cancellation, tolerating any token."""
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - a broken token must not mask the run
            return False
    return bool(token)
