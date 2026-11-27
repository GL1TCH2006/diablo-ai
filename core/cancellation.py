from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class CancellationRequested(Exception):
    pass


class CancellationToken:
    """Thread-safe cooperative cancellation primitive."""

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._request: Optional["CancellationRequest"] = None

    def request(
        self,
        reason: str = "cancelled",
        *,
        source: str = "api",
        metadata: Optional[dict] = None,
        task_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> "CancellationRequest":
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
    def timestamp(self) -> Optional[float]:
        with self._lock:
            return self._request.timestamp if self._request else None

    @property
    def request_data(self) -> Optional["CancellationRequest"]:
        with self._lock:
            return self._request

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        request = self.request_data
        if request is not None:
            raise CancellationRequested(request)

    def reset(self) -> bool:
        with self._lock:
            if self._request is None:
                return False
            self._request = None
            self._event.clear()
            return True


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


def cancellation_requested(token: Any | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    try:
        if callable(checker):
            return bool(checker())
        return bool(getattr(token, "cancelled", False))
    except Exception:
        return True


def raise_if_cancelled(token: Any | None) -> None:
    if cancellation_requested(token):
        raise CancellationRequested("Cancellation requested.")
