from __future__ import annotations

"""Agent bridge protocols and deterministic adapters for the companion UI."""

import threading
import uuid
from collections.abc import Mapping
from typing import Any, Callable, Protocol

from .models import AgentEvent, UIStatus


class AgentBridgeError(Exception):
    """Raised when an agent bridge cannot accept or process work."""


class AgentBridge(Protocol):
    """Minimal asynchronous-friendly boundary between UI and future agent code."""

    def submit_message(self, text: str, task_id: str = "") -> Any:
        ...

    def cancel_task(self, task_id: str = "") -> bool:
        ...

    def subscribe_to_events(self, callback: Callable[[AgentEvent | dict], None]) -> Callable[[], None]:
        ...

    def current_status(self) -> UIStatus:
        ...

    def close(self) -> None:
        ...


class MockAgentBridge:
    """Deterministic bridge used by the standalone UI and headless tests."""

    def __init__(self, *, step_delay: float = 0.0, fail_on: str | None = None) -> None:
        self.step_delay = max(0.0, float(step_delay))
        self.fail_on = fail_on
        self._callbacks: list[Callable[[AgentEvent | dict], None]] = []
        self._lock = threading.RLock()
        self._status = UIStatus.READY
        self._closed = False
        self._cancelled: dict[str, threading.Event] = {}
        self._pending_cancelled: set[str] = set()

    def subscribe_to_events(self, callback: Callable[[AgentEvent | dict], None]) -> Callable[[], None]:
        with self._lock:
            self._callbacks.append(callback)
        def unsubscribe() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return unsubscribe

    def _emit(self, event: AgentEvent) -> None:
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                continue

    def _set_status(self, status: UIStatus, message: str, task_id: str, event_type: str = "status") -> None:
        with self._lock:
            self._status = status
        self._emit(
            AgentEvent(
                type=event_type,
                message=message,
                status=status.value,
                task_id=task_id,
            )
        )

    def _wait(self, cancelled: threading.Event) -> bool:
        if self.step_delay:
            return cancelled.wait(self.step_delay)
        return cancelled.is_set()

    def submit_message(self, text: str, task_id: str = "") -> Any:
        if not isinstance(text, str) or not text.strip():
            raise AgentBridgeError("message must not be empty")
        with self._lock:
            if self._closed:
                raise AgentBridgeError("agent bridge is closed")
            task_id = task_id or uuid.uuid4().hex
            cancelled = threading.Event()
            self._cancelled[task_id] = cancelled
            if task_id in self._pending_cancelled:
                self._pending_cancelled.discard(task_id)
                cancelled.set()

        self._set_status(UIStatus.THINKING, "Message received", task_id)
        if self.fail_on and self.fail_on.casefold() in text.casefold():
            self._set_status(UIStatus.ERROR, "Mock bridge failure", task_id, event_type="error")
            self._finish(task_id)
            return {"success": False, "error": "mock_failure"}

        if self._wait(cancelled):
            self._set_status(UIStatus.READY, "Task cancelled", task_id, event_type="cancelled")
            self._finish(task_id)
            return {"success": False, "cancelled": True}

        self._set_status(UIStatus.EXECUTING, "Executing request", task_id)
        if self._wait(cancelled):
            self._set_status(UIStatus.READY, "Task cancelled", task_id, event_type="cancelled")
            self._finish(task_id)
            return {"success": False, "cancelled": True}

        self._set_status(UIStatus.VERIFYING, "Verifying result", task_id)
        if self._wait(cancelled):
            self._set_status(UIStatus.READY, "Task cancelled", task_id, event_type="cancelled")
            self._finish(task_id)
            return {"success": False, "cancelled": True}

        self._emit(
            AgentEvent(
                type="message",
                message=f"Mock response: {text}",
                task_id=task_id,
                metadata={"role": "diablo", "source": "mock"},
            )
        )
        self._set_status(UIStatus.SUCCESS, "Task completed", task_id)
        self._finish(task_id)
        return {"success": True}

    def _finish(self, task_id: str) -> None:
        with self._lock:
            self._cancelled.pop(task_id, None)

    def cancel_task(self, task_id: str = "") -> bool:
        with self._lock:
            if task_id:
                event = self._cancelled.get(task_id)
            else:
                events = list(self._cancelled.values())
                event = events[0] if events else None
            if event is None:
                if task_id:
                    self._pending_cancelled.add(task_id)
                    return True
                return False
            event.set()
            return True

    def current_status(self) -> UIStatus:
        with self._lock:
            return self._status

    def close(self) -> None:
        with self._lock:
            self._closed = True
            events = list(self._cancelled.values())
        for event in events:
            event.set()


class CoreEventBusBridge:
    """Adapter for the existing S1 ``EventBus`` without coupling UI to Agent."""

    def __init__(self, bus: Any, submitter: Callable[..., Any] | None = None, canceler: Callable[..., Any] | None = None) -> None:
        self._bus = bus
        self._submitter = submitter
        self._canceler = canceler
        self._callbacks: list[Callable[[AgentEvent | dict], None]] = []
        self._subscribed = False
        self._status = UIStatus.READY
        self._closed = False

    def subscribe_to_events(self, callback: Callable[[AgentEvent | dict], None]) -> Callable[[], None]:
        self._callbacks.append(callback)
        if not self._subscribed:
            self._bus.subscribe("status", self._on_status)
            self._bus.subscribe("task", self._on_task)
            self._bus.subscribe("task_done", self._on_task_done)
            self._bus.subscribe("tool", self._on_tool)
            self._subscribed = True

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
        return unsubscribe

    def _emit(self, event: AgentEvent) -> None:
        for callback in list(self._callbacks):
            try:
                callback(event)
            except Exception:
                continue

    def _status_value(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return ""
        try:
            status = UIStatus.from_value(normalized).value
        except ValueError:
            return ""
        if status in {"ready", "thinking", "executing", "verifying", "success", "cancelled", "error"}:
            return status
        return ""

    def _on_status(self, data: dict) -> None:
        value = data.get("value") or data.get("state") or ""
        status = self._status_value(value)
        if status:
            try:
                self._status = UIStatus.from_value(status)
            except ValueError:
                status = ""
        self._emit(
            AgentEvent(
                type="status",
                message=f"Agent state: {value or 'unknown'}",
                status=status,
                metadata={"source_event": data},
            )
        )

    def _on_task(self, data: dict) -> None:
        lines = data.get("lines") or []
        message = "\n".join(str(line) for line in lines) if isinstance(lines, list) else str(lines)
        self._emit(
            AgentEvent(
                type="activity",
                message=message or "Task update",
                status=UIStatus.EXECUTING.value,
                metadata={"source_event": data},
            )
        )

    def _on_task_done(self, data: dict) -> None:
        source_status = data.get("status") or data.get("state") or ""
        status = self._status_value(source_status)
        if not status:
            status = UIStatus.ERROR.value
        outcome = data.get("outcome")
        if isinstance(outcome, Mapping):
            outcome_text = str(outcome.get("message") or outcome.get("outcome") or "")
        else:
            outcome_text = str(outcome or "")
        if not outcome_text:
            outcome_text = {
                UIStatus.SUCCESS.value: "Task completed",
                UIStatus.CANCELLED.value: "Task cancelled",
                UIStatus.ERROR.value: "Task failed",
            }.get(status, "Task finished with an unknown status")
        event_type = {
            UIStatus.SUCCESS.value: "success",
            UIStatus.CANCELLED.value: "cancelled",
        }.get(status, "error")
        self._status = UIStatus.from_value(status)
        self._emit(
            AgentEvent(
                type=event_type,
                message=outcome_text[:4000],
                status=status,
                task_id=str(data.get("task_id") or data.get("run_id") or ""),
                metadata={"source_event": data},
            )
        )

    def _on_tool(self, data: dict) -> None:
        tool_status = str(data.get("status") or "").lower()
        if tool_status in {"error", "blocked", "denied"}:
            status = UIStatus.ERROR.value
            message = f"{data.get('tool', 'tool')}: {data.get('detail', tool_status)}"
        elif tool_status == "cancelled":
            status = UIStatus.CANCELLED.value
            message = f"{data.get('tool', 'tool')}: cancelled"
        elif tool_status in {"running", "pending_confirmation"}:
            status = UIStatus.EXECUTING.value
            message = f"{data.get('tool', 'tool')}: {tool_status.replace('_', ' ')}"
        else:
            status = ""
            message = f"{data.get('tool', 'tool')}: {tool_status or 'update'}"
        self._emit(
            AgentEvent(
                type="activity",
                message=message,
                status=status,
                metadata={"source_event": data},
            )
        )

    def submit_message(self, text: str, task_id: str = "") -> Any:
        if self._closed:
            raise AgentBridgeError("agent bridge is closed")
        if self._submitter is None:
            raise AgentBridgeError("no agent submitter is configured")
        return self._submitter(text, task_id=task_id)

    def cancel_task(self, task_id: str = "") -> bool:
        if self._canceler is None:
            return False
        result = self._canceler(task_id)
        return bool(result)

    def current_status(self) -> UIStatus:
        return self._status

    def close(self) -> None:
        self._closed = True
        self._callbacks.clear()


__all__ = [
    "AgentBridge",
    "AgentBridgeError",
    "MockAgentBridge",
    "CoreEventBusBridge",
]
