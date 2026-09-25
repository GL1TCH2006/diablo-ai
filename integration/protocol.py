from __future__ import annotations

"""Protocols and result envelopes for Blender–Vision integration."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from vision.protocol import to_json_safe


class BlenderLayerProtocol(Protocol):
    """Structural interface required from the S2.1 Blender layer."""

    def render(self, path, **kwargs) -> dict:
        ...

    def last_error(self) -> dict | None:
        ...

    def close(self) -> None:
        ...


class VisionLayerProtocol(Protocol):
    """Structural interface required from the S2.2 Vision layer."""

    def validate_image(self, image_path) -> dict:
        ...

    def inspect_render(self, image_path, context=None) -> dict:
        ...

    def describe(self, image_path, context=None, **kwargs) -> dict:
        ...

    def compare(self, image_path, reference_image, context=None, **kwargs) -> dict:
        ...

    def last_error(self) -> dict | None:
        ...


@dataclass(slots=True)
class IntegrationResult:
    """Structured result returned by successful integration workflows."""

    success: bool
    operation: str
    data: dict | None = None
    error: dict | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe result envelope."""
        return to_json_safe(
            {
                "success": self.success,
                "operation": self.operation,
                "data": self.data,
                "error": self.error,
                "request_id": self.request_id,
                "duration_ms": self.duration_ms,
            }
        )

    @classmethod
    def success_result(
        cls,
        operation: str,
        data: dict,
        request_id: str = "",
        duration_ms: float = 0.0,
    ) -> "IntegrationResult":
        return cls(
            success=True,
            operation=operation,
            data=data,
            error=None,
            request_id=request_id,
            duration_ms=duration_ms,
        )

    @classmethod
    def failure_result(
        cls,
        operation: str,
        error: dict,
        request_id: str = "",
        duration_ms: float = 0.0,
    ) -> "IntegrationResult":
        return cls(
            success=False,
            operation=operation,
            data=None,
            error=error,
            request_id=request_id,
            duration_ms=duration_ms,
        )


def elapsed_ms(started: float) -> float:
    """Return elapsed milliseconds for a workflow."""
    return (time.perf_counter() - started) * 1000
