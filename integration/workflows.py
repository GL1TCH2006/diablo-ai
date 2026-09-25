from __future__ import annotations

"""Production Blender and Vision workflow composition."""

import inspect as py_inspect
import json
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from vision.protocol import to_json_safe

from .capabilities import BlenderVisionIntegration
from .contracts import CapabilityCancellationError, CapabilityRequest, CapabilityResult, CapabilityStatus
from .protocol import IntegrationResult


class WorkflowStatus(str, Enum):
    SUCCESS = "success"
    COMPLETED = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    PERMISSION_REQUIRED = "permission_required"

    @classmethod
    def from_value(cls, value: "WorkflowStatus | str") -> "WorkflowStatus":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {"completed": "success", "complete": "success", "done": "success", "failed": "failure", "error": "failure", "canceled": "cancelled", "permission": "permission_required"}
        normalized = aliases.get(normalized, normalized)
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"unknown workflow status: {value!r}") from exc


class WorkflowError(Exception):
    code = "workflow_error"
    default_code = "workflow_error"

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        stage: str = "",
        code: str = "",
        details: dict | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.message = message or type(self).__name__
        self.operation = operation
        self.stage = stage
        self.code = code or self.default_code
        self.details = dict(details or {})
        self.cause = cause
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(
            {
                "error": self.code,
                "operation": self.operation,
                "stage": self.stage,
                "message": self.message,
                "details": self.details,
            }
        )


class WorkflowValidationError(WorkflowError):
    code = "invalid_workflow"
    default_code = "invalid_workflow"


class _WorkflowCancelled(Exception):
    pass


_NO_VERIFIER = object()


@dataclass(slots=True)
class WorkflowResult:
    status: WorkflowStatus
    operation: str
    data: dict | None = None
    error: dict | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = WorkflowStatus.from_value(self.status)
        if self.data is not None and not isinstance(self.data, dict):
            self.data = {"value": self.data}
        if self.error is not None and not isinstance(self.error, dict):
            self.error = {"message": str(self.error)}
        self.metadata = dict(self.metadata or {})

    @property
    def success(self) -> bool:
        return self.status is WorkflowStatus.SUCCESS

    @property
    def cancelled(self) -> bool:
        return self.status is WorkflowStatus.CANCELLED

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def display(self) -> str:
        if self.success:
            return str(self.data or {})
        return str((self.error or {}).get("message") or self.status.value)

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(
            {
                "status": self.status.value,
                "operation": self.operation,
                "data": self.data,
                "error": self.error,
                "request_id": self.request_id,
                "task_id": self.task_id,
                "metadata": dict(self.metadata),
                "duration_ms": self.duration_ms,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowResult":
        if not isinstance(data, Mapping):
            raise WorkflowValidationError("WorkflowResult must be an object")
        return cls(
            status=WorkflowStatus.from_value(data.get("status", "failure")),
            operation=str(data.get("operation") or ""),
            data=dict(data["data"]) if isinstance(data.get("data"), Mapping) else None,
            error=dict(data["error"]) if isinstance(data.get("error"), Mapping) else None,
            request_id=str(data.get("request_id") or uuid.uuid4().hex),
            task_id=str(data.get("task_id") or ""),
            duration_ms=float(data.get("duration_ms") or 0.0),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def success_result(
        cls,
        operation: str,
        data: dict,
        *,
        request_id: str = "",
        task_id: str = "",
        duration_ms: float = 0.0,
        metadata: dict | None = None,
    ) -> "WorkflowResult":
        return cls(
            status=WorkflowStatus.SUCCESS,
            operation=operation,
            data=dict(data),
            request_id=request_id or uuid.uuid4().hex,
            task_id=task_id,
            duration_ms=duration_ms,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def failure_result(
        cls,
        operation: str,
        error: dict,
        *,
        status: WorkflowStatus = WorkflowStatus.FAILURE,
        data: dict | None = None,
        request_id: str = "",
        task_id: str = "",
        duration_ms: float = 0.0,
        metadata: dict | None = None,
    ) -> "WorkflowResult":
        return cls(
            status=status,
            operation=operation,
            data=dict(data) if isinstance(data, dict) else None,
            error=dict(error or {}),
            request_id=request_id or uuid.uuid4().hex,
            task_id=task_id,
            duration_ms=duration_ms,
            metadata=dict(metadata or {}),
        )


@dataclass(slots=True)
class ScenePlan:
    objects: list[dict] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)
    camera: dict = field(default_factory=dict)
    lights: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    render: dict = field(default_factory=dict)
    reference_image: str = ""
    max_iterations: int = 1
    comparison_threshold: float = 0.01
    save_path: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.objects = _as_mapping_list(self.objects, "objects")
        self.materials = _as_mapping_list(self.materials, "materials")
        self.assignments = _as_mapping_list(self.assignments, "assignments")
        self.lights = _as_mapping_list(self.lights, "lights")
        self.metadata = _as_mapping(self.metadata, "metadata")
        self.steps = _as_mapping_list(self.steps or self.metadata.get("steps", []), "steps")
        self.camera = _as_mapping(self.camera, "camera")
        self.render = _as_mapping(self.render, "render")
        self.reference_image = str(
            self.reference_image
            or self.render.get("reference_image")
            or self.render.get("reference")
            or ""
        )
        self.save_path = str(
            self.save_path
            or self.render.get("save_path")
            or self.metadata.get("save_path")
            or ""
        )
        try:
            self.max_iterations = max(1, min(8, int(self.max_iterations)))
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("max_iterations must be an integer", details={"field": "max_iterations"}) from exc
        try:
            self.comparison_threshold = float(self.comparison_threshold)
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("comparison_threshold must be numeric", details={"field": "comparison_threshold"}) from exc
        if not 0.0 <= self.comparison_threshold <= 1.0:
            raise WorkflowValidationError(
                "comparison_threshold must be between 0 and 1",
                details={"field": "comparison_threshold"},
            )
        self.validate()

    @classmethod
    def from_dict(cls, data: "ScenePlan | Mapping[str, Any] | None") -> "ScenePlan":
        if isinstance(data, cls):
            return data
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise WorkflowValidationError("ScenePlan must be an object")
        scene = data.get("scene") if isinstance(data.get("scene"), Mapping) else {}
        return cls(
            objects=data.get("objects", data.get("scene_objects", scene.get("objects", scene.get("scene_objects", [])))),
            materials=data.get("materials", data.get("scene_materials", scene.get("materials", []))),
            assignments=data.get("assignments", scene.get("assignments", [])),
            camera=data.get("camera", data.get("scene_camera", scene.get("camera", {}))),
            lights=data.get("lights", data.get("scene_lights", scene.get("lights", []))),
            steps=data.get("steps", data.get("actions", scene.get("steps", []))),
            render=data.get("render", scene.get("render", {})),
            reference_image=data.get("reference_image", data.get("reference", "")),
            max_iterations=data.get("max_iterations", data.get("iterations", 1)),
            comparison_threshold=data.get("comparison_threshold", data.get("threshold", 0.01)),
            save_path=data.get("save_path", ""),
            metadata=data.get("metadata", {}),
        )

    def validate(self) -> "ScenePlan":
        if not isinstance(self.reference_image, str):
            raise WorkflowValidationError("reference_image must be text")
        for field_name, values in (
            ("objects", self.objects),
            ("materials", self.materials),
            ("assignments", self.assignments),
            ("lights", self.lights),
            ("steps", self.steps),
        ):
            for index, value in enumerate(values):
                if not isinstance(value, Mapping):
                    raise WorkflowValidationError(
                        f"{field_name}[{index}] must be an object",
                        details={"field": field_name, "index": index},
                    )
        if not isinstance(self.camera, Mapping):
            raise WorkflowValidationError("camera must be an object")
        if not isinstance(self.render, Mapping):
            raise WorkflowValidationError("render must be an object")
        return self

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(
            {
                "objects": [dict(item) for item in self.objects],
                "materials": [dict(item) for item in self.materials],
                "assignments": [dict(item) for item in self.assignments],
                "camera": dict(self.camera),
                "lights": [dict(item) for item in self.lights],
                "steps": [dict(item) for item in self.steps],
                "render": dict(self.render),
                "reference_image": self.reference_image,
                "max_iterations": self.max_iterations,
                "comparison_threshold": self.comparison_threshold,
                "save_path": self.save_path,
                "metadata": dict(self.metadata),
            }
        )

    def to_context(self, max_chars: int = 1800) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)] + "..."

    def with_updates(self, updates: Mapping[str, Any]) -> "ScenePlan":
        values = self.to_dict()
        values.update(dict(updates or {}))
        return ScenePlan.from_dict(values)


@dataclass(slots=True)
class SceneState:
    name: str = ""
    object_count: int = 0
    objects: list[dict | str] = field(default_factory=list)
    cameras: list[str] = field(default_factory=list)
    lights: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    active_camera: str = ""
    world: str = ""
    render: dict = field(default_factory=dict)
    units: dict = field(default_factory=dict)
    frame: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "SceneState":
        value = dict(payload or {})
        root = value.get("scene") if isinstance(value.get("scene"), Mapping) else value
        root = dict(root or {})
        objects = _as_state_objects(root.get("objects", []))
        object_count = _safe_int(root.get("object_count"), len(objects))
        return cls(
            name=str(root.get("name") or ""),
            object_count=object_count,
            objects=objects,
            cameras=_as_name_list(root.get("cameras", [])),
            lights=_as_name_list(root.get("lights", [])),
            materials=_as_name_list(root.get("materials", [])),
            active_camera=str(root.get("active_camera") or ""),
            world=str(root.get("world") or ""),
            render=dict(root.get("render") or {}) if isinstance(root.get("render"), Mapping) else {},
            units=dict(root.get("units") or {}) if isinstance(root.get("units"), Mapping) else {},
            frame={
                key: root[key]
                for key in ("frame_current", "frame_start", "frame_end")
                if key in root
            },
            extra={
                key: value
                for key, value in root.items()
                if key not in {"name", "object_count", "objects", "cameras", "lights", "materials", "active_camera", "world", "render", "units", "frame_current", "frame_start", "frame_end"}
            },
        )

    @classmethod
    def from_scene(cls, payload: Mapping[str, Any] | None) -> "SceneState":
        return cls.from_payload(payload)

    def to_dict(self, max_items: int = 16) -> dict[str, Any]:
        limit = max(1, int(max_items))
        return to_json_safe(
            {
                "name": self.name,
                "object_count": self.object_count,
                "objects": [_object_label(item) for item in self.objects[:limit]],
                "objects_truncated": max(0, len(self.objects) - limit),
                "cameras": self.cameras[:limit],
                "lights": self.lights[:limit],
                "materials": self.materials[:limit],
                "active_camera": self.active_camera,
                "world": self.world,
                "render": dict(self.render),
                "units": dict(self.units),
                "frame": dict(self.frame),
            }
        )

    def to_compact(self, max_items: int = 12) -> dict[str, Any]:
        return self.to_dict(max_items=max_items)

    def to_summary(self, max_items: int = 12) -> dict[str, Any]:
        return self.to_compact(max_items=max_items)

    def to_context(self, max_chars: int = 1400) -> str:
        text = json.dumps(self.to_compact(), ensure_ascii=False, separators=(",", ":"))
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)] + "..."


class WorkflowEventPublisher:
    """Publish workflow progress using the existing EventBus vocabulary."""

    def __init__(self, bus: Any = None, event_sink: Callable[..., Any] | None = None, *, task_id: str = "", request_id: str = "") -> None:
        self.bus = bus
        self.event_sink = event_sink
        self.task_id = task_id or ""
        self.request_id = request_id or uuid.uuid4().hex

    def _payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return to_json_safe({"task_id": self.task_id, "request_id": self.request_id, **dict(payload)})

    def emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        value = self._payload(payload)
        bus_emit = getattr(self.bus, "emit", None)
        if callable(bus_emit):
            try:
                bus_emit(kind, value)
            except Exception:
                pass
        sink = self.event_sink
        if callable(sink):
            try:
                sink(kind, value)
            except TypeError:
                try:
                    sink(value)
                except Exception:
                    pass
            except Exception:
                pass

    def status(self, value: str, message: str, *, operation: str = "") -> None:
        payload = {"key": "state", "value": value, "message": message, "operation": operation}
        emit_status = getattr(self.bus, "emit_status", None)
        if callable(emit_status):
            try:
                emit_status("state", value)
            except Exception:
                pass
        else:
            self.emit("status", payload)
        self.emit("workflow_status", payload)

    def tool(self, status: str, detail: str, *, operation: str = "") -> None:
        name = f"blender_vision.{operation or 'workflow'}"
        payload = {"tool": name, "status": status, "detail": detail, "operation": operation}
        emit_tool = getattr(self.bus, "emit_tool", None)
        if callable(emit_tool):
            try:
                emit_tool(name, status, detail)
            except Exception:
                pass
        else:
            self.emit("tool", payload)
        self.emit("workflow_tool", payload)

    def task(self, message: str, *, operation: str = "", status: str = "executing") -> None:
        self.emit("task", {"lines": [str(message)[:1000]], "status": status, "operation": operation})


class BlenderVisionWorkflow:
    """Compose scene construction, rendering, and Vision verification."""

    _operations = (
        "build_scene",
        "run",
        "render_and_verify",
        "inspect_scene_state",
        "analyze_reference",
        "compare_reference",
    )

    def __init__(
        self,
        integration: BlenderVisionIntegration | Any | None = None,
        *,
        blender: Any | None = None,
        vision: Any | None = None,
        events: Any | None = None,
        event_sink: Callable[..., Any] | None = None,
        interruption_manager: Any | None = None,
        work_dir: str | Path | None = None,
        max_iterations: int = 1,
    ) -> None:
        if integration is not None and (blender is not None or vision is not None):
            raise ValueError("integration cannot be combined with blender or vision")
        self._owns_integration = False
        if integration is None:
            integration = BlenderVisionIntegration(
                blender=blender,
                vision=vision,
                work_dir=work_dir,
            )
            self._owns_integration = True
        self._integration = integration
        self._events = events
        self._event_sink = event_sink
        self._interruption_manager = interruption_manager
        self._tokens: dict[tuple[str, str], Any] = {}
        self._max_iterations = max(1, min(8, int(max_iterations)))
        self._last_error: dict | None = None
        self._last_result: WorkflowResult | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._run_lock = threading.RLock()

    @property
    def integration(self) -> Any:
        return self._integration

    @property
    def blender(self) -> Any:
        return getattr(self._integration, "blender", None)

    @property
    def vision(self) -> Any:
        return getattr(self._integration, "vision", None)

    @property
    def closed(self) -> bool:
        return self._closed

    def capabilities(self) -> dict[str, Any]:
        stack = None
        detector = getattr(self.integration, "capabilities", None)
        if callable(detector):
            try:
                stack = detector()
            except Exception as exc:
                stack = {"available": False, "error": str(exc)}
        return {
            "name": "blender_vision_workflow",
            "available": not self._closed,
            "operations": list(self._operations),
            "max_iterations": self._max_iterations,
            "features": ["scene_plan", "compact_scene_state", "reference_analysis", "bounded_verification"],
            "stack": stack,
        }

    def can_execute(self, operation: str) -> bool:
        return operation in self._operations

    def last_error(self) -> dict | None:
        with self._lock:
            return dict(self._last_error) if isinstance(self._last_error, dict) else None

    def last_result(self) -> WorkflowResult | None:
        with self._lock:
            return self._last_result

    @staticmethod
    def _token_key(task_id: str, request_id: str = "") -> tuple[str, str]:
        return str(task_id or "*"), str(request_id or "")

    def bind_task(self, task_id: str, token: Any | None = None, *, request_id: str = "") -> Any:
        key = self._token_key(task_id, request_id)
        if token is not None:
            with self._lock:
                self._tokens[key] = token
            return token
        manager = self._interruption_manager
        manager_token = getattr(manager, "token_for", None) if manager is not None else None
        if callable(manager_token):
            try:
                value = manager_token(task_id)
            except Exception:
                value = None
            if value is not None:
                with self._lock:
                    self._tokens[key] = value
                return value
        with self._lock:
            if key in self._tokens:
                return self._tokens[key]
            return self._tokens.get(self._token_key(task_id, ""))

    def release_task(self, task_id: str = "", *, request_id: str = "") -> None:
        with self._lock:
            if request_id:
                self._tokens.pop(self._token_key(task_id, request_id), None)
            else:
                self._tokens.pop(self._token_key(task_id, ""), None)

    def cancel(
        self,
        task_id: str = "",
        *,
        request_id: str = "",
        source: str = "workflow",
        reason: str = "user_interruption",
    ) -> bool:
        token = None
        with self._lock:
            if request_id:
                token = self._tokens.get(self._token_key(task_id, request_id))
            else:
                candidates = [
                    value
                    for (bound_task, _), value in self._tokens.items()
                    if not task_id or bound_task == str(task_id or "*")
                ]
                if len(candidates) > 1:
                    return False
                if candidates:
                    token = candidates[0]
        if token is not None:
            request = getattr(token, "request", None)
            if callable(request):
                try:
                    request(reason, source=source)
                    return True
                except TypeError:
                    try:
                        request(reason)
                        return True
                    except Exception:
                        return False
                except Exception:
                    return False
            return False
        if request_id:
            return False
        manager = self._interruption_manager
        request_cancel = getattr(manager, "request_cancel", None) if manager is not None else None
        if callable(request_cancel):
            try:
                outcome = request_cancel(task_id, source=source, reason=reason)
                return bool(getattr(outcome, "accepted", outcome))
            except TypeError:
                try:
                    return bool(request_cancel(task_id))
                except Exception:
                    return False
            except Exception:
                return False
        return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with self._run_lock:
            if self._owns_integration:
                close = getattr(self._integration, "close", None)
                if callable(close):
                    close()
        with self._lock:
            self._tokens.clear()

    def __enter__(self) -> "BlenderVisionWorkflow":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _cancelled(token: Any | None) -> bool:
        if token is None:
            return False
        checker = getattr(token, "is_cancelled", None)
        try:
            if callable(checker):
                return bool(checker())
            if hasattr(token, "cancelled"):
                return bool(getattr(token, "cancelled", False))
            raise_checker = getattr(token, "raise_if_cancelled", None)
            if callable(raise_checker):
                raise_checker()
            return False
        except Exception:
            return True

    def _token_for(self, task_id: str, token: Any | None, request_id: str = "") -> Any:
        if token is not None:
            return token
        if request_id:
            with self._lock:
                exact = self._tokens.get(self._token_key(task_id, request_id))
            if exact is not None:
                return exact
        manager = self._interruption_manager
        manager_token = getattr(manager, "token_for", None) if manager is not None else None
        if callable(manager_token):
            try:
                value = manager_token(task_id)
            except Exception:
                value = None
            if value is not None:
                return value
        with self._lock:
            if request_id:
                exact = self._tokens.get(self._token_key(task_id, request_id))
                if exact is not None:
                    return exact
            return self._tokens.get(self._token_key(task_id, ""))

    @staticmethod
    def _error_status(code: str) -> WorkflowStatus:
        normalized = str(code or "").lower().replace("-", "_")
        if "cancel" in normalized:
            return WorkflowStatus.CANCELLED
        if "invalid" in normalized or "validation" in normalized:
            return WorkflowStatus.INVALID_REQUEST
        if "permission" in normalized or "forbidden" in normalized:
            return WorkflowStatus.PERMISSION_REQUIRED
        if "unavailable" in normalized or "not_found" in normalized:
            return WorkflowStatus.UNAVAILABLE
        return WorkflowStatus.FAILURE

    @staticmethod
    def _exception_payload(exc: BaseException, operation: str, stage: str) -> dict[str, Any]:
        to_dict = getattr(exc, "to_dict", None)
        if callable(to_dict):
            try:
                value = to_dict()
                if isinstance(value, Mapping):
                    return dict(value)
            except Exception:
                pass
        return {
            "error": str(getattr(exc, "code", type(exc).__name__)),
            "operation": operation,
            "stage": stage,
            "message": str(exc) or type(exc).__name__,
        }

    def _publish_result(self, result: WorkflowResult, publisher: WorkflowEventPublisher) -> None:
        with self._lock:
            self._last_result = result
            self._last_error = dict(result.error) if isinstance(result.error, dict) else None
        if result.success:
            publisher.tool("success", "Workflow completed", operation=result.operation)
            publisher.status("ready", "Workflow completed", operation=result.operation)
            publisher.task("Workflow completed", operation=result.operation, status="success")
        elif result.cancelled:
            publisher.tool("cancelled", "Workflow cancelled", operation=result.operation)
            publisher.status("cancelled", "Workflow cancelled", operation=result.operation)
            publisher.task("Workflow cancelled", operation=result.operation, status="cancelled")
        else:
            detail = str((result.error or {}).get("message") or "Workflow failed")
            publisher.tool("error", detail, operation=result.operation)
            publisher.status("error", detail, operation=result.operation)
            publisher.task(detail, operation=result.operation, status="error")
        publisher.emit("workflow_completed", result.to_dict())

    def _execute(
        self,
        operation: str,
        callback: Callable[[WorkflowEventPublisher], Any],
        *,
        task_id: str = "",
        cancellation_token: Any | None = None,
        metadata: dict | None = None,
    ) -> WorkflowResult:
        if self._closed:
            result = WorkflowResult.failure_result(
                operation,
                {"error": "workflow_closed", "message": "Workflow is closed"},
                status=WorkflowStatus.UNAVAILABLE,
                task_id=task_id,
            )
            return result
        request_id = uuid.uuid4().hex
        token = self._token_for(task_id, cancellation_token)
        publisher = WorkflowEventPublisher(
            self._events,
            self._event_sink,
            task_id=task_id,
            request_id=request_id,
        )
        started = time.perf_counter()
        publisher.status("executing", f"Starting {operation}", operation=operation)
        publisher.tool("running", f"Starting {operation}", operation=operation)
        publisher.emit("workflow_started", {"operation": operation, "task_id": task_id})
        try:
            with self._run_lock:
                if self._cancelled(token):
                    raise _WorkflowCancelled()
                value = callback(publisher)
                if self._cancelled(token):
                    raise _WorkflowCancelled()
            duration = (time.perf_counter() - started) * 1000
            if isinstance(value, WorkflowResult):
                result = value
                result.request_id = request_id
                result.task_id = task_id
                result.duration_ms = duration
                if metadata:
                    result.metadata.update(metadata)
            else:
                result = WorkflowResult.success_result(
                    operation,
                    value if isinstance(value, dict) else {"value": value},
                    request_id=request_id,
                    task_id=task_id,
                    duration_ms=duration,
                    metadata=metadata,
                )
        except _WorkflowCancelled:
            result = WorkflowResult.failure_result(
                operation,
                {"error": "cancelled", "message": "Workflow cancelled"},
                status=WorkflowStatus.CANCELLED,
                request_id=request_id,
                task_id=task_id,
                duration_ms=(time.perf_counter() - started) * 1000,
                metadata=metadata,
            )
        except Exception as exc:
            if self._cancelled(token):
                result = WorkflowResult.failure_result(
                    operation,
                    {"error": "cancelled", "message": "Workflow cancelled"},
                    status=WorkflowStatus.CANCELLED,
                    request_id=request_id,
                    task_id=task_id,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    metadata=metadata,
                )
            else:
                payload = self._exception_payload(exc, operation, "workflow")
                if isinstance(exc, WorkflowError):
                    payload = exc.to_dict()
                    if not payload.get("operation"):
                        payload["operation"] = operation
                    if not payload.get("stage"):
                        payload["stage"] = "validation"
                result = WorkflowResult.failure_result(
                    operation,
                    payload,
                    status=self._error_status(str(payload.get("error", ""))),
                    request_id=request_id,
                    task_id=task_id,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    metadata=metadata,
                )
        self._publish_result(result, publisher)
        self.release_task(task_id)
        return result

    @staticmethod
    def _invoke(fn: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        try:
            signature = py_inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(**kwargs)
        parameters = signature.parameters.values()
        if any(parameter.kind is py_inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return fn(**kwargs)
        accepted = {
            name: value
            for name, value in kwargs.items()
            if name in signature.parameters
        }
        return fn(**accepted)

    def _call_layer(self, method: str, **kwargs: Any) -> dict[str, Any]:
        layer = self.blender
        fn = getattr(layer, method, None) if layer is not None else None
        if not callable(fn):
            raise WorkflowError(
                f"Blender operation is unavailable: {method}",
                operation=method,
                stage="blender",
                code="unsupported_operation",
            )
        value = self._invoke(fn, kwargs)
        return self._normalize_value(value, method, "blender")

    def _call_vision(self, method: str, **kwargs: Any) -> dict[str, Any]:
        layer = self.vision
        fn = getattr(layer, method, None) if layer is not None else None
        if not callable(fn):
            raise WorkflowError(
                f"Vision operation is unavailable: {method}",
                operation=method,
                stage="vision",
                code="unsupported_operation",
            )
        value = self._invoke(fn, kwargs)
        return self._normalize_value(value, method, "vision")

    def _call_integration(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        layer = self.integration
        fn = getattr(layer, method, None) if layer is not None else None
        if not callable(fn):
            raise WorkflowError(
                f"Integration operation is unavailable: {method}",
                operation=method,
                stage="integration",
                code="unsupported_operation",
            )
        if not args:
            value = self._invoke(fn, kwargs)
        else:
            try:
                signature = py_inspect.signature(fn)
            except (TypeError, ValueError):
                value = fn(*args, **kwargs)
            else:
                parameters = signature.parameters.values()
                if any(parameter.kind is py_inspect.Parameter.VAR_KEYWORD for parameter in parameters):
                    value = fn(*args, **kwargs)
                else:
                    accepted = {
                        name: item
                        for name, item in kwargs.items()
                        if name in signature.parameters
                    }
                    value = fn(*args, **accepted)
        return self._normalize_value(value, method, "integration")

    def _normalize_value(self, value: Any, operation: str, stage: str) -> dict[str, Any]:
        if isinstance(value, WorkflowResult):
            if not value.success:
                raise WorkflowError(
                    str((value.error or {}).get("message") or "Workflow operation failed"),
                    operation=operation,
                    stage=stage,
                    code=str((value.error or {}).get("error") or "workflow_failure"),
                    details=value.error or {},
                )
            return dict(value.data or {})
        if isinstance(value, IntegrationResult):
            if not value.success:
                raise WorkflowError(
                    str((value.error or {}).get("message") or "Integration operation failed"),
                    operation=operation,
                    stage=stage,
                    code=str((value.error or {}).get("error") or "integration_failure"),
                    details=value.error or {},
                )
            return dict(value.data or {})
        if isinstance(value, CapabilityResult):
            if not value.success:
                raise WorkflowError(
                    str((value.error or {}).get("message") or "Capability operation failed"),
                    operation=operation,
                    stage=stage,
                    code=str((value.error or {}).get("error") or "capability_failure"),
                    details=value.error or {},
                )
            return dict(value.data or {})
        if isinstance(value, Mapping):
            result = dict(value)
            status = result.get("status")
            if status is not None and str(status).lower() not in {"success", "ok", "completed"}:
                raise WorkflowError(
                    str(result.get("message") or result.get("error") or "Operation failed"),
                    operation=operation,
                    stage=stage,
                    code=str(result.get("error") or status),
                    details=result,
                )
            if result.get("success") is False:
                error = result.get("error")
                if isinstance(error, Mapping):
                    error = dict(error)
                else:
                    error = {"error": "operation_failed", "message": str(error or "Operation failed")}
                raise WorkflowError(
                    str(error.get("message") or "Operation failed"),
                    operation=operation,
                    stage=stage,
                    code=str(error.get("error") or "operation_failed"),
                    details=error,
                )
            if "data" in result and ("success" in result or "operation" in result):
                data = result.get("data")
                return dict(data) if isinstance(data, Mapping) else {"value": data}
            return result
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return self._normalize_value(value.to_dict(), operation, stage)
        return {"value": value}

    def _raise_if_cancelled(self, token: Any | None) -> None:
        if self._cancelled(token):
            raise _WorkflowCancelled()

    def _scene_state_data(self) -> dict[str, Any]:
        value = self._call_layer("inspect_scene")
        return SceneState.from_payload(value).to_dict()

    def inspect_scene_state(self, *, task_id: str = "", cancellation_token: Any | None = None) -> WorkflowResult:
        def run(_publisher: WorkflowEventPublisher) -> dict[str, Any]:
            return {"state": self._scene_state_data()}

        return self._execute("inspect_scene_state", run, task_id=task_id, cancellation_token=cancellation_token)

    def scene_state(self, *, task_id: str = "", cancellation_token: Any | None = None) -> WorkflowResult:
        return self.inspect_scene_state(task_id=task_id, cancellation_token=cancellation_token)

    def _dispatch_step(self, step: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(step)
        operation = str(value.pop("operation", value.pop("action", "")) or "")
        if not operation:
            raise WorkflowValidationError("workflow step requires operation")
        aliases = {
            "add_object": "create_object",
            "new_object": "create_object",
            "add_material": "create_material",
            "new_material": "create_material",
            "set_camera": "configure_camera",
            "add_light": "configure_light",
            "new_light": "configure_light",
        }
        operation = aliases.get(operation, operation)
        params = value.pop("params", None)
        if isinstance(params, Mapping):
            value.update(dict(params))
        if operation in {"create_object", "delete_object", "modify_object", "transform_object", "create_material", "assign_material", "configure_camera", "configure_light", "save", "export", "render"}:
            return self._call_layer(operation, **value)
        if operation in {"inspect_scene", "list_objects", "inspect_object"}:
            return self._call_layer(operation, **value)
        raise WorkflowValidationError(
            f"unsupported workflow step: {operation}",
            details={"operation": operation},
        )

    def _apply_refinement(self, refinement: Any, token: Any | None = None) -> bool:
        if refinement is None or refinement is False:
            return False
        if isinstance(refinement, ScenePlan):
            self._build_scene_data(refinement, ensure_render_setup=False, token=token)
            return True
        if isinstance(refinement, Mapping):
            if "plan" in refinement:
                self._build_scene_data(ScenePlan.from_dict(refinement["plan"]), ensure_render_setup=False, token=token)
                return True
            operations = refinement.get("steps") or refinement.get("operations")
            if operations:
                for step in _as_mapping_list(operations, "operations"):
                    self._raise_if_cancelled(token)
                    self._dispatch_step(step)
                return True
            self._build_scene_data(ScenePlan.from_dict(refinement), ensure_render_setup=False, token=token)
            return True
        return False

    def _build_scene_data(
        self,
        plan: ScenePlan,
        *,
        ensure_render_setup: bool = True,
        token: Any | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise WorkflowError("Workflow is closed", operation="build_scene", code="workflow_closed")
        records: list[dict[str, Any]] = []

        def record(operation: str, value: Any) -> None:
            records.append({"operation": operation, "status": "success", "summary": _result_summary(value)})

        for step in plan.steps:
            self._raise_if_cancelled(token)
            value = self._dispatch_step(step)
            record(str(step.get("operation", step.get("action", "step"))), value)

        for material in plan.materials:
            self._raise_if_cancelled(token)
            value = self._call_layer("create_material", **dict(material))
            record("create_material", value)

        assignments: list[dict[str, Any]] = []
        for item in plan.objects:
            self._raise_if_cancelled(token)
            value = dict(item)
            material_name = value.pop("material", value.pop("material_name", ""))
            transform = value.pop("transform", None)
            object_name = str(value.get("name") or "")
            operation = str(value.pop("operation", value.pop("action", "create_object")))
            if operation in {"delete_object", "delete", "remove_object"}:
                result = self._call_layer("delete_object", name=str(value.get("name") or ""))
                record("delete_object", result)
                continue
            if operation in {"modify_object", "transform_object"}:
                result = self._dispatch_step({"operation": operation, **value})
                record(operation, result)
                continue
            result = self._call_layer("create_object", **value)
            record("create_object", result)
            if material_name:
                assignments.append({"object_name": object_name or str(_result_name(result) or ""), "material_name": str(material_name)})
            if isinstance(transform, Mapping):
                transform_value = self._call_layer("transform_object", name=object_name or str(_result_name(result) or ""), **dict(transform))
                record("transform_object", transform_value)

        for assignment in plan.assignments:
            self._raise_if_cancelled(token)
            value = self._call_layer("assign_material", **dict(assignment))
            record("assign_material", value)
        for assignment in assignments:
            if not assignment.get("object_name"):
                continue
            value = self._call_layer("assign_material", **assignment)
            record("assign_material", value)

        if plan.camera:
            self._raise_if_cancelled(token)
            camera = dict(plan.camera)
            camera.setdefault("make_active", True)
            value = self._call_layer("configure_camera", **camera)
            record("configure_camera", value)
        for light in plan.lights:
            self._raise_if_cancelled(token)
            value = self._call_layer("configure_light", **dict(light))
            record("configure_light", value)

        if ensure_render_setup:
            state = SceneState.from_payload(self._call_layer("inspect_scene"))
            if not state.cameras:
                value = self._call_layer(
                    "configure_camera",
                    name="Camera",
                    location=[7, -6, 5],
                    rotation=[55, 0, 45],
                    lens=42,
                    make_active=True,
                )
                record("configure_camera", value)
            if not state.lights:
                value = self._call_layer(
                    "configure_light",
                    name="Light",
                    light_type="SUN",
                    rotation=[50, 0, 30],
                    energy=3.0,
                )
                record("configure_light", value)

        saved = None
        if plan.save_path:
            self._raise_if_cancelled(token)
            saved = self._call_layer("save", path=plan.save_path)
            record("save", saved)
        self._raise_if_cancelled(token)
        return {
            "plan": plan.to_dict(),
            "steps": records,
            "state": self._scene_state_data(),
            "saved": _result_summary(saved) if saved is not None else None,
        }

    def build_scene(
        self,
        plan: ScenePlan | Mapping[str, Any] | None = None,
        *,
        ensure_render_setup: bool = True,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        def run(_publisher: WorkflowEventPublisher) -> dict[str, Any]:
            scene_plan = ScenePlan.from_dict(plan)
            token = self._token_for(task_id, cancellation_token)
            return self._build_scene_data(scene_plan, ensure_render_setup=ensure_render_setup, token=token)

        return self._execute("build_scene", run, task_id=task_id, cancellation_token=cancellation_token)

    def build(
        self,
        plan: ScenePlan | Mapping[str, Any] | None = None,
        *,
        ensure_render_setup: bool = True,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        return self.build_scene(
            plan,
            ensure_render_setup=ensure_render_setup,
            task_id=task_id,
            cancellation_token=cancellation_token,
        )

    def _render_data(
        self,
        path: str | Path,
        render_options: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        options = dict(render_options or {})
        options.pop("path", None)
        options.pop("output_path", None)
        options.pop("reference_image", None)
        if "format" in options and "img_format" not in options:
            options["img_format"] = options.pop("format")
        method = getattr(self.integration, "render_and_inspect", None)
        if callable(method):
            return self._call_integration(
                "render_and_inspect",
                str(path),
                **options,
                context=dict(context or {}),
            )
        render = self._call_layer("render", path=str(path), **options)
        inspection = self._call_vision("inspect_render", image_path=str(path), context=dict(context or {}))
        if inspection.get("valid") is not True:
            raise WorkflowError(
                "render failed structural Vision inspection",
                operation="render_and_inspect",
                stage="vision_inspect",
                code="invalid_render",
                details={"inspection": inspection},
            )
        return {
            "render": render,
            "render_path": str(path),
            "inspection": inspection,
            "valid": True,
        }

    def _reference_kind(self, value: Mapping[str, Any]) -> tuple[str, str]:
        backend = str(value.get("backend") or "")
        if not backend:
            vision = self.vision
            preferred = str(getattr(vision, "preferred_backend", "") or "")
            detector = getattr(vision, "detect_capabilities", None)
            details = detector() if callable(detector) else {}
            available = details.get("available_backends", []) if isinstance(details, Mapping) else []
            if preferred and preferred != "fallback" and preferred in available:
                backend = preferred
            else:
                semantic = [str(name) for name in available if str(name) not in {"", "fallback"}]
                backend = semantic[0] if semantic else "fallback"
        return backend, "semantic" if backend not in {"", "fallback"} else "deterministic"

    def analyze_reference(
        self,
        reference_path: str | Path,
        *,
        detail_level: str = "medium",
        focus: str | None = None,
        context: Mapping[str, Any] | None = None,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        def run(_publisher: WorkflowEventPublisher) -> dict[str, Any]:
            if not reference_path:
                raise WorkflowValidationError("reference_path is required", details={"field": "reference_path"})
            method = getattr(self.integration, "describe_render", None)
            if callable(method):
                value = self._call_integration(
                    "describe_render",
                    str(reference_path),
                    detail_level=detail_level,
                    focus=focus,
                    context=dict(context or {}),
                )
            else:
                self._call_vision("validate_image", image_path=str(reference_path))
                value = self._call_vision(
                    "describe",
                    image_path=str(reference_path),
                    detail_level=detail_level,
                    focus=focus,
                    context=dict(context or {}),
                )
            backend, evidence_kind = self._reference_kind(value)
            description = value.get("description")
            if isinstance(description, Mapping):
                compact_description = {
                    "text": str(description.get("description") or description.get("text") or "")[:1800],
                    "metadata": dict(description.get("metadata") or {}),
                    "render_hints": dict(description.get("render_hints") or {}),
                }
                deterministic_analysis = dict(description.get("analysis") or {})
            else:
                compact_description = {"text": str(description or value.get("text") or "")[:1800]}
                deterministic_analysis = dict(value.get("analysis") or {})
            image = dict(value.get("image") or {})
            if not image and isinstance(description, Mapping):
                image = dict(description.get("metadata") or {})
            if not image:
                image = dict(value.get("metadata") or {})
            deterministic_inspection = None
            inspect = getattr(self.vision, "inspect", None)
            if callable(inspect):
                deterministic_inspection = _compact_value(self._call_vision("inspect", image_path=str(reference_path)))
            return {
                "reference_path": str(reference_path),
                "description": compact_description,
                "image": image,
                "deterministic_analysis": _compact_value(deterministic_analysis or deterministic_inspection or {}),
                "backend": backend,
                "evidence": {
                    "kind": evidence_kind,
                    "deterministic": evidence_kind == "deterministic",
                    "semantic": evidence_kind == "semantic",
                },
            }

        return self._execute("analyze_reference", run, task_id=task_id, cancellation_token=cancellation_token)

    def _compare_data(
        self,
        path: str | Path,
        reference_path: str | Path,
        *,
        threshold: float,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not reference_path:
            raise WorkflowValidationError("reference_path is required", details={"field": "reference_path"})
        if not 0.0 <= float(threshold) <= 1.0:
            raise WorkflowValidationError(
                "comparison threshold must be between 0 and 1",
                details={"field": "threshold"},
            )
        method = getattr(self.integration, "compare_renders", None)
        try:
            if callable(method):
                value = self._call_integration(
                    "compare_renders",
                    str(path),
                    str(reference_path),
                    threshold=threshold,
                    context=dict(context or {}),
                )
            else:
                self._call_vision("validate_image", image_path=str(path))
                self._call_vision("validate_image", image_path=str(reference_path))
                value = self._call_vision(
                    "compare",
                    image_path=str(path),
                    reference_image=str(reference_path),
                    threshold=threshold,
                    context=dict(context or {}),
                )
        except WorkflowError as exc:
            message = str(exc.message or exc).lower()
            if "pillow" not in message and "numpy" not in message and "missing_dependency" not in message:
                raise
            value = _byte_image_comparison(path, reference_path)
        comparison = value.get("comparison") if isinstance(value.get("comparison"), Mapping) else value
        if not isinstance(comparison, Mapping):
            comparison = {}
        diff_ratio = comparison.get("diff_ratio")
        identical = comparison.get("identical")
        boolean_fields = ("success", "verified", "passed", "valid", "identical")
        if any(key in comparison and not isinstance(comparison[key], bool) for key in boolean_fields):
            verified = False
        elif any(comparison.get(key) is False for key in ("success", "verified", "passed", "valid")):
            verified = False
        elif identical is True:
            verified = True
        elif isinstance(diff_ratio, (int, float)) and not isinstance(diff_ratio, bool):
            verified = diff_ratio <= threshold
        elif any(comparison.get(key) is True for key in ("verified", "passed", "valid")):
            verified = True
        else:
            verified = False
        return {
            "render_path": str(path),
            "reference_path": str(reference_path),
            "comparison": dict(comparison),
            "verified": verified,
            "threshold": threshold,
        }

    def compare_reference(
        self,
        path: str | Path,
        reference_path: str | Path,
        *,
        threshold: float = 0.01,
        context: Mapping[str, Any] | None = None,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        def run(_publisher: WorkflowEventPublisher) -> dict[str, Any]:
            return self._compare_data(path, reference_path, threshold=float(threshold), context=context)

        return self._execute("compare_reference", run, task_id=task_id, cancellation_token=cancellation_token)

    def compare_to_reference(
        self,
        path: str | Path,
        reference_path: str | Path,
        *,
        threshold: float = 0.01,
        context: Mapping[str, Any] | None = None,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        return self.compare_reference(
            path,
            reference_path,
            threshold=threshold,
            context=context,
            task_id=task_id,
            cancellation_token=cancellation_token,
        )

    def _callback_value(self, callback: Callable[..., Any], payload: dict[str, Any], iteration: int) -> Any:
        if not callable(callback):
            return None
        try:
            signature = py_inspect.signature(callback)
            parameters = list(signature.parameters.values())
            accepts_varargs = any(parameter.kind is py_inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
            positional = [
                parameter
                for parameter in parameters
                if parameter.kind in (py_inspect.Parameter.POSITIONAL_ONLY, py_inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            if accepts_varargs or len(positional) >= 2:
                return callback(payload, iteration)
            return callback(payload)
        except (TypeError, ValueError):
            return callback(payload, iteration)

    def _verification_value(self, value: Any) -> tuple[bool, str, dict[str, Any]]:
        if value is _NO_VERIFIER:
            return True, "", {}
        if value is None:
            return False, "verification callback returned no verdict", {}
        if isinstance(value, bool):
            return value, "" if value else "verification callback rejected the render", {}
        if isinstance(value, Mapping):
            for key in ("verified", "passed", "valid", "success"):
                if key in value:
                    raw = value[key]
                    if not isinstance(raw, bool):
                        return False, f"verification callback field {key!r} must be boolean", dict(value)
                    reason = str(value.get("reason") or value.get("detail") or value.get("message") or "")
                    return raw, reason, dict(value)
            if "evidence" in value:
                return False, "verification callback supplied evidence without a boolean verdict", dict(value)
            return False, "verification callback returned an unkeyed mapping", dict(value)
        if isinstance(value, tuple) and len(value) >= 2:
            if not isinstance(value[0], bool):
                return False, "verification callback tuple verdict must be boolean", {"reason": str(value[1])}
            return value[0], str(value[1]), {"reason": str(value[1])}
        return False, "verification callback returned a malformed verdict", {"value": str(value)}

    def _render_evidence_valid(self, render: Any, path: str | Path) -> tuple[bool, str]:
        if not isinstance(render, Mapping):
            return False, "render result is not an object"
        if render.get("valid") is not True:
            return False, "render result has no valid=true structural evidence"
        inspection = render.get("inspection")
        if not isinstance(inspection, Mapping) or inspection.get("valid") is not True:
            return False, "render result has no valid=true inspection evidence"
        nested = render.get("render") if isinstance(render.get("render"), Mapping) else {}
        if render.get("exists") is False or nested.get("exists") is False:
            return False, "render result reports a missing artifact"
        artifact_path = nested.get("path") or render.get("render_path") or render.get("path") or str(path)
        try:
            requested_path = Path(str(path)).expanduser().resolve()
            reported_path = Path(str(artifact_path)).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            return False, "render artifact path is invalid"
        if reported_path != requested_path:
            return False, "render artifact path does not match requested path"
        if not reported_path.is_file():
            return False, "render artifact file is missing"
        return True, ""

    def _render_and_verify_data(
        self,
        path: str | Path,
        *,
        plan: ScenePlan | None,
        reference_path: str | Path | None,
        max_iterations: int | None,
        threshold: float | None,
        render_options: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None,
        verifier: Callable[..., Any] | None,
        refine: Callable[..., Any] | None,
        token: Any | None,
    ) -> WorkflowResult:
        scene_plan = plan or ScenePlan()
        if not path:
            raise WorkflowValidationError("render path is required", details={"field": "path"})
        default_iterations = plan.max_iterations if plan is not None else self._max_iterations
        iterations = max(1, min(8, int(default_iterations if max_iterations is None else max_iterations)))
        try:
            compare_threshold = float(threshold if threshold is not None else scene_plan.comparison_threshold)
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("comparison threshold must be numeric", details={"field": "threshold"}) from exc
        if not 0.0 <= compare_threshold <= 1.0:
            raise WorkflowValidationError(
                "comparison threshold must be between 0 and 1",
                details={"field": "threshold"},
            )
        reference = str(reference_path or scene_plan.reference_image or "")
        options = dict(render_options or scene_plan.render or {})
        attempts: list[dict[str, Any]] = []
        final_render: dict[str, Any] = {}
        last_payload: dict[str, Any] = {}
        verified = False
        for iteration in range(1, iterations + 1):
            self._raise_if_cancelled(token)
            render = self._render_data(path, options, context)
            self._raise_if_cancelled(token)
            render_valid, render_reason = self._render_evidence_valid(render, path)
            comparison: dict[str, Any] | None = None
            deterministic_valid = render_valid
            if reference:
                comparison = self._compare_data(
                    path,
                    reference,
                    threshold=compare_threshold,
                    context=context,
                )
                deterministic_valid = deterministic_valid and bool(comparison.get("verified"))
            payload = {
                "iteration": iteration,
                "render": _compact_value(render),
                "render_reason": render_reason,
                "comparison": _compact_value(comparison) if comparison else None,
                "deterministic_valid": deterministic_valid,
            }
            callback_value = _NO_VERIFIER if verifier is None else self._callback_value(verifier, payload, iteration)
            callback_valid, callback_reason, callback_data = self._verification_value(callback_value)
            current_valid = deterministic_valid and callback_valid
            evidence = {
                "kind": "deterministic" if verifier is None else "deterministic_and_callback",
                "deterministic": True,
                "render_valid": render_valid,
                "render_reason": render_reason,
                "comparison": bool(comparison.get("verified")) if comparison else None,
            }
            if callback_value is not _NO_VERIFIER:
                evidence["callback"] = {
                    "valid": callback_valid,
                    "reason": callback_reason,
                    "data": callback_data,
                }
            attempt = {
                "iteration": iteration,
                "valid": current_valid,
                "render_path": str(path),
                "evidence": evidence,
            }
            attempts.append(attempt)
            final_render = render
            last_payload = payload
            if current_valid:
                verified = True
                break
            if iteration >= iterations or refine is None:
                break
            refinement = self._callback_value(refine, payload, iteration)
            if not self._apply_refinement(refinement, token=token):
                break
        state = None
        if callable(getattr(self.blender, "inspect_scene", None)):
            try:
                state = self._scene_state_data()
            except Exception as exc:
                state = {"error": str(exc) or type(exc).__name__}
        data = {
            "path": str(path),
            "reference_path": reference or None,
            "attempts": attempts,
            "iterations": len(attempts),
            "verified": verified,
            "render": _compact_value(final_render),
            "last": _compact_value(last_payload),
            "state": state,
        }
        if verified:
            return WorkflowResult.success_result("render_and_verify", data)
        error = {
            "error": "verification_failed",
            "message": "Render did not satisfy the bounded verification criteria.",
            "details": {"attempts": attempts},
        }
        return WorkflowResult.failure_result(
            "render_and_verify",
            error,
            data=data,
        )

    def render_and_verify(
        self,
        path: str | Path | None = None,
        *,
        plan: ScenePlan | Mapping[str, Any] | None = None,
        reference_path: str | Path | None = None,
        max_iterations: int | None = None,
        threshold: float | None = None,
        render_options: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        verifier: Callable[..., Any] | None = None,
        refine: Callable[..., Any] | None = None,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        def run(publisher: WorkflowEventPublisher) -> WorkflowResult:
            scene_plan = ScenePlan.from_dict(plan) if plan is not None else ScenePlan()
            target = path or scene_plan.render.get("path") or scene_plan.render.get("output_path")
            if not target:
                raise WorkflowValidationError("render path is required", details={"field": "path"})
            token = self._token_for(task_id, cancellation_token)
            if scene_plan.to_dict() != ScenePlan().to_dict():
                self._raise_if_cancelled(token)
                self._build_scene_data(scene_plan, ensure_render_setup=True, token=token)
            publisher.emit("verification_started", {"path": str(target), "reference": reference_path or scene_plan.reference_image or None})
            publisher.status("verifying", "Verifying render", operation="render_and_verify")
            result = self._render_and_verify_data(
                target,
                plan=scene_plan,
                reference_path=reference_path,
                max_iterations=max_iterations,
                threshold=threshold,
                render_options=render_options,
                context=context,
                verifier=verifier,
                refine=refine,
                token=token,
            )
            publisher.emit("verification_completed", {"verified": result.success, "iterations": (result.data or {}).get("iterations", 0)})
            return result

        return self._execute("render_and_verify", run, task_id=task_id, cancellation_token=cancellation_token)

    def render(
        self,
        path: str | Path | None = None,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.render_and_verify(path, **kwargs)

    def verify(
        self,
        path: str | Path | None = None,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.render_and_verify(path, **kwargs)

    def render_and_inspect(
        self,
        path: str | Path | None = None,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.render_and_verify(path, **kwargs)

    def inspect(
        self,
        *,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        return self.inspect_scene_state(task_id=task_id, cancellation_token=cancellation_token)

    def analyze_image(
        self,
        reference_path: str | Path,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.analyze_reference(reference_path, **kwargs)

    def compare_images(
        self,
        path: str | Path,
        reference_path: str | Path,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.compare_reference(path, reference_path, **kwargs)

    def run(
        self,
        plan: ScenePlan | Mapping[str, Any] | None = None,
        *,
        output_path: str | Path | None = None,
        reference_path: str | Path | None = None,
        max_iterations: int | None = None,
        threshold: float | None = None,
        render_options: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        verifier: Callable[..., Any] | None = None,
        refine: Callable[..., Any] | None = None,
        task_id: str = "",
        cancellation_token: Any | None = None,
    ) -> WorkflowResult:
        def callback(publisher: WorkflowEventPublisher) -> WorkflowResult:
            scene_plan = ScenePlan.from_dict(plan)
            target = output_path or scene_plan.render.get("path") or scene_plan.render.get("output_path")
            token = self._token_for(task_id, cancellation_token)
            self._raise_if_cancelled(token)
            build_data = self._build_scene_data(scene_plan, ensure_render_setup=True, token=token)
            publisher.emit("scene_built", {"state": build_data.get("state")})
            if not target:
                raise WorkflowValidationError("render path is required", details={"field": "output_path"})
            publisher.status("verifying", "Verifying render", operation="run")
            result = self._render_and_verify_data(
                target,
                plan=scene_plan,
                reference_path=reference_path,
                max_iterations=max_iterations,
                threshold=threshold,
                render_options=render_options,
                context=context,
                verifier=verifier,
                refine=refine,
                token=token,
            )
            data = dict(result.data or {})
            data["build"] = build_data
            if result.success:
                return WorkflowResult.success_result("run", data)
            return WorkflowResult.failure_result("run", result.error or {}, data=data)

        return self._execute("run", callback, task_id=task_id, cancellation_token=cancellation_token)

    def execute(
        self,
        plan: ScenePlan | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.run(plan, **kwargs)

    def build_and_render(
        self,
        plan: ScenePlan | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkflowResult:
        return self.run(plan, **kwargs)


SceneWorkflow = BlenderVisionWorkflow


class BlenderVisionWorkflowCapabilityAdapter:
    """Expose the workflow through the frozen S2.7 capability protocol."""

    name = "blender_vision_workflow"

    def __init__(self, workflow: BlenderVisionWorkflow) -> None:
        self.workflow = workflow

    def capabilities(self) -> dict[str, Any]:
        report = self.workflow.capabilities()
        report["name"] = self.name
        return report

    def can_execute(self, operation: str) -> bool:
        return self.workflow.can_execute(operation)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        try:
            request.check_cancelled()
        except CapabilityCancellationError:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "request cancelled before execution")
        except Exception as exc:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, str(exc) or type(exc).__name__)
        self.workflow.bind_task(request.task_id, request.cancellation_token, request_id=request.request_id)
        try:
            return self._execute_request(request)
        finally:
            self.workflow.release_task(request.task_id, request_id=request.request_id)

    def _execute_request(self, request: CapabilityRequest) -> CapabilityResult:
        request.check_cancelled()
        params = dict(request.params or {})
        context = dict(request.context or {})
        embedded_context = params.pop("context", None)
        if isinstance(embedded_context, Mapping):
            context.update(dict(embedded_context))
        operation = request.operation
        if operation == "build_scene":
            result = self.workflow.build_scene(
                params.get("plan", params.get("scene_plan")),
                ensure_render_setup=bool(params.get("ensure_render_setup", True)),
                task_id=request.task_id,
                cancellation_token=request.cancellation_token,
            )
        elif operation == "run":
            result = self.workflow.run(
                params.get("plan", params.get("scene_plan")),
                output_path=params.get("output_path", params.get("path")),
                reference_path=params.get("reference_path", params.get("reference")),
                max_iterations=params.get("max_iterations"),
                threshold=params.get("threshold", params.get("comparison_threshold")),
                render_options=params.get("render_options"),
                context=context,
                task_id=request.task_id,
                cancellation_token=request.cancellation_token,
            )
        elif operation == "render_and_verify":
            result = self.workflow.render_and_verify(
                params.get("path", params.get("output_path")),
                plan=params.get("plan", params.get("scene_plan")),
                reference_path=params.get("reference_path", params.get("reference")),
                max_iterations=params.get("max_iterations"),
                threshold=params.get("threshold", params.get("comparison_threshold")),
                render_options=params.get("render_options"),
                context=context,
                task_id=request.task_id,
                cancellation_token=request.cancellation_token,
            )
        elif operation == "inspect_scene_state":
            result = self.workflow.inspect_scene_state(
                task_id=request.task_id,
                cancellation_token=request.cancellation_token,
            )
        elif operation == "analyze_reference":
            result = self.workflow.analyze_reference(
                params.get("reference_path", params.get("path")),
                detail_level=params.get("detail_level", "medium"),
                focus=params.get("focus"),
                context=context,
                task_id=request.task_id,
                cancellation_token=request.cancellation_token,
            )
        elif operation == "compare_reference":
            result = self.workflow.compare_reference(
                params.get("path"),
                params.get("reference_path", params.get("reference")),
                threshold=float(params.get("threshold", 0.01)),
                context=context,
                task_id=request.task_id,
                cancellation_token=request.cancellation_token,
            )
        else:
            return CapabilityResult.failure(request, CapabilityStatus.INVALID_REQUEST, f"unsupported operation: {operation}")
        status_map = {
            WorkflowStatus.SUCCESS: CapabilityStatus.SUCCESS,
            WorkflowStatus.CANCELLED: CapabilityStatus.CANCELLED,
            WorkflowStatus.INVALID_REQUEST: CapabilityStatus.INVALID_REQUEST,
            WorkflowStatus.UNAVAILABLE: CapabilityStatus.UNAVAILABLE,
            WorkflowStatus.PERMISSION_REQUIRED: CapabilityStatus.PERMISSION_REQUIRED,
            WorkflowStatus.FAILURE: CapabilityStatus.FAILURE,
        }
        return CapabilityResult(
            status=status_map[result.status],
            capability=self.name,
            operation=operation,
            data=result.data,
            error=result.error,
            request_id=request.request_id,
            task_id=request.task_id,
            metadata=result.metadata,
            duration_ms=result.duration_ms,
        )

    def cancel(self, task_id: str = "", *, request_id: str = "") -> bool:
        return self.workflow.cancel(task_id, request_id=request_id, source="capability_adapter")


WorkflowCapabilityAdapter = BlenderVisionWorkflowCapabilityAdapter
BlenderVisionWorkflowAdapter = BlenderVisionWorkflowCapabilityAdapter
SceneWorkflowCapabilityAdapter = BlenderVisionWorkflowCapabilityAdapter


def create_blender_vision_workflow(
    integration: BlenderVisionIntegration | Any | None = None,
    **kwargs: Any,
) -> BlenderVisionWorkflow:
    return BlenderVisionWorkflow(integration, **kwargs)


create_scene_workflow = create_blender_vision_workflow


def _byte_image_comparison(path: str | Path, reference_path: str | Path) -> dict[str, Any]:
    left = Path(path)
    right = Path(reference_path)
    try:
        left_bytes = left.read_bytes()
        right_bytes = right.read_bytes()
    except OSError as exc:
        raise WorkflowError(
            str(exc) or "image could not be read",
            operation="compare_reference",
            stage="comparison",
            code="invalid_render",
        ) from exc
    identical = left_bytes == right_bytes
    dimensions: list[int] | None = None
    if left_bytes[:8] == b"\x89PNG\r\n\x1a\n" and right_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        if len(left_bytes) >= 24 and len(right_bytes) >= 24 and left_bytes[12:16] == b"IHDR" and right_bytes[12:16] == b"IHDR":
            left_size = [int.from_bytes(left_bytes[16:20], "big"), int.from_bytes(left_bytes[20:24], "big")]
            right_size = [int.from_bytes(right_bytes[16:20], "big"), int.from_bytes(right_bytes[20:24], "big")]
            dimensions = left_size if left_size == right_size else None
            if left_size != right_size:
                return {
                    "success": False,
                    "identical": False,
                    "comparison_mode": "byte_identity",
                    "dimensions": [left_size, right_size],
                    "code": "dimension_mismatch",
                }
    return {
        "success": True,
        "identical": identical,
        "comparison_mode": "byte_identity",
        "diff_ratio": 0.0 if identical else 1.0,
        "dimensions": [dimensions] if dimensions else None,
    }


def _as_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkflowValidationError(f"{field_name} must be an object", details={"field": field_name})
    return dict(value)


def _as_mapping_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise WorkflowValidationError(f"{field_name} must be a list", details={"field": field_name})
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise WorkflowValidationError(
                f"{field_name}[{index}] must be an object",
                details={"field": field_name, "index": index},
            )
        result.append(dict(item))
    return result


def _as_state_objects(value: Any) -> list[dict | str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict | str] = []
    for item in value:
        if isinstance(item, Mapping):
            result.append(dict(item))
        else:
            result.append(str(item))
    return result


def _as_name_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item.get("name") if isinstance(item, Mapping) else item) for item in value]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _object_label(value: dict | str) -> dict | str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return str(value)
    label = {"name": str(value.get("name") or "")}
    for key in ("type", "location", "rotation_degrees", "scale", "dims", "hidden", "hide_viewport", "materials", "light_type", "lens_mm"):
        if key in value:
            label[key] = value[key]
    return label


def _result_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    return ""


def _result_summary(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        summary = {}
        for key in ("name", "path", "exists", "count", "deleted", "missing", "valid", "render_path"):
            if key in value:
                summary[key] = value[key]
        if not summary:
            summary = {"keys": sorted(str(key) for key in value.keys())[:12]}
        return to_json_safe(summary)
    return to_json_safe(value)


def _compact_value(value: Any, max_items: int = 12, max_string: int = 1800) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in list(value.items())[:max_items]:
            result[str(key)] = _compact_value(item, max_items, max_string)
        return result
    if isinstance(value, list):
        return [_compact_value(item, max_items, max_string) for item in value[:max_items]]
    if isinstance(value, tuple):
        return [_compact_value(item, max_items, max_string) for item in value[:max_items]]
    if isinstance(value, str) and len(value) > max_string:
        return value[: max(0, max_string - 3)] + "..."
    return to_json_safe(value)


__all__ = [
    "BlenderVisionWorkflow",
    "BlenderVisionWorkflowAdapter",
    "BlenderVisionWorkflowCapabilityAdapter",
    "SceneWorkflow",
    "SceneWorkflowCapabilityAdapter",
    "ScenePlan",
    "SceneState",
    "WorkflowCapabilityAdapter",
    "WorkflowError",
    "WorkflowEventPublisher",
    "WorkflowResult",
    "WorkflowStatus",
    "WorkflowValidationError",
    "create_blender_vision_workflow",
    "create_scene_workflow",
]
