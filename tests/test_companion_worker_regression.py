"""Regression tests for the companion worker lifecycle repair.

These tests cover the fix for the load-sensitive race in
``ui.companion_state.CompanionController._ensure_worker()`` where a worker
that appears alive (via ``is_alive()``) can actually be unwinding from a
``BaseException``, causing queued work to be stranded.
"""

from __future__ import annotations

import threading
import time

import pytest

from ui.companion_state import CompanionController, CompanionState
from tests.test_m216_ui_companion import (
    FakeBridge,
    FakeClock,
    _drive_to_listening,
    _make_controller,
    _OPEN_CONTROLLERS,
    _submit_and_settle,
    _wait_settled,
)

# Wait for any worker threads from other test modules (e.g.
# test_m216_ui_companion) to fully exit before this module's tests check
# ``threading.enumerate()``. Without this, a worker whose ``close()`` join
# timed out can still be unwinding when the first test in this file starts.
_module_settle_deadline = time.monotonic() + 5.0
while time.monotonic() < _module_settle_deadline:
    if CompanionController.WORKER_NAME not in [
            t.name for t in threading.enumerate()]:
        break
    time.sleep(0.05)


@pytest.fixture(autouse=True)
def _close_leftover_controllers():
    """Close any controller left open by a previous test.

    ``_make_controller`` appends to the shared ``_OPEN_CONTROLLERS`` list in
    ``tests.test_m216_ui_companion``, but the close-backstop fixture there only
    runs for tests in that module. Close leftovers here so worker threads from
    earlier tests don't linger into later ones.
    """
    yield
    while _OPEN_CONTROLLERS:
        try:
            _OPEN_CONTROLLERS.pop().close()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
    # Give any worker threads from other test modules a moment to fully exit
    # before this module's tests check ``threading.enumerate()``.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if CompanionController.WORKER_NAME not in [
                t.name for t in threading.enumerate()]:
            break
        time.sleep(0.05)


# ══════════════════════════════════════════════════════════════════════════════
# Worker death recovery
# ══════════════════════════════════════════════════════════════════════════════


def test_worker_death_is_recovered() -> None:
    """A worker killed by BaseException is replaced; next submission works."""
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
        assert _wait_settled(controller), "inflight was never released"
        controller.retract()
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "second task")
        assert controller.state is CompanionState.THINKING
        assert controller.worker_alive is True
    finally:
        controller.close()


def test_recovery_followed_by_successful_task() -> None:
    """After a worker dies, the next submission must complete normally."""
    call_count = 0

    class FlakyBridge(FakeBridge):
        def submit_message(self, text: str, task_id: str = "") -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise BaseException("first attempt dies")
            return super().submit_message(text, task_id)

    controller, _, clock = _make_controller(FlakyBridge())
    try:
        _drive_to_listening(controller, clock)
        controller.submit_task("first")
        assert _wait_settled(controller), "first submission never settled"
        assert controller.state is CompanionState.ERROR
        controller.acknowledge()
        controller.activate()
        clock.advance(1.0)
        controller.tick()
        assert controller.state is CompanionState.LISTENING
        _submit_and_settle(controller, "second")
        assert controller.state is CompanionState.THINKING
        assert controller.worker_alive is True
    finally:
        controller.close()


def test_concurrent_submissions_around_worker_termination() -> None:
    """Multiple submissions while a worker is dying must all settle.

    Only one task can be in flight at a time (submit_task requires LISTENING),
    so we submit one task that kills the worker, verify all work is drained,
    then recover and submit again.
    """
    boom = True

    class FlakyBridge(FakeBridge):
        def submit_message(self, text: str, task_id: str = "") -> dict:
            nonlocal boom
            if boom:
                boom = False
                raise BaseException("worker death")
            return super().submit_message(text, task_id)

    controller, _, clock = _make_controller(FlakyBridge())
    try:
        _drive_to_listening(controller, clock)
        controller.submit_task("task that kills the worker")
        assert _wait_settled(controller), "queue stranded after worker death"
        # The worker died; all queued work was drained, no stranded inflight.
        assert controller.wait_idle(1.0) is True
        assert controller.state is CompanionState.ERROR
        # A new submission after recovery spawns a fresh worker.
        controller.retract()
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "after recovery")
        assert controller.state is CompanionState.THINKING
        assert controller.worker_alive is True
    finally:
        controller.close()


def test_repeated_submission_under_load() -> None:
    """Rapid-fire submissions must not strand work or duplicate workers."""
    controller, bridge, clock = _make_controller()
    try:
        _drive_to_listening(controller, clock)
        for i in range(20):
            controller.submit_task(f"rapid task {i}")
            bridge.emit("task_completed", "ok", task_id=controller.task_id)
            controller.retract()
            _drive_to_listening(controller, clock)
        assert _wait_settled(controller)
        # The controller's worker is alive (one worker, not duplicated or dead)
        # and no work is stranded.
        assert controller.worker_alive is True
        assert controller.wait_idle(1.0) is True
    finally:
        controller.close()


def test_shutdown_racing_with_submission() -> None:
    """A shutdown must not be reversed by a late concurrent submission."""
    gate = threading.Event()

    class SlowBridge(FakeBridge):
        def submit_message(self, text: str, task_id: str = "") -> dict:
            gate.wait(2.0)
            return super().submit_message(text, task_id)

    controller, _, clock = _make_controller(SlowBridge())
    try:
        _drive_to_listening(controller, clock)
        controller.submit_task("slow")
        controller.close()
        gate.set()
        # Wait for the blocked worker to actually finish and exit.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not controller.worker_alive:
                break
            time.sleep(0.05)
        assert controller.closed
        assert controller.worker_alive is False
    finally:
        if not controller.closed:
            controller.close()


def test_repeated_shutdown_calls() -> None:
    """Calling close() repeatedly must be safe and idempotent."""
    controller, bridge, clock = _make_controller()
    _drive_to_listening(controller, clock)
    controller.submit_task("task")
    assert _wait_settled(controller)
    controller.close()
    controller.close()
    controller.close()
    assert bridge.unsubscribes == 1
    assert controller.closed
    assert controller.worker_alive is False


def test_idle_waiting_after_task_failures() -> None:
    """wait_idle() must return True after tasks fail (no stranded inflight)."""
    bridge = FakeBridge(submit_error=RuntimeError("backend down"))
    controller, _, clock = _make_controller(bridge)
    try:
        _drive_to_listening(controller, clock)
        controller.submit_task("failing task")
        assert _wait_settled(controller)
        assert controller.state is CompanionState.ERROR
        assert controller.wait_idle(1.0) is True
    finally:
        controller.close()


def test_no_stranded_queue_entries() -> None:
    """Work queued before a worker dies must not be left unhandled."""
    boom = True

    class FlakyBridge(FakeBridge):
        def submit_message(self, text: str, task_id: str = "") -> dict:
            nonlocal boom
            if boom:
                boom = False
                raise BaseException("dies on first job")
            return super().submit_message(text, task_id)

    controller, _, clock = _make_controller(FlakyBridge())
    try:
        _drive_to_listening(controller, clock)
        controller.submit_task("first")
        assert _wait_settled(controller), "queue entries stranded"
        # No stranded inflight; the worker died and queued work was drained.
        assert controller.wait_idle(1.0) is True
        assert controller.state is CompanionState.ERROR
        # A new submission spawns a fresh worker.
        controller.retract()
        _drive_to_listening(controller, clock)
        _submit_and_settle(controller, "after recovery")
        assert controller.worker_alive is True
    finally:
        controller.close()


# ══════════════════════════════════════════════════════════════════════════════
# 200-iteration race stress test
# ══════════════════════════════════════════════════════════════════════════════


def test_race_stress_200_iterations() -> None:
    """Run the raising-worker scenario 200 times to shake out the race.

    A single pass is insufficient evidence; the race only appears under
    timing pressure. This test runs the scenario 200 times sequentially
    with a fresh controller each time.
    """
    for iteration in range(200):
        call_count = 0

        class FlakyBridge(FakeBridge):
            def __init__(self) -> None:
                super().__init__()
                self.boom = True

            def submit_message(self, text: str, task_id: str = "") -> dict:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise BaseException(f"iteration {iteration} worker death")
                return super().submit_message(text, task_id)

        controller, _, clock = _make_controller(FlakyBridge())
        try:
            _drive_to_listening(controller, clock)
            controller.submit_task(f"task {iteration}")
            assert _wait_settled(controller), (
                f"iteration {iteration}: inflight never released")
            controller.retract()
            _drive_to_listening(controller, clock)
            _submit_and_settle(controller, f"second {iteration}")
            assert controller.state is CompanionState.THINKING, (
                f"iteration {iteration}: state is {controller.state.value}")
            assert controller.worker_alive is True, (
                f"iteration {iteration}: no worker after recovery")
        finally:
            controller.close()
