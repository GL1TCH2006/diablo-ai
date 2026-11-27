"""Unit tests for ``core.planner_agent.PlannerAgent`` (M3.1 untracked module).

All tests are bounded and read-only: they use fake tools, no real subprocesses,
no network, no paid APIs, and no temporary directories.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.planner_agent import PlannerAgent
from config.permissions import PermissionLevel
from config.settings import Settings
from core.events import EventBus
from core.planner import ExecutionPlan, ExecutionPlanRecord, PlanStep, PlanStepRecord
from core.task_manager import StepStatus
from tools.registry import ToolRegistry, ToolSpec, ToolResult


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def ok_tool(**kwargs):
        return ToolResult(True, output="ok")

    for name in (
        "applications.open",
        "filesystem.write_file",
        "python.run_script",
        "fake.ok",
    ):
        registry.register(ToolSpec(description="test tool", name=name, fn=ok_tool, level=PermissionLevel.SAFE))

    return registry


def _make_agent(registry=None) -> PlannerAgent:
    settings = Settings()
    settings.max_result_chars = 8000
    return PlannerAgent(
        settings=settings,
        router=None,
        events=EventBus(),
        registry=registry or _make_registry(),
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(cancellation_token=None)


def test_create_plan_unmatched_goal_returns_empty_plan():
    agent = _make_agent()
    plan = agent.create_plan("frobnicate the widget", _context())
    assert plan.steps == ()
    assert plan.goal == "frobnicate the widget"


def test_create_plan_open_app_produces_one_step():
    agent = _make_agent()
    plan = agent.create_plan("open calculator", _context())
    assert len(plan.steps) == 1
    assert plan.steps[0].tool == "applications.open"
    assert plan.steps[0].args.get("app") == "calculator"


def test_create_plan_hello_world_python_produces_two_steps():
    agent = _make_agent()
    plan = agent.create_plan("create a hello world python file", _context())
    tools = [step.tool for step in plan.steps]
    assert "filesystem.write_file" in tools
    assert "python.run_script" in tools


def test_validate_plan_valid_plan_is_valid():
    agent = _make_agent()
    plan = ExecutionPlan(
        goal="g",
        steps=[PlanStep(step_id="s1", tool="fake.ok", args={})],
    )
    result = agent.validate_plan(plan)
    assert result.valid is True
    assert result.errors == []


def test_validate_plan_unknown_tool_is_invalid():
    agent = _make_agent()
    plan = ExecutionPlan(
        goal="g",
        steps=[PlanStep(step_id="s1", tool="fake.does_not_exist", args={})],
    )
    result = agent.validate_plan(plan)
    assert result.valid is False
    assert any("unknown tool" in e for e in result.errors)


def test_validate_plan_empty_steps_is_invalid():
    agent = _make_agent()
    plan = ExecutionPlan(goal="g", steps=())
    result = agent.validate_plan(plan)
    assert result.valid is False


def test_replan_returns_original_plan_when_step_not_failed():
    agent = _make_agent()
    record = ExecutionPlanRecord(
        goal="g",
        steps=[PlanStepRecord(step_id="s1", tool="fake.ok", args={}, status=StepStatus.PENDING)],
    )
    result = agent.replan(record, "s1", "boom", _context())
    # replan() is a bounded stub in M3.1: it returns the original plan as an
    # ExecutionPlan when the step is not failed.
    assert isinstance(result, ExecutionPlan)
    assert result.goal == "g"
    assert len(result.steps) == 1
    assert result.steps[0].step_id == "s1"


def test_planner_exposes_underlying_planner():
    agent = _make_agent()
    assert agent.planner is not None
    assert agent.router is None


def test_shutdown_is_not_implemented():
    # PlannerAgent intentionally has no shutdown (no resources to release);
    # accessing attributes must not raise.
    agent = _make_agent()
    assert agent._registry is not None
    assert agent._events is not None
