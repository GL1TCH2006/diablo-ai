"""M2.17 - the edge companion's view of persistent autonomous work (Part 41).

The milestone asks the companion to display goal state and explicitly forbids a
separate autonomous UI backend. These tests hold that line: the companion renders a
bounded projection and has no authority over any goal.

The negative assertions matter as much as the positive ones. A companion that
acquired even a ``create_goal`` helper would be a second way to make durable
decisions, and the whole safety argument for the goal layer would quietly weaken.
"""

from __future__ import annotations

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


class TestGoalProjection:
    def test_a_goal_is_rendered_from_a_summary(self, controller) -> None:
        goal = create_goal("make the tests pass", required_evidence=2)
        goal.transition(GoalState.ACTIVE)
        goal.progress_summary = "task 2 of 3"
        controller.apply_goal_projection(summarise_goal(goal))
        snap = controller.snapshot()
        assert snap.goal_active is True
        assert "make the tests pass" in snap.goal_line
        assert "task 2 of 3" in snap.goal_line

    def test_goal_state_is_shown(self, controller) -> None:
        controller.apply_goal_projection(
            {"state": "resumable", "objective": "continue the work"})
        assert "[resumable]" in controller.snapshot().goal_line

    def test_goal_text_is_redacted_at_the_display_boundary(self, controller) -> None:
        controller.apply_goal_projection({
            "state": "active",
            "objective": "deploy with api_key=sk-abcdefgh12345678",
            "progress_summary": "",
        })
        assert "sk-abcdefgh12345678" not in controller.snapshot().goal_line

    def test_goal_line_is_bounded(self, controller) -> None:
        controller.apply_goal_projection({"state": "active", "objective": "x" * 5000})
        assert len(controller.snapshot().goal_line) <= 200

    def test_clearing_the_projection_is_possible(self, controller) -> None:
        controller.apply_goal_projection({"state": "active", "objective": "work"})
        assert controller.apply_goal_projection(None) is True
        assert controller.snapshot().goal_line == ""

    def test_unchanged_projection_reports_no_change(self, controller) -> None:
        summary = {"state": "active", "objective": "same work"}
        assert controller.apply_goal_projection(summary) is True
        assert controller.apply_goal_projection(summary) is False

    def test_a_terminal_goal_is_shown_as_inactive(self, controller) -> None:
        controller.apply_goal_projection({"state": "completed", "objective": "done"})
        assert controller.snapshot().goal_active is False

    def test_a_goal_object_is_accepted_directly(self, controller) -> None:
        goal = create_goal("direct object")
        goal.transition(GoalState.ACTIVE)
        controller.apply_goal_projection(goal)
        assert "direct object" in controller.snapshot().goal_line

    def test_garbage_projection_never_raises(self, controller) -> None:
        for value in (None, 42, "a string", [], {}, {"state": None}):
            controller.apply_goal_projection(value)
        assert isinstance(controller.snapshot().goal_line, str)

    def test_projection_survives_into_the_serialised_snapshot(self, controller) -> None:
        controller.apply_goal_projection(
            {"state": "active", "objective": "serialise me"})
        data = controller.snapshot().to_dict()
        assert data["goal_active"] is True
        assert "serialise me" in data["goal_line"]


class TestNoAuthorityOverGoals:
    """The companion renders goals. It does not own them."""

    @pytest.mark.parametrize("word", [
        "goal_store", "scheduler", "schedule", "create_goal", "cancel_goal",
        "request_cancel_goal", "autonomy",
    ])
    def test_the_controller_exposes_no_goal_authority(self, controller, word) -> None:
        offenders = [name for name in dir(controller) if word in name.lower()]
        assert offenders == [], (
            f"the companion grew goal authority ({word}): {offenders}")

    def test_the_companion_module_does_not_import_autonomy(self) -> None:
        """A UI that imports the scheduler could grow into a second driver."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "ui" / "companion_state.py").read_text(
            encoding="utf-8")
        assert "from core.autonomy import" not in source
        assert "from core.goals import" not in source, (
            "the companion state module imports the goal model; it should receive "
            "a projection, not the authority")

    def test_projection_accepts_a_mapping_so_no_import_is_needed(self, controller) -> None:
        """Which is exactly why the projection is duck-typed."""
        controller.apply_goal_projection({"state": "active", "objective": "duck typed"})
        assert "duck typed" in controller.snapshot().goal_line


class TestCompanionStillWorksWithoutGoals:
    def test_the_projection_is_optional(self, controller) -> None:
        """The companion is a companion first; goals are additive."""
        controller.activate()
        controller.finish_awakening()
        controller.submit_task("open calculator")
        snap = controller.snapshot()
        assert snap.state.value == "thinking"
        assert snap.goal_line == ""
        assert controller.wait_idle(2.0)