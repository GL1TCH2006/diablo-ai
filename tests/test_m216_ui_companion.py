"""M2.16.UI - deterministic tests for the edge companion's logic and geometry.

Everything here runs headless with no display and no Tk, which is the point: the
companion's behaviour must be verifiable on a build machine that has no screen.

Two subjects:

* :mod:`ui.edge` - pure docking arithmetic over the M2.16.4 display abstraction.
* :mod:`ui.companion_state` - the state machine, timing, cancellation semantics and
  event filtering, driven through a fake bridge and a fake clock.

The fake clock matters. Nothing here sleeps; the peek animation and the retraction
dwell are advanced by moving time forward explicitly, so these tests are fast and
deterministic instead of flaky on a loaded CI box.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from core.windows.coordinates import Rect
from ui.companion_state import (
    CompanionController,
    CompanionError,
    CompanionEvent,
    CompanionEventKind,
    CompanionState,
)
from ui.edge import CompanionGeometry, place_at_edge, select_monitor

# ═════════════════════════════════════════════════════════════ fakes


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class FakeBridge:
    """A stand-in for ``ui.bridge.AgentBridge``.

    Records what the controller asked for and lets a test push backend events at
    will. Deliberately dumb: it does no permission checking and no task identity,
    because those belong to the Agent and the companion must not be tested as if it
    owned them.
    """

    def __init__(self, *, submit_result: Any = None, submit_error: Exception | None = None,
                 cancel_error: Exception | None = None) -> None:
        self.submissions: list[tuple[str, str]] = []
        self.cancellations: list[str] = []
        self.subscribe_calls = 0
        self.unsubscribes = 0
        self._handler: Any = None
        self._submit_result = submit_result if submit_result is not None else {"success": True}
        self._submit_error = submit_error
        self._cancel_error = cancel_error

    def subscribe_to_events(self, handler: Any) -> Any:
        self.subscribe_calls += 1
        self._handler = handler

        def _unsubscribe() -> None:
            self.unsubscribes += 1
            self._handler = None

        return _unsubscribe

    def submit_message(self, text: str, task_id: str = "") -> Any:
        self.submissions.append((text, task_id))
        if self._submit_error is not None:
            raise self._submit_error
        return self._submit_result

    def cancel_task(self, task_id: str) -> None:
        self.cancellations.append(task_id)
        if self._cancel_error is not None:
            raise self._cancel_error

    # -- test helpers ---------------------------------------------------

    def emit(self, kind: Any, message: str = "", task_id: str = "", summary: str = "") -> None:
        assert self._handler is not None, "controller is not attached"
        self._handler({"kind": str(kind), "message": message,
                       "task_id": task_id, "summary": summary})

    def emit_raw(self, payload: Any) -> None:
        assert self._handler is not None, "controller is not attached"
        self._handler(payload)


@dataclass
class FakeDisplay:
    """The subset of ``DisplayInfo`` that ``ui.edge`` actually reads."""

    display_id: str
    bounds: Rect
    work_area: Rect
    primary: bool = False
    scale_percent: int | None = 100
    dpi_x: int | None = 96


def _rect(left: int, top: int, right: int, bottom: int) -> Rect:
    return Rect.from_dict({"left": left, "top": top, "right": right, "bottom": bottom})


PRIMARY = FakeDisplay(
    display_id=r"\\.\DISPLAY1",
    bounds=_rect(0, 0, 1920, 1080),
    work_area=_rect(0, 0, 1920, 1032),
    primary=True,
)
# A second monitor placed to the *left* of the primary, i.e. negative virtual
# desktop coordinates. Worth having: sign errors in docking code hide on a
# single-monitor layout.
LEFT_MONITOR = FakeDisplay(
    display_id=r"\\.\DISPLAY2",
    bounds=_rect(-1280, -200, 0, 800),
    work_area=_rect(-1280, -200, 0, 760),
    scale_percent=125,
)


#: Every controller built here, so a test that forgets to close one cannot leak a
#: submission thread into the next test. Added by :func:`_make_controller`.
_OPEN_CONTROLLERS: list[CompanionController] = []


@pytest.fixture(autouse=True)
def _close_leftover_controllers():
    """Close any controller a test left open.

    Most tests close explicitly, which is the right thing and is asserted by
    ``test_close_stops_the_worker``. This is the backstop: without it a single
    forgotten ``close()`` leaves a live submission thread, and the *next* test that
    counts worker threads fails for a reason that has nothing to do with itself.
    """
    yield
    while _OPEN_CONTROLLERS:
        try:
            _OPEN_CONTROLLERS.pop().close()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass


def _make_controller(bridge: FakeBridge | None = None, clock: FakeClock | None = None,
                     **kwargs: Any) -> tuple[CompanionController, FakeBridge, FakeClock]:
    the_bridge = bridge if bridge is not None else FakeBridge()
    the_clock = clock if clock is not None else FakeClock()
    controller = CompanionController(the_bridge, clock=the_clock, **kwargs)
    controller.attach()
    _OPEN_CONTROLLERS.append(controller)
    return controller, the_bridge, the_clock


def _drive_to_listening(controller: CompanionController, clock: FakeClock) -> None:
    controller.activate()
    clock.advance(1.0)
    controller.tick()


def _submit_and_settle(controller: CompanionController, text: str) -> str:
    """Submit and wait for the backend worker to finish with the turn.

    Submission is asynchronous by design (the view calls it from Tk's main thread),
    so a test that wants to observe the *result* has to wait for the worker. Tests
    that only care about the state machine do not need this.
    """
    job = controller.submit_task(text)
    assert controller.wait_idle(2.0), "backend submission never settled"
    return job


# ═════════════════════════════════════════════════════════════ geometry


class TestGeometry:
    def test_every_edge_is_fully_visible_when_extended(self) -> None:
        for side in ("left", "right", "top", "bottom"):
            place = place_at_edge(PRIMARY, side, peek=1.0)
            overlap = _overlap(place, PRIMARY.work_area)
            assert overlap == (place.width, place.height), (
                f"{side} is not fully on screen when extended: {place.to_dict()}")

    def test_retraction_keeps_a_reachable_strip(self) -> None:
        """A fully retracted companion must still be clickable."""
        for side in ("left", "right", "top", "bottom"):
            place = place_at_edge(PRIMARY, side, peek=0.0)
            visible_w, visible_h = _overlap(place, PRIMARY.work_area)
            assert visible_w > 0 and visible_h > 0, (
                f"{side} retracted out of reach entirely: {place.to_dict()}")
            assert visible_w >= 14 and visible_h >= 14, (
                f"{side} left an unreachable sliver: {place.to_dict()}")

    def test_retraction_actually_retracts(self) -> None:
        """The sleeping form must cover far less than the expanded one."""
        for side in ("left", "right", "top", "bottom"):
            full = place_at_edge(PRIMARY, side, peek=1.0)
            rest = place_at_edge(PRIMARY, side, peek=0.0)
            assert rest.width * rest.height < (full.width * full.height) / 4, (
                f"{side} sleeping footprint is not meaningfully smaller")

    def test_uses_work_area_not_bounds(self) -> None:
        """Docking to bounds would put the companion on the taskbar."""
        bottom = place_at_edge(PRIMARY, "bottom", peek=1.0)
        assert bottom.top + bottom.height <= PRIMARY.work_area.bottom
        assert PRIMARY.bounds.bottom > PRIMARY.work_area.bottom

    def test_peek_is_clamped(self) -> None:
        assert place_at_edge(PRIMARY, "right", peek=-5.0).to_dict() == \
            place_at_edge(PRIMARY, "right", peek=0.0).to_dict()
        assert place_at_edge(PRIMARY, "right", peek=9.0).to_dict() == \
            place_at_edge(PRIMARY, "right", peek=1.0).to_dict()

    def test_peek_is_monotonic(self) -> None:
        previous = None
        for step in range(11):
            place = place_at_edge(PRIMARY, "right", peek=step / 10.0)
            if previous is not None:
                assert place.left <= previous.left + 1, "peek moved the wrong way"
            previous = place

    def test_unknown_edge_falls_back_rather_than_raising(self) -> None:
        assert place_at_edge(PRIMARY, "diagonal", peek=1.0).edge == "right"

    def test_negative_virtual_desktop_coordinates(self) -> None:
        """A monitor left of the origin must dock correctly."""
        for side in ("left", "right", "top", "bottom"):
            place = place_at_edge(LEFT_MONITOR, side, peek=1.0)
            visible_w, visible_h = _overlap(place, LEFT_MONITOR.work_area)
            assert visible_w > 0 and visible_h > 0, (
                f"{side} misplaced on a negative-coordinate monitor")

    def test_scale_scales_the_companion(self) -> None:
        base = place_at_edge(PRIMARY, "right", peek=1.0)
        scaled = place_at_edge(
            FakeDisplay("x", PRIMARY.bounds, PRIMARY.work_area, scale_percent=150),
            "right", peek=1.0)
        assert scaled.scale == pytest.approx(1.5)
        assert scaled.width > base.width

    @pytest.mark.parametrize("bad", [0, -100, None, "nonsense"])
    def test_absurd_dpi_does_not_produce_a_broken_window(self, bad: Any) -> None:
        display = FakeDisplay("x", PRIMARY.bounds, PRIMARY.work_area, scale_percent=bad)
        place = place_at_edge(display, "right", peek=1.0)
        assert 1.0 <= place.scale <= 2.0
        assert place.width > 0 and place.height > 0

    def test_tiny_monitor_does_not_overflow(self) -> None:
        tiny = FakeDisplay("t", _rect(0, 0, 300, 200), _rect(0, 0, 300, 200))
        for side in ("left", "right", "top", "bottom"):
            place = place_at_edge(tiny, side, peek=1.0)
            assert place.width <= 300 and place.height <= 200

    def test_geometry_string_is_tk_compatible(self) -> None:
        place = place_at_edge(PRIMARY, "right", peek=1.0)
        text = place.geometry_string()
        assert text.startswith(f"{place.width}x{place.height}+")

    def test_placement_is_serialisable(self) -> None:
        data = place_at_edge(PRIMARY, "top", peek=0.5).to_dict()
        assert set(data) == {"left", "top", "width", "height", "edge",
                             "display_id", "scale", "visible_ratio"}
        assert data["edge"] == "top"


def _overlap(place: Any, area: Rect) -> tuple[int, int]:
    width = max(0, min(place.left + place.width, area.right) - max(place.left, area.left))
    height = max(0, min(place.top + place.height, area.bottom) - max(place.top, area.top))
    return width, height


# ═════════════════════════════════════════════════════════════ monitor choice


class TestMonitorSelection:
    def test_prefers_the_monitor_under_the_pointer(self) -> None:
        chosen = select_monitor([PRIMARY, LEFT_MONITOR], point=(-400, 100))
        assert chosen is LEFT_MONITOR

    def test_falls_back_to_primary_without_a_point(self) -> None:
        assert select_monitor([LEFT_MONITOR, PRIMARY]) is PRIMARY

    def test_first_fallback_strategy(self) -> None:
        assert select_monitor([LEFT_MONITOR, PRIMARY], fallback="first") is LEFT_MONITOR

    def test_no_primary_flag_still_returns_something(self) -> None:
        neither = [FakeDisplay("a", PRIMARY.bounds, PRIMARY.work_area, primary=False)]
        assert select_monitor(neither) is neither[0]

    def test_empty_and_unusable_inputs_return_none(self) -> None:
        assert select_monitor([]) is None
        assert select_monitor(None) is None
        broken = [FakeDisplay("x", None, None)]
        assert select_monitor(broken) is None

    def test_garbage_point_does_not_raise(self) -> None:
        assert select_monitor([PRIMARY], point=("x", None)) is PRIMARY

    def test_docks_to_the_selected_monitor(self) -> None:
        display = select_monitor([PRIMARY, LEFT_MONITOR], point=(-640, 0))
        assert display is LEFT_MONITOR
        place = place_at_edge(display, "right", peek=1.0)
        assert place.display_id == LEFT_MONITOR.display_id
        assert place.left + place.width <= LEFT_MONITOR.work_area.right + 1


# ═════════════════════════════════════════════════════════════ lifecycle


class TestLifecycle:
    def test_starts_sleeping(self) -> None:
        controller, _, _ = _make_controller()
        snap = controller.snapshot()
        assert snap.state is CompanionState.SLEEPING
        assert snap.sleeping and snap.peek == 0.0 and not snap.expanded

    def test_construction_does_not_subscribe(self) -> None:
        """Wiring is explicit so ``close()`` provably unsubscribes."""
        bridge = FakeBridge()
        controller = CompanionController(bridge, clock=FakeClock())
        assert bridge.subscribe_calls == 0
        assert controller.subscribed is False
        assert controller.attach() is True
        assert controller.subscribed is True

    def test_double_attach_is_refused(self) -> None:
        bridge = FakeBridge()
        controller = CompanionController(bridge, clock=FakeClock())
        assert controller.attach() is True
        assert controller.attach() is False
        assert bridge.subscribe_calls == 1

    def test_activate_then_peek_completes_into_listening(self) -> None:
        controller, _, clock = _make_controller()
        assert controller.activate() is True
        assert controller.state is CompanionState.AWAKENING
        clock.advance(0.1)
        assert controller.snapshot().peek == pytest.approx(0.1 / 0.28, abs=0.02)
        clock.advance(1.0)
        controller.tick()
        assert controller.state is CompanionState.LISTENING

    def test_activate_is_idempotent_while_expanded(self) -> None:
        controller, _, clock = _make_controller()
        _drive_to_listening(controller, clock)
        assert controller.activate() is True
        assert controller.state is CompanionState.LISTENING

    def test_activate_from_error_recovers_and_keeps_the_reason(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("do the thing")
        bridge.emit("task_failed", "boom", task_id=controller.task_id)
        assert controller.state is CompanionState.ERROR
        assert controller.activate() is True
        assert controller.state is CompanionState.LISTENING, (
            "activate from ERROR must reopen the companion")
        assert controller.snapshot().terminal_message == "boom", (
            "the failure reason vanished before it could be read")

    def test_interaction_during_returning_does_not_lose_task_state(self) -> None:
        """Part 14: interacting mid-retraction keeps the result available."""
        controller, bridge, clock = _make_controller(dwell_seconds=30.0)
        _drive_to_listening(controller, clock)
        job = controller.submit_task("open calculator")
        bridge.emit("task_completed", "Calculator opened.",
                    task_id=job)
        assert controller.state is CompanionState.RETURNING
        assert controller.activate() is True
        snap = controller.snapshot()
        assert snap.state is CompanionState.LISTENING
        assert snap.task_id == job
        assert snap.task_text == "open calculator"
        assert snap.terminal_message == "Calculator opened."

    def test_a_finished_task_cannot_emit_again_after_reopening(self) -> None:
        """The stale-event guard must survive the task id staying on screen."""
        controller, bridge, clock = _make_controller(dwell_seconds=30.0)
        _drive_to_listening(controller, clock)
        job = controller.submit_task("open calculator")
        bridge.emit("task_completed", "Calculator opened.", task_id=job)
        controller.activate()
        before = len(controller.events)
        bridge.emit("step", "late work", task_id=job)
        bridge.emit("task_failed", "late failure", task_id=job)
        assert controller.state is CompanionState.LISTENING, (
            "a terminal task dragged the reopened companion out of LISTENING")
        assert len(controller.events) == before

    def test_a_new_task_after_reopening_clears_the_old_result(self) -> None:
        controller, bridge, clock = _make_controller(dwell_seconds=30.0)
        _drive_to_listening(controller, clock)
        job = controller.submit_task("first")
        bridge.emit("task_completed", "First done.", task_id=job)
        controller.activate()
        second = controller.submit_task("second")
        assert second != job
        snap = controller.snapshot()
        assert snap.terminal_message == ""
        assert snap.task_text == "second"
        assert snap.progress[-1] == "second"

    def test_close_is_idempotent_and_blocks_further_work(self) -> None:
        controller, bridge, _ = _make_controller()
        controller.close()
        controller.close()
        assert controller.closed and bridge.unsubscribes == 1
        with pytest.raises(CompanionError) as excinfo:
            controller.activate()
        assert excinfo.value.code == "companion_closed"

    def test_no_bridge_is_survivable(self) -> None:
        controller = CompanionController(None, clock=FakeClock())
        assert controller.attach() is False
        controller.close()


# ═════════════════════════════════════════════════════════════ tasks


class TestTaskSubmission:
    def test_submit_requires_a_task(self) -> None:
        controller, _, clock = _make_controller()
        _drive_to_listening(controller, clock)
        with pytest.raises(CompanionError) as excinfo:
            controller.submit_task("   ")
        assert excinfo.value.code == "empty_task"

    def test_submit_refused_while_sleeping(self) -> None:
        controller, bridge, _ = _make_controller()
        with pytest.raises(CompanionError) as excinfo:
            controller.submit_task("do it")
        assert excinfo.value.code == "invalid_state"
        assert bridge.submissions == []

    def test_submit_during_animation_is_accepted(self) -> None:
        controller, bridge, clock = _make_controller()
        controller.activate()
        assert controller.state is CompanionState.AWAKENING
        _submit_and_settle(controller, "open calculator")
        assert bridge.submissions and controller.state is CompanionState.THINKING

    def test_backend_identity_is_not_forced(self) -> None:
        """The companion keeps its own handle; the Agent owns task identity."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "open calculator")
        text, job_id = bridge.submissions[0]
        assert text == "open calculator"
        assert job_id == controller.task_id

    def test_submission_failure_shows_an_error_not_a_crash(self) -> None:
        bridge = FakeBridge(submit_error=RuntimeError("backend down"))
        controller, _, clock = _make_controller(bridge)
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "do it")
        assert controller.state is CompanionState.ERROR
        assert "backend down" in controller.snapshot().terminal_message

    def test_explicit_refusal_is_treated_as_failure(self) -> None:
        bridge = FakeBridge(submit_result={"success": False, "error": "permission denied"})
        controller, _, clock = _make_controller(bridge)
        _drive_to_listening(controller, clock)
        controller.submit_task("delete everything")
        assert controller.wait_idle(2.0)
        assert controller.state is CompanionState.ERROR
        assert "permission denied" in controller.snapshot().terminal_message

    def test_companion_recovers_after_a_failure(self) -> None:
        bridge = FakeBridge(submit_result={"success": False, "error": "no"})
        controller, _, clock = _make_controller(bridge)
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "do it")
        controller.acknowledge()
        controller.activate()
        clock.advance(1.0)
        controller.tick()
        assert controller.state is CompanionState.LISTENING


# ═════════════════════════════════════════════════════════════ events


class TestEventHandling:
    def test_progress_moves_thinking_to_executing(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        task_id = controller.task_id
        assert controller.state is CompanionState.THINKING
        bridge.emit("executing", "Opening Calculator", task_id=task_id, summary="Opening")
        assert controller.state is CompanionState.EXECUTING
        assert "Opening" in controller.snapshot().progress

    def test_permission_is_labelled_distinctly(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("write a file")
        bridge.emit("permission", "Needs permission", task_id=controller.task_id)
        assert "Needs permission" in controller.snapshot().progress

    def test_terminal_completion_reaches_returning(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        bridge.emit("task_completed", "Calculator is open.",
                    task_id=controller.task_id)
        assert controller.state is CompanionState.RETURNING
        assert controller.snapshot().terminal_message == "Calculator is open."

    def test_stale_task_events_are_ignored(self) -> None:
        """A late event from a finished task must not revive the companion."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        first = controller.submit_task("first task")
        bridge.emit("executing", "working", task_id=first)
        bridge.emit("task_completed", "done", task_id=first)
        controller.retract()
        controller.activate()
        clock.advance(1.0)
        controller.tick()
        second = controller.submit_task("second task")
        assert second != first
        bridge.emit("task_completed", "stale result", task_id=first)
        assert controller.state is CompanionState.THINKING, (
            "a stale terminal event ended the current task")
        assert controller.snapshot().terminal_message == ""

    def test_unknown_event_kinds_are_filtered_out(self) -> None:
        """The vocabulary is closed; internal logs must not reach the UI."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        before = len(controller.events)
        bridge.emit_raw({"kind": "debug_trace", "message": "internal detail"})
        bridge.emit_raw({"kind": "", "message": "no kind"})
        bridge.emit_raw(None)
        bridge.emit_raw("a bare string")
        assert len(controller.events) == before

    def test_events_outside_the_vocabulary_do_not_crash(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        bridge.emit_raw({"kind": 12345})
        bridge.emit_raw({"kind": "step", "message": "fine", "task_id": ""})

    def test_history_is_bounded(self) -> None:
        """A long-running companion must not grow without limit."""
        controller, bridge, clock = _make_controller(max_progress=5)
        _drive_to_listening(controller, clock)
        controller.submit_task("long task")
        for index in range(200):
            bridge.emit("step", f"step {index}",
                        task_id=controller.task_id, summary=f"step {index}")
        snap = controller.snapshot()
        assert len(snap.progress) <= 5
        assert len(snap.events) <= 20
        assert "step 199" in snap.progress

    def test_snapshot_is_serialisable(self) -> None:
        controller, _, _ = _make_controller()
        data = controller.snapshot().to_dict()
        assert data["state"] == "sleeping"
        assert isinstance(data["events"], list)
        assert isinstance(data["progress"], list)


# ═════════════════════════════════════════════════════════════ cancellation


class TestCancellation:
    def test_cancel_is_a_request_not_an_outcome(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("long task")
        task_id = controller.task_id
        assert controller.cancel() is True
        assert bridge.cancellations == [task_id]
        assert controller.state is CompanionState.THINKING, (
            "pressing stop must not claim the task stopped")
        assert controller.cancellation_requested is True
        assert controller.snapshot().can_cancel is False
        assert "Stopping" in controller.snapshot().status_line or \
            controller.snapshot().cancellation_requested

    def test_cancelled_terminates_only_on_backend_confirmation(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("long task")
        task_id = controller.task_id
        controller.cancel()
        bridge.emit("task_cancelled", "Stopped.", task_id=task_id)
        assert controller.state is CompanionState.RETURNING
        assert controller.snapshot().terminal_message == "Stopped."
        assert controller.cancellation_requested is False

    def test_task_completing_after_a_cancel_request_still_completes(self) -> None:
        """A cancel request that loses the race must not be shown as success."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("task")
        task_id = controller.task_id
        controller.cancel()
        bridge.emit("task_completed", "Finished first.", task_id=task_id)
        assert controller.snapshot().terminal_message == "Finished first."

    def test_cancel_is_refused_outside_execution(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        assert controller.cancel() is False
        assert bridge.cancellations == []

    def test_repeat_cancel_does_not_ask_the_backend_twice(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("task")
        controller.cancel()
        assert controller.cancel() is True
        assert len(bridge.cancellations) == 1

    def test_failed_cancel_request_stays_honest(self) -> None:
        bridge = FakeBridge(cancel_error=RuntimeError("no such task"))
        controller, _, clock = _make_controller(bridge)
        _drive_to_listening(controller, clock)
        controller.submit_task("task")
        controller.cancel()
        assert controller.state is CompanionState.THINKING, (
            "a failed cancel request is not a cancellation")
        assert any("Could not request cancellation" in event.message
                   for event in controller.events)

    def test_close_cancels_an_in_flight_task(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("task")
        task_id = controller.task_id
        controller.close()
        assert task_id in bridge.cancellations
        assert bridge.unsubscribes == 1

    def test_close_does_not_cancel_an_idle_companion(self) -> None:
        controller, bridge, _ = _make_controller()
        controller.close()
        assert bridge.cancellations == []


# ═════════════════════════════════════════════════════════════ timing


class TestTiming:
    def test_result_dwells_before_retracting(self) -> None:
        controller, bridge, clock = _make_controller(dwell_seconds=5.0)
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        bridge.emit("task_completed", "Done.", task_id=controller.task_id)
        assert controller.state is CompanionState.RETURNING
        clock.advance(1.0)
        controller.tick()
        assert controller.state is CompanionState.RETURNING, (
            "the result retracted before it could be read")
        assert controller.snapshot().peek > 0.0

    def test_dwell_expires_into_sleeping(self) -> None:
        controller, bridge, clock = _make_controller(dwell_seconds=5.0)
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        bridge.emit("task_completed", "Done.", task_id=controller.task_id)
        clock.advance(6.0)
        controller.tick()
        assert controller.state is CompanionState.SLEEPING
        assert controller.snapshot().sleeping is True

    def test_retract_clears_task_state(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        bridge.emit("task_completed", "Done.", task_id=controller.task_id)
        controller.retract()
        snap = controller.snapshot()
        assert snap.task_text == "" and snap.task_id == ""
        assert snap.terminal_message == "" and snap.progress == ()

    def test_error_is_not_silently_dismissed(self) -> None:
        controller, _, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.fail("something broke")
        clock.advance(3600.0)
        controller.tick()
        assert controller.state is CompanionState.ERROR, (
            "an unattended error cleared itself")

    def test_acknowledge_retracts_an_error(self) -> None:
        controller, _, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.fail("something broke")
        assert controller.acknowledge() is True
        assert controller.state is CompanionState.RETURNING
        assert controller.acknowledge() is False

    def test_zero_timing_is_not_a_hang(self) -> None:
        controller, bridge, clock = _make_controller(peek_seconds=0.0, dwell_seconds=0.0)
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        bridge.emit("task_completed", "Done.", task_id=controller.task_id)
        controller.tick()
        assert controller.state is CompanionState.SLEEPING

    def test_failed_terminal_event_cannot_revive_a_retracted_companion(self) -> None:
        """The transition table, not luck, is what enforces this."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("do it")
        task_id = controller.task_id
        bridge.emit("task_completed", "Done.", task_id=task_id)
        controller.retract()
        bridge.emit("step", "late work", task_id=task_id)
        assert controller.state is CompanionState.SLEEPING

    def test_reuse_after_a_completed_task(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        for index in range(3):
            controller.submit_task(f"task {index}")
            bridge.emit("task_completed", "ok", task_id=controller.task_id)
            controller.retract()
            controller.activate()
            clock.advance(1.0)
            controller.tick()
        assert controller.state is CompanionState.LISTENING


# ═════════════════════════════════════════════════════ submission threading


class TestSubmissionThreading:
    """Part 19: the UI thread must never block on a model or tool turn.

    This is the regression cover for a real defect: submission used to call the
    bridge inline, so the Tk main thread sat inside ``Agent.respond`` for the whole
    turn and the companion froze.
    """

    def test_submit_returns_before_the_backend_finishes(self) -> None:
        gate = threading.Event()
        entered = threading.Event()

        class SlowBridge(FakeBridge):
            def submit_message(self, text: str, task_id: str = "") -> dict:
                entered.set()
                gate.wait(5.0)
                return super().submit_message(text, task_id)

        bridge = SlowBridge()
        controller, _, clock = _make_controller(bridge)
        try:
            _drive_to_listening(controller, clock)
            started = time.monotonic()
            controller.submit_task("a slow task")
            elapsed = time.monotonic() - started
            assert elapsed < 0.5, (
                f"submit_task blocked the caller for {elapsed:.2f}s")
            assert entered.wait(2.0), "the worker never reached the backend"
            assert controller.wait_idle(0.3) is False, (
                "wait_idle reported idle while the backend was still blocked")
            gate.set()
            assert controller.wait_idle(2.0)
        finally:
            gate.set()
            controller.close()

    def test_submission_runs_on_a_named_daemon_worker(self) -> None:
        seen: list[str] = []

        class RecordingBridge(FakeBridge):
            def submit_message(self, text: str, task_id: str = "") -> dict:
                seen.append(threading.current_thread().name)
                return super().submit_message(text, task_id)

        controller, _, clock = _make_controller(RecordingBridge())
        try:
            assert controller.worker_alive is False
            _drive_to_listening(controller, clock)
            _submit_and_settle(controller, "a task")
            assert seen == [CompanionController.WORKER_NAME], seen
            assert controller.worker_alive is True
        finally:
            controller.close()

    def test_only_one_worker_ever_exists(self) -> None:
        controller, _, clock = _make_controller()
        try:
            _drive_to_listening(controller, clock)
            for _ in range(5):
                controller.retract()
                _drive_to_listening(controller, clock)
                controller.submit_task("task")
            assert controller.wait_idle(2.0)
            workers = [t for t in threading.enumerate()
                       if t.name == CompanionController.WORKER_NAME]
            assert len(workers) == 1, f"{len(workers)} submission workers exist"
        finally:
            controller.close()

    def test_close_stops_the_worker(self) -> None:
        controller, _, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("task")
        assert controller.wait_idle(2.0)
        assert controller.worker_alive is True
        controller.close()
        assert controller.worker_alive is False
        names = [t.name for t in threading.enumerate()]
        assert CompanionController.WORKER_NAME not in names, (
            "the submission thread outlived close()")

    def test_no_worker_thread_survives_a_busy_shutdown(self) -> None:
        gate = threading.Event()

        class BlockingBridge(FakeBridge):
            def submit_message(self, text: str, task_id: str = "") -> dict:
                gate.wait(3.0)
                return {"success": True}

        controller, _, clock = _make_controller(BlockingBridge())
        _drive_to_listening(controller, clock)
        controller.submit_task("slow")
        controller.close()
        gate.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if CompanionController.WORKER_NAME not in [
                    t.name for t in threading.enumerate()]:
                break
            time.sleep(0.05)
        assert CompanionController.WORKER_NAME not in [
            t.name for t in threading.enumerate()], (
            "a blocked backend call left the submission thread alive")

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_raising_worker_is_recovered_from(self) -> None:
        """A pathological backend must not leave the companion permanently stuck.

        The worker deliberately catches ``Exception``, not ``BaseException``:
        swallowing ``KeyboardInterrupt`` or ``SystemExit`` inside a daemon thread
        would be worse than letting the thread end. What matters is that the
        controller *recovers* - ``_inflight`` is decremented in a ``finally``, and
        the next submission starts a fresh worker because the old one is dead.
        """
        class FlakyBridge(FakeBridge):
            def __init__(self) -> None:
                super().__init__()
                self.boom = True

            def submit_message(self, text: str, task_id: str = "") -> dict:
                if self.boom:
                    self.boom = False
                    raise BaseException("not an Exception subclass")
                return super().submit_message(text, task_id)

        controller, _, clock = _make_controller(FlakyBridge())
        try:
            _drive_to_listening(controller, clock)
            controller.submit_task("task")
            assert controller.wait_idle(2.0), "inflight was never released"

            # The next submission must work regardless of whether the worker survived.
            controller.retract()
            _drive_to_listening(controller, clock)
            _submit_and_settle(controller, "second task")
            assert controller.state is CompanionState.THINKING
            assert controller.worker_alive is True, (
                "the controller could not recover a worker")
        finally:
            controller.close()


# ═════════════════════════════════════════════════════ single ownership


class TestSingleOwnership:
    """Part 17 - no duplicate windows, subscriptions, workers or shutdown paths."""

    def test_repeated_launch_does_not_double_subscribe(self) -> None:
        bridge = FakeBridge()
        controller = CompanionController(bridge, clock=FakeClock())
        for _ in range(5):
            controller.attach()
        assert bridge.subscribe_calls == 1, "duplicate backend subscriptions"

    def test_repeated_shutdown_unsubscribes_once(self) -> None:
        controller, bridge, _ = _make_controller()
        for _ in range(5):
            controller.close()
        assert bridge.unsubscribes == 1

    def test_cannot_resubscribe_after_close(self) -> None:
        controller, bridge, _ = _make_controller()
        controller.close()
        assert controller.attach() is False
        assert bridge.subscribe_calls == 1

    def test_conceptual_subscribe_does_not_duplicate(self) -> None:
        controller, bridge, _ = _make_controller()
        first = controller.subscribe_events()
        second = controller.subscribe_events()
        assert bridge.subscribe_calls == 1
        assert first is second
        first()

    def test_conceptual_api_surface(self) -> None:
        """Part 2 names this contract; it must exist and agree with the rest."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        assert controller.get_state() is CompanionState.LISTENING
        job = controller.submit_task("open calculator")
        current = controller.get_current_task()
        assert current["task_id"] == job
        assert current["task_text"] == "open calculator"
        assert "handle" not in str(current).lower()
        assert controller.cancel_task(job) is True
        assert bridge.cancellations == [job]
        controller.shutdown()
        assert controller.closed

    def test_cancel_task_ignores_a_supplied_identity(self) -> None:
        """A caller cannot redirect cancellation at some other task."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        real = controller.submit_task("real task")
        controller.cancel_task("some-other-task-id")
        assert bridge.cancellations == [real], (
            "the companion cancelled a task the caller named")


# ═════════════════════════════════════════════════════ backend failure


class TestBackendFailure:
    def test_backend_unavailable_at_submit_is_recoverable(self) -> None:
        bridge = FakeBridge(submit_error=ConnectionError("backend is not running"))
        controller, _, clock = _make_controller(bridge)
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "do it")
        assert controller.state is CompanionState.ERROR
        assert "backend is not running" in controller.snapshot().terminal_message
        controller.acknowledge()
        controller.activate()
        clock.advance(1.0)
        controller.tick()
        assert controller.state is CompanionState.LISTENING

    def test_backend_disconnect_mid_task_reaches_error(self) -> None:
        """Part 26 requires surviving a backend that disappears mid-task."""
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("long task")
        assert controller.state is CompanionState.THINKING
        controller.fail("lost connection to the backend")
        assert controller.state is CompanionState.ERROR
        assert controller.snapshot().can_cancel is False

    def test_malformed_backend_events_do_not_kill_the_companion(self) -> None:
        controller, bridge, clock = _make_controller()
        _drive_to_listening(controller, clock)
        controller.submit_task("task")
        for payload in ({"kind": None}, {"kind": []}, {"kind": "step"},
                        {"kind": "step", "timestamp": "soon"}, 42, object()):
            bridge.emit_raw(payload)
        controller.handle_event(CompanionEvent(
            kind=CompanionEventKind.TASK_COMPLETED,
            message="still alive", task_id=controller.task_id))
        assert controller.state is CompanionState.RETURNING

    def test_a_failing_event_sink_cannot_break_the_machine(self) -> None:
        def exploding(_event: Any) -> None:
            raise RuntimeError("view is gone")

        bridge = FakeBridge()
        controller = CompanionController(bridge, clock=FakeClock(), on_event=exploding)
        controller.attach()
        controller.activate()
        controller.finish_awakening()
        controller.submit_task("task")
        assert controller.state is CompanionState.THINKING


# ═════════════════════════════════════════════════════════════ thread safety


class TestConcurrency:
    def test_events_are_safe_from_another_thread(self) -> None:
        """Backend events arrive off the UI thread; the lock must hold."""
        import threading

        controller, bridge, clock = _make_controller(max_progress=200)
        _drive_to_listening(controller, clock)
        controller.submit_task("concurrent task")
        task_id = controller.task_id

        def worker(index: int) -> None:
            for step in range(25):
                bridge.emit("step", f"t{index}-{step}",
                            task_id=task_id, summary=f"t{index}-{step}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        snap = controller.snapshot()
        assert len(snap.events) == len(set(id(e) for e in snap.events))
        assert len(snap.progress) <= 200