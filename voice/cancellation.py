from __future__ import annotations

"""Reusable cancellation tokens and voice interruption routing."""

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .events import VoiceEvent


class CancellationError(Exception):
    """Raised by cooperative operations when cancellation is observed."""

    def __init__(self, request: "CancellationRequest") -> None:
        self.request = request
        super().__init__(request.reason)


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    task_id: str
    source: str = "api"
    reason: str = "cancelled"
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class CancellationOutcome:
    request: CancellationRequest
    accepted: bool
    bridge_accepted: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "accepted": self.accepted,
            "bridge_accepted": self.bridge_accepted,
            "error": self.error,
        }


class CancellationToken:
    """Thread-safe cooperative cancellation primitive."""

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._request: CancellationRequest | None = None

    def request(
        self,
        reason: str = "cancelled",
        *,
        source: str = "api",
        metadata: dict | None = None,
        task_id: str | None = None,
        timestamp: float | None = None,
    ) -> CancellationRequest:
        with self._lock:
            if self._request is not None:
                return self._request
            self._request = CancellationRequest(
                task_id=task_id or self.task_id,
                source=source,
                reason=reason,
                timestamp=timestamp if timestamp is not None else time.time(),
                metadata=dict(metadata or {}),
            )
            self._event.set()
            return self._request

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._request.reason if self._request else ""

    @property
    def source(self) -> str:
        with self._lock:
            return self._request.source if self._request else ""

    @property
    def timestamp(self) -> float | None:
        with self._lock:
            return self._request.timestamp if self._request else None

    @property
    def request_data(self) -> CancellationRequest | None:
        with self._lock:
            return self._request

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        request = self.request_data
        if request is not None:
            raise CancellationError(request)

    def reset(self) -> bool:
        with self._lock:
            if self._request is None:
                return False
            self._request = None
            self._event.clear()
            return True


class CancellationBridge(Protocol):
    def cancel_task(self, task_id: str = "") -> bool:
        ...


class InterruptionManager:
    """Connect voice cancellation requests to a bridge and cooperative tokens."""

    def __init__(
        self,
        bridge: CancellationBridge | None = None,
        event_sink: Callable[[VoiceEvent], None] | None = None,
        cancel_callback: Callable[[str], Any] | None = None,
    ) -> None:
        self._bridge = bridge
        self._event_sink = event_sink
        self._cancel_callback = cancel_callback
        self._lock = threading.RLock()
        self._tokens: dict[str, CancellationToken] = {}
        self._outcomes: dict[str, CancellationOutcome] = {}
        self._subscribers: list[Callable[[VoiceEvent], None]] = []
        self._closed = False

    def subscribe(self, callback: Callable[[VoiceEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def _emit(self, event: VoiceEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                continue
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception:
                pass

    def token_for(self, task_id: str) -> CancellationToken:
        key = task_id or "*"
        with self._lock:
            token = self._tokens.get(key)
            if token is None:
                token = CancellationToken(task_id=task_id)
                self._tokens[key] = token
            return token

    register_task = token_for

    def is_cancelled(self, task_id: str) -> bool:
        return self.token_for(task_id).is_cancelled()

    @staticmethod
    def _bridge_result(value: Any) -> tuple[bool, str]:
        if hasattr(value, "success"):
            success = getattr(value, "success")
            if not isinstance(success, bool):
                return False, "bridge returned a malformed success flag"
            if success:
                return True, ""
            error = getattr(value, "error", None)
            if isinstance(error, Mapping):
                return False, str(error.get("message") or error.get("error") or "bridge rejected cancellation")
            return False, str(error or "bridge rejected cancellation")
        if isinstance(value, bool):
            return (True, "") if value else (False, "bridge rejected cancellation")
        if isinstance(value, Mapping):
            success = value.get("success")
            if not isinstance(success, bool):
                return False, "bridge returned a malformed cancellation result"
            if success:
                return True, ""
            error = value.get("error")
            if isinstance(error, Mapping):
                return False, str(error.get("message") or error.get("error") or "bridge rejected cancellation")
            return False, str(error or "bridge rejected cancellation")
        return False, "bridge returned a malformed cancellation result"

    def request_cancel(
        self,
        task_id: str = "",
        *,
        source: str = "voice",
        reason: str = "user_interruption",
        metadata: dict | None = None,
    ) -> CancellationOutcome:
        key = task_id or "*"
        wildcard = not bool(task_id)
        with self._lock:
            if self._closed:
                request = CancellationRequest(
                    task_id=task_id,
                    source=source,
                    reason=reason,
                    metadata=dict(metadata or {}),
                )
                return CancellationOutcome(request=request, accepted=False, error="interruption_manager_closed")
            existing = self._outcomes.get(key)
            if existing is not None:
                return existing
            request = CancellationRequest(
                task_id=task_id,
                source=source,
                reason=reason,
                metadata=dict(metadata or {}),
            )
            token = self.token_for(task_id)
            token.request(
                reason,
                source=source,
                metadata=metadata,
                task_id=task_id,
                timestamp=request.timestamp,
            )
            self._outcomes[key] = CancellationOutcome(request=request, accepted=True)
            outcome = self._outcomes[key]

        self._emit(
            VoiceEvent(
                type="task_cancel_requested",
                message="Cancellation requested",
                status="cancelling",
                source=source,
                task_id=task_id,
                metadata={"reason": reason, **dict(metadata or {})},
            )
        )
        bridge_accepted = False
        error = ""
        if self._cancel_callback is not None:
            try:
                bridge_accepted, error = self._bridge_result(self._cancel_callback(task_id))
            except Exception as exc:
                error = str(exc) or type(exc).__name__
        elif self._bridge is not None:
            try:
                bridge_accepted, error = self._bridge_result(self._bridge.cancel_task(task_id))
            except Exception as exc:
                error = str(exc) or type(exc).__name__
        with self._lock:
            outcome.bridge_accepted = bridge_accepted
            outcome.error = error
        if error:
            self._emit(
                VoiceEvent(
                    type="task_cancel_failed",
                    message=error,
                    status="error",
                    source=source,
                    task_id=task_id,
                    metadata={"reason": reason, "bridge_accepted": bridge_accepted},
                )
            )
            if wildcard:
                self.release_task("")
            return outcome
        self._emit(
            VoiceEvent(
                type="task_cancelled",
                message="Task cancellation propagated",
                status="cancelled",
                source=source,
                task_id=task_id,
                metadata={
                    "reason": reason,
                    "bridge_accepted": bridge_accepted,
                    "cooperative_token": True,
                },
            )
        )
        if wildcard:
            self.release_task("")
        return outcome

    cancel_all = request_cancel

    def release_task(self, task_id: str) -> None:
        key = task_id or "*"
        with self._lock:
            self._tokens.pop(key, None)
            self._outcomes.pop(key, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()


__all__ = [
    "CancellationBridge",
    "CancellationError",
    "CancellationOutcome",
    "CancellationRequest",
    "CancellationToken",
    "InterruptionManager",
]
