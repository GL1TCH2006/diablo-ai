from __future__ import annotations

"""JSON request/response protocol between Diablo and Blender.

Requests produced here are flat JSON dictionaries. The Blender-side dispatch
runner (``blender/scripts/runner.py``) reads the request file, executes the
operation, and writes a result file of the same shape.

Request shape::

    {
        "request_id": "hex",
        "operation": "create_object",
        "params": {"primitive": "cube", "location": [0, 0, 0], ...},
        "_session": {"open_file": "...", "save_file": "..."}   # internal only
    }

Result shape::

    {
        "success": true,
        "operation": "create_object",
        "data": {...},
        "error": null,
        "request_id": "hex"
    }

Failed result shape::

    {
        "success": false,
        "operation": "create_object",
        "data": null,
        "error": {"code": "blender_script_error", "message": "...", "traceback": "..."},
        "request_id": "hex"
    }

Transform convention: location/scale are ``[x, y, z]`` floats in Blender units
(metres by default). ``rotation`` is ``[x, y, z]`` **degrees** (Euler XYZ) for
human/AI predictability; the Blender-side ops convert to radians internally.
"""

import json
import uuid
from typing import Any

from core.logging_setup import get_logger

log = get_logger("blender.protocol")

#: Operations recognised by the Blender-side dispatcher.
SUPPORTED_OPERATIONS = frozenset(
    {
        "inspect_scene",
        "list_objects",
        "inspect_object",
        "create_object",
        "delete_object",
        "modify_object",
        "transform_object",
        "create_material",
        "assign_material",
        "configure_camera",
        "configure_light",
        "execute_script",
        "save",
        "export",
        "render",
    }
)


def new_request_id() -> str:
    return uuid.uuid4().hex


def build_request(
    operation: str,
    params: dict | None = None,
    request_id: str | None = None,
    session: dict | None = None,
) -> dict:
    """Build a JSON-safe request dictionary for one Blender operation."""
    if operation not in SUPPORTED_OPERATIONS:
        log.warning("unknown blob op requested: %s", operation)
    payload: dict[str, Any] = {
        "request_id": request_id or new_request_id(),
        "operation": operation,
        "params": dict(params) if params else {},
    }
    if session:
        payload["_session"] = dict(session)
    return payload


def validate_result(payload: dict, operation: str = "") -> dict:
    """Structural validation of a parsed result; raises ValueError on failure."""
    if not isinstance(payload, dict):
        raise ValueError(f"blender result is not an object: {type(payload).__name__}")
    if "success" not in payload:
        raise ValueError("blender result is missing the 'success' field")
    if operation and payload.get("operation", "") != operation:
        # Blender may echo "" for runner-level failures; a mismatch is only fatal
        # when the echoed operation is non-empty and different.
        echoed = payload.get("operation", "")
        if echoed not in ("", operation):
            raise ValueError(f"operation mismatch: expected {operation!r}, got {echoed!r}")
    return payload


def parse_result(text: str, operation: str = "") -> dict:
    """Parse raw result text into a validated result dict."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        fragment = (text or "")[:200].replace("\n", " ")
        raise ValueError(f"malformed blender result JSON ({exc}): {fragment}") from exc
    return validate_result(payload, operation)


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
        return float(value)  # e.g. numpy scalars, mathutils components
    except (TypeError, ValueError):
        return str(value)


def vec3(value) -> list[float]:
    """Coerce an iterable into a 3-float list (padded/truncated as needed)."""
    if value is None:
        return [0.0, 0.0, 0.0]
    out = [float(v) for v in value]
    while len(out) < 3:
        out.append(0.0)
    return out[:3]