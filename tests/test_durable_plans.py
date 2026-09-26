"""M2.12 P2 - durable execution plans.

ExecutionPlan used to be a frozen local that vanished when the request ended.
These tests pin the durable record that replaced it: persistence, the step state
machine, operational ``depends_on`` gating, write-ahead step state, restart
classification of interrupted work, and read-only inspection.

Identity is unchanged throughout: ``TaskManager`` remains authoritative for
``task_id``/``run_id`` and the plan carries no identity of its own.
"""
import json

import pytest

from config.permissions import PermissionManager
from core.events import EventBus
from core.planner import (
    PLAN_SCHEMA_VERSION,
    ExecutionPlan,
    ExecutionPlanRecord,
    Intent,
    PlanStepError,
    PlanStepRecord,
    migrate_plan_record,
    side_effect_key,
    tool_is_read_only,
)
from core.task_manager import (
    STEP_SATISFIED_STATES,
    STEP_TERMINAL_STATES,
    STEP_UNSATISFIABLE_STATES,
    StepStatus,
    TaskStep,
    step_transition_allowed,
)
from core.task_store import TaskRecord, TaskStore
from tools import build_registry
from tools.tasks import task_info


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


class _Scripted:
    """Minimal provider returning queued tool calls, then plain text."""

    def __init__(self, calls=None):
        self.queue = list(calls or [])
        self.turn = 0

    @property
    def name(self):
        return "scripted"

    @property
    def model_label(self):
        return "scripted/p2"

    @property
    def available(self):
        return True

    def is_available(self):
        return True

    def list_models(self):
        return []

    def chat(self, messages, tools=None, model=None):
        from ai.provider import AIResponse

        self.turn += 1
        # One queued call per turn, so multi-turn re-planning can be exercised.
        if self.queue:
            return AIResponse(text="", tool_calls=[self.queue.pop(0)])
        return AIResponse(text="All done.")


def _agent(settings, calls=None, registry=None, permissions=None):
    from core.agent import Agent

    return Agent(
        provider=_Scripted(calls),
        settings=settings,
        registry=registry if registry is not None else build_registry(settings),
        permissions=permissions if permissions is not None else PermissionManager(settings),
        events=EventBus(),
        confirmer=None,
        model_name="",
        model_catalog=[],
    )


def _call(name, args):
    from ai.provider import ToolCall

    return ToolCall(id="c1", name=name, arguments=args)


def _write_plan(steps):
    plan = ExecutionPlanRecord(goal="g", created_at="20260101-000000", steps=steps)
    return plan


def _write_step(step_id, tool="filesystem.write_file", args=None, **kwargs):
    return PlanStepRecord(
        step_id=step_id,
        tool=tool,
        args=dict(args or {"path": "x"}),
        side_effect_key=side_effect_key(tool, args or {"path": "x"}),
        **kwargs,
    )


# ----------------------------------------------------------------------
# plan persistence
# ----------------------------------------------------------------------


def test_plan_persists_on_task_record(tmp_path):
    store = TaskStore(tmp_path)
    plan = ExecutionPlanRecord.from_plan(
        ExecutionPlan.from_intent(Intent("n", [("system.info", {"components": ["cpu"]})], "s"), goal="g")
    )
    store.create(
        TaskRecord(
            task_id="t1", project_name="p", project_root="", title="t", goal="g",
            created_at="20260101-000000", plan=plan,
        )
    )
    loaded = store.get("t1")
    assert loaded.plan is not None
    assert loaded.plan.plan_version == PLAN_SCHEMA_VERSION
    assert [s.step_id for s in loaded.plan.steps] == ["step-1"]


def test_plan_round_trips_through_disk(tmp_path):
    store = TaskStore(tmp_path)
    plan = _write_plan([_write_step("s1"), _write_step("s2", tool="python.run_script", depends_on=("s1",))])
    plan.step("s1").begin_attempt("20260101-000001")
    plan.step("s1").finish(StepStatus.DONE, detail="ok", result_summary="done", now="20260101-000002")
    store.create(
        TaskRecord(task_id="rt", project_name="p", project_root="", title="t", goal="g",
                   created_at="now", plan=plan)
    )
    got = store.get("rt").plan
    assert got.to_dict() == plan.to_dict()
    assert got.step("s1").status is StepStatus.DONE
    assert got.step("s1").attempts == 1


def test_persisted_plan_declares_its_version(tmp_path):
    store = TaskStore(tmp_path)
    store.create(
        TaskRecord(task_id="v", project_name="p", project_root="", title="t", goal="g",
                   created_at="now", plan=_write_plan([_write_step("s1")]))
    )
    raw = json.loads((tmp_path / "tasks" / "v.json").read_text(encoding="utf-8"))
    assert raw["plan"]["plan_version"] == PLAN_SCHEMA_VERSION


def test_plan_carries_no_task_identity(tmp_path):
    """The plan must not become a second source of request identity."""
    plan = _write_plan([_write_step("s1")])
    blob = json.dumps(plan.to_dict())
    assert "task_id" not in blob
    assert "run_id" not in blob
    assert "request_id" not in blob


def test_task_summary_stays_lean(tmp_path):
    from core.task_store import serialize_task_summary

    record = TaskRecord(task_id="s", project_name="p", project_root="", title="t", goal="g",
                        created_at="now", plan=_write_plan([_write_step("s1")]))
    assert "plan" not in serialize_task_summary(record)


# ----------------------------------------------------------------------
# backward compatibility
# ----------------------------------------------------------------------


def test_legacy_record_without_plan_reads_as_none(tmp_path):
    store = TaskStore(tmp_path)
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / "old.json").write_text(
        json.dumps({"task_id": "old", "project_name": "p", "project_root": "", "title": "t",
                    "goal": "g", "created_at": "20200101-000000", "status": "completed"}),
        encoding="utf-8",
    )
    assert store.get("old").plan is None
    assert store.get("old").title == "t"


def test_explicit_null_plan_reads_as_none(tmp_path):
    store = TaskStore(tmp_path)
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / "n.json").write_text(
        json.dumps({"task_id": "n", "project_name": "p", "project_root": "", "title": "t",
                    "goal": "g", "created_at": "now", "status": "pending", "plan": None}),
        encoding="utf-8",
    )
    assert store.get("n").plan is None


def test_unreadable_plan_is_discarded_not_treated_as_empty(tmp_path):
    """A plan we cannot read must never look like a plan with zero steps.

    Otherwise a resume would conclude that no work ever ran.
    """
    store = TaskStore(tmp_path)
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / "bad.json").write_text(
        json.dumps({"task_id": "bad", "project_name": "p", "project_root": "", "title": "t",
                    "goal": "g", "created_at": "now", "status": "pending",
                    "plan": {"plan_version": 1, "steps": [{"tool": "system.info"}]}}),
        encoding="utf-8",
    )
    assert store.get("bad").plan is None


def test_migration_upgrades_unversioned_plan():
    migrated = migrate_plan_record(
        {"goal": "g", "steps": [{"step_id": "s1", "tool": "filesystem.write_file", "args": {"path": "x"}}]}
    )
    assert migrated["plan_version"] == PLAN_SCHEMA_VERSION
    step = migrated["steps"][0]
    assert step["status"] == StepStatus.PENDING.value
    assert step["attempts"] == 0
    assert step["interrupted"] is False
    assert step["side_effect_key"]


def test_migration_rejects_non_object():
    with pytest.raises(PlanStepError):
        migrate_plan_record(["not", "a", "plan"])


# ----------------------------------------------------------------------
# step state machine
# ----------------------------------------------------------------------


def test_six_original_step_states_are_preserved():
    for name in ("PENDING", "RUNNING", "DONE", "FAILED", "CANCELLED", "BLOCKED"):
        assert hasattr(StepStatus, name)


def test_skipped_is_added():
    assert StepStatus.SKIPPED.value == "skipped"
    assert StepStatus.SKIPPED.symbol


def test_every_status_has_a_symbol():
    for status in StepStatus:
        assert isinstance(status.symbol, str) and status.symbol


def test_legal_step_transitions():
    legal = [
        (StepStatus.PENDING, StepStatus.RUNNING),
        (StepStatus.PENDING, StepStatus.SKIPPED),
        (StepStatus.PENDING, StepStatus.BLOCKED),
        (StepStatus.PENDING, StepStatus.CANCELLED),
        (StepStatus.RUNNING, StepStatus.DONE),
        (StepStatus.RUNNING, StepStatus.FAILED),
        (StepStatus.RUNNING, StepStatus.CANCELLED),
        (StepStatus.FAILED, StepStatus.PENDING),
    ]
    for src, dest in legal:
        assert step_transition_allowed(src, dest), f"{src.value}->{dest.value}"


def test_illegal_step_transitions():
    illegal = [
        (StepStatus.DONE, StepStatus.RUNNING),
        (StepStatus.SKIPPED, StepStatus.RUNNING),
        (StepStatus.CANCELLED, StepStatus.RUNNING),
        (StepStatus.BLOCKED, StepStatus.RUNNING),
        (StepStatus.RUNNING, StepStatus.PENDING),
        (StepStatus.DONE, StepStatus.FAILED),
    ]
    for src, dest in illegal:
        assert not step_transition_allowed(src, dest), f"{src.value}->{dest.value}"


def test_terminal_and_satisfied_sets_are_disjoint_and_complete():
    assert StepStatus.DONE in STEP_TERMINAL_STATES
    assert StepStatus.SKIPPED in STEP_TERMINAL_STATES
    assert not (STEP_TERMINAL_STATES & {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.FAILED})
    assert STEP_SATISFIED_STATES == frozenset({StepStatus.DONE})
    assert STEP_UNSATISFIABLE_STATES == frozenset(
        {StepStatus.FAILED, StepStatus.CANCELLED, StepStatus.BLOCKED, StepStatus.SKIPPED}
    )


def test_checked_setter_enforces_and_plain_setter_does_not():
    from core.task_manager import TaskManager

    manager = TaskManager()
    step = TaskStep(name="s")
    manager.set_step_status_checked(step, StepStatus.RUNNING)
    assert step.status is StepStatus.RUNNING
    manager.set_step_status_checked(step, StepStatus.DONE, "ok")
    with pytest.raises(ValueError):
        manager.set_step_status_checked(step, StepStatus.RUNNING)
    # backward-compatible path stays unvalidated
    manager.set_status(step, StepStatus.RUNNING)
    assert step.status is StepStatus.RUNNING


def test_step_lookup_by_id():
    from core.task_manager import TaskManager

    manager = TaskManager()
    step = manager.add_step("write", step_id="step-7")
    assert manager.step_by_id("step-7") is step
    assert manager.step_by_id("missing") is None
    assert manager.step_by_id("") is None


def test_skipped_counts_as_failure_for_task_outcome():
    from core.task_manager import TaskManager

    manager = TaskManager()
    step = manager.add_step("downstream")
    manager.set_status(step, StepStatus.SKIPPED, "dep failed")
    assert manager.any_failed()
    assert manager.final_status() == "failed"


# ----------------------------------------------------------------------
# dependency gating (operational, not merely validated)
# ----------------------------------------------------------------------


def test_depends_on_gates_execution_order():
    plan = _write_plan([
        _write_step("a", tool="system.info", args={"components": ["cpu"]}),
        _write_step("b", tool="system.info", args={"components": ["memory"]}, depends_on=("a",)),
    ])
    assert [s.step_id for s in plan.next_executable()] == ["a"]
    plan.step("a").finish(StepStatus.DONE, "ok")
    assert [s.step_id for s in plan.next_executable()] == ["b"]


def test_dependent_step_does_not_run_before_its_dependency():
    agent_step = _write_step("b", depends_on=("a",))
    index = {"a": _write_step("a"), "b": agent_step}
    assert not agent_step.dependencies_satisfied(index)
    index["a"].finish(StepStatus.DONE, "ok")
    assert agent_step.dependencies_satisfied(index)


def test_failed_dependency_cascades_skip():
    plan = _write_plan([
        _write_step("a"),
        _write_step("b", depends_on=("a",)),
        _write_step("c", depends_on=("b",)),
    ])
    plan.step("a").finish(StepStatus.FAILED, "boom")
    plan.propagate_skipped()
    assert plan.step("b").status is StepStatus.SKIPPED
    assert plan.step("c").status is StepStatus.SKIPPED
    assert plan.next_executable() == []


@pytest.mark.parametrize("blocking", [StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.CANCELLED, StepStatus.SKIPPED])
def test_every_unsatisfiable_dependency_state_blocks_the_dependent(blocking):
    plan = _write_plan([_write_step("a"), _write_step("b", depends_on=("a",))])
    plan.step("a").finish(blocking, "x")
    plan.propagate_skipped()
    assert plan.step("b").status is StepStatus.SKIPPED
    assert plan.next_step() is None


def test_unknown_dependency_does_not_deadlock_the_plan():
    plan = _write_plan([_write_step("a", depends_on=("ghost",))])
    plan.propagate_skipped()
    assert plan.step("a").status is StepStatus.SKIPPED
    assert plan.next_step() is None


def test_skip_propagation_is_idempotent():
    plan = _write_plan([_write_step("a"), _write_step("b", depends_on=("a",))])
    plan.step("a").finish(StepStatus.FAILED, "x")
    assert plan.propagate_skipped() == ["b"]
    assert plan.propagate_skipped() == []


def test_cycle_detection():
    plan = _write_plan([
        _write_step("a", depends_on=("b",)),
        _write_step("b", depends_on=("a",)),
    ])
    assert plan.check_cycles() == ["a", "b"]


def test_acyclic_plan_reports_no_cycles():
    plan = _write_plan([_write_step("a"), _write_step("b", depends_on=("a",))])
    assert plan.check_cycles() == []


def test_completion_requires_every_step_terminal():
    plan = _write_plan([_write_step("a"), _write_step("b", depends_on=("a",))])
    assert not plan.is_complete
    plan.step("a").finish(StepStatus.DONE, "ok")
    assert not plan.is_complete
    plan.step("b").finish(StepStatus.DONE, "ok")
    assert plan.is_complete


# ----------------------------------------------------------------------
# write-ahead step state
# ----------------------------------------------------------------------


def test_begin_attempt_is_write_ahead():
    step = _write_step("a")
    assert step.status is StepStatus.PENDING
    step.begin_attempt("20260101-000000")
    assert step.status is StepStatus.RUNNING
    assert step.attempts == 1
    assert step.started_at == "20260101-000000"
    assert step.finished_at == ""


def test_attempts_increment_across_retries():
    step = _write_step("a")
    step.begin_attempt()
    step.finish(StepStatus.FAILED, "boom")
    step.status = StepStatus.PENDING
    step.begin_attempt()
    assert step.attempts == 2


def test_finish_rejects_non_terminal_targets():
    step = _write_step("a")
    step.begin_attempt()
    with pytest.raises(PlanStepError):
        step.finish(StepStatus.RUNNING, "still going")


def test_finish_clears_interrupted_flag():
    step = _write_step("a")
    step.begin_attempt()
    step.mark_interrupted()
    step.finish(StepStatus.DONE, "ok")
    assert step.interrupted is False


def test_write_ahead_reaches_disk_before_dispatch(tmp_path):
    """A hard kill mid-dispatch must leave RUNNING on disk, not PENDING."""
    settings = _settings(tmp_path)
    registry = build_registry(settings)
    holder = {}
    observed = {}

    def spy(**kwargs):
        store = holder["agent"]._task_store
        record = store.get(holder["agent"]._tasks.task_id)
        step = record.plan.step("step-1")
        observed["status"] = step.status
        observed["attempts"] = step.attempts
        observed["side_effect_key"] = step.side_effect_key
        raise KeyboardInterrupt("simulated hard death")

    registry.get("filesystem.write_file").fn = spy
    agent = _agent(settings, registry=registry)
    holder["agent"] = agent
    with pytest.raises(KeyboardInterrupt):
        agent.respond("hello python world")

    assert observed["status"] is StepStatus.RUNNING
    assert observed["attempts"] >= 1
    assert observed["side_effect_key"]

    record = agent._task_store.get(agent._tasks.task_id)
    on_disk = record.plan.step("step-1")
    assert on_disk.status is StepStatus.RUNNING
    assert on_disk.side_effect_key
    # a never-started later step is untouched
    assert record.plan.step("step-2").status is StepStatus.PENDING


def test_side_effecting_and_read_only_tools_are_distinguished():
    assert tool_is_read_only("filesystem.read_file")
    assert tool_is_read_only("system.info")
    assert not tool_is_read_only("filesystem.write_file")
    assert not tool_is_read_only("git.commit")
    assert side_effect_key("filesystem.read_file", {"path": "x"}) == ""
    assert side_effect_key("filesystem.write_file", {"path": "x"})


def test_side_effect_key_is_stable_and_argument_sensitive():
    a = side_effect_key("filesystem.write_file", {"path": "x", "content": "1"})
    b = side_effect_key("filesystem.write_file", {"content": "1", "path": "x"})
    c = side_effect_key("filesystem.write_file", {"path": "y", "content": "1"})
    assert a == b
    assert a != c


# ----------------------------------------------------------------------
# restart reconciliation
# ----------------------------------------------------------------------


def test_interrupted_step_is_classified_not_replayed(tmp_path):
    settings = _settings(tmp_path)
    store = TaskStore(settings.sessions_dir)
    plan = _write_plan([_write_step("s1"), _write_step("s2", depends_on=("s1",))])
    plan.step("s1").begin_attempt("20260101-000000")
    store.create(
        TaskRecord(task_id="crashed", project_name="p", project_root="", title="t", goal="g",
                   created_at="20260101-000000", status="pending", plan=plan)
    )

    agent = _agent(settings)  # constructing an Agent runs reconciliation

    recovered = store.get("crashed")
    step = recovered.plan.step("s1")
    assert step.interrupted is True
    assert step.status is StepStatus.RUNNING, "status must stay ambiguous"
    assert step.is_replayable is False, "a side-effecting step must never auto-replay"
    assert recovered.metadata["lifecycle"]["interrupted_steps"] == ["s1"]
    assert "crashed" in agent.request_context.recovered_task_ids


def test_interrupted_read_only_step_is_replayable(tmp_path):
    plan = _write_plan([_write_step("s1", tool="system.info", args={"components": ["cpu"]})])
    plan.step("s1").begin_attempt()
    plan.step("s1").mark_interrupted()
    assert plan.step("s1").is_replayable is True


def test_interrupted_side_effecting_step_is_not_replayable(tmp_path):
    plan = _write_plan([_write_step("s1")])
    plan.step("s1").begin_attempt()
    plan.step("s1").mark_interrupted()
    assert plan.step("s1").is_replayable is False


def test_completed_step_is_not_flagged_interrupted(tmp_path):
    settings = _settings(tmp_path)
    store = TaskStore(settings.sessions_dir)
    plan = _write_plan([_write_step("s1")])
    plan.step("s1").begin_attempt()
    plan.step("s1").finish(StepStatus.DONE, "ok")
    store.create(
        TaskRecord(task_id="done", project_name="p", project_root="", title="t", goal="g",
                   created_at="now", status="pending", plan=plan)
    )
    _agent(settings)
    step = store.get("done").plan.step("s1")
    assert step.interrupted is False
    assert step.status is StepStatus.DONE


def test_never_started_step_is_left_pending(tmp_path):
    settings = _settings(tmp_path)
    store = TaskStore(settings.sessions_dir)
    plan = _write_plan([_write_step("s1"), _write_step("s2", depends_on=("s1",))])
    plan.step("s1").begin_attempt()
    store.create(
        TaskRecord(task_id="mix", project_name="p", project_root="", title="t", goal="g",
                   created_at="now", status="pending", plan=plan)
    )
    _agent(settings)
    recovered = store.get("mix").plan
    assert recovered.step("s1").interrupted is True
    assert recovered.step("s2").status is StepStatus.PENDING
    assert recovered.step("s2").interrupted is False


def test_task_with_unresolved_step_is_not_recorded_completed(tmp_path):
    """An in-process hard death must not leave a 'completed' task behind."""
    settings = _settings(tmp_path)
    registry = build_registry(settings)

    def spy(**kwargs):
        raise KeyboardInterrupt("hard death")

    registry.get("filesystem.write_file").fn = spy
    agent = _agent(settings, registry=registry)
    with pytest.raises(KeyboardInterrupt):
        agent.respond("hello python world")

    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status != "completed"
    step = record.plan.step("step-1")
    assert step.interrupted is True
    assert step.status is StepStatus.RUNNING


def _settings(tmp_path):
    from config.settings import Settings

    data = tmp_path / "data"
    settings = Settings(
        base_dir=tmp_path, data_dir=data, memory_dir=data / "m", logs_dir=data / "l",
        backups_dir=data / "b", scratch_dir=tmp_path / "w", sessions_dir=data / "s",
        allowed_roots=[tmp_path], max_turns=2,
    )
    for path in (data, data / "m", data / "l", tmp_path / "w", data / "s"):
        path.mkdir(parents=True, exist_ok=True)
    return settings


def _recovered_ids(settings):
    agent = _agent(settings)
    return agent.request_context.recovered_task_ids


# ----------------------------------------------------------------------
# expected_result / recovery_hint
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected,output,exit_code,needle",
    [
        ({"type": "output_contains", "value": "Hello World"}, "hello world", None, "found"),
        ({"type": "output_contains", "value": "Hello World"}, "nothing", None, "NOT found"),
        ({"type": "exit_code", "value": 0}, "", 0, "got 0"),
        ({"type": "exit_code", "value": 0}, "", 3, "got 3"),
    ],
)
def test_expected_result_vocabulary_is_evaluated(tmp_path, expected, output, exit_code, needle):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    from core.executor import ExecutionOutcome

    step = _write_step("s1", expected_result=expected)
    outcome = ExecutionOutcome(tool="t", success=True, output=output, exit_code=exit_code)
    note = agent._evaluate_expected_result(step, outcome)
    assert needle in note


def test_expected_result_file_exists(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    from core.executor import ExecutionOutcome

    present = tmp_path / "present.txt"
    present.write_text("x", encoding="utf-8")
    step = _write_step("s1", expected_result={"type": "file_exists", "path": str(present)})
    assert "exists" in agent._evaluate_expected_result(step, ExecutionOutcome(tool="t"))
    step.expected_result = {"type": "file_exists", "path": str(tmp_path / "absent.txt")}
    assert "missing" in agent._evaluate_expected_result(step, ExecutionOutcome(tool="t"))


def test_expected_result_absent_returns_none(tmp_path):
    agent = _agent(_settings(tmp_path))
    from core.executor import ExecutionOutcome

    assert agent._evaluate_expected_result(_write_step("s1"), ExecutionOutcome(tool="t")) is None


def test_expected_result_is_populated_and_recorded_by_a_real_run(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("hello python world")
    record = agent._task_store.get(agent._tasks.task_id)
    run_step = record.plan.step("step-2")
    assert run_step.tool == "python.run_script"
    assert run_step.expected_result == {"type": "output_contains", "value": "Hello World"}
    assert "expected output" in run_step.detail
    assert run_step.result_summary


def test_recovery_hint_is_derived_from_observed_failure(tmp_path):
    settings = _settings(tmp_path)
    registry = build_registry(settings)

    def boom(**kwargs):
        from tools.registry import ToolResult

        return ToolResult(False, error="disk on fire")

    registry.get("filesystem.write_file").fn = boom
    agent = _agent(settings, registry=registry)
    agent.respond("hello python world")
    record = agent._task_store.get(agent._tasks.task_id)
    step = record.plan.step("step-1")
    assert step.status is StepStatus.FAILED
    assert "disk on fire" in step.recovery_hint
    assert "Previous attempt failed" in step.recovery_hint


def test_recovery_hint_survives_persistence(tmp_path):
    settings = _settings(tmp_path)
    registry = build_registry(settings)

    def boom(**kwargs):
        from tools.registry import ToolResult

        return ToolResult(False, error="nope")

    registry.get("filesystem.write_file").fn = boom
    agent = _agent(settings, registry=registry)
    agent.respond("hello python world")
    reloaded = TaskStore(settings.sessions_dir).get(agent._tasks.task_id)
    assert reloaded.plan.step("step-1").recovery_hint


def test_blocked_step_records_that_it_never_executed(tmp_path):
    from config.permissions import Decision
    from core.agent import Agent

    settings = _settings(tmp_path)
    registry = build_registry(settings)

    class _Deny(PermissionManager):
        def evaluate(self, *args, **kwargs):
            return Decision(allow=False, level=args[2], reason="policy says no")

    agent = Agent(
        provider=_Scripted(), settings=settings, registry=registry,
        permissions=_Deny(settings), events=EventBus(), confirmer=None,
        model_name="", model_catalog=[],
    )
    agent.respond("hello python world")
    record = agent._task_store.get(agent._tasks.task_id)
    step = record.plan.step("step-1")
    assert step.status is StepStatus.BLOCKED
    assert "did not execute" in step.recovery_hint
    assert step.attempts == 1


# ----------------------------------------------------------------------
# task.plan read-only inspection
# ----------------------------------------------------------------------


def test_task_plan_is_exposed_read_only(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("show me system information about cpu and memory")
    task_id = agent._tasks.task_id

    payload = json.loads(task_info(task_id, settings).output)
    plan = payload["plan"]
    assert plan["present"] is True
    assert plan["plan_version"] == PLAN_SCHEMA_VERSION
    assert plan["counts"]["done"] == 1
    assert plan["is_complete"] is True
    assert plan["steps"][0]["status"] == "done"
    assert plan["steps"][0]["attempts"] == 1


def test_task_plan_withholds_args_by_default(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("show me system information about cpu and memory")
    task_id = agent._tasks.task_id

    plan = json.loads(task_info(task_id, settings).output)["plan"]
    args = plan["steps"][0]["args"]
    assert args["__withheld__"] is True
    assert args["arg_count"] >= 1
    assert "components" not in json.dumps(args)


def test_task_plan_args_available_on_explicit_request(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("show me system information about cpu and memory")
    task_id = agent._tasks.task_id

    plan = json.loads(task_info(task_id, settings, include_plan_args=True).output)["plan"]
    assert "components" in plan["steps"][0]["args"]


def test_task_plan_reports_absent_plan(tmp_path):
    settings = _settings(tmp_path)
    store = TaskStore(settings.sessions_dir)
    store.create(
        TaskRecord(task_id="bare", project_name="p", project_root="", title="t", goal="g", created_at="now")
    )
    plan = json.loads(task_info("bare", settings).output)["plan"]
    assert plan == {"present": False}


def test_task_plan_exposes_next_executable_and_interrupted(tmp_path):
    settings = _settings(tmp_path)
    store = TaskStore(settings.sessions_dir)
    plan = _write_plan([_write_step("s1"), _write_step("s2", depends_on=("s1",))])
    plan.step("s1").begin_attempt()
    plan.step("s1").mark_interrupted()
    store.create(
        TaskRecord(task_id="iv", project_name="p", project_root="", title="t", goal="g",
                   created_at="now", plan=plan)
    )
    view = json.loads(task_info("iv", settings).output)["plan"]
    assert view["interrupted_step_ids"] == ["s1"]
    # s2's dependency is unresolved (RUNNING, not DONE and not terminal), so s2
    # must not be offered as runnable. Unresolved work blocks its dependents.
    assert view["next_executable_step_ids"] == []


def test_task_info_still_reports_legacy_record_fields(tmp_path):
    settings = _settings(tmp_path)
    store = TaskStore(settings.sessions_dir)
    store.create(
        TaskRecord(task_id="legacy", project_name="p", project_root="", title="T", goal="G",
                   created_at="now", status="completed", outcome="done")
    )
    payload = json.loads(task_info("legacy", settings).output)
    assert payload["title"] == "T"
    assert payload["status"] == "completed"
    assert payload["outcome"] == "done"
    assert payload["plan"] == {"present": False}


# ----------------------------------------------------------------------
# execution regression
# ----------------------------------------------------------------------


def test_successful_run_persists_a_complete_plan(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    reply = agent.respond("show me system information about cpu and memory")
    assert "System check" in reply
    plan = agent._task_store.get(agent._tasks.task_id).plan
    assert plan.is_complete
    assert plan.counts()["failed"] == 0
    assert plan.step("step-1").result_summary


def test_multi_step_intent_executes_in_order_and_records_each_step(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("hello python world")
    plan = agent._task_store.get(agent._tasks.task_id).plan
    assert [s.tool for s in plan.steps] == ["filesystem.write_file", "python.run_script"]
    assert all(s.status is StepStatus.DONE for s in plan.steps)
    assert [s.step_id for s in plan.steps] == ["step-1", "step-2"]


def test_task_manager_step_ids_are_linked_to_plan(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("show me system information about cpu and memory")
    assert [s.step_id for s in agent._tasks.steps] == ["step-1"]
    assert agent._tasks.step_by_id("step-1") is agent._tasks.steps[0]


def test_llm_loop_persists_plan_for_model_tool_calls(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings, calls=[_call("system.info", {"components": ["cpu"]})])
    agent.respond("gather details about the repository layout please")
    plan = agent._task_store.get(agent._tasks.task_id).plan
    assert plan is not None
    assert plan.step("c1").tool == "system.info"
    assert plan.step("c1").status is StepStatus.DONE


def test_cancellation_marks_the_plan_step_cancelled(tmp_path):
    from core.cancellation import CancellationToken

    settings = _settings(tmp_path)
    agent = _agent(settings)

    class _Cancel(CancellationToken):
        def is_cancelled(self):
            return True

    agent.respond("show me system information about cpu and memory", cancellation_token=_Cancel())
    plan = agent._task_store.get(agent._tasks.task_id).plan
    if plan is not None and plan.steps:
        assert plan.steps[0].status is StepStatus.CANCELLED


def test_plan_is_replaced_per_turn_and_superseded_is_retained(tmp_path):
    settings = _settings(tmp_path)
    calls = [
        _call("system.info", {"components": ["cpu"]}),
        _call("system.info", {"components": ["memory"]}),
    ]
    agent = _agent(settings, calls=calls)
    agent.respond("gather details about the repository layout please")
    plan = agent._task_store.get(agent._tasks.task_id).plan
    assert plan is not None
    assert plan.superseded, "a re-planned turn must not erase the earlier plan"
    assert ExecutionPlanRecord.from_dict(plan.to_dict()).superseded == plan.superseded


def test_superseded_history_is_bounded():
    previous = None
    for index in range(ExecutionPlanRecord.MAX_SUPERSEDED + 5):
        current = _write_plan([_write_step(f"s{index}")])
        current.absorb(previous)
        previous = current
    assert len(previous.superseded) <= ExecutionPlanRecord.MAX_SUPERSEDED


def test_dependent_step_is_skipped_when_its_dependency_fails(tmp_path):
    settings = _settings(tmp_path)
    registry = build_registry(settings)

    def boom(**kwargs):
        from tools.registry import ToolResult

        return ToolResult(False, error="denied by fixture")

    registry.get("filesystem.write_file").fn = boom
    agent = _agent(settings, registry=registry)
    agent.respond("hello python world")
    plan = agent._task_store.get(agent._tasks.task_id).plan
    assert plan.step("step-1").status is StepStatus.FAILED
    assert plan.step("step-2").status is StepStatus.SKIPPED
    assert plan.step("step-2").attempts == 0


def test_active_plan_is_reset_between_requests(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("show me system information about cpu and memory")
    assert agent.active_plan is not None
    agent._begin_request_context()
    assert agent.active_plan is None


def test_greeting_turn_persists_no_plan(tmp_path):
    settings = _settings(tmp_path)
    agent = _agent(settings)
    agent.respond("hello")
    assert agent._task_store.get(agent._tasks.task_id).plan is None
