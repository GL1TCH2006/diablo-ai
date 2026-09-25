from __future__ import annotations

"""Structured, JSON-serializable Blender errors.

Every error carries the operation, the exact command that was run (when a
subprocess was involved), the process return code, and captured stdout/stderr
so failures can be surfaced to a caller without losing diagnostic context.

The capability layer records the last error via ``to_dict()`` so it can also be
returned as structured data through ``last_error()``.
"""


class BlenderError(Exception):
    """Base class for all Diablo Blender subsystem errors."""

    code: str = "blender_error"
    default_code: str = "blender_error"

    def __init__(
        self,
        operation: str = "",
        message: str = "",
        *,
        command=None,
        return_code=None,
        stdout: str = "",
        stderr: str = "",
        code: str = "",
        timeout: float | None = None,
    ) -> None:
        self.operation = operation or ""
        self.message = message or str(self)
        self.command = list(command) if command else None
        self.return_code = return_code
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        self.timeout = timeout
        self.code = code or self.default_code
        super().__init__(self.message)

    def to_dict(self, max_stream_chars: int = 4000) -> dict:
        """JSON-safe structured failure description."""
        return {
            "error": self.code,
            "operation": self.operation,
            "message": self.message,
            "command": self.command,
            "return_code": self.return_code,
            "timeout": self.timeout,
            "stdout": self.stdout[:max_stream_chars],
            "stderr": self.stderr[:max_stream_chars],
        }

    def __str__(self) -> str:
        parts = [self.code]
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.message:
            parts.append(self.message)
        if self.return_code is not None:
            parts.append(f"rc={self.return_code}")
        return " | ".join(parts)


class BlenderNotFoundError(BlenderError):
    """Blender executable could not be located."""

    code = "blender_not_found"
    default_code = "blender_not_found"


class BlenderTimeoutError(BlenderError):
    """A Blender subprocess exceeded its configured timeout."""

    code = "blender_timeout"
    default_code = "blender_timeout"


class BlenderProcessError(BlenderError):
    """The Blender subprocess exited with a non-zero return code."""

    code = "blender_process_error"
    default_code = "blender_process_error"


class BlenderScriptError(BlenderError):
    """A Blender Python script reported a failure inside the result JSON."""

    code = "blender_script_error"
    default_code = "blender_script_error"


class BlenderProtocolError(BlenderError):
    """The Blender subprocess produced output that could not be parsed."""

    code = "blender_protocol_error"
    default_code = "blender_protocol_error"


# Error codes emitted by the Blender-side dispatch runner.
C_UNKNOWN_OPERATION = "unknown_operation"
C_SCRIPT_ERROR = "blender_script_error"  # same as BlenderScriptError.code
C_RUNNER_FAILURE = "runner_failure"


def condense(exc: BlenderError) -> str:
    """Short single-line summary used for log messages."""
    return f"{exc.code}: {exc.message[:160]}"