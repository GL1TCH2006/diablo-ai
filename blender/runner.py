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

The runner is kept subprocess-clean so it can be tested without Blender being
installed: ``_execute`` is the only place a real child process is spawned.
"""

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_setup import get_logger

from .errors import (
    BlenderNotFoundError,
    BlenderProcessError,
    BlenderProtocolError,
    BlenderTimeoutError,
)
from .protocol import parse_result

log = get_logger("blender.runner")

#: Blender-side dispatch script shipped with this package.
DEFAULT_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
DISPATCH_SCRIPT = "runner.py"


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


class BlenderRunner:
    """Executes one JSON request in a background Blender subprocess."""

    def __init__(
        self,
        exe: str | Path | None = None,
        scripts_dir: Path | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.exe = str(exe) if exe is not None else None
        self.scripts_dir = Path(scripts_dir) if scripts_dir else DEFAULT_SCRIPTS_DIR
        self.dispatch_script = self.scripts_dir / DISPATCH_SCRIPT
        self.timeout = timeout
        self._last_result: RunnerResult | None = None

    # ------------------------------------------------------------------ exe

    def resolve_exe(self) -> str:
        """Return a usable Blender executable path or raise BlenderNotFoundError."""
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
        """Spawn the subprocess. Override point for tests (never needs Blender)."""
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

    def run(
        self,
        request: dict,
        timeout: float | None = None,
        scene_file: Path | str | None = None,
    ) -> RunnerResult:
        """Run one request; raises BlenderError subclasses for infra failures."""
        operation = request.get("operation", "")
        request_id = request.get("request_id", "")
        timeout = timeout or self.timeout
        started = time.perf_counter()

        with tempfile.TemporaryDirectory(prefix="diablo_blender_") as tmp:
            tmp_path = Path(tmp)
            req_file = tmp_path / "request.json"
            out_file = tmp_path / "result.json"
            req_file.write_text(
                json.dumps(request), encoding="utf-8",
            )
            cmd = self.build_command(request, req_file, out_file, scene_file)

            try:
                proc = self._execute(cmd, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                log.warning("blender timed out after %ss: %s", timeout, operation)
                raise BlenderTimeoutError(
                    operation=operation,
                    message=f"blender did not finish within {timeout}s",
                    command=cmd,
                    stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                    stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                    timeout=timeout,
                ) from exc

            duration_s = time.perf_counter() - started
            stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
            stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
            rc = getattr(proc, "returncode", 0)

            if rc != 0:
                log.warning("blender exited rc=%s for op %s", rc, operation)
                raise BlenderProcessError(
                    operation=operation,
                    message=f"blender exited with code {rc}",
                    command=cmd,
                    return_code=rc,
                    stdout=stdout,
                    stderr=stderr,
                )

            if not out_file.is_file():
                raise BlenderProtocolError(
                    operation=operation,
                    message="blender produced no result file",
                    command=cmd,
                    stdout=stdout,
                    stderr=stderr,
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
            )
            if not result.success:
                error = payload.get("error") or {}
                result.error_code = error.get("error") or error.get("code") or ""
                result.error_message = error.get("message") or "blender script failed"
            self._last_result = result
            return result

    @property
    def last_result(self) -> RunnerResult | None:
        return self._last_result