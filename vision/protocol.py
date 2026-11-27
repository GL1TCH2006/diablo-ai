from __future__ import annotations

"""JSON request/response protocol for vision analysis.

Request shape::

    {
        "request_id": "hex",
        "operation": "describe",
        "image_path": "/path/to/image.png",
        "params": {"detail_level": "medium", "focus": "objects"},
        "context": {"source": "blender_render", "scene_name": "Scene"}
    }

Result shape::

    {
        "success": true,
        "operation": "describe",
        "data": {
            "description": "A red cube on a gray plane...",
            "objects": [{"name": "cube", "color": "red", "position": "center"}],
            "scene_summary": "Simple studio setup with one object"
        },
        "error": null,
        "request_id": "hex",
        "backend": "fallback",
        "duration_ms": 12
    }

Failed result shape::

    {
        "success": false,
        "operation": "describe",
        "data": null,
        "error": {"code": "vision_backend_error", "message": "...", "details": {}},
        "request_id": "hex",
        "backend": "fallback",
        "duration_ms": 5
    }
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


# Operations recognised by vision backends.
SUPPORTED_OPERATIONS = frozenset(
    {
        "describe",
        "detect_objects",
        "compare",
        "extract_text",
        "analyze_ui",
        "inspect_render",
    }
)


@dataclass(slots=True)
class VisionRequest:
    """Structured vision analysis request."""

    operation: str
    image_path: str
    params: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self) -> None:
        if self.operation not in SUPPORTED_OPERATIONS:
            raise ValueError(f"unsupported operation: {self.operation}")
        if not self.image_path:
            raise ValueError("image_path is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation": self.operation,
            "image_path": self.image_path,
            "params": dict(self.params),
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VisionRequest":
        return cls(
            request_id=data.get("request_id", uuid.uuid4().hex),
            operation=data["operation"],
            image_path=data["image_path"],
            params=data.get("params", {}),
            context=data.get("context", {}),
        )


@dataclass(slots=True)
class VisionResult:
    """Structured vision analysis result."""

    success: bool
    operation: str
    data: dict | None = None
    error: dict | None = None
    request_id: str = ""
    backend: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "operation": self.operation,
            "data": self.data,
            "error": self.error,
            "request_id": self.request_id,
            "backend": self.backend,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def success_result(
        cls,
        operation: str,
        data: dict,
        request_id: str = "",
        backend: str = "",
        duration_ms: float = 0.0,
    ) -> "VisionResult":
        return cls(
            success=True,
            operation=operation,
            data=data,
            error=None,
            request_id=request_id,
            backend=backend,
            duration_ms=duration_ms,
        )

    @classmethod
    def failure_result(
        cls,
        operation: str,
        error: dict,
        request_id: str = "",
        backend: str = "",
        duration_ms: float = 0.0,
    ) -> "VisionResult":
        return cls(
            success=False,
            operation=operation,
            data=None,
            error=error,
            request_id=request_id,
            backend=backend,
            duration_ms=duration_ms,
        )


def build_request(
    operation: str,
    image_path: str,
    params: dict | None = None,
    context: dict | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe request dictionary for one vision operation."""
    if operation not in SUPPORTED_OPERATIONS:
        raise ValueError(f"unsupported operation: {operation}")
    payload: dict[str, Any] = {
        "request_id": request_id or uuid.uuid4().hex,
        "operation": operation,
        "image_path": image_path,
        "params": dict(params) if params else {},
        "context": dict(context) if context else {},
    }
    return payload


def parse_result(text: str, operation: str = "") -> dict:
    """Parse raw result text into a validated result dict."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        fragment = (text or "")[:200].replace("\n", " ")
        raise ValueError(f"malformed vision result JSON ({exc}): {fragment}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"vision result is not an object: {type(payload).__name__}")
    if "success" not in payload:
        raise ValueError("vision result is missing the 'success' field")
    if operation and payload.get("operation", "") != operation:
        echoed = payload.get("operation", "")
        if echoed not in ("", operation):
            raise ValueError(f"operation mismatch: expected {operation!r}, got {echoed!r}")
    return payload


def to_json_safe(value: Any) -> Any:
    """Recursively coerce values into JSON-safe primitives."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)