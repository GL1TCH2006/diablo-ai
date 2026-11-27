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
        request_id: str = "",
        diagnostics: dict | None = None,
    ) -> None:
        self.operation = operation or ""
        self.message = message or str(self)
        self.command = list(command) if command else None
        self.return_code = return_code
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        self.timeout = timeout
        self.code = code or self.default_code
        # Diagnostic attribution only. This is never an authoritative identity:
        # S1 owns request_id, and CapabilityResult assigns it from the request.
        # It is populated exclusively from the trusted correlation scope so a
        # failure can be traced back to the call that produced it.
        self.request_id = request_id or ""
        self.diagnostics = dict(diagnostics or {})
        super().__init__(self.message)

    def to_dict(self, max_stream_chars: int = 4000) -> dict:
        """JSON-safe structured failure description."""
        payload = {
            "error": self.code,
            "operation": self.operation,
            "message": self.message,
            "command": self.command,
            "return_code": self.return_code,
            "timeout": self.timeout,
            "stdout": self.stdout[:max_stream_chars],
            "stderr": self.stderr[:max_stream_chars],
        }
        if self.request_id:
            # Attribution, not authority. See the note on self.request_id.
            payload["request_id"] = self.request_id
        if self.diagnostics:
            payload["diagnostics"] = dict(self.diagnostics)
        return payload

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


class BlenderSpawnError(BlenderError):
    """The Blender child process could not be created at all.

    Raised instead of letting a raw ``OSError`` from ``Popen`` escape. Covers
    the generic spawn failure that is neither a missing executable nor a
    permission problem (too many handles, bad working directory, ...).
    """

    code = "blender_spawn_error"
    default_code = "blender_spawn_error"


class BlenderPermissionError(BlenderError):
    """The Blender executable or its working directory is not accessible."""

    code = "blender_permission_denied"
    default_code = "blender_permission_denied"


class BlenderTimeoutError(BlenderError):
    """A Blender subprocess exceeded its configured timeout."""

    code = "blender_timeout"
    default_code = "blender_timeout"


class BlenderCancelledError(BlenderError):
    """A Blender subprocess was terminated because its request was cancelled.

    Distinct from a timeout on purpose: cancellation must never be reported as a
    generic failure, and the capability boundary maps this code to ``CANCELLED``.

    When the child could **not** be confirmed dead, the code becomes
    :data:`C_CANCELLATION_PENDING` instead, because S2 must never claim a
    termination it did not achieve.
    """

    code = "blender_cancelled"
    default_code = "blender_cancelled"

    @property
    def terminated(self) -> bool:
        """Whether the child was confirmed dead. Never assumed."""
        return bool(self.diagnostics.get("cancellation_terminated", False))


class BlenderClosedError(BlenderError):
    """An operation was attempted on a layer that has been closed.

    A lifecycle fault rather than caller-attributable invalid input, so it maps
    to ``FAILURE`` and never to ``INVALID_REQUEST``.
    """

    code = "blender_closed"
    default_code = "blender_closed"


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

#: Reported when a cancellation arrived but the child could not be confirmed
#: terminated. The ratified contract requires this exact code plus
#: ``metadata.cancellation_terminated = false``; S2 must not claim a termination
#: it did not achieve, and must not report SUCCESS.
C_CANCELLATION_PENDING = "cancellation_pending"

#: Every stable error code this subsystem can produce. The capability boundary
#: maps these to statuses by exact code, never by inspecting message text, so an
#: infrastructure failure can never be misreported as invalid caller input.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "blender_error",
        "blender_not_found",
        "blender_spawn_error",
        "blender_permission_denied",
        "blender_timeout",
        "blender_cancelled",
        "blender_closed",
        "blender_process_error",
        "blender_script_error",
        "blender_protocol_error",
        C_UNKNOWN_OPERATION,
        C_RUNNER_FAILURE,
        C_CANCELLATION_PENDING,
    }
)


def condense(exc: BlenderError) -> str:
    """Short single-line summary used for log messages."""
    return f"{exc.code}: {exc.message[:160]}"