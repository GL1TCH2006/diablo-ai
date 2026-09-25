from __future__ import annotations

"""Structured errors for the Blender–Vision integration layer."""

from typing import Any


class BlenderVisionError(Exception):
    """Base class for integration failures with stage-aware diagnostics."""

    code = "blender_vision_error"
    default_code = "blender_vision_error"

    def __init__(
        self,
        operation: str = "",
        message: str = "",
        *,
        stage: str = "",
        source_error: dict | None = None,
        source_last_error: dict | None = None,
        details: dict | None = None,
        cause: BaseException | None = None,
        code: str = "",
    ) -> None:
        self.operation = operation or ""
        self.stage = stage or ""
        self.message = message or str(self)
        self.source_error = dict(source_error) if isinstance(source_error, dict) else source_error
        self.source_last_error = (
            dict(source_last_error) if isinstance(source_last_error, dict) else source_last_error
        )
        self.details = dict(details) if isinstance(details, dict) else (details or {})
        self.cause = cause
        self.code = code or self.default_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe structured error description."""
        return {
            "error": self.code,
            "operation": self.operation,
            "stage": self.stage,
            "message": self.message,
            "details": self.details,
            "source_error": self.source_error,
            "source_last_error": self.source_last_error,
        }

    def __str__(self) -> str:
        parts = [self.code]
        if self.stage:
            parts.append(f"stage={self.stage}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.message:
            parts.append(self.message)
        return " | ".join(parts)


class BlenderStageError(BlenderVisionError):
    """Base class for failures in the Blender stage."""

    code = "blender_stage_error"
    default_code = "blender_stage_error"


class BlenderUnavailableError(BlenderStageError):
    """Blender could not be resolved or launched."""

    code = "blender_unavailable"
    default_code = "blender_unavailable"


class RenderFailureError(BlenderStageError):
    """Blender did not produce a usable render result."""

    code = "blender_render_failed"
    default_code = "blender_render_failed"


class VisionStageError(BlenderVisionError):
    """Base class for failures in the Vision stage."""

    code = "vision_stage_error"
    default_code = "vision_stage_error"


class InvalidRenderError(VisionStageError):
    """The render path is missing, unreadable, or not a supported image."""

    code = "invalid_render"
    default_code = "invalid_render"


class ComparisonError(VisionStageError):
    """Two render artifacts could not be compared."""

    code = "comparison_failed"
    default_code = "comparison_failed"


class VisionBackendUnavailableError(VisionStageError):
    """No usable Vision backend was available for an operation."""

    code = "vision_backend_unavailable"
    default_code = "vision_backend_unavailable"


class VisionOperationUnsupportedError(VisionStageError):
    """The selected Vision backend does not support an operation."""

    code = "vision_operation_unsupported"
    default_code = "vision_operation_unsupported"
