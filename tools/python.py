from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.cancellation import cancellation_requested
from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("python_tool")

# Bounded output handling (mirroring BlenderRunner pattern)
MAX_STREAM_CHARS = 64 * 1024
POLL_INTERVAL_S = 0.05
TERMINATE_GRACE_S = 5.0
KILL_GRACE_S = 5.0
_READ_CHUNK = 65536


class _BoundedStream:
    """Retains a bounded prefix of a stream while draining it to EOF."""

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


def _stop_process(proc: subprocess.Popen, operation: str) -> bool:
    """Terminate then kill the child, both bounded, and reap it.

    Returns ``True`` only when the child is *confirmed* dead.
    """
    if proc.poll() is not None:
        return True
    log.warning("terminating python script (pid=%s) for %s", proc.pid, operation)
    for action, grace in (("terminate", TERMINATE_GRACE_S), ("kill", KILL_GRACE_S)):
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
        proc.wait(timeout=KILL_GRACE_S)
        return True
    except subprocess.TimeoutExpired:
        log.error("python script (pid=%s) survived kill for %s", proc.pid, operation)
        return False


def run_script(
    script: str,
    args: list | None = None,
    timeout: int = 120,
    _tool_context: Any = None,
) -> ToolResult:
    """Run a Python script with the Diablo interpreter.

    IMPORTANT: subprocess execution is NOT a security sandbox. A Python
    script has the same privileges as Diablo. Access is controlled by the
    permission system and working-directory restrictions, not by isolation.

    Cancellation is supported via the ``_tool_context`` parameter which
    carries a cancellation token. When cancellation is requested, the
    script process is terminated (with a bounded kill fallback) and reaped.
    """
    target = Path(script)
    if not target.exists():
        return ToolResult(False, error=f"Script not found: {target}")
    if not target.is_file():
        return ToolResult(False, error=f"Not a file: {target}")

    # Check cancellation before starting
    cancel_token = getattr(_tool_context, "cancellation_token", None) if _tool_context else None
    if cancellation_requested(cancel_token):
        return ToolResult(False, error="Cancelled before script started.", cancelled=True)

    argv = [sys.executable, str(target)] + (list(args) if args else [])
    cwd = str(target.parent)
    deadline = time.monotonic() + max(0.0, float(timeout))
    operation = f"run_script:{target.name}"

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return ToolResult(False, error=f"Failed to start script: {exc}")

    out = _BoundedStream()
    err = _BoundedStream()
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    cancelled = False
    reaped = True
    try:
        while True:
            if cancellation_requested(cancel_token):
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
            reaped = _stop_process(proc, operation)
    finally:
        # Reap: never leave a child running, whatever happened above.
        for reader in readers:
            reader.join(timeout=KILL_GRACE_S + 1.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (ValueError, OSError):
                    pass

    stdout = out.text()
    stderr = err.text()

    if cancelled:
        return ToolResult(
            False,
            output=stdout,
            error=stderr or "Script cancelled.",
            cancelled=True,
            exit_code=proc.returncode,
        )
    if timed_out:
        return ToolResult(
            False,
            output=stdout,
            error=stderr or f"Script timed out after {timeout}s.",
            exit_code=proc.returncode,
        )

    output = (stdout or "").strip()
    stderr_stripped = (stderr or "").strip()
    if proc.returncode != 0:
        return ToolResult(False, output=output, error=stderr_stripped or f"Exit code {proc.returncode}.", exit_code=proc.returncode)
    return ToolResult(True, output=output, exit_code=0)