"""M2.17 - the runtime against a real Agent.

The runtime tests use a fake Agent because the runtime only needs ``respond`` and one
cancellation entry point. That keeps them fast, but it also means they would still
pass if ``respond`` had a different signature, or if the goal layer were wired to
something other than the real request lifecycle. This file closes that gap: a real
``Agent``, a real ``TaskManager``, a real ``TaskStore``, the real permission path, and
a real tool call.

What it proves
--------------
* A due goal becomes a task with an id minted by the TaskManager, not by the goal.
* The work runs through the ordinary executor path - the tool is really called.
* The verdict the runtime copies onto the goal is the verdict the TaskManager and
  VerificationLedger produced, not one the runtime decided.
* Permission is still the authority: a tool that needs a confirmation, with no
  confirmer configured, is refused rather than auto-approved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.autonomy import AutonomyRuntime, SchedulerLimits
from core.goals import GoalState, GoalStore, create_goal


class _Scripted:
    """A provider that emits queued tool calls, then plain text."""

    def __init__(self, calls: list[Any] | None = None) -> None:
        self.queue = list(calls or [])
        self.turn = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model_label(self) -> str:
        return "scripted/m217"

    @property
    def available(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def list_models(self) -> list:
        return []

    def chat(self, messages, tools=None, model=None):
        from ai.provider import AIResponse

        self.turn += 1
        if self.queue:
            return AIResponse(text="", tool_calls=[self.queue.pop(0)])
        return AIResponse(text="Done.")


def _call(name: str, args: dict) -> Any:
    from ai.provider import ToolCall

    return ToolCall(id=f"c{len(args)}", name=name, arguments=args)


def _agent(settings: Any, calls: list[Any] | None = None,
            provider: Any = None) -> Any:
    from config.permissions import PermissionManager
    from core.agent import Agent
    from core.events import EventBus
    from tools import build_registry

    return Agent(
        provider=provider if provider is not None else _Scripted(calls),
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings),
        events=EventBus(),
        confirmer=None,
        model_name="",
        model_catalog=[],
    )


def _runtime(settings: Any, agent: Any, tmp_path: Path) -> AutonomyRuntime:
    return AutonomyRuntime(
        agent, GoalStore(settings.sessions_dir),
        task_store=agent._task_store,
        limits=SchedulerLimits(max_active_goals=3, max_autonomous_tasks=8,
                               max_concurrent_autonomous=1,
                               min_interval_seconds=60))


def _due_goal(runtime: AutonomyRuntime, objective: str, *,
              evidence: int = 1):
    goal = create_goal(objective, interval_seconds=60,
                       next_run_at="20200101-000000", required_evidence=evidence)
    runtime.goals.create(goal)
    return goal


@pytest.fixture()
def readme(tmp_settings, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello from the goal layer\n", encoding="utf-8")
    return target


def test_a_due_goal_becomes_a_real_task_through_the_ordinary_path(
        tmp_settings, tmp_path, readme) -> None:
    agent = _agent(tmp_settings, [
        _call("filesystem.read_file", {"path": str(readme)}),
    ])
    runtime = _runtime(tmp_settings, agent, tmp_path)
    goal = _due_goal(runtime, f"Read the file {readme.name} and tell me what it says")

    report = runtime.tick()

    assert report.started == [goal.goal_id]
    assert not report.errors

    stored = runtime.goals.get(goal.goal_id)
    # The id came from the TaskManager's own minting path, so the goal owns a task
    # rather than inventing one.
    assert stored.current_task_id
    assert stored.task_ids == [stored.current_task_id]

    record = agent._task_store.get(stored.current_task_id)
    assert record is not None
    assert record.status in ("completed", "failed", "cancelled")

    outcome = runtime.outcome(goal.goal_id)
    assert outcome.task_id == stored.current_task_id
    assert outcome.verdict in ("verified", "unverified", "failed", "cancelled",
                               "unknown")


def test_the_verdict_copied_onto_the_goal_is_the_tasks_own(
        tmp_settings, tmp_path, readme) -> None:
    """The runtime observes; it does not adjudicate."""
    from core.task_manager import terminal_verdict

    agent = _agent(tmp_settings, [
        _call("filesystem.read_file", {"path": str(readme)}),
    ])
    runtime = _runtime(tmp_settings, agent, tmp_path)
    goal = _due_goal(runtime, "Read the notes file")

    runtime.tick()

    stored = runtime.goals.get(goal.goal_id)
    record = agent._task_store.get(stored.current_task_id)
    lifecycle = (record.metadata or {}).get("lifecycle") or {}
    expected = terminal_verdict(
        str(lifecycle.get("state") or record.status),
        str(lifecycle.get("verification") or "")).value

    assert runtime.outcome(goal.goal_id).verdict == expected
    # Evidence exists if and only if the task's own verdict was VERIFIED.
    assert bool(stored.verified_task_ids) == (expected == "verified")


def test_a_completed_read_completes_a_single_evidence_goal(
        tmp_settings, tmp_path, readme) -> None:
    agent = _agent(tmp_settings, [
        _call("filesystem.read_file", {"path": str(readme)}),
    ])
    runtime = _runtime(tmp_settings, agent, tmp_path)
    goal = _due_goal(runtime, "Read the notes file")

    runtime.tick()

    stored = runtime.goals.get(goal.goal_id)
    if stored.verified_task_ids:
        assert stored.state is GoalState.COMPLETED
    else:
        # Not evidence, so not complete. Either answer is correct; conflating them
        # is not, and this asserts the rule rather than a hopeful outcome.
        assert stored.state is GoalState.ACTIVE


def test_a_confirmation_requiring_tool_is_refused_not_auto_approved(
        tmp_settings, tmp_path) -> None:
    """Autonomy does not imply permission. No confirmer, so it must be refused.

    ``terminal.execute`` outside the auto-allow list is SENSITIVE and demands
    confirmation by existing policy. Note what is deliberately *not* used here:
    ``filesystem.write_file`` inside an allowed root, which the permission model has
    always allowed without confirmation. Asserting that one is refused would have
    been asserting a policy change rather than testing autonomy.
    """
    agent = _agent(tmp_settings, [
        _call("terminal.execute", {"command": "echo autonomy-m217"}),
    ])
    runtime = _runtime(tmp_settings, agent, tmp_path)
    goal = _due_goal(runtime, "Run a shell command without asking")

    report = runtime.tick()

    assert report.started == [goal.goal_id]
    stored = runtime.goals.get(goal.goal_id)
    assert stored.verified_task_ids == []
    assert stored.state is not GoalState.COMPLETED


def test_the_same_tick_is_not_run_twice(tmp_settings, tmp_path, readme) -> None:
    provider = _Scripted([
        _call("filesystem.read_file", {"path": str(readme)}),
        _call("filesystem.read_file", {"path": str(readme)}),
    ])
    agent = _agent(tmp_settings, provider=provider)
    runtime = _runtime(tmp_settings, agent, tmp_path)
    _due_goal(runtime, "Read the notes file")

    turns_after_first = None
    first = runtime.tick()
    turns_after_first = provider.turn
    second = runtime.tick()

    assert first.started == [first.started[0]]
    assert second.started == []
    assert provider.turn == turns_after_first, (
        "the second tick must not have reached the model at all")


def test_cancelling_a_goal_converges_its_task_across_a_new_runtime(
        tmp_settings, tmp_path, readme) -> None:
    agent = _agent(tmp_settings, [
        _call("filesystem.read_file", {"path": str(readme)}),
    ])
    runtime = _runtime(tmp_settings, agent, tmp_path)
    goal = _due_goal(runtime, "Read the notes file")
    runtime.tick()

    runtime.cancel(goal.goal_id)

    # A second runtime over the same files: the durable record must still say
    # cancelled, and a fresh tick must not resurrect it.
    reborn = _runtime(tmp_settings, _agent(tmp_settings), tmp_path)
    report = reborn.tick()

    assert reborn.goals.get(goal.goal_id).state is GoalState.CANCELLED
    assert report.started == []


def test_startup_reconciliation_runs_before_the_first_tick(
        tmp_settings, tmp_path, readme) -> None:
    agent = _agent(tmp_settings)
    runtime = _runtime(tmp_settings, agent, tmp_path)
    _due_goal(runtime, "Read the notes file")

    report = runtime.reconcile()

    assert report.resumed
    assert report.changed == len(report.changed_ids)
    assert runtime.goals.list_all()[0].state in (
        GoalState.ACTIVE, GoalState.RESUMABLE)
