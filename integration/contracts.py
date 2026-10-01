from __future__ import annotations

"""Core-facing capability contracts and S2 capability adapters.

The registry deliberately does not import or modify S1 Agent/Executor classes.
Core owns permission decisions, task persistence, and final verification; this
layer only provides a stable request/result boundary for capability providers.
"""

import inspect
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from .errors import BlenderVisionError

#: Blender operation inventory, owned by :mod:`blender.operations`. Imported
#: lazily-safe: `blender` is a leaf package with no dependency on `integration`.
from blender.operations import OPERATION_NAMES as BLENDER_OPERATION_NAMES

#: The operations the shipped deterministic Vision backend implements. Used as
#: the advertised floor when a layer offers no discovery hook, so the model is
#: never shown an operation no available backend can service (M2.14.1).
VISION_FALLBACK_OPERATIONS = ("compare", "describe", "inspect_render")

#: Stable error code -> capability status.
#:
#: Exact code matching only. Infrastructure outcomes (missing executable, denied
#: permission, spawn failure) keep their own identity, and nothing in this table
#: can yield INVALID_REQUEST, which is reserved for caller-attributable invalid
#: input. Mirrors `blender.errors.ERROR_CODES`.
_STATUS_BY_ERROR_CODE: dict[str, "CapabilityStatus"] = {}

#: Exact exception types that imply a specific status when no code is present.
_STATUS_BY_EXCEPTION_TYPE: list[tuple[type, "CapabilityStatus"]] = []


class CapabilityStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    PERMISSION_REQUIRED = "permission_required"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"

    @classmethod
    def from_value(cls, value: "CapabilityStatus | str") -> "CapabilityStatus":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        # Kept in step with WorkflowStatus.from_value so both halves of the
        # contract deserialize the same vocabulary.
        aliases = {"completed": "success", "complete": "success", "done": "success", "error": "failure", "failed": "failure", "canceled": "cancelled", "permission": "permission_required"}
        normalized = aliases.get(normalized, normalized)
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"unknown capability status: {value!r}") from exc


def _build_status_tables() -> None:
    """Populate the exact-match status tables.

    Imported lazily so this module keeps no import-time dependency on the
    Blender error hierarchy, and so the mapping is stated in one readable place.
    """
    from blender.errors import (
        C_CANCELLATION_PENDING,
        BlenderCancelledError,
        BlenderClosedError,
        BlenderError,
        BlenderNotFoundError,
        BlenderPermissionError,
        BlenderProcessError,
        BlenderProtocolError,
        BlenderScriptError,
        BlenderSpawnError,
        BlenderTimeoutError,
    )

    _STATUS_BY_ERROR_CODE.update({
        # A cancelled child is a cancellation, never a generic failure. This
        # includes the "could not terminate it" variant, which the ratified
        # contract still classifies as cancelled (with distinct metadata).
        BlenderCancelledError.code: CapabilityStatus.CANCELLED,
        C_CANCELLATION_PENDING: CapabilityStatus.CANCELLED,
        # The executable is missing or unusable: the capability is unavailable.
        BlenderNotFoundError.code: CapabilityStatus.UNAVAILABLE,
        BlenderSpawnError.code: CapabilityStatus.UNAVAILABLE,
        # Access denied is a permission outcome, not a malformed request.
        BlenderPermissionError.code: CapabilityStatus.PERMISSION_REQUIRED,
        # Everything else is a plain failure. A lifecycle fault (closed layer) is
        # deliberately a failure too: it is not caller-attributable invalid input.
        BlenderClosedError.code: CapabilityStatus.FAILURE,
        BlenderTimeoutError.code: CapabilityStatus.FAILURE,
        BlenderProcessError.code: CapabilityStatus.FAILURE,
        BlenderScriptError.code: CapabilityStatus.FAILURE,
        BlenderProtocolError.code: CapabilityStatus.FAILURE,
        BlenderError.code: CapabilityStatus.FAILURE,
    })
    _STATUS_BY_EXCEPTION_TYPE.extend((
        (BlenderCancelledError, CapabilityStatus.CANCELLED),
        (BlenderNotFoundError, CapabilityStatus.UNAVAILABLE),
        (BlenderPermissionError, CapabilityStatus.PERMISSION_REQUIRED),
    ))


_build_status_tables()


class CapabilityCancellationError(Exception):
    def __init__(self, request: "CapabilityRequest") -> None:
        self.request = request
        super().__init__(f"capability request cancelled: {request.request_id}")


@dataclass(slots=True)
class CapabilityRequest:
    capability: str
    operation: str
    params: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    task_id: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancellation_token: Any = field(default=None, repr=False, compare=False)

    def is_cancelled(self) -> bool:
        token = self.cancellation_token
        if token is None:
            return False
        checker = getattr(token, "is_cancelled", None)
        return bool(checker()) if callable(checker) else False

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise CapabilityCancellationError(self)
        token = self.cancellation_token
        raise_checker = getattr(token, "raise_if_cancelled", None)
        if callable(raise_checker):
            raise_checker()

    def request_cancellation(self, reason: str = "cancelled", source: str = "capability_registry") -> bool:
        token = self.cancellation_token
        if token is None:
            return False
        request = getattr(token, "request", None)
        if callable(request):
            try:
                request(reason, source=source)
            except TypeError:
                request(reason)
            return True
        cancel = getattr(token, "cancel", None)
        if callable(cancel):
            cancel()
            return True
        return False

    cancel = request_cancellation

    def __post_init__(self) -> None:
        self.capability = str(self.capability or "")
        self.operation = str(self.operation or "")
        self.task_id = str(self.task_id or "")
        self.request_id = str(self.request_id or "") or uuid.uuid4().hex

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "operation": self.operation,
            "params": dict(self.params),
            "context": dict(self.context),
            "task_id": self.task_id,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilityRequest":
        if not isinstance(data, dict):
            raise ValueError("capability request must be an object")
        if "capability" not in data or "operation" not in data:
            raise ValueError("capability request requires capability and operation")
        return cls(
            capability=str(data["capability"]),
            operation=str(data["operation"]),
            params=dict(data.get("params") or {}),
            context=dict(data.get("context") or {}),
            task_id=str(data.get("task_id") or ""),
            request_id=str(data.get("request_id") or uuid.uuid4().hex),
        )


@dataclass(slots=True)
class CapabilityResult:
    status: CapabilityStatus
    capability: str
    operation: str
    data: dict | None = None
    error: dict | None = None
    request_id: str = ""
    task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def success(self) -> bool:
        return self.status is CapabilityStatus.SUCCESS

    @property
    def cancelled(self) -> bool:
        return self.status is CapabilityStatus.CANCELLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "capability": self.capability,
            "operation": self.operation,
            "data": self.data,
            "error": self.error,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "metadata": dict(self.metadata),
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilityResult":
        if not isinstance(data, dict):
            raise ValueError("capability result must be an object")
        if "status" not in data:
            raise ValueError("capability result is missing status")
        return cls(
            status=CapabilityStatus.from_value(data["status"]),
            capability=str(data.get("capability") or ""),
            operation=str(data.get("operation") or ""),
            data=data.get("data") if isinstance(data.get("data"), dict) else None,
            error=data.get("error") if isinstance(data.get("error"), dict) else None,
            request_id=str(data.get("request_id") or ""),
            task_id=str(data.get("task_id") or ""),
            metadata=dict(data.get("metadata") or {}),
            duration_ms=float(data.get("duration_ms") or 0.0),
        )

    @classmethod
    def from_value(
        cls,
        value: Any,
        request: CapabilityRequest,
        started: float,
    ) -> "CapabilityResult":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            raw_status = value.get("status")
            if raw_status:
                try:
                    status = CapabilityStatus(raw_status)
                except ValueError:
                    status = CapabilityStatus.SUCCESS if value.get("success") else CapabilityStatus.FAILURE
            else:
                status = CapabilityStatus.SUCCESS if value.get("success", True) else CapabilityStatus.FAILURE
            return cls(
                status=status,
                capability=request.capability,
                operation=request.operation,
                data=value.get("data") if isinstance(value.get("data"), dict) else (value if status is CapabilityStatus.SUCCESS else None),
                error=value.get("error") if isinstance(value.get("error"), dict) else None,
                request_id=request.request_id,
                task_id=request.task_id,
                metadata=dict(value.get("metadata") or {}),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return cls(
            status=CapabilityStatus.SUCCESS,
            capability=request.capability,
            operation=request.operation,
            data={"value": value},
            request_id=request.request_id,
            task_id=request.task_id,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    @classmethod
    def failure(
        cls,
        request: CapabilityRequest,
        status: CapabilityStatus,
        message: str,
        *,
        code: str = "",
        metadata: dict | None = None,
        started: float = 0.0,
    ) -> "CapabilityResult":
        return cls(
            status=status,
            capability=request.capability,
            operation=request.operation,
            error={"error": code or status.value, "message": message},
            request_id=request.request_id,
            task_id=request.task_id,
            metadata=dict(metadata or {}),
            duration_ms=(time.perf_counter() - started) * 1000 if started else 0.0,
        )


@dataclass(slots=True)
class CapabilityEvent:
    type: str
    message: str
    capability: str = ""
    operation: str = ""
    status: str = ""
    task_id: str = ""
    request_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "capability": self.capability,
            "operation": self.operation,
            "status": self.status,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilityEvent":
        if not isinstance(data, dict) or "type" not in data:
            raise ValueError("capability event must be an object with type")
        return cls(
            type=str(data["type"]),
            message=str(data.get("message") or ""),
            capability=str(data.get("capability") or ""),
            operation=str(data.get("operation") or ""),
            status=str(data.get("status") or ""),
            task_id=str(data.get("task_id") or ""),
            request_id=str(data.get("request_id") or ""),
            metadata=dict(data.get("metadata") or {}),
            timestamp=float(data.get("timestamp") or time.time()),
        )


class Capability(Protocol):
    name: str

    def capabilities(self) -> dict:
        ...

    def can_execute(self, operation: str) -> bool:
        ...

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        ...

    def cancel(self, task_id: str = "", *, request_id: str = "") -> bool:
        ...


class CoreEventBusPublisher:
    """Publish capability events through an injected S1 EventBus-like object."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus

    def publish(self, event: CapabilityEvent) -> None:
        self._bus.emit(event.type, event.to_dict())


class CapabilityRegistry:
    """Select and execute capabilities while leaving authorization to Core."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._active_requests: dict[str, CapabilityRequest] = {}
        self._active_lock = threading.RLock()

    def register(self, capability: Capability) -> None:
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    @staticmethod
    def _active_key(request: CapabilityRequest) -> str:
        return request.request_id

    def _register_active(self, request: CapabilityRequest) -> bool:
        key = self._active_key(request)
        with self._active_lock:
            if key in self._active_requests:
                return False
            self._active_requests[key] = request
            return True

    def _release_active(self, request: CapabilityRequest) -> None:
        with self._active_lock:
            self._active_requests.pop(self._active_key(request), None)

    def _active_for_cancel(self, capability: str, task_id: str, request_id: str = "") -> list[CapabilityRequest]:
        with self._active_lock:
            requests = list(self._active_requests.values())
        if request_id:
            return [
                request
                for request in requests
                if request.request_id == request_id and (not capability or request.capability == capability)
            ]
        if capability:
            requests = [request for request in requests if request.capability == capability]
        if task_id:
            requests = [request for request in requests if request.task_id == task_id]
        return requests

    def capabilities(self) -> list[dict]:
        reports = []
        for capability in self._capabilities.values():
            try:
                report = capability.capabilities()
            except Exception as exc:  # noqa: BLE001 - capability discovery is isolated
                report = {"name": capability.name, "available": False, "error": str(exc)}
            reports.append(report)
        return reports

    @staticmethod
    def _authorized(value: Any) -> bool:
        if hasattr(value, "allow"):
            return bool(getattr(value, "allow"))
        if isinstance(value, dict):
            return bool(value.get("allow", value.get("authorized", False)))
        return bool(value)

    def execute(
        self,
        request: CapabilityRequest,
        *,
        authorize: Callable[[CapabilityRequest], Any] | None = None,
        event_publisher: CoreEventBusPublisher | None = None,
    ) -> CapabilityResult:
        started = time.perf_counter()
        if not request.capability or not request.operation:
            return CapabilityResult.failure(request, CapabilityStatus.INVALID_REQUEST, "capability and operation are required", started=started)
        capability = self.get(request.capability)
        if capability is None:
            return CapabilityResult.failure(request, CapabilityStatus.UNAVAILABLE, f"capability unavailable: {request.capability}", started=started)
        if not capability.can_execute(request.operation):
            return CapabilityResult.failure(request, CapabilityStatus.INVALID_REQUEST, f"operation unsupported: {request.operation}", started=started)
        if authorize is not None:
            try:
                if not self._authorized(authorize(request)):
                    return CapabilityResult.failure(request, CapabilityStatus.PERMISSION_REQUIRED, "permission was not granted", started=started)
            except Exception as exc:  # noqa: BLE001 - permission errors are structured
                return CapabilityResult.failure(request, CapabilityStatus.PERMISSION_REQUIRED, str(exc) or type(exc).__name__, started=started)
        try:
            request.check_cancelled()
        except CapabilityCancellationError:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "request cancelled before execution", started=started)
        except Exception as exc:  # noqa: BLE001 - external token errors are cancellation failures
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, str(exc) or type(exc).__name__, started=started)

        if not self._register_active(request):
            return CapabilityResult.failure(
                request,
                CapabilityStatus.FAILURE,
                "request identity is already active",
                code="duplicate_request_id",
                started=started,
            )
        try:
            if event_publisher is not None:
                event_publisher.publish(CapabilityEvent(type="capability_started", message=f"{request.capability}.{request.operation} started", capability=request.capability, operation=request.operation, task_id=request.task_id, request_id=request.request_id))
            try:
                result = CapabilityResult.from_value(capability.execute(request), request, started)
            except CapabilityCancellationError:
                result = CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "capability observed cancellation", started=started)
            except PermissionError as exc:
                result = CapabilityResult.failure(request, CapabilityStatus.PERMISSION_REQUIRED, str(exc) or type(exc).__name__, started=started)
            except Exception as exc:  # noqa: BLE001 - capability boundary isolation
                result = CapabilityResult.failure(request, CapabilityStatus.FAILURE, str(exc) or type(exc).__name__, code=type(exc).__name__, started=started)
            if event_publisher is not None:
                event_publisher.publish(CapabilityEvent(type=f"capability_{result.status.value}", message=result.error.get("message", "capability completed") if result.error else "capability completed", capability=request.capability, operation=request.operation, status=result.status.value, task_id=request.task_id, request_id=request.request_id))
            return result
        finally:
            self._release_active(request)

    @staticmethod
    def _supports_request_scope(target: Any) -> bool:
        cancel = getattr(target, "cancel", None)
        if not callable(cancel):
            return False
        try:
            parameters = inspect.signature(cancel).parameters
        except (TypeError, ValueError):
            return False
        return "request_id" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )

    def _cancel_target(self, target: Any, task_id: str, request_id: str) -> Any:
        cancel = getattr(target, "cancel", None)
        if not callable(cancel):
            return False
        if request_id:
            return cancel(task_id, request_id=request_id)
        return cancel(task_id)

    def cancel(
        self,
        capability: str,
        task_id: str = "",
        *,
        request_id: str = "",
        reason: str = "cancelled",
        source: str = "capability_registry",
    ) -> CapabilityResult:
        request = CapabilityRequest(capability=capability, operation="cancel", task_id=task_id)
        target = self.get(capability) if capability else None
        if capability and target is None:
            return CapabilityResult.failure(request, CapabilityStatus.UNAVAILABLE, f"capability unavailable: {capability}")
        active = self._active_for_cancel(capability, task_id, request_id)
        if not active:
            return CapabilityResult.failure(
                request,
                CapabilityStatus.FAILURE,
                "no active request to cancel",
                metadata={"active_requests": 0},
            )
        if len(active) > 1:
            return CapabilityResult.failure(
                request,
                CapabilityStatus.FAILURE,
                "ambiguous cancellation target; specify request_id",
                code="ambiguous_request",
                metadata={"active_requests": len(active)},
            )
        active_request = active[0]
        if request_id and active_request.capability != capability:
            return CapabilityResult.failure(
                request,
                CapabilityStatus.FAILURE,
                "request identity is not active for this capability",
                code="request_not_active",
                metadata={"active_requests": 1},
            )
        token_accepted = False
        target_accepted = False
        errors: list[str] = []
        try:
            token_accepted = active_request.request_cancellation(reason, source=source)
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)
        if target is not None:
            try:
                if request_id:
                    if self._supports_request_scope(target):
                        value = self._cancel_target(target, task_id, request_id)
                        target_accepted = bool(getattr(value, "success", value))
                else:
                    value = self._cancel_target(target, task_id, "")
                    target_accepted = bool(getattr(value, "success", value))
            except Exception as exc:  # noqa: BLE001 - cancellation boundary isolation
                errors.append(str(exc) or type(exc).__name__)
        if token_accepted or target_accepted:
            return CapabilityResult(
                status=CapabilityStatus.CANCELLED,
                capability=active_request.capability,
                operation="cancel",
                request_id=request.request_id,
                task_id=active_request.task_id,
                metadata={"active_requests": 1, "errors": errors},
            )
        message = "capability did not accept cancellation"
        if request_id and not self._supports_request_scope(target) and not token_accepted:
            message = "capability does not support request-scoped cancellation"
        if errors:
            message = f"{message}: {'; '.join(errors)}"
        return CapabilityResult.failure(request, CapabilityStatus.FAILURE, message, metadata={"active_requests": 1})

    def cancel_request(
        self,
        request_id: str,
        *,
        reason: str = "cancelled",
        source: str = "capability_registry",
    ) -> CapabilityResult:
        active = self._active_for_cancel("", "", request_id)
        if not active:
            request = CapabilityRequest(capability="", operation="cancel", task_id="")
            return CapabilityResult.failure(
                request,
                CapabilityStatus.FAILURE,
                "no active request to cancel",
                metadata={"active_requests": 0},
            )
        if len(active) > 1:
            request = CapabilityRequest(capability="", operation="cancel", task_id="")
            return CapabilityResult.failure(
                request,
                CapabilityStatus.FAILURE,
                "ambiguous cancellation target; specify request_id",
                code="ambiguous_request",
                metadata={"active_requests": len(active)},
            )
        target_request = active[0]
        return self.cancel(
            target_request.capability,
            target_request.task_id,
            request_id=request_id,
            reason=reason,
            source=source,
        )


def register_status_codes(mapping: dict[str, str | CapabilityStatus]) -> int:
    """Extend the exact-match error-code -> status table.

    M2.15. A capability layer that lives *outside* this package - the native
    Windows layer is S1 core, and ``test_contract_module_has_no_s1_core_dependency``
    correctly forbids this module from importing it - registers its own codes
    here, at construction time, so the dependency runs core -> integration and
    never the other way round.

    Only string codes and :class:`CapabilityStatus` values are accepted, and an
    unknown status is refused. A caller cannot widen the vocabulary by accident.

    Returns the number of codes registered.
    """
    added = 0
    for code, status in (mapping or {}).items():
        if not isinstance(code, str) or not code:
            raise ValueError(f"status codes must be non-empty strings, got {code!r}")
        if isinstance(status, CapabilityStatus):
            value = status
        else:
            try:
                value = CapabilityStatus(str(status))
            except ValueError as exc:
                raise ValueError(
                    f"unknown CapabilityStatus {status!r} for code {code!r}") from exc
        _STATUS_BY_ERROR_CODE[code] = value
        added += 1
    return added


class _LayerAdapter:
    name = "layer"
    _operations: tuple[str, ...] = ()

    def __init__(self, layer: Any) -> None:
        self.layer = layer

    def capabilities(self) -> dict:
        detector = getattr(self.layer, "detect_capabilities", None)
        if callable(detector):
            try:
                details = detector()
            except Exception as exc:  # noqa: BLE001 - discovery is isolated
                details = {"available": False, "error": str(exc)}
        else:
            detector = getattr(self.layer, "detect", None)
            if callable(detector):
                try:
                    detected = detector()
                    details = {"available": bool(getattr(detected, "installed", True)), "details": str(detected)}
                except Exception as exc:  # noqa: BLE001
                    details = {"available": False, "error": str(exc)}
            else:
                details = {"available": True}
        return {"name": self.name, "operations": list(self._operations_for_layer()), **details}

    def _operations_for_layer(self) -> tuple[str, ...]:
        """Operations this adapter advertises. Overridden where the truth is
        discovered from the layer rather than fixed by the protocol."""
        return self._operations

    def can_execute(self, operation: str) -> bool:
        return operation in self._operations_for_layer()

    def _dispatch(self, operation: str, params: dict) -> Any:
        method = getattr(self.layer, operation)
        return method(**dict(params))

    @contextmanager
    def _correlation_scope(self, request: CapabilityRequest):
        """Bind the trusted S1 request identity on the layer for this call.

        S1 owns ``request_id`` (see the ratified cross-layer contract). Layers
        that expose ``request_correlation`` use it to put that same id on their
        wire envelope, so the Blender/Vision backend observes the id S1 owns
        rather than one the protocol layer minted for itself. Layers without
        the hook are unaffected.
        """
        scope = getattr(self.layer, "request_correlation", None)
        if not callable(scope):
            yield self.layer
            return
        with scope(request.request_id, request.context):
            yield self.layer

    @contextmanager
    def _cancellation_scope(self, request: CapabilityRequest):
        """Bind the request's cancellation token on the layer for this call.

        Layers exposing ``cancellation_scope`` can then honour a cancellation
        that arrives while the operation is already running, rather than only
        observing one that was set beforehand. Layers without the hook are
        unaffected.
        """
        scope = getattr(self.layer, "cancellation_scope", None)
        if not callable(scope) or request.cancellation_token is None:
            yield self.layer
            return
        with scope(request.cancellation_token):
            yield self.layer

    @contextmanager
    def _scopes(self, request: CapabilityRequest):
        """Bind every per-call layer scope this capability understands."""
        with self._correlation_scope(request):
            with self._cancellation_scope(request):
                yield self.layer

    @staticmethod
    def _status_for_exception(exc: BaseException) -> CapabilityStatus:
        """Map an exception onto a capability status by stable code.

        Status is decided by exact match against a known error code (or an exact
        exception type), never by searching the message for words like
        "invalid". That is what keeps an infrastructure failure from being
        misreported as caller-attributable invalid input. An unrecognised code
        is a plain failure; nothing here can produce ``INVALID_REQUEST``.
        """
        if isinstance(exc, CapabilityCancellationError):
            return CapabilityStatus.CANCELLED
        if isinstance(exc, PermissionError):
            return CapabilityStatus.PERMISSION_REQUIRED
        code = str(getattr(exc, "code", "") or "")
        if code:
            mapped = _STATUS_BY_ERROR_CODE.get(code)
            if mapped is not None:
                return mapped
        for exc_type, status in _STATUS_BY_EXCEPTION_TYPE:
            if isinstance(exc, exc_type):
                return status
        return CapabilityStatus.FAILURE

    @staticmethod
    def _cancellation_metadata(exc: BaseException) -> dict:
        """Ratified cancellation attribution to surface on the result.

        The contract requires that a cancellation which could not terminate its
        child be distinguishable from one that did, via stable result metadata
        rather than message wording. Only the two ratified keys are promoted; the
        rest of the diagnostics stay on the error object.
        """
        diagnostics = getattr(exc, "diagnostics", None)
        if not isinstance(diagnostics, dict):
            return {}
        metadata: dict = {}
        for key in ("cancellation_terminated", "interruptible"):
            if key in diagnostics:
                metadata[key] = diagnostics[key]
        return metadata

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        try:
            request.check_cancelled()
        except CapabilityCancellationError:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "request cancelled before execution")
        except Exception as exc:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, str(exc) or type(exc).__name__)
        if not self.can_execute(request.operation):
            return CapabilityResult.failure(request, CapabilityStatus.INVALID_REQUEST, f"unsupported operation: {request.operation}")
        try:
            with self._scopes(request):
                value = self._dispatch(request.operation, request.params)
            return CapabilityResult.from_value(value, request, time.perf_counter())
        except PermissionError as exc:
            return CapabilityResult.failure(request, CapabilityStatus.PERMISSION_REQUIRED, str(exc) or type(exc).__name__)
        except CapabilityCancellationError:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "capability observed cancellation")
        except Exception as exc:  # noqa: BLE001 - normalize implementation errors
            status = self._status_for_exception(exc)
            metadata = self._cancellation_metadata(exc)
            return CapabilityResult.failure(
                request, status, str(exc) or type(exc).__name__,
                code=getattr(exc, "code", type(exc).__name__),
                metadata=metadata or None,
            )

    def cancel(self, task_id: str = "", *, request_id: str = "") -> bool:
        cancel = getattr(self.layer, "cancel_task", None)
        if not callable(cancel):
            return False
        if request_id:
            try:
                parameters = inspect.signature(cancel).parameters
            except (TypeError, ValueError):
                return False
            if "request_id" not in parameters and not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            ):
                return False
            return bool(cancel(task_id, request_id=request_id))
        return bool(cancel(task_id))


class BlenderCapabilityAdapter(_LayerAdapter):
    name = "blender"
    _operations = BLENDER_OPERATION_NAMES


class VisionCapabilityAdapter(_LayerAdapter):
    name = "vision"

    #: The protocol ceiling: every operation the Vision contract defines. This
    #: is the upper bound, not the answer - three of these cannot be serviced by
    #: any shipped backend, and advertising them is what M2.14.1 removed.
    _operations = (
        "validate_image", "inspect", "inspect_render", "describe", "detect_objects",
        "compare", "extract_text", "analyze_ui",
    )

    #: Operations the layer serves itself, with no backend involved.
    #: ``validate_image`` reads the file header and ``inspect`` runs the
    #: deterministic inspector directly, so both work on a machine with no
    #: backend at all. They must not be filtered away by backend discovery.
    _LAYER_NATIVE = ("validate_image", "inspect")

    def _operations_for_layer(self) -> tuple[str, ...]:
        """Advertise exactly what the bound layer can actually do right now.

        M2.14.1. The advertised list used to be the fixed protocol tuple of eight
        names. Three of them - ``detect_objects``, ``extract_text`` and
        ``analyze_ui`` - have no shipped backend, so every call raised: the model
        was offered tools that could only fail.

        Having a method is not sufficient to advertise it. The layer defines all
        eight, because the protocol requires it to, and those three dispatch
        straight to ``_run``, which then finds no backend and raises. So an
        operation is advertised only when

        * the layer genuinely implements it, **and**
        * it is layer-native, or a registered backend currently supports it.

        Backend support is read from the layer itself rather than restated here,
        so registering a new backend needs no change to this file.
        """
        native = [op for op in self._LAYER_NATIVE
                  if callable(getattr(self.layer, op, None))]

        served = [
            op for op in self._operations
            if op not in native
            and callable(getattr(self.layer, op, None))
            and self._layer_supports(op)
        ]

        merged = list(dict.fromkeys(native + served))
        return tuple(merged) if merged else VISION_FALLBACK_OPERATIONS

    def _layer_supports(self, operation: str) -> bool:
        """Ask the layer whether a registered backend can serve ``operation``."""
        supported = getattr(self.layer, "is_operation_supported", None)
        if callable(supported):
            try:
                return bool(supported(operation))
            except Exception:  # noqa: BLE001 - discovery is isolated
                return False
        detector = getattr(self.layer, "detect_capabilities", None)
        if callable(detector):
            try:
                details = detector() or {}
            except Exception:  # noqa: BLE001 - discovery is isolated
                return False
            reported = details.get("supported_operations")
            if isinstance(reported, (list, tuple)):
                return operation in {str(op) for op in reported}
        return False


class WindowsCapabilityAdapter(_LayerAdapter):
    """Adapter for the M2.15 native Windows systems layer.

    Sits beside the Blender and Vision adapters and uses the same
    :meth:`_LayerAdapter.execute`, so the trusted ``request_id``, the cancellation
    token and the status vocabulary all arrive by the existing path. Nothing here
    re-implements any of it.

    The operation list is discovered from the layer rather than restated, so a
    machine without pywin32 advertises no operations instead of advertising ones
    that would raise.
    """

    name = "windows"

    #: Operations that change machine state. Declared here so the bridge can
    #: escalate exactly these to an explicit confirmation, which is what stops a
    #: model escalating from a query to a process-control primitive.
    #:
    #: M2.16 added the four window lifecycle verbs. They are mutating because they
    #: change what the user sees on their screen - a minimize is not a read, and
    #: classifying it SAFE on the grounds that it is "just a ShowWindow call"
    #: would be exactly the name-based reasoning this declaration exists to
    #: replace.
    _MUTATING = frozenset({
        "terminate",
        "window_focus",
        "window_minimize",
        "window_restore",
        "window_close",
        "mouse_move",
        "mouse_left_click",
        "mouse_right_click",
        "mouse_double_click",
        "key_press",
        "key_release",
        "key_tap",
        "hotkey",
        "type_text",
    })

    def _operations_for_layer(self) -> tuple[str, ...]:
        operations = getattr(self.layer, "operations", None)
        if callable(operations):
            try:
                found = operations()
            except Exception:  # noqa: BLE001 - discovery is isolated
                found = None
            if isinstance(found, (list, tuple)) and found:
                return tuple(str(op) for op in found)
        return ()

    def is_mutating(self, operation: str) -> bool:
        return operation in self._MUTATING

    @property
    def mutating_operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._MUTATING))


class BlenderVisionCapabilityAdapter(_LayerAdapter):
    name = "blender_vision"
    _operations = (
        "render_and_inspect", "render_and_describe", "render_and_compare", "inspect_render",
        "verify_render", "describe_render", "compare_renders",
    )

    @staticmethod
    def _merge_context(request: CapabilityRequest) -> tuple[dict, dict]:
        """Split trusted request context from call params for one integration call.

        Mirrors :class:`~integration.workflows.BlenderVisionWorkflowCapabilityAdapter`:
        the trusted ``request.context`` is the base and a caller-supplied
        ``params["context"]`` is layered on top, so S1 provenance (``source``,
        ``run_id``, ``task_id``) is never dropped. The integration still stamps
        its own ``integration_operation`` and internal ``request_id`` on top, so
        S2.3's internal correlation is unchanged.
        """
        params = dict(request.params or {})
        context = dict(request.context or {})
        embedded = params.pop("context", None)
        if isinstance(embedded, dict):
            context.update(embedded)
        return params, context

    def _dispatch_request(self, request: CapabilityRequest) -> Any:
        """Dispatch one integration call with the request's trusted context.

        ``_LayerAdapter._dispatch`` forwards ``params`` verbatim, which drops the
        trusted context Core attached to the request. Every S2.3 entry point
        accepts a ``context`` keyword, so pass the merged context explicitly.
        ``params`` is otherwise forwarded unchanged, and nothing is stored on the
        adapter, so concurrent requests stay isolated.
        """
        params, context = self._merge_context(request)
        method = getattr(self.layer, request.operation)
        return method(**{**params, "context": context})

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        try:
            request.check_cancelled()
        except CapabilityCancellationError:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "request cancelled before execution")
        except Exception as exc:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, str(exc) or type(exc).__name__)
        if not self.can_execute(request.operation):
            return CapabilityResult.failure(request, CapabilityStatus.INVALID_REQUEST, f"unsupported operation: {request.operation}")
        try:
            with self._scopes(request):
                value = self._dispatch_request(request)
        except BlenderVisionError as exc:
            status = CapabilityStatus.UNAVAILABLE if "unavailable" in exc.code else CapabilityStatus.FAILURE
            return CapabilityResult.failure(request, status, exc.message, code=exc.code)
        except CapabilityCancellationError:
            return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "capability observed cancellation")
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult.failure(request, CapabilityStatus.FAILURE, str(exc) or type(exc).__name__, code=type(exc).__name__)
        if isinstance(value, CapabilityResult):
            return value
        if hasattr(value, "success") and hasattr(value, "to_dict"):
            if not value.success:
                return CapabilityResult(status=CapabilityStatus.FAILURE, capability=request.capability, operation=request.operation, error=value.to_dict().get("error"), request_id=request.request_id, task_id=request.task_id)
            return CapabilityResult(status=CapabilityStatus.SUCCESS, capability=request.capability, operation=request.operation, data=value.to_dict().get("data"), request_id=request.request_id, task_id=request.task_id)
        return CapabilityResult.from_value(value, request, time.perf_counter())


class MockCapability:
    """Deterministic capability for Core-boundary tests."""

    name = "mock"

    def __init__(self, result: CapabilityResult | None = None, operations: tuple[str, ...] = ("execute",), cancel_result: bool = True) -> None:
        self.result = result
        self.operations = set(operations)
        self.cancel_result = cancel_result
        self.requests: list[CapabilityRequest] = []
        self.cancel_calls: list[str] = []

    def capabilities(self) -> dict:
        return {"name": self.name, "available": True, "operations": sorted(self.operations)}

    def can_execute(self, operation: str) -> bool:
        return operation in self.operations

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        request.check_cancelled()
        self.requests.append(request)
        if self.result is not None:
            return self.result
        return CapabilityResult(status=CapabilityStatus.SUCCESS, capability=self.name, operation=request.operation, data={"executed": True}, request_id=request.request_id, task_id=request.task_id)

    def cancel(self, task_id: str = "") -> bool:
        self.cancel_calls.append(task_id)
        return self.cancel_result


__all__ = [
    "Capability",
    "CapabilityCancellationError",
    "CapabilityEvent",
    "CapabilityRegistry",
    "CapabilityRequest",
    "CapabilityResult",
    "CapabilityStatus",
    "CoreEventBusPublisher",
    "BlenderCapabilityAdapter",
    "VisionCapabilityAdapter",
    "BlenderVisionCapabilityAdapter",
    "MockCapability",
]
