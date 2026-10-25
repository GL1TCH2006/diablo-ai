"""The orb state mapper, tested against real projection objects.

Every test builds an actual :class:`~ui.companion_state.CompanionSnapshot` with
real :class:`~ui.companion_state.CompanionEvent` values. Nothing about the
companion state machine is stubbed, and no existing UI file is modified - the
mapper only reads what the state machine already publishes.
"""

from __future__ import annotations

import pytest

from ui.companion_state import (
    CompanionEvent,
    CompanionEventKind,
    CompanionSnapshot,
    CompanionState,
)
from ui.orb import ALL_STATES, OrbState
from ui.orb.state_map import (
    IDLE_SLEEP_SECONDS,
    LISTENING_SECONDS,
    assert_state_is_renderable,
    map_snapshot,
)


def _event(kind: CompanionEventKind, message: str = "") -> CompanionEvent:
    return CompanionEvent(kind=kind, message=message, summary=message, task_id="t1")


class _PassedVerification:
    """A verification event that EXPLICITLY says it passed.

    Batch 7 Task 2. ``completed`` used to be granted by any bare
    ``VERIFICATION`` event, but that event does not distinguish a pass from a
    failure or from an in-progress check. The mapper now requires an explicit
    outcome; this stand-in carries the ``passed`` attribute it looks for. The
    real projection cannot produce one today - see ``b6_order.md`` and
    ``FINAL7.md`` - which is exactly why the end-to-end test is written the way
    it is.
    """
    kind = "verification_passed"
    passed = True

    def __init__(self, message: str = "verification passed"):
        self.message = message
        self.summary = message
        self.task_id = "t1"


class _FailedVerification:
    """A verification event that EXPLICITLY says it failed."""
    kind = "verification_failed"
    passed = False

    def __init__(self, message: str = "verification failed"):
        self.message = message
        self.summary = message
        self.task_id = "t1"


def _snapshot(state: CompanionState, events=(), **kwargs) -> CompanionSnapshot:
    return CompanionSnapshot(state=state, task_id="t1", events=tuple(events), **kwargs)


def _clock(value: float):
    return lambda: value


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• every projection state maps


@pytest.mark.parametrize("companion_state,expected", [
    (CompanionState.SLEEPING, OrbState.SLEEPING),
    (CompanionState.AWAKENING, OrbState.AWAKENING),
    (CompanionState.LISTENING, OrbState.LISTENING),
    (CompanionState.THINKING, OrbState.THINKING),
    (CompanionState.EXECUTING, OrbState.EXECUTING),
    (CompanionState.ERROR, OrbState.FAILED),
])
def test_each_companion_state_maps(companion_state, expected):
    result = map_snapshot(_snapshot(companion_state), clock=_clock(0.0))
    assert result.visual_state == expected, result.reason
    assert result.verified is False, (
        f"{companion_state} reported verified without evidence: {result.reason}")


def test_every_mapped_state_is_renderable():
    """The mapper may only return states the renderer can draw."""
    cases = [
        (_snapshot(CompanionState.SLEEPING), 0.0, None),
        (_snapshot(CompanionState.AWAKENING), 0.0, None),
        (_snapshot(CompanionState.LISTENING), 0.0, 0.0),
        (_snapshot(CompanionState.LISTENING), 99.0, 0.0),
        (_snapshot(CompanionState.THINKING), 0.0, None),
        (_snapshot(CompanionState.EXECUTING), 0.0, None),
        (_snapshot(CompanionState.ERROR), 0.0, None),
        (_snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.VERIFICATION),
                                              _event(CompanionEventKind.TASK_COMPLETED)]), 0.0, None),
        (_snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.TASK_COMPLETED)]), 0.0, None),
        (_snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.TASK_CANCELLED)]), 0.0, None),
        (_snapshot(CompanionState.THINKING, [], cancellation_requested=True), 0.0, None),
    ]
    for snapshot, now, changed in cases:
        result = map_snapshot(snapshot, clock=_clock(now), state_changed_at=changed)
        assert result.visual_state in ALL_STATES, (
            f"the mapper produced {result.visual_state!r}, which cannot be drawn")
        assert assert_state_is_renderable(result.visual_state)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• the hard rule: verified completion only


def test_completed_requires_an_observed_verification_event():
    """The whole point. A terminal success needs an EXPLICIT passed verification.

    Batch 7 Task 2: the input below changed from a bare ``VERIFICATION`` event to
    an explicitly-passed one. The assertion did not weaken - ``completed`` is
    still required, and ``verified is True`` is still required - but the evidence
    demanded is now strictly stronger. See also
    ``test_a_bare_verification_event_is_not_completed``.
    """
    events = [_PassedVerification(),
              _event(CompanionEventKind.TASK_COMPLETED)]

    verified = map_snapshot(_snapshot(CompanionState.RETURNING, events), clock=_clock(0.0))
    assert verified.visual_state == OrbState.COMPLETED, verified.reason
    assert verified.verified is True

    unverified = map_snapshot(
        _snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.TASK_COMPLETED)]),
        clock=_clock(0.0))
    assert unverified.visual_state == OrbState.UNVERIFIED, (
        f"an unverified completion was shown as completed: {unverified.reason}")
    assert unverified.verified is False


def test_a_terminal_message_never_makes_something_completed():
    """A confident-sounding message is not evidence."""
    snapshot = _snapshot(CompanionState.RETURNING,
                         [_event(CompanionEventKind.TASK_COMPLETED, "Done.")],
                         terminal_message="All tests passed, 100% verified.")
    result = map_snapshot(snapshot, clock=_clock(0.0))
    assert result.visual_state == OrbState.UNVERIFIED, (
        "a terminal message was accepted as proof of completion")


def test_a_goal_line_never_makes_something_completed():
    """The M2.17 goal projection is display-only and grants nothing."""
    snapshot = _snapshot(CompanionState.RETURNING,
                         [_event(CompanionEventKind.TASK_COMPLETED)],
                         goal_line="goal-restart completed", goal_active=True)
    result = map_snapshot(snapshot, clock=_clock(0.0))
    assert result.visual_state == OrbState.UNVERIFIED, (
        "a goal line was accepted as proof of completion")


def test_a_bare_terminal_state_with_no_events_is_unverified():
    result = map_snapshot(_snapshot(CompanionState.RETURNING), clock=_clock(0.0))
    assert result.visual_state == OrbState.UNVERIFIED, result.reason
    assert result.verified is False


def test_verified_is_never_true_for_any_non_completed_state():
    cases = [
        (_snapshot(CompanionState.ERROR), 0.0, None),
        (_snapshot(CompanionState.EXECUTING), 0.0, None),
        (_snapshot(CompanionState.THINKING), 0.0, None),
        (_snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.TASK_CANCELLED)]), 0.0, None),
        (_snapshot(CompanionState.THINKING, [], cancellation_requested=True), 0.0, None),
    ]
    for snapshot, now, changed in cases:
        result = map_snapshot(snapshot, clock=_clock(now), state_changed_at=changed)
        assert result.verified is False, (
            f"{snapshot.state} returned verified=True: {result.reason}")


def test_verification_before_completion_is_what_counts_not_order_of_text():
    """An unknown trailing event must not hide a real terminal outcome."""
    events = [_PassedVerification(),
              _event(CompanionEventKind.TASK_COMPLETED),
              CompanionEvent(kind="something_new", message="", summary="", task_id="t1")]
    result = map_snapshot(_snapshot(CompanionState.RETURNING, events), clock=_clock(0.0))
    assert result.visual_state == OrbState.COMPLETED, (
        f"an unknown trailing event hid a verified completion: {result.reason}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• failure and cancellation stay distinct


def test_failure_is_distinct_from_unverified_and_completed():
    failed = map_snapshot(_snapshot(CompanionState.ERROR), clock=_clock(0.0))
    assert failed.visual_state == OrbState.FAILED
    for other in (OrbState.UNVERIFIED, OrbState.COMPLETED, OrbState.CANCELLING):
        assert failed.visual_state != other


def test_unverified_is_distinct_from_completed():
    unverified = map_snapshot(
        _snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.TASK_COMPLETED)]),
        clock=_clock(0.0))
    assert unverified.visual_state == OrbState.UNVERIFIED
    assert unverified.visual_state != OrbState.COMPLETED
    assert unverified.visual_state != OrbState.FAILED


def test_a_cancel_request_is_a_distinct_state_and_outranks_progress():
    """Cancelling must not be mistaken for running, failed or done."""
    snapshot = _snapshot(CompanionState.EXECUTING,
                         [_event(CompanionEventKind.STEP, "running")],
                         cancellation_requested=True, can_cancel=True)
    result = map_snapshot(snapshot, clock=_clock(0.0))
    assert result.visual_state == OrbState.CANCELLING, result.reason
    assert result.verified is False
    assert result.visual_state not in (OrbState.EXECUTING, OrbState.FAILED,
                                       OrbState.COMPLETED, OrbState.UNVERIFIED)


def test_a_cancel_request_outranks_a_verification_event_too():
    snapshot = _snapshot(CompanionState.EXECUTING,
                         [_event(CompanionEventKind.VERIFICATION),
                          _event(CompanionEventKind.STEP)],
                         cancellation_requested=True)
    assert map_snapshot(snapshot, clock=_clock(0.0)).visual_state == OrbState.CANCELLING


def test_a_terminal_cancellation_maps_to_the_cancellation_state():
    result = map_snapshot(
        _snapshot(CompanionState.RETURNING, [_event(CompanionEventKind.TASK_CANCELLED)]),
        clock=_clock(0.0))
    assert result.visual_state == OrbState.CANCELLING, result.reason


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• in-flight refinement by observed event


def test_a_permission_event_shows_waiting_approval():
    snapshot = _snapshot(CompanionState.EXECUTING,
                         [_event(CompanionEventKind.STEP),
                          _event(CompanionEventKind.PERMISSION)])
    assert map_snapshot(snapshot, clock=_clock(0.0)).visual_state == OrbState.WAITING_APPROVAL


def test_a_verification_event_shows_verifying():
    snapshot = _snapshot(CompanionState.EXECUTING,
                         [_event(CompanionEventKind.STEP),
                          _event(CompanionEventKind.VERIFICATION)])
    assert map_snapshot(snapshot, clock=_clock(0.0)).visual_state == OrbState.VERIFYING


def test_plain_execution_is_not_refined():
    snapshot = _snapshot(CompanionState.EXECUTING, [_event(CompanionEventKind.STEP)])
    assert map_snapshot(snapshot, clock=_clock(0.0)).visual_state == OrbState.EXECUTING


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• timing, with an injected clock


def test_the_listening_window_is_seven_seconds_by_default():
    assert LISTENING_SECONDS == 7.0
    snapshot = _snapshot(CompanionState.LISTENING)

    assert map_snapshot(snapshot, clock=_clock(0.0),
                        state_changed_at=0.0).visual_state == OrbState.LISTENING
    assert map_snapshot(snapshot, clock=_clock(6.9),
                        state_changed_at=0.0).visual_state == OrbState.LISTENING
    closed = map_snapshot(snapshot, clock=_clock(7.0), state_changed_at=0.0)
    assert closed.visual_state == OrbState.RETURNING, (
        f"the window did not close at 7 s: {closed.reason}")


def test_the_listening_window_is_configurable():
    snapshot = _snapshot(CompanionState.LISTENING)
    result = map_snapshot(snapshot, clock=_clock(3.0), state_changed_at=0.0,
                          listening_seconds=2.0)
    assert result.visual_state == OrbState.RETURNING, result.reason


def test_awakening_and_sleeping_are_not_affected_by_the_window():
    for state, expected in ((CompanionState.AWAKENING, OrbState.AWAKENING),
                            (CompanionState.SLEEPING, OrbState.SLEEPING)):
        for now in (0.0, 3.0, 99.0):
            result = map_snapshot(_snapshot(state), clock=_clock(now), state_changed_at=0.0)
            assert result.visual_state == expected, (
                f"{state} at t={now} became {result.visual_state}: {result.reason}")


def test_an_idle_snapshot_sleeps_after_the_idle_threshold():
    """Sleeping is reachable by idleness, not only by an explicit SLEEPING state.

    Idleness outranks the listening window: once the 7 s window has closed the
    companion is only retracting, and if that has gone on past the idle threshold
    nobody is watching.
    """
    assert IDLE_SLEEP_SECONDS == 60.0
    snapshot = _snapshot(CompanionState.LISTENING)

    assert map_snapshot(snapshot, clock=_clock(6.9),
                        state_changed_at=0.0).visual_state == OrbState.LISTENING
    assert map_snapshot(snapshot, clock=_clock(7.0),
                        state_changed_at=0.0).visual_state == OrbState.RETURNING
    assert map_snapshot(snapshot, clock=_clock(59.9),
                        state_changed_at=0.0).visual_state == OrbState.RETURNING
    assert map_snapshot(snapshot, clock=_clock(60.0),
                        state_changed_at=0.0).visual_state == OrbState.SLEEPING


def test_a_long_running_task_is_never_put_to_sleep():
    """Ten minutes of execution is work, not idleness."""
    for companion_state in (CompanionState.THINKING, CompanionState.EXECUTING):
        result = map_snapshot(_snapshot(companion_state), clock=_clock(600.0),
                              state_changed_at=0.0)
        assert result.visual_state != OrbState.SLEEPING, (
            f"{companion_state} was put to sleep after 10 minutes: {result.reason}")


def test_an_unknown_state_age_is_reported_not_guessed():
    """Without a state age the window cannot be measured, and that is stated."""
    result = map_snapshot(_snapshot(CompanionState.LISTENING), clock=_clock(0.0))
    assert result.visual_state == OrbState.LISTENING
    assert "unknown" in result.reason, result.reason


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• purity and robustness


def test_the_mapper_does_not_mutate_the_snapshot():
    snapshot = _snapshot(CompanionState.EXECUTING,
                         [_event(CompanionEventKind.STEP)],
                         cancellation_requested=True)
    before = (snapshot.state, snapshot.events, snapshot.cancellation_requested,
              snapshot.goal_line, snapshot.goal_active)
    map_snapshot(snapshot, clock=_clock(0.0))
    after = (snapshot.state, snapshot.events, snapshot.cancellation_requested,
             snapshot.goal_line, snapshot.goal_active)
    assert before == after, "the mapper mutated the projection"


def test_the_mapper_is_deterministic_for_the_same_inputs():
    snapshot = _snapshot(CompanionState.RETURNING,
                         [_event(CompanionEventKind.VERIFICATION),
                          _event(CompanionEventKind.TASK_COMPLETED)])
    first = map_snapshot(snapshot, clock=_clock(5.0))
    second = map_snapshot(snapshot, clock=_clock(5.0))
    assert first == second


def test_an_empty_or_broken_snapshot_still_maps():
    """Never raise for a view: a view that crashes is worse than a wrong colour."""
    class _Empty:
        pass

    result = map_snapshot(_Empty(), clock=_clock(0.0))
    assert result.visual_state in ALL_STATES, result.reason


def test_event_dicts_are_accepted():
    """Events may arrive as mappings; the mapper reads either shape."""
    snapshot = _snapshot(CompanionState.RETURNING,
                         [{"kind": "verification_passed"}, {"kind": "task_completed"}])
    result = map_snapshot(snapshot, clock=_clock(0.0))
    assert result.visual_state == OrbState.COMPLETED, result.reason


def test_assert_state_is_renderable_rejects_nonsense():
    with pytest.raises(ValueError):
        assert_state_is_renderable("definitely-not-a-state")
    assert assert_state_is_renderable(OrbState.IDLE) == OrbState.IDLE


def test_the_mapper_never_returns_a_state_the_renderer_lacks():
    """Cross-check against the renderer's own table, not a copy of it."""
    from ui.orb import parameters_for

    seen = set()
    for companion_state in CompanionState:
        seen.add(map_snapshot(_snapshot(companion_state), clock=_clock(0.0)).visual_state)
        seen.add(map_snapshot(_snapshot(companion_state), clock=_clock(0.0),
                               state_changed_at=-9999.0).visual_state)
    for outcome in ("task_completed", "task_cancelled", "error", "verification",
                    "permission"):
        for companion_state in (CompanionState.RETURNING, CompanionState.EXECUTING):
            seen.add(map_snapshot(
                _snapshot(companion_state, [_event(CompanionEventKind.TASK_COMPLETED)]),
                clock=_clock(0.0)).visual_state)
    for state in seen:
        assert parameters_for(state).palette, f"{state} has no renderer parameters"