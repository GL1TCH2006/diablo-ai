"""M2.17 - the companion is actually *fed* the goal projection.

``apply_goal_projection`` had twenty passing tests and zero production callers. The
projection was implemented, redacted, bounded, rendered - and unreachable, because
nothing ever handed it a summary. That is the same defect as the unwired goal
runtime, found a second time in the same milestone, and it is the reason these tests
exist: they exercise the *path*, not the destination.

What is asserted here:
* a ``goal_provider`` callable reaches the view and updates the rendered line;
* the refresh is throttled rather than running 30 times a second;
* a provider that raises, or returns nonsense, costs one frame and nothing more;
* the whole seam is read-only - the view holds a callable, never a GoalStore.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from core.autonomy import summarise_goal
from core.goals import GoalState, create_goal


class _Bridge:
    def subscribe_to_events(self, handler: Any) -> Any:
        return lambda: None

    def submit_message(self, text: str, task_id: str = "") -> dict:
        return {"success": True}

    def cancel_task(self, task_id: str = "") -> bool:
        return True


@pytest.fixture()
def controller():
    from ui.companion_state import CompanionController

    controller = CompanionController(_Bridge())
    controller.attach()
    try:
        yield controller
    finally:
        controller.close()


class _App:
    """The goal-refresh half of the view, without Tk.

    Constructing the real ``EdgeCompanionApp`` needs a display, and the logic under
    test - throttle, tolerate a bad provider, hand one row to the controller - is
    display-independent. Binding the real unbound method to this stand-in keeps the
    tested code the shipped code rather than a copy of it.
    """

    def __init__(self, controller: Any, provider: Any, refresh: float = 0.0):
        from ui.edge_companion import EdgeCompanionApp

        self.controller = controller
        self._closed = False
        self._goal_provider = provider
        self._goal_refresh = max(0.0, float(refresh))
        self._last_goal_refresh = 0.0
        self._refresh_goal_projection = (
            EdgeCompanionApp._refresh_goal_projection.__get__(self))

    def close(self) -> None:
        self._closed = True


def _goal(objective: str = "finish the migration"):
    goal = create_goal(objective)
    goal.transition(GoalState.ACTIVE)
    return goal


def test_a_provider_reaches_the_rendered_goal_line(controller) -> None:
    app = _App(controller, lambda: [_goal() and summarise_goal(_goal())])
    try:
        assert app._refresh_goal_projection(force=True) is True
        snapshot = controller.snapshot()
        assert "finish the migration" in snapshot.goal_line
        assert snapshot.goal_active is True
    finally:
        app.close()


def test_the_runtime_snapshot_shape_is_accepted(controller) -> None:
    """The provider returns a list of rows; the view takes the first."""
    app = _App(controller, lambda: [summarise_goal(_goal("row one")),
                                   summarise_goal(_goal("row two"))])
    try:
        app._refresh_goal_projection(force=True)
        line = controller.snapshot().goal_line
        assert "row one" in line
        assert "row two" not in line
    finally:
        app.close()


def test_a_single_mapping_is_accepted_too(controller) -> None:
    app = _App(controller, lambda: summarise_goal(_goal("mapping form")))
    try:
        app._refresh_goal_projection(force=True)
        assert "mapping form" in controller.snapshot().goal_line
    finally:
        app.close()


def test_no_active_goals_clears_the_line(controller) -> None:
    app = _App(controller, lambda: [summarise_goal(_goal())])
    try:
        app._refresh_goal_projection(force=True)
        assert controller.snapshot().goal_line
        app._goal_provider = lambda: []
        app._refresh_goal_projection(force=True)
        assert controller.snapshot().goal_line == ""
        assert controller.snapshot().goal_active is False
    finally:
        app.close()


def test_the_refresh_is_throttled(controller) -> None:
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return [summarise_goal(_goal())]

    app = _App(controller, provider, refresh=60.0)
    try:
        assert app._refresh_goal_projection(force=True) is True
        assert calls["n"] == 1
        # Not forced: the throttle must refuse, or the provider runs every frame.
        assert app._refresh_goal_projection() is False
        assert app._refresh_goal_projection() is False
        assert calls["n"] == 1
    finally:
        app.close()


def test_a_raising_provider_costs_one_frame(controller) -> None:
    def provider():
        raise RuntimeError("the store is gone")

    app = _App(controller, provider)
    try:
        assert app._refresh_goal_projection(force=True) is False
        # Still alive, and the next frame is not poisoned by the failure.
        app._goal_provider = lambda: [summarise_goal(_goal("recovered"))]
        assert app._refresh_goal_projection(force=True) is True
        assert "recovered" in controller.snapshot().goal_line
    finally:
        app.close()


def test_junk_from_the_provider_is_ignored(controller) -> None:
    for junk in (None, 0, "", object(), [None], [object()]):
        app = _App(controller, lambda junk=junk: junk)
        try:
            app._refresh_goal_projection(force=True)
        finally:
            app.close()
    # The controller is still usable and still blank rather than showing junk.
    assert controller.snapshot().goal_line == ""


def test_a_closed_app_stops_refreshing(controller) -> None:
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return [summarise_goal(_goal())]

    app = _App(controller, provider)
    app.close()
    assert app._refresh_goal_projection(force=True) is False
    assert calls["n"] == 0


def test_no_provider_means_no_goal_line_and_no_error(controller) -> None:
    app = _App(controller, None)
    try:
        assert app._refresh_goal_projection(force=True) is False
        assert controller.snapshot().goal_line == ""
    finally:
        app.close()


def test_the_view_holds_a_callable_never_a_store() -> None:
    """The seam cannot become authority, even by accident."""
    import inspect

    from ui.edge_companion import EdgeCompanionApp, launch_edge_companion

    for func in (EdgeCompanionApp._refresh_goal_projection,
                 launch_edge_companion):
        text = inspect.getsource(func)
        for forbidden in ("GoalStore", "core.goals", "AutonomyScheduler",
                          "mark_completed", "request_cancel", "transition"):
            assert forbidden not in text, (
                f"the companion view reached {forbidden}; it may only display")


def test_the_launcher_threads_the_provider_through() -> None:
    """A provider supplied to the launcher must reach the app, not be dropped."""
    import inspect

    from ui.edge_companion import launch_edge_companion
    from ui.launch import build_edge_companion

    assert "goal_provider" in inspect.signature(launch_edge_companion).parameters
    assert "goal_provider" in inspect.signature(build_edge_companion).parameters
    build_src = inspect.getsource(build_edge_companion)
    assert "goal_provider=goal_provider" in build_src


def test_main_wires_the_runtime_projection_to_the_companion() -> None:
    """The end-to-end wiring, asserted at the source rather than trusted."""
    import inspect

    from main import main

    source = inspect.getsource(main)
    assert "autonomy_runtime.snapshot" in source, (
        "main.py must hand the companion the runtime's read-only projection")