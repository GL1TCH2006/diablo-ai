"""Map the companion's projection onto an orb visual state.

A **pure function**. It reads a :class:`~ui.companion_state.CompanionSnapshot`
(or any object with the same attributes) and returns what the orb should show. It
imports nothing from the companion state machine's internals, holds no state
between calls, and never mutates the snapshot. The clock is injected, so the
timing rules are testable without sleeping.

Why it is a projection and not a decision
========================================

The orb *displays*. It never decides that work succeeded. The hard rule this
module exists to enforce:

    **``completed`` is only ever returned when the projection itself shows
    independently verified completion.**

That matters because the projection is lossy. ``CompanionSnapshot`` carries no
verification flag - it has ``state``, ``task_text``, ``status_line``,
``terminal_message``, ``events`` and two goal scalars. Verification reaches a view
only as observed ``CompanionEventKind.VERIFICATION`` events.

So the rule this module follows is fail-closed:

* a terminal success **with** a verification event seen â†’ ``completed``
* a terminal success **without** one â†’ ``unverified``, never ``completed``
* a failure â†’ ``failed``
* a cancellation â†’ ``cancelling``, distinct from all three

There is no path through this module that returns ``completed`` on the strength
of a terminal message, a status line, a goal line, or the absence of an error. If
the projection cannot prove verification, the orb says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .renderer import ALL_STATES, OrbState

__all__ = [
    "OrbProjection",
    "map_snapshot",
    "LISTENING_SECONDS",
    "IDLE_SLEEP_SECONDS",
]

#: How long the companion waits for input before the listening window closes.
LISTENING_SECONDS = 7.0

#: How long the companion may sit idle before the orb is allowed to sleep.
IDLE_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class OrbProjection:
    """What the orb should draw, and why.

    :attr:`reason` exists so a test can assert *which branch fired*, not merely
    that the output changed. A mapping that silently returns the right answer for
    the wrong reason is worse than one that fails.
    """

    visual_state: str
    reason: str
    #: True only when ``visual_state`` is ``completed`` **and** verification was
    #: observed in the projection. Never True otherwise.
    verified: bool = False


def _event_kinds(events: Sequence[Any]) -> list[str]:
    """Kind names for a snapshot's events, oldest first, best effort."""
    kinds: list[str] = []
    for event in events or ():
        kind = getattr(event, "kind", None)
        if kind is None and isinstance(event, dict):
            kind = event.get("kind")
        kinds.append(str(getattr(kind, "value", kind) or "").strip().lower())
    return kinds


def _last_matching(kinds: Sequence[str], *wanted: str) -> bool:
    """Whether the most recent event of any of ``wanted`` kinds is the last event.

    Scans backwards for the first recognisable kind, so trailing unrecognised
    events do not hide a terminal outcome. This matters: an unknown event after a
    completion must not make the orb forget it completed.
    """
    terminal = {
        "task_completed", "task_cancelled", "task_failed", "error", "verification",
        "permission", "permission_required", "recovery", "step", "executing",
        "thinking", "task_accepted",
    }
    for kind in reversed(list(kinds)):
        if kind in terminal:
            return kind in wanted
    return False


def _verification_outcome(events: Sequence[Any]) -> str:
    """The strongest verification OUTCOME the event list carries: passed/failed/none.

    ── Batch 7 Task 2. ──
    ``_saw_verification`` used to answer a different question: "was any
    verification event seen at all?", and it accepted BOTH ``"verification"`` and
    ``"verifying"``. That is now wrong in two ways:

    * ``"verifying"`` is verification *in progress*. Counting it as evidence of
      verification let an in-flight check make the orb claim ``completed``;
    * a bare ``"verification"`` event does not say whether the check PASSED. The
      agent emits ``verification_completed`` for a pass and ``verification_failed``
      for a failure, and the projection collapses both onto the single
      ``CompanionEventKind.VERIFICATION``, so the orb was inferring an outcome it
      had never been told.

    So the rule is now explicit and fail-closed: ``passed`` requires an event that
    SAYS it passed. Three ways to say it, in priority order:

    1. a duck-typed attribute ``passed is True`` on the event object;
    2. a duck-typed ``outcome`` attribute in ``{"passed", "verified"}``;
    3. a kind string of ``"verification_passed"`` (forward-compatible: a future
       projection can add a distinct kind without changing this module).

    A ``failed`` outcome from any of those three spellings, or ``passed is False``,
    returns ``"failed"``. Anything else - including a bare ``"verification"`` or a
    ``"verifying"`` - returns ``"none"``.
    """
    passed = False
    failed = False
    for event in events or ():
        kind = getattr(event, "kind", None)
        if kind is None and isinstance(event, dict):
            kind = event.get("kind")
        name = str(getattr(kind, "value", kind) or "").strip().lower()

        if name in ("verification_failed", "verify_failed"):
            failed = True
            continue
        if name == "verification_passed":
            passed = True
            continue

        explicit = getattr(event, "passed", None)
        outcome = getattr(event, "outcome", None)
        if explicit is True:
            passed = True
            continue
        if explicit is False:
            failed = True
            continue
        if outcome is not None:
            token = str(getattr(outcome, "value", outcome) or "").strip().lower()
            if token in ("passed", "verified"):
                passed = True
                continue
            if token in ("failed", "error"):
                failed = True
                continue
    # A failure wins over a pass. Fail-closed: if verification said "no" at any
    # point before the completion, the completion is not trustworthy, and a later
    # pass does not erase that. Order within the prefix is not consulted - a
    # re-verification that passed after an earlier failure is treated as the
    # failure it followed.
    if failed:
        return "failed"
    if passed:
        return "passed"
    return "none"


def _saw_verification(kinds: Sequence[str]) -> bool:
    """Whether any verification activity was observed at all.

    Kept for diagnostics and for the ``verifying`` visual state. It is **not** a
    licence to claim ``completed`` - that needs ``_verification_outcome``.
    """
    return any(kind in ("verification", "verifying",
                        "verification_passed", "verification_failed")
               for kind in kinds)


def _passed_before_completion(events: Sequence[Any]) -> str:
    """Outcome of verification evidence that appears BEFORE the completion event.

    The completion event is LOCATED, not assumed to be last. Batch 7 Task 2 found
    this the hard way: slicing ``events[:-1]`` handed a trailing verification to
    the prefix, so ``[task_completed, verification_passed]`` produced
    ``completed`` - the exact case the ordering rule exists to prevent.

    Only the slice strictly before the last ``task_completed`` counts. A
    verification that arrives after the completion is not evidence about the work
    that preceded it, and in production the terminal event short-circuits the
    projection anyway, so a trailing verification cannot re-open a finished task.
    """
    items = list(events or ())
    cut = -1
    for i, event in enumerate(items):
        kind = getattr(event, "kind", None)
        if kind is None and isinstance(event, dict):
            kind = event.get("kind")
        name = str(getattr(kind, "value", kind) or "").strip().lower()
        if name == "task_completed":
            cut = i
    if cut < 0:
        return _verification_outcome(items)
    return _verification_outcome(items[:cut])



def map_snapshot(
    snapshot: Any,
    *,
    clock: Callable[[], float],
    now: float | None = None,
    state_changed_at: float | None = None,
    listening_seconds: float = LISTENING_SECONDS,
    idle_sleep_seconds: float = IDLE_SLEEP_SECONDS,
) -> OrbProjection:
    """Project a companion snapshot onto an orb visual state.

    :param snapshot: a ``CompanionSnapshot`` or anything with the same fields.
    :param clock: injected, monotonic seconds. Required - this module never reads
        a global clock, which is what makes the timing rules testable.
    :param now: override for ``clock()``. Useful when a caller already sampled the
        clock for the same frame.
    :param state_changed_at: when the companion entered its current state. When
        omitted, ``sleeping``/``awakening``/``listening`` timing cannot be
        measured and the function says so in ``reason`` rather than guessing.
    """
    moment = float(clock()) if now is None else float(now)
    events = getattr(snapshot, "events", ()) or ()
    kinds = _event_kinds(events)
    state = str(getattr(getattr(snapshot, "state", None), "value",
                        getattr(snapshot, "state", "")) or "").strip().lower()
    cancelling = bool(getattr(snapshot, "cancellation_requested", False))
    elapsed = None
    if state_changed_at is not None:
        elapsed = max(0.0, moment - float(state_changed_at))

    # 1. A cancellation request is a distinct state and outranks everything else.
    #    The companion deliberately stays in its current state here (its own
    #    ``cancel()`` docstring says flipping to "stopped" on button press would be
    #    claiming an outcome it has not observed), so the orb has to carry the
    #    fact that something is being stopped.
    if cancelling:
        return OrbProjection(
            OrbState.CANCELLING,
            "cancellation requested; the task has not been observed to stop",
            verified=False,
        )

    # 2. Sleep and wake are states in their own right.
    if state == "sleeping":
        return OrbProjection(OrbState.SLEEPING, "companion is sleeping", verified=False)
    if state == "awakening":
        return OrbProjection(OrbState.AWAKENING, "companion is waking", verified=False)

    # 3. The listening window closes after its timeout, and the orb shows the
    #    retraction that follows.
    if state == "listening":
        if elapsed is None:
            return OrbProjection(OrbState.LISTENING,
                                 "listening; state age unknown, window assumed open",
                                 verified=False)
        # Idleness outranks the window. Once the window has closed the companion is
        # only retracting; if that has been going on for longer than the idle
        # threshold nobody is watching, and sleeping is the honest picture.
        if elapsed >= float(idle_sleep_seconds):
            return OrbProjection(
                OrbState.SLEEPING,
                f"listening but idle for {elapsed:.1f}s, over the "
                f"{float(idle_sleep_seconds):.0f}s threshold",
                verified=False)
        if elapsed >= float(listening_seconds):
            return OrbProjection(
                OrbState.RETURNING,
                f"listening window closed after {elapsed:.1f}s",
                verified=False)
        return OrbProjection(
            OrbState.LISTENING,
            f"listening, {float(listening_seconds) - elapsed:.1f}s of window left",
            verified=False)

    # 4. A failure is a failure, whatever else is true.
    if state == "error":
        return OrbProjection(OrbState.FAILED, "companion reported an error", verified=False)

    # 5. Terminal outcomes.
    #
    # ── EVENT ORDERING (Batch 6 Task 6, finding L2) ──────────────────────────
    # ``completed`` requires the verification event to come BEFORE
    # ``task_completed`` in the event list, i.e. the snapshot must read
    # ``[..., verification, task_completed]``.
    #
    # The reverse order - ``[task_completed, verification]`` - maps to
    # ``unverified``, not ``completed``. That is deliberate fail-closed behaviour,
    # not an oversight: ``_last_matching`` (below) scans backwards for the most
    # recent RECOGNISED kind, and ``"verification"`` is in that set while
    # ``"verifying"`` is not. With the verification last, the scan stops on it,
    # finds it is not ``task_completed``, and declines - so the terminal outcome
    # is reported as unverified.
    #
    # In production this is the correct order: the agent verifies the result and
    # *then* reports the task complete. Measured in `data/run_reports/b6_order.md`.
    if state == "returning":
        if _last_matching(kinds, "task_cancelled"):
            return OrbProjection(
                OrbState.CANCELLING, "task reached a cancelled terminal state",
                verified=False)
        if _last_matching(kinds, "task_completed"):
            # ── Batch 7 Task 2: an explicit PASSED verification, and only from
            # before the completion event. A bare "verification" or a "verifying"
            # event no longer counts, so the orb can no longer claim a completion
            # it was never told was checked.
            outcome = _passed_before_completion(events)
            if outcome == "passed":
                return OrbProjection(
                    OrbState.COMPLETED,
                    "terminal success with an explicit passed verification that "
                    "preceded it",
                    verified=True)
            if outcome == "failed":
                # Verification said no. That is not the same as "unchecked", so
                # it must not collapse into UNVERIFIED either.
                return OrbProjection(
                    OrbState.FAILED,
                    "terminal success contradicted by a failed verification",
                    verified=False)
            # Fail closed. A completion the projection cannot back with an explicit
            # pass is reported as unverified, because that is what it is.
            return OrbProjection(
                OrbState.UNVERIFIED,
                "terminal success with no explicit passed verification; reporting "
                "unverified rather than completed",
                verified=False)
        return OrbProjection(
            OrbState.UNVERIFIED,
            "terminal state reached with no recognised outcome event",
            verified=False)

    # 6. In-flight states, refined by what was last observed.
    if state == "executing":
        if _last_matching(kinds, "permission", "permission_required"):
            return OrbProjection(
                OrbState.WAITING_APPROVAL, "permission required; awaiting a decision",
                verified=False)
        if _last_matching(kinds, "verification"):
            return OrbProjection(
                OrbState.VERIFYING, "a verification event was observed", verified=False)
        return OrbProjection(OrbState.EXECUTING, "tools are running", verified=False)

    if state == "thinking":
        return OrbProjection(OrbState.THINKING, "planning or deciding", verified=False)

    if state == "":
        return OrbProjection(OrbState.IDLE, "no state reported", verified=False)

    # 7. An unrecognised state still draws something readable, but never after a
    #    long silence. Note the idle threshold is deliberately NOT applied to the
    #    in-flight states above: a task that has been executing for ten minutes is
    #    working, not idle, and putting the orb to sleep mid-task would be a lie.
    if elapsed is not None and elapsed >= float(idle_sleep_seconds):
        return OrbProjection(
            OrbState.SLEEPING,
            f"unrecognised state idle for {elapsed:.1f}s",
            verified=False)

    return OrbProjection(
        OrbState.IDLE,
        f"unrecognised companion state {state!r}; falling back to idle",
        verified=False)


def visual_state(snapshot: Any, **kwargs) -> str:
    """Convenience: just the state name."""
    return map_snapshot(snapshot, **kwargs).visual_state


def assert_state_is_renderable(name: str) -> str:
    """Validate a state name against the renderer's table. Raises ``ValueError``.

    Kept separate from :func:`map_snapshot` because the mapper is called on every
    animation frame and must not pay for validation; call this at wiring time.
    """
    if name not in ALL_STATES:
        raise ValueError(f"not a renderable orb state: {name!r}")
    return name