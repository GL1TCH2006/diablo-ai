"""M2.16.UI - the Diablo edge companion's state machine and controller.

Why this module exists separately from the view
==============================================

The companion is a *product form*: a small presence at a screen edge that peeks
out when called, accepts a task, shows progress, reports the result and retracts.
None of that is rendering. If the state machine lived inside a Tk widget tree it
could only be tested by building a window, which is exactly the testing that tends
to be skipped on a headless box.

So the logic lives here, with no import of ``tkinter`` anywhere in the module, and
``ui/edge_companion.py`` is a thin renderer that reads a :class:`CompanionSnapshot`
and draws it. A test can drive this controller from initial state through
activation, submission, cancellation, failure and shutdown with no display.

The state machine
=================

Seven states, and every transition is declared:

===============  =========================================================
``SLEEPING``     a small presence docked to an edge; no task, no input
``AWAKENING``    sliding/peeking out of the edge
``LISTENING``    expanded, waiting for a task
``THINKING``     task accepted, backend has it, no tool confirmed yet
``EXECUTING``    backend reported real execution progress
``RETURNING``    terminal result reached; dwelling before retraction
``ERROR``        something failed; awaiting acknowledgement or retraction
===============  =========================================================

Two properties are enforced here rather than left to the view.

**Cancelling is a request, not an outcome.** ``cancel()`` asks the backend to stop
and records ``cancellation_requested``. The companion does *not* move to a
terminal state until the backend says so, because "the user pressed stop" and "the
task stopped" are different facts and the UI must not conflate them.

**Terminal status is derived, never inferred.** The companion reaches
``RETURNING`` because a backend event said the task finished, not because errors
stopped arriving.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

__all__ = [
    "CompanionState",
    "CompanionEventKind",
    "CompanionSnapshot",
    "CompanionEvent",
    "CompanionController",
    "CompanionError",
    "EDGE_SIDES",
]


class CompanionState(str, Enum):
    """The companion's visible lifecycle."""

    SLEEPING = "sleeping"
    AWAKENING = "awakening"
    LISTENING = "listening"
    THINKING = "thinking"
    EXECUTING = "executing"
    RETURNING = "returning"
    ERROR = "error"

    @classmethod
    def from_value(cls, value: "CompanionState | str") -> "CompanionState":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower().replace("-", "_")
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(f"unknown companion state: {value!r}") from exc

    @property
    def label(self) -> str:
        return _STATE_LABELS[self]


_STATE_LABELS = {
    CompanionState.SLEEPING: "Sleeping",
    CompanionState.AWAKENING: "Waking",
    CompanionState.LISTENING: "What is the task?",
    CompanionState.THINKING: "Working...",
    CompanionState.EXECUTING: "Working...",
    CompanionState.RETURNING: "Done",
    CompanionState.ERROR: "Something went wrong",
}

#: Which screen edges a companion may dock to.
EDGE_SIDES = ("left", "right", "top", "bottom")


class CompanionError(Exception):
    """A companion operation that could not be honoured.

    Carries a stable ``code`` so the view can render a concise reason instead of a
    traceback, and so tests can assert on the reason rather than on wording.
    """

    def __init__(self, message: str, *, code: str = "companion_error") -> None:
        super().__init__(message)
        self.code = code


# ═════════════════════════════════════════════════════════════ events


class CompanionEventKind(str, Enum):
    """The narrow event vocabulary the companion renders (Part 10).

    Deliberately small and closed. The backend emits a great deal more, and
    widening this list is how a UI ends up rendering internal logs: what arrives
    here is what a person needs to see to know whether their task is progressing.

    Nothing here carries a native handle, a prompt, or a credential.
    """

    ACTIVATED = "activated"
    TASK_ACCEPTED = "task_accepted"
    THINKING = "thinking"
    EXECUTING = "executing"
    STEP = "step"
    VERIFICATION = "verification"
    RECOVERY = "recovery"
    PERMISSION = "permission"
    CANCELLATION_REQUESTED = "cancellation_requested"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    ERROR = "error"
    RETRACTING = "retracting"
    SLEEPING = "sleeping"


#: Event kinds that mean the backend has genuinely started executing. Used to move
#: ``THINKING -> EXECUTING`` rather than assuming it on submission.
#:
#: ``PERMISSION`` is deliberately *not* here even though it also implies execution:
#: it is routed first and labelled distinctly, because a permission prompt is the one
#: thing the user must act on and must not read as ordinary progress.
_EXECUTION_KINDS = frozenset({
    CompanionEventKind.EXECUTING,
    CompanionEventKind.STEP,
    CompanionEventKind.VERIFICATION,
    CompanionEventKind.RECOVERY,
})

_TERMINAL_KINDS = frozenset({
    CompanionEventKind.TASK_COMPLETED,
    CompanionEventKind.TASK_FAILED,
    CompanionEventKind.TASK_CANCELLED,
})

#: Kinds that count as *task* progress, and so belong in the scannable progress list.
#:
#: Lifecycle events are excluded on purpose. ``retract()`` clears the progress list
#: and then announces itself, which put a "Sleeping" entry at the top of the
#: progress list of a companion that had just finished - reading as work performed.
#: Deciding this here rather than at each call site is what keeps it from creeping
#: back in.
_PROGRESS_KINDS = frozenset({
    CompanionEventKind.TASK_ACCEPTED,
    CompanionEventKind.THINKING,
    CompanionEventKind.EXECUTING,
    CompanionEventKind.STEP,
    CompanionEventKind.VERIFICATION,
    CompanionEventKind.RECOVERY,
    CompanionEventKind.PERMISSION,
    CompanionEventKind.TASK_COMPLETED,
    CompanionEventKind.TASK_CANCELLED,
    CompanionEventKind.ERROR,
})


@dataclass(frozen=True)
class CompanionEvent:
    """One structured, display-safe event."""

    kind: CompanionEventKind
    message: str
    task_id: str = ""
    #: Short label for the progress list, e.g. ``"Finding target"``. Kept
    #: separate from ``message`` so a view can show a scannable list and a
    #: detailed line without re-deriving either from the other.
    summary: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "task_id": self.task_id,
            "summary": self.summary,
            "timestamp": self.timestamp,
        }


# ═════════════════════════════════════════════════════════════ snapshot


@dataclass(frozen=True)
class CompanionSnapshot:
    """Everything the view needs to draw one frame.

    A view that reaches past this into the controller can skip the state machine;
    one that renders only this cannot.
    """

    state: CompanionState
    task_text: str = ""
    task_id: str = ""
    status_line: str = ""
    #: 0.0 fully retracted, 1.0 fully extended. The view multiplies this by
    #: nothing and simply interpolates its geometry, so the animation is
    #: interruptible at any frame.
    peek: float = 0.0
    progress: tuple[str, ...] = ()
    events: tuple[CompanionEvent, ...] = ()
    can_cancel: bool = False
    cancellation_requested: bool = False
    terminal_message: str = ""
    sleeping: bool = True

    @property
    def expanded(self) -> bool:
        """Whether the companion should show its full panel."""
        return self.state is not CompanionState.SLEEPING

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "task_text": self.task_text,
            "task_id": self.task_id,
            "status_line": self.status_line,
            "peek": round(float(self.peek), 4),
            "progress": list(self.progress),
            "can_cancel": self.can_cancel,
            "cancellation_requested": self.cancellation_requested,
            "terminal_message": self.terminal_message,
            "sleeping": self.sleeping,
            "events": [event.to_dict() for event in self.events],
        }


# ═════════════════════════════════════════════════════════════ controller


class CompanionController:
    """The companion's behaviour, with no window and no toolkit.

    Owns the state machine, the current task, the event history and the timing
    that drives the peek animation and the retraction dwell. It talks to the
    backend only through the injected ``bridge``, which is the existing
    ``ui.bridge.AgentBridge`` boundary - so permissions, task identity and
    cancellation stay owned by the Agent and this controller learns about them
    only as events.
    """

    #: Explicit transition table. Anything not listed here is refused, which is
    #: what stops a late or duplicated backend event from dragging a retracting
    #: companion back into execution.
    _TRANSITIONS: dict[CompanionState, frozenset[CompanionState]] = {
        CompanionState.SLEEPING: frozenset({CompanionState.AWAKENING}),
        CompanionState.AWAKENING: frozenset({CompanionState.LISTENING,
                                             CompanionState.SLEEPING}),
        CompanionState.LISTENING: frozenset({CompanionState.THINKING,
                                            CompanionState.RETURNING,
                                            CompanionState.SLEEPING,
                                            CompanionState.ERROR}),
        CompanionState.THINKING: frozenset({CompanionState.EXECUTING,
                                            CompanionState.RETURNING,
                                            CompanionState.ERROR,
                                            CompanionState.LISTENING}),
        CompanionState.EXECUTING: frozenset({CompanionState.RETURNING,
                                             CompanionState.ERROR,
                                             CompanionState.THINKING}),
        CompanionState.RETURNING: frozenset({CompanionState.SLEEPING,
                                             CompanionState.LISTENING,
                                             CompanionState.AWAKENING}),
        CompanionState.ERROR: frozenset({CompanionState.RETURNING,
                                         CompanionState.SLEEPING,
                                         CompanionState.LISTENING,
                                         CompanionState.AWAKENING}),
    }

    def __init__(
        self,
        bridge: Any,
        *,
        peek_seconds: float = 0.28,
        dwell_seconds: float = 6.0,
        max_progress: int = 40,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[CompanionEvent], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._peek_seconds = max(0.0, float(peek_seconds))
        self._dwell_seconds = max(0.0, float(dwell_seconds))
        self._max_progress = max(1, int(max_progress))
        self._clock = clock
        self._on_event = on_event

        self._lock = threading.RLock()
        self._state = CompanionState.SLEEPING
        self._task_text = ""
        self._task_id = ""
        self._events: list[CompanionEvent] = []
        self._progress: list[str] = []
        self._terminal_message = ""
        self._cancellation_requested = False
        #: Task ids that have already reached a terminal state. A task that has
        #: finished cannot emit further events, so anything arriving for one of
        #: these is stale by definition.
        #:
        #: This is what makes it safe for ``activate()`` to keep the finished task on
        #: screen while the companion reopens: without it, a late event for the
        #: retained id would drag a LISTENING companion straight back into EXECUTING.
        self._terminal_task_ids: set[str] = set()
        self._state_changed_at = self._clock()
        self._closed = False
        self._unsubscribe: Callable[[], None] | None = None
        # Deliberately *not* subscribed here. Construction must not have the side
        # effect of wiring the controller to a live backend: the caller decides when
        # the companion starts listening, which is what makes ``close()`` provably
        # unsubscribing and the whole lifecycle testable without a backend.

    # -- lifecycle ------------------------------------------------------

    def attach(self) -> bool:
        """Subscribe to backend events.

        Explicit rather than implicit. Returns ``False`` when already subscribed,
        when there is no bridge, or after ``close()``.
        """
        with self._lock:
            if self._closed or self._unsubscribe is not None:
                return False
            if self._bridge is None:
                return False
            self._unsubscribe = self._bridge.subscribe_to_events(self._ingest)
            return True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscribed(self) -> bool:
        return self._unsubscribe is not None

    # -- introspection --------------------------------------------------

    @property
    def state(self) -> CompanionState:
        with self._lock:
            return self._state

    @property
    def task_id(self) -> str:
        with self._lock:
            return self._task_id

    @property
    def task_text(self) -> str:
        with self._lock:
            return self._task_text

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self._cancellation_requested

    @property
    def events(self) -> tuple[CompanionEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> CompanionSnapshot:
        with self._lock:
            state = self._state
            elapsed = self._clock() - self._state_changed_at
            if state is CompanionState.AWAKENING:
                peek = _clamp(elapsed / self._peek_seconds) if self._peek_seconds else 1.0
            elif state is CompanionState.SLEEPING:
                peek = 0.0
            elif state is CompanionState.RETURNING:
                # Retract over the dwell window so a result stays readable, then the
                # view shows a shrinking companion rather than a pop-out.
                if self._dwell_seconds:
                    remaining = self._dwell_seconds - elapsed
                    peek = _clamp(max(0.0, remaining / self._dwell_seconds))
                else:
                    peek = 1.0
            else:
                peek = 1.0
            return CompanionSnapshot(
                state=state,
                task_text=self._task_text,
                task_id=self._task_id,
                status_line=_STATE_LABELS[state],
                peek=peek,
                progress=tuple(self._progress),
                events=tuple(self._events[-self._max_progress:]),
                can_cancel=state in (CompanionState.THINKING,
                                     CompanionState.EXECUTING)
                and not self._cancellation_requested,
                cancellation_requested=self._cancellation_requested,
                terminal_message=self._terminal_message,
                sleeping=state is CompanionState.SLEEPING,
            )

    # -- state machine --------------------------------------------------

    def _transition(self, target: CompanionState) -> bool:
        """Move to ``target`` if the table allows it. Never raises."""
        with self._lock:
            if target is self._state:
                return True
            if target not in self._TRANSITIONS[self._state]:
                return False
            self._state = target
            self._state_changed_at = self._clock()
            return True

    def _emit(self, kind: CompanionEventKind, message: str, *,
              task_id: str = "", summary: str = "") -> CompanionEvent:
        event = CompanionEvent(
            kind=kind,
            message=str(message or ""),
            task_id=str(task_id or self._task_id),
            summary=str(summary or ""),
        )
        with self._lock:
            self._events.append(event)
            # Bounded: a long-running companion must not accumulate history without
            # limit. Trimming the oldest is right for a product UI that only shows
            # recent progress.
            if len(self._events) > self._max_progress * 4:
                del self._events[: len(self._events) - self._max_progress * 4]
            if event.summary and event.kind in _PROGRESS_KINDS:
                self._progress.append(event.summary)
                if len(self._progress) > self._max_progress:
                    del self._progress[: len(self._progress) - self._max_progress]
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - a sink must not break the machine
                pass
        return event

    # -- interaction ----------------------------------------------------

    def activate(self, reason: str = "user") -> bool:
        """Wake the companion, or hold it open.

        Valid from ``SLEEPING``, from ``RETURNING`` (a user reaching back for the
        result) and from ``ERROR``. Idempotent in effect: activating an already
        expanded companion does not restart its animation.

        Interrupting ``RETURNING`` or ``ERROR`` keeps the task text and the result
        on screen. Part 14 asks that interacting during retraction leave the
        companion available and *not lose task state*, so this deliberately does
        not clear the current task - submitting a new one does that, and only then.
        """
        with self._lock:
            if self._closed:
                raise CompanionError("companion is closed", code="companion_closed")
            state = self._state
        if state is CompanionState.AWAKENING:
            return True
        if state in (CompanionState.LISTENING, CompanionState.THINKING,
                     CompanionState.EXECUTING):
            return True
        if state is CompanionState.SLEEPING:
            self._transition(CompanionState.AWAKENING)
            self._emit(CompanionEventKind.ACTIVATED,
                       "Diablo is listening.", summary="Awakened")
            return True
        # RETURNING / ERROR: the user interrupted the dwell. Keep the visible task
        # and result, and just reopen the panel. State is preserved here on purpose.
        self._transition(CompanionState.LISTENING)
        self._emit(CompanionEventKind.ACTIVATED,
                   "Diablo is listening.", summary="Awakened")
        return True

    def finish_awakening(self) -> bool:
        """Complete the peek animation and settle into ``LISTENING``.

        Called by the view's timer rather than on a timer thread, so the animation
        stays on the UI thread and the transition happens at a frame boundary.
        Calling it in ``AWAKENING`` is the only legal way in.
        """
        with self._lock:
            if self._state is not CompanionState.AWAKENING:
                return self._state is CompanionState.LISTENING
        return self._transition(CompanionState.LISTENING)

    def submit_task(self, text: str) -> str:
        """Hand a task to the backend through the existing request lifecycle.

        Returns the task id the *companion* used for its own bookkeeping. It is not
        forwarded to the agent: task identity is owned by the Agent's
        TaskManager, exactly as ``ui/launch.py`` already arranges. Pushing a
        UI-chosen id into the backend would be a second source of identity, and a
        model-visible one.
        """
        clean = str(text or "").strip()
        if not clean:
            raise CompanionError("Enter a task before sending.", code="empty_task")
        with self._lock:
            if self._closed:
                raise CompanionError("companion is closed", code="companion_closed")
            if self._state is CompanionState.AWAKENING:
                # Accepting a task mid-animation is normal; settle first so the
                # task is not lost to a transition refusal.
                self._transition(CompanionState.LISTENING)
            if self._state is not CompanionState.LISTENING:
                raise CompanionError(
                    f"cannot accept a task while {self._state.value}.",
                    code="invalid_state")
            job_id = uuid.uuid4().hex
            self._task_id = job_id
            self._task_text = clean
            self._terminal_message = ""
            self._cancellation_requested = False
            self._progress.clear()
        self._emit(CompanionEventKind.TASK_ACCEPTED,
                   clean, task_id=job_id, summary=clean)
        self._transition(CompanionState.THINKING)
        self._submit_to_backend(clean, job_id)
        return job_id

    def _submit_to_backend(self, text: str, job_id: str) -> None:
        """Call the bridge, turning a backend failure into an ``ERROR`` state.

        The companion survives a failed submission (Part 20): it shows a concise
        reason and can accept another task.
        """
        try:
            result = self._bridge.submit_message(text, task_id=job_id)
        except Exception as exc:  # noqa: BLE001 - backend errors are expected here
            self.fail(exc if isinstance(exc, str) else
                      f"{type(exc).__name__}: {exc}")
            return
        # A bridge that answers synchronously may have produced no events at all
        # (a refusal, for instance). Treat an explicit falsy ``success`` as a
        # failure rather than waiting forever for a terminal event.
        if isinstance(result, Mapping) and result.get("success") is False:
            message = str(result.get("error") or result.get("message")
                          or "the backend refused the task")
            self.fail(message)
            return
        if isinstance(result, Mapping) and result.get("cancelled") is True:
            self._cancelled_terminal("Cancelled.")

    def cancel(self, reason: str = "user requested") -> bool:
        """Ask the backend to stop. **This does not end the task.**

        The companion stays in its current state and shows
        ``cancellation_requested`` until the backend reports a terminal state. A UI
        that flipped to "Stopped" on button press would be claiming an outcome it
        has not observed.
        """
        with self._lock:
            if self._closed:
                return False
            state = self._state
            task_id = self._task_id
            if state not in (CompanionState.THINKING, CompanionState.EXECUTING):
                return False
            if self._cancellation_requested:
                return True
            self._cancellation_requested = True
        self._emit(CompanionEventKind.CANCELLATION_REQUESTED,
                   "Stopping...", summary="Stop requested")
        try:
            self._bridge.cancel_task(task_id)
        except Exception as exc:  # noqa: BLE001
            # The request itself failed. Stay honest: we are not cancelled, and we
            # are not still running either - the backend may or may not have seen it.
            self._emit(CompanionEventKind.ERROR,
                       f"Could not request cancellation: {exc}",
                       summary="Cancel failed")
        return True

    # -- backend events -------------------------------------------------

    def _ingest(self, payload: Any) -> None:
        """Entry point for the bridge's event callback."""
        try:
            event = self._coerce_event(payload)
        except Exception as exc:  # noqa: BLE001 - a malformed event is not fatal
            self._emit(CompanionEventKind.ERROR,
                       f"Ignored a malformed backend event: {exc}",
                       summary="Malformed event")
            return
        if event is not None:
            self.handle_event(event)

    def _coerce_event(self, payload: Any) -> CompanionEvent | None:
        """Narrow a backend payload into a displayable companion event.

        Delegates to :mod:`ui.companion_events`, which owns the vocabulary mapping
        and the redaction rules. Kept as a method so the controller keeps one entry
        point and a test can still reach the behaviour directly.
        """
        from ui.companion_events import map_backend_event

        return map_backend_event(payload)

    def handle_event(self, event: CompanionEvent) -> None:
        """Apply one backend event to the state machine."""
        if self._closed:
            return
        kind = event.kind
        with self._lock:
            task_id = self._task_id
            state = self._state
        # Ignore events for a task that is not the current one: a stale event from a
        # finished task must not move a companion that has since been reused.
        if event.task_id and task_id and event.task_id != task_id:
            return
        # A task that already reached a terminal state cannot emit again. Checked
        # separately from the identity test above because it must also fire while
        # that same id is still the displayed one - which is exactly what happens
        # when the user reopens the companion to read a finished result.
        if event.task_id and event.task_id in self._terminal_task_ids:
            return

        if kind is CompanionEventKind.THINKING:
            self._emit(CompanionEventKind.THINKING, event.message,
                       summary=event.summary or "Thinking")
            self._transition(CompanionState.THINKING)
            return
        if kind is CompanionEventKind.PERMISSION:
            # A permission prompt is not an error and not progress noise; it is the
            # one thing the user must act on, so it is surfaced distinctly. This is
            # checked *before* ``_EXECUTION_KINDS`` so it is not swallowed as a
            # generic execution step.
            self._emit(kind, event.message, summary=event.summary or "Needs permission")
            self._transition(CompanionState.EXECUTING)
            return
        if kind in _EXECUTION_KINDS:
            self._emit(kind, event.message, summary=event.summary)
            self._transition(CompanionState.EXECUTING)
            return
        if kind in _TERMINAL_KINDS:
            if kind is CompanionEventKind.TASK_COMPLETED:
                self._succeed(event.message)
            elif kind is CompanionEventKind.TASK_CANCELLED:
                self._cancelled_terminal(event.message)
            else:
                self.fail(event.message or "The task failed.")
            return
        if kind is CompanionEventKind.ERROR:
            self.fail(event.message or "The backend reported an error.")
            return
        # ACTIVATED / TASK_ACCEPTED / others are informative; record and move on.
        self._emit(kind, event.message, summary=event.summary)

    # -- terminal transitions -------------------------------------------

    def _mark_terminal(self, task_id: str = "") -> None:
        """Record that a task has reached a terminal state.

        Bounded, because the companion is long-lived and this set would otherwise
        grow for the lifetime of the process.
        """
        with self._lock:
            ident = str(task_id or self._task_id or "")
            if not ident:
                return
            self._terminal_task_ids.add(ident)
            if len(self._terminal_task_ids) > 512:
                # Sets are unordered, so drop an arbitrary old member. Only its
                # staleness value is lost, never correctness for live tasks.
                self._terminal_task_ids.pop()

    def _succeed(self, message: str = "") -> None:
        with self._lock:
            self._terminal_message = str(message or "Done.")
        self._mark_terminal()
        self._emit(CompanionEventKind.TASK_COMPLETED,
                   self._terminal_message, summary="Done")
        self._transition(CompanionState.RETURNING)

    def _cancelled_terminal(self, message: str = "") -> None:
        with self._lock:
            self._terminal_message = str(message or "Stopped.")
            self._cancellation_requested = False
        self._mark_terminal()
        self._emit(CompanionEventKind.TASK_CANCELLED,
                   self._terminal_message, summary="Stopped")
        self._transition(CompanionState.RETURNING)

    def fail(self, message: str) -> None:
        """Enter ``ERROR`` with a concise, displayable reason."""
        text = str(message or "").strip()
        # Compact, and never a traceback: Part 11 forbids dumping internal logs into
        # the UI. The full detail stays in the backend log.
        compact = text.splitlines()[0][:280] if text else "Unknown error."
        with self._lock:
            self._terminal_message = compact
            self._cancellation_requested = False
        self._mark_terminal()
        self._emit(CompanionEventKind.ERROR, compact, summary="Failed")
        self._transition(CompanionState.ERROR)

    # -- timers ---------------------------------------------------------

    def tick(self) -> CompanionSnapshot:
        """Advance time-driven transitions and return the current frame.

        Called from the view's UI-thread timer. There is deliberately no timer
        thread here: animation and dwell belong to the UI thread, which is what
        keeps the animation interruptible and keeps the controller's state changes
        at frame boundaries rather than racing them from another thread.
        """
        with self._lock:
            state = self._state
            elapsed = self._clock() - self._state_changed_at
        # The `self._*_seconds and` guards that used to sit here made a zero window
        # mean "never fire" rather than "fire immediately", so a companion configured
        # with no animation and no dwell sat in RETURNING forever. Zero is a legal
        # request for no delay, so it is compared, not skipped.
        if state is CompanionState.AWAKENING and elapsed >= self._peek_seconds:
            self.finish_awakening()
        elif state is CompanionState.RETURNING and elapsed >= self._dwell_seconds:
            self.retract()
        return self.snapshot()

    def acknowledge(self) -> bool:
        """Dismiss an ``ERROR`` and begin retracting.

        Explicit rather than implicit: an error the user has not looked at should
        not silently disappear.
        """
        with self._lock:
            if self._state is not CompanionState.ERROR:
                return False
        self._emit(CompanionEventKind.RETRACTING, "Retracting.", summary="Retracting")
        return self._transition(CompanionState.RETURNING)

    def retract(self) -> bool:
        """Go back to ``SLEEPING``, clearing task state."""
        with self._lock:
            if self._state is CompanionState.SLEEPING:
                return True
            self._task_text = ""
            self._task_id = ""
            self._terminal_message = ""
            self._cancellation_requested = False
            self._progress.clear()
        moved = self._transition(CompanionState.SLEEPING)
        if moved:
            self._emit(CompanionEventKind.SLEEPING, "Diablo is sleeping.",
                       summary="Sleeping")
        return moved

    # -- conceptual client API (Part 2) --------------------------------
    #
    # The prompt names this surface explicitly. These are thin, stable spellings of
    # behaviour that already exists above, added so the boundary a UI depends on is
    # visible in one place and reviewable as a contract rather than inferred from
    # the implementation. They deliberately contain no new behaviour.

    def get_state(self) -> CompanionState:
        """Current lifecycle state."""
        return self.state

    def get_current_task(self) -> dict[str, Any]:
        """The displayed task, without backend internals.

        Returns a plain dict of display-safe fields. No native handle, no prompt and
        no tool payload crosses this boundary.
        """
        with self._lock:
            return {
                "task_id": self._task_id,
                "task_text": self._task_text,
                "terminal_message": self._terminal_message,
                "cancellation_requested": self._cancellation_requested,
            }

    def cancel_task(self, task_id: str = "") -> bool:
        """Conceptual-API cancellation.

        ``task_id`` is accepted for interface compatibility and deliberately
        ignored: the companion cancels the task it is actually displaying, and
        trusts nothing a caller supplies about identity.
        """
        _ = task_id
        return self.cancel()

    def subscribe_events(self) -> Callable[[], None]:
        """Conceptual-API event subscription; returns the unsubscribe callable.

        Deliberately takes no callback. The controller owns the single authoritative
        subscription, and a second one handed to the same bridge is precisely the
        duplicate subscription Part 17 and Part 30 forbid. An already-valid
        subscription is returned as-is rather than duplicated.
        """
        self.attach()
        with self._lock:
            unsubscribe = self._unsubscribe
        return unsubscribe if unsubscribe is not None else (lambda: None)

    def shutdown(self) -> None:
        """Conceptual-API shutdown. See :meth:`close`."""
        self.close()

    # -- shutdown -------------------------------------------------------

    def close(self) -> None:
        """Unsubscribe, cancel anything in flight, and stop accepting work.

        Order matters: unsubscribe first so no event arrives while tearing down,
        then cancel through the existing bridge so the backend is not left running
        a task whose UI has gone.

        Synchronous by design. The bridge's ``cancel_task`` is a request that the
        backend answers on its own event stream, so there is nothing here worth
        waiting on and no shutdown timeout to mis-tune.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            unsubscribe = self._unsubscribe
            self._unsubscribe = None
            task_id = self._task_id
            in_flight = self._state in (CompanionState.THINKING,
                                        CompanionState.EXECUTING)
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        if in_flight and self._bridge is not None:
            try:
                self._bridge.cancel_task(task_id)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._state = CompanionState.SLEEPING
            self._state_changed_at = self._clock()


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)