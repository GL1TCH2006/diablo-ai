"""Unit tests for ``core.executor_agent.ExecutorAgent`` (M3.1 untracked module).

All tests are bounded and read-only: they use fake tools, no real subprocesses,
no network, no paid APIs, and no temporary directories.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.executor_agent import ExecutorAgent
from core.async_executor import AsyncExecutor
from config.permissions import PermissionLevel, PermissionManager
from config.settings import Settings
from core.events import EventBus
from core.planner import ExecutionPlanRecord, PlanStepRecord
from core.task_manager import StepStatus
from tools.registry import ToolRegistry, ToolSpec, ToolResult


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def ok_tool(**kwargs):
        return ToolResult(True, output="ok output")

    def fail_tool(**kwargs):
        return ToolResult(False, error="tool reported failure")

    registry.register(ToolSpec(description="test tool", name="fake.ok", fn=ok_tool, level=PermissionLevel.SAFE))
    registry.register(ToolSpec(description="test tool", name="fake.fail", fn=fail_tool, level=PermissionLevel.SAFE))
    return registry


def _make_agent() -> ExecutorAgent:
    settings = Settings()
    settings.max_result_chars = 8000
    async_executor = AsyncExecutor(
        registry=_make_registry(),
        permissions=PermissionManager(settings),
        events=EventBus(),
        max_concurrent=2,
    )
    return ExecutorAgent(
        async_executor=async_executor,
        registry=_make_registry(),
        permissions=PermissionManager(settings),
        events=EventBus(),
    )


def _step(step_id: str, tool: str, depends_on=()) -> PlanStepRecord:
    return PlanStepRecord(
        step_id=step_id,
        tool=tool,
        args={},
        depends_on=depends_on,
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(cancellation_token=None)


def test_execute_plan_single_success_step():
    agent = _make_agent()
    plan = ExecutionPlanRecord(goal="ok", steps=[_step("s1", "fake.ok")])
    result = asyncio.run(agent.execute_plan(plan, _context()))
    assert result.success is True
    assert result.completed_steps == 1
    assert result.total_steps == 1
    assert result.cancelled is False


def test_execute_plan_failure_stops_and_does_not_run_dependents():
    agent = _make_agent()
    plan = ExecutionPlanRecord(
        goal="fail",
        steps=[
            _step("s1", "fake.fail"),
            _step("s2", "fake.ok", depends_on=("s1",)),
        ],
    )
    result = asyncio.run(agent.execute_plan(plan, _context()))
    assert result.success is False
    assert result.completed_steps == 0
    assert result.total_steps == 2
    # The dependent step must not run after its predecessor failed.
    assert plan.steps[1].status != StepStatus.DONE


def test_execute_plan_dependency_gating_runs_both_steps():
    agent = _make_agent()
    plan = ExecutionPlanRecord(
        goal="chain",
        steps=[
            _step("s1", "fake.ok"),
            _step("s2", "fake.ok", depends_on=("s1",)),
        ],
    )
    result = asyncio.run(agent.execute_plan(plan, _context()))
    assert result.success is True
    assert result.completed_steps == 2
    assert plan.steps[0].status == StepStatus.DONE
    assert plan.steps[1].status == StepStatus.DONE


def test_execute_plan_unknown_tool_records_error_and_stops():
    agent = _make_agent()
    plan = ExecutionPlanRecord(goal="unk", steps=[_step("s1", "fake.missing")])
    result = asyncio.run(agent.execute_plan(plan, _context()))
    assert result.success is False
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "error"


def test_execute_plan_empty_plan_is_not_successful():
    agent = _make_agent()
    plan = ExecutionPlanRecord(goal="empty", steps=[])
    result = asyncio.run(agent.execute_plan(plan, _context()))
    assert result.success is False
    assert result.completed_steps == 0
    assert result.total_steps == 0


def test_cancel_all_returns_true():
    agent = _make_agent()
    assert agent.cancel() is True


def test_cancel_task_unknown_task_returns_false():
    agent = _make_agent()
    assert agent.cancel("no-such-task") is False


def test_get_plan_status_idle_when_no_active_plan():
    agent = _make_agent()
    status = agent.get_plan_status()
    assert status == {"status": "idle"}


def test_set_callbacks_stores_callbacks():
    agent = _make_agent()
    calls = []
    agent.set_callbacks(
        on_step_start=lambda *a: calls.append("start"),
        on_step_progress=lambda *a: calls.append("progress"),
        on_step_complete=lambda *a: calls.append("complete"),
        on_plan_start=lambda *a: calls.append("plan-start"),
        on_plan_complete=lambda *a: calls.append("plan-complete"),
    )
    assert agent._on_step_start is not None
    assert agent._on_step_progress is not None
    assert agent._on_step_complete is not None
    assert agent._on_plan_start is not None
    assert agent._on_plan_complete is not None


def test_shutdown_does_not_raise():
    agent = _make_agent()
    agent.shutdown()
