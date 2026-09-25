from __future__ import annotations

"""Structured, JSON-serializable vision errors.

Every error carries the operation, the image path (when applicable), and
captured context so failures can be surfaced to a caller without losing
diagnostic information.
"""

from typing import Any


class VisionError(Exception):
    """Base class for all Diablo vision subsystem errors."""

    code: str = "vision_error"
    default_code: str = "vision_error"

    def __init__(
        self,
        operation: str = "",
        message: str = "",
        *,
        image_path: str | None = None,
        backend: str | None = None,
        code: str = "",
        details: dict | None = None,
    ) -> None:
        self.operation = operation or ""
        self.message = message or str(self)
        self.image_path = image_path
        self.backend = backend
        self.code = code or self.default_code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe structured failure description."""
        return {
            "error": self.code,
            "operation": self.operation,
            "message": self.message,
            "image_path": self.image_path,
            "backend": self.backend,
            "details": self.details,
        }

    def __str__(self) -> str:
        parts = [self.code]
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.message:
            parts.append(self.message)
        if self.image_path:
            parts.append(f"image={self.image_path}")
        if self.backend:
            parts.append(f"backend={self.backend}")
        return " | ".join(parts)


class VisionBackendNotFoundError(VisionError):
    """No suitable vision backend available."""

    code = "vision_backend_not_found"
    default_code = "vision_backend_not_found"


class VisionBackendError(VisionError):
    """A vision backend raised an error during inference."""

    code = "vision_backend_error"
    default_code = "vision_backend_error"


class VisionImageError(VisionError):
    """Image could not be loaded, is corrupt, or has unsupported format."""

    code = "vision_image_error"
    default_code = "vision_image_error"


class VisionModelError(VisionError):
    """Model-specific error (missing weights, incompatible architecture, etc.)."""

    code = "vision_model_error"
    default_code = "vision_model_error"


class VisionUnsupportedOperationError(VisionError):
    """Requested operation not supported by any available backend."""

    code = "vision_unsupported_operation"
    default_code = "vision_unsupported_operation"


def condense(exc: VisionError) -> str:
    """Short single-line summary used for log messages."""
    return f"{exc.code}: {exc.message[:160]}"