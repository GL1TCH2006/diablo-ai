from __future__ import annotations

"""Headless UI controller and non-blocking worker orchestration."""

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .bridge import AgentBridge, MockAgentBridge
from .models import AgentEvent, UIActionResult, UIActivity, UIControllerError, UIStatus


@dataclass(slots=True)
class _WorkItem:
    job_id: str
    text: str


_STOP = object()


class UIController:
    """Own UI state, activity history, and the bridge worker boundary.

    The controller is deliberately independent of Tk widgets. A Tk view calls
    ``pump_events`` from its UI thread, while bridge work runs on one daemon
    worker thread and communicates through thread-safe queues.
    """

    _ALLOWED_TRANSITIONS = {
        UIStatus.IDLE: {UIStatus.READY, UIStatus.THINKING, UIStatus.ERROR, UIStatus.CLOSED},
        UIStatus.READY: {UIStatus.IDLE, UIStatus.THINKING, UIStatus.ERROR, UIStatus.CLOSED},
        UIStatus.THINKING: {
            UIStatus.EXECUTING,
            UIStatus.VERIFYING,
            UIStatus.SUCCESS,
            UIStatus.ERROR,
            UIStatus.CANCELLED,
            UIStatus.IDLE,
            UIStatus.READY,
            UIStatus.CLOSED,
        },
        UIStatus.EXECUTING: {
            UIStatus.VERIFYING,
            UIStatus.SUCCESS,
            UIStatus.ERROR,
            UIStatus.CANCELLED,
            UIStatus.READY,
            UIStatus.IDLE,
            UIStatus.CLOSED,
        },
        UIStatus.VERIFYING: {
            UIStatus.SUCCESS,
            UIStatus.ERROR,
            UIStatus.CANCELLED,
            UIStatus.READY,
            UIStatus.IDLE,
            UIStatus.CLOSED,
        },
        UIStatus.SUCCESS: {UIStatus.READY, UIStatus.THINKING, UIStatus.IDLE, UIStatus.CLOSED},
        UIStatus.CANCELLED: {UIStatus.READY, UIStatus.THINKING, UIStatus.IDLE, UIStatus.CLOSED},
        UIStatus.ERROR: {UIStatus.READY, UIStatus.THINKING, UIStatus.IDLE, UIStatus.CLOSED},
        UIStatus.CLOSED: set(),
    }

    def __init__(self, bridge: AgentBridge | None = None, *, start_worker: bool = False) -> None:
        self._owns_bridge = bridge is None
        self._bridge = bridge or MockAgentBridge()
        self._events: queue.Queue[AgentEvent] = queue.Queue()
        self._work: queue.Queue[Any] = queue.Queue()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._active_jobs: set[str] = set()
        self._state = UIStatus.READY
        self._state_history: list[UIStatus] = [UIStatus.READY]
        self._activities: list[UIActivity] = [
            UIActivity(role="system", text="Ready", status=UIStatus.READY.value)
        ]
        self._last_error: dict[str, Any] | None = None
        self._unsubscribe = self._bridge.subscribe_to_events(self._enqueue_event)
        self._compact = False
        if start_worker:
            self.start()

    @property
    def bridge(self) -> AgentBridge:
        return self._bridge

    @property
    def state(self) -> UIStatus:
        return self._state

    @property
    def status_text(self) -> str:
        return self._state.label

    @property
    def state_history(self) -> tuple[str, ...]:
        return tuple(item.value for item in self._state_history)

    @property
    def activities(self) -> tuple[UIActivity, ...]:
        return tuple(self._activities)

    @property
    def last_error(self) -> dict[str, Any] | None:
        return dict(self._last_error) if isinstance(self._last_error, dict) else None

    @property
    def is_busy(self) -> bool:
        return bool(self._active_jobs)

    @property
    def active_job_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._active_jobs))

    @property
    def event_queue_size(self) -> int:
        return self._events.qsize()

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        self._compact = bool(compact)

    def toggle_compact(self) -> bool:
        self._compact = not self._compact
        return self._compact

    def start(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="diablo-ui-worker",
                daemon=True,
            )
            self._thread.start()
            return True

    def _ensure_worker(self) -> None:
        if not self.start():
            raise UIControllerError("UI controller is closed", code="ui_closed")

    def _action_error(self, code: str, message: str) -> UIActionResult:
        error = {"error": code, "message": message}
        self._last_error = error
        self._activities.append(UIActivity(role="error", text=message, metadata=error))
        return UIActionResult(success=False, error=error)

    def send_message(self, text: str) -> UIActionResult:
        if self._closed:
            return self._action_error("ui_closed", "UI controller is closed")
        if not isinstance(text, str):
            return self._action_error("invalid_message", "Message must be text")
        clean_text = text.strip()
        if not clean_text:
            return self._action_error("empty_message", "Enter a message before sending")

        job_id = uuid.uuid4().hex
        self._activities.append(
            UIActivity(
                role="user",
                text=clean_text,
                status=UIStatus.THINKING.value,
                task_id=job_id,
            )
        )
        if not self._transition(UIStatus.THINKING):
            return self._action_error("invalid_state", "Cannot start a task from the current state")
        with self._lock:
            self._active_jobs.add(job_id)
        try:
            self._ensure_worker()
            self._work.put(_WorkItem(job_id=job_id, text=clean_text))
        except Exception as exc:
            with self._lock:
                self._active_jobs.discard(job_id)
            return self._action_error("worker_start_failed", str(exc))
        return UIActionResult(success=True, job_id=job_id)

    def cancel_task(self, task_id: str = "") -> UIActionResult:
        if self._closed:
            return self._action_error("ui_closed", "UI controller is closed")
        with self._lock:
            job_ids = [task_id] if task_id else list(self._active_jobs)
        if not job_ids:
            return self._action_error("no_active_task", "There is no active task to cancel")
        try:
            for job_id in job_ids:
                self._bridge.cancel_task(job_id)
        except Exception as exc:
            return self._action_error("cancel_failed", str(exc))
        return UIActionResult(success=True, job_id=job_id if len(job_ids) == 1 else "")

    def _transition(self, target: UIStatus) -> bool:
        with self._lock:
            if target is self._state:
                return True
            if target not in self._ALLOWED_TRANSITIONS[self._state]:
                return False
            self._state = target
            self._state_history.append(target)
        return True

    def _enqueue_event(self, payload: AgentEvent | dict) -> None:
        if self._closed:
            return
        if isinstance(payload, AgentEvent):
            event = payload
        else:
            try:
                event = AgentEvent.from_dict(payload)
            except Exception as exc:
                event = AgentEvent(
                    type="error",
                    message="Malformed agent event",
                    status=UIStatus.ERROR.value,
                    metadata={
                        "error": "malformed_event",
                        "reason": str(exc),
                        "raw_type": type(payload).__name__,
                    },
                )
        self._events.put(event)

    def _worker_loop(self) -> None:
        while True:
            item = self._work.get()
            try:
                if item is _STOP:
                    return
                if isinstance(item, _WorkItem):
                    self._run_work(item)
            finally:
                self._work.task_done()

    def _run_work(self, item: _WorkItem) -> None:
        try:
            result = self._bridge.submit_message(item.text, task_id=item.job_id)
            if isinstance(result, AgentEvent):
                self._enqueue_event(result)
        except Exception as exc:
            if not self._closed:
                self._enqueue_event(
                    AgentEvent(
                        type="error",
                        message=str(exc) or type(exc).__name__,
                        status=UIStatus.ERROR.value,
                        task_id=item.job_id,
                        metadata={
                            "error": "bridge_failure",
                            "exception": type(exc).__name__,
                        },
                    )
                )
        finally:
            with self._lock:
                self._active_jobs.discard(item.job_id)

    def pump_events(self, max_events: int | None = None) -> int:
        processed = 0
        while max_events is None or processed < max_events:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._apply_event(event)
            processed += 1
        return processed

    process_events = pump_events

    def _apply_event(self, event: AgentEvent) -> None:
        target: UIStatus | None = None
        if event.status:
            try:
                target = UIStatus.from_value(event.status)
            except ValueError as exc:
                self._last_error = {
                    "error": "invalid_event_status",
                    "message": str(exc),
                    "event": event.to_dict(),
                }

        if target is not None and not self._transition(target):
            self._last_error = {
                "error": "invalid_state_transition",
                "message": f"cannot transition from {self._state.value} to {target.value}",
                "event": event.to_dict(),
            }

        role = str(event.metadata.get("role") or "")
        if not role:
            if event.type == "message":
                role = "diablo"
            elif event.type in {"error", "cancelled"}:
                role = "error" if event.type == "error" else "status"
            elif event.type in {"status", "success"}:
                role = "status"
            else:
                role = "system"
        self._activities.append(
            UIActivity(
                role=role,
                text=event.message,
                timestamp=event.timestamp,
                status=event.status,
                task_id=event.task_id,
                metadata=dict(event.metadata),
            )
        )
        if event.type == "error" or target == UIStatus.ERROR:
            source_code = str(event.metadata.get("error") or "agent_error")
            self._last_error = {
                "error": source_code,
                "message": event.message,
                "event": event.to_dict(),
            }

    def wait_for_idle(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            self.pump_events()
            if not self.is_busy and self.event_queue_size == 0:
                return True
            time.sleep(0.005)
        self.pump_events()
        return not self.is_busy and self.event_queue_size == 0

    def close(self, timeout: float = 1.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active_jobs = list(self._active_jobs)
            thread = self._thread
        if self._unsubscribe is not None:
            self._unsubscribe()
        for job_id in active_jobs:
            try:
                self._bridge.cancel_task(job_id)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            self._work.put(_STOP)
            thread.join(timeout=max(0.0, timeout))
        if self._owns_bridge:
            self._bridge.close()
        self._state = UIStatus.CLOSED
        self._state_history.append(UIStatus.CLOSED)

    def __enter__(self) -> "UIController":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


__all__ = ["UIController"]
