"""M2.12 post-P3 end-to-end composition audit tests.

These are *composition* tests, not unit tests: each one drives the real
``Agent.respond()`` entry point and asserts that the request-scoped context,
durable plan, TaskManager lifecycle, VerificationLedger and RecoveryPolicy
compose into one coherent execution lifecycle.

Every test here corresponds to a real invariant established by tracing the
production call paths, or to a defect the audit confirmed and fixed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from config.permissions import PermissionManager
from core.agent import Agent
from core.events import EventBus
from core.recovery import FailureCategory, FailureObservation, RecoveryAction, RecoveryPolicy
from core.task_manager import StepStatus, TaskManager, TaskState
from core.verification import VerificationResult, VerificationState
from memory.preferences import PreferencesStore
from tools import build_registry
from tools.registry import ToolRegistry, ToolResult, ToolSpec


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _spec(name: str, fn, required=()) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        required=list(required),
        fn=fn,
    )


def _registry(specs) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def _response(*calls, text=""):
    return SimpleNamespace(tool_calls=list(calls), text=text)


def _call(name, **kwargs):
    return SimpleNamespace(name=name, arguments=kwargs, id=f"c-{name}")


class _Provider:
    def __init__(self, script):
        self.script = list(script)
        self.model_label = "audit"
        self.name = "audit"

    def chat(self, messages, tools=None, model=None):
        return self.script.pop(0) if self.script else _response(text="done")


def _agent(settings, provider, registry, confirmer=None):
    prefs = PreferencesStore(settings.memory_dir)
    return Agent(
        provider=provider,
        settings=settings,
        registry=registry,
        permissions=PermissionManager(settings, prefs),
        events=EventBus(),
        confirmer=confirmer,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )


def _ledger_of(agent):
    return agent._tasks.ledger


def _persisted_lifecycle(agent):
    record = agent._task_store.get(agent._tasks.task_id)
    assert record is not None
    return (record.metadata or {}).get("lifecycle", {})


# ==========================================================================
# 1. success -> ledger -> DONE
# ==========================================================================


def test_success_records_adjudicated_ledger_entry_and_completes(tmp_settings):
    registry = build_registry(tmp_settings)
    registry.get("python.run_script").fn = lambda **kw: ToolResult(
        True, output="Hello World", exit_code=0
    )
    provider = _Provider([_response(_call("python.run_script", code="print('Hello World')"))])
    agent = _agent(tmp_settings, provider, registry)

    agent.respond("Create a Python file that prints Hello World")

    assert agent._tasks.state.value == "completed"
    assert agent._tasks.verification is VerificationState.PASSED
    entry = _ledger_of(agent).latest
    assert entry is not None
    assert entry.state is VerificationState.PASSED
    assert entry.requested_state is VerificationState.PASSED
    assert entry.downgraded is False
    # The PASSED is evidence-backed, not a bare assertion.
    assert entry.trust.value == "observed"
    assert entry.evidence.get("exists") is True
    assert entry.evidence.get("output_matched") is True
    # Identity on the entry is the authoritative task identity, not a minted one.
    assert entry.task_id == agent._tasks.task_id
    assert entry.run_id == agent._tasks.run_id
    # The persisted projection agrees with the authoritative scalar.
    lifecycle = _persisted_lifecycle(agent)
    assert lifecycle["verification"] == "passed"
    assert lifecycle["verification_ledger"]["entries"][-1]["state"] == "passed"


# ==========================================================================
# 2. failure -> observation -> retry -> success
# ==========================================================================


def test_recoverable_failure_retries_then_completes(tmp_settings):
    invocations = []

    def flaky(**kwargs):
        invocations.append(1)
        if len(invocations) == 1:
            return ToolResult(False, error="ValueError: transient")
        return ToolResult(True, output="recovered")

    registry = _registry([_spec("project.flaky", flaky)])
    provider = _Provider([
        _response(_call("project.flaky")),
        _response(_call("project.flaky")),
        _response(text="recovered"),
    ])
    agent = _agent(tmp_settings, provider, registry)

    agent.respond("please execute the project flaky operation now")

    assert len(invocations) == 2, "the retry must actually re-dispatch the tool"
    assert agent._tasks.recovery_attempts == 1
    assert agent._tasks.state.value == "completed"
    # The failed attempt is retired, not mutated: a superseded plan holds it.
    plan = agent._active_plan
    assert plan is not None and plan.superseded
    retired = plan.superseded[-1]["steps"][0]
    assert retired["status"] == "failed"
    assert retired["attempts"] == 1
    # DONE is terminal for the successful attempt.
    assert plan.steps[0].status is StepStatus.DONE


def test_retry_does_not_duplicate_step_identity(tmp_settings):
    invocations = []

    def flaky(**kwargs):
        invocations.append(1)
        return ToolResult(False, error="ValueError: transient") if len(invocations) == 1 else ToolResult(True)

    registry = _registry([_spec("project.flaky", flaky)])
    provider = _Provider([
        _response(_call("project.flaky")),
        _response(_call("project.flaky")),
        _response(text="ok"),
    ])
    agent = _agent(tmp_settings, provider, registry)
    agent.respond("please execute the project flaky operation now")

    plan = agent._active_plan
    live_ids = [step.step_id for step in plan.steps]
    retired_ids = [step["step_id"] for sup in plan.superseded for step in sup["steps"]]
    assert live_ids == ["c-project.flaky"]
    # The same logical step id is reused across the retry, never duplicated.
    assert retired_ids == ["c-project.flaky"]
    assert len(live_ids) == len(set(live_ids))


def test_later_executed_failure_invalidates_an_earlier_passed_verification(tmp_settings):
    """A PASSED recorded earlier must not survive a later executed failure.

    This is the real stale-verification guard: ``invalidate_verification`` fires
    when a subsequent *executed* operation fails, so the task cannot end on a
    PASSED that predates the failure.
    """
    vpass = VerificationResult(VerificationState.PASSED, "ok", "render_file", {"exists": True})
    registry = _registry([
        _spec("project.p", lambda **kw: ToolResult(True, output="x", verification=vpass)),
        _spec("project.f", lambda **kw: ToolResult(False, error="ValueError: later failure")),
    ])
    provider = _Provider([
        _response(_call("project.p"), _call("project.f")),
        _response(text="ok"),
    ])
    agent = _agent(tmp_settings, provider, registry)
    agent.respond("please execute the project p operation now")

    ledger = _ledger_of(agent)
    # The PASSED was genuinely adjudicated...
    assert any(entry.state is VerificationState.PASSED for entry in ledger.entries)
    # ...and the later executed failure invalidated it, so it cannot survive.
    assert agent._tasks.verification is not VerificationState.PASSED
    invalidated = [h for h in agent._tasks.verification_history if h["state"] == "passed"]
    assert invalidated, "the displaced PASSED must remain auditable"
    assert agent._tasks.state.value == "failed"


# ==========================================================================
# 3. failure -> bounded retries -> ABORT
# ==========================================================================


def test_repeated_failure_is_bounded_and_ends_failed(tmp_settings):
    invocations = []
    registry = _registry([_spec("project.op", lambda **kw: (
        invocations.append(1), ToolResult(False, error="ValueError: nope"))[1])])
    provider = _Provider([_response(_call("project.op")) for _ in range(8)])
    agent = _agent(tmp_settings, provider, registry)

    reply = agent.respond("please execute the project op operation now")

    budget = max(0, int(getattr(tmp_settings, "max_recovery_attempts", 3)))
    assert len(invocations) == budget + 1, "retries must be bounded, never unbounded"
    assert agent._tasks.state.value == "failed"
    assert agent._tasks.verification is VerificationState.FAILED
    assert "bounded recovery" in reply


def test_bounded_failure_records_the_terminal_verdict_in_the_ledger(tmp_settings):
    """Regression: the terminal FAILED used to bypass the ledger entirely.

    The four persistence/reconciliation sites in ``_finish_tracking`` called
    ``set_verification`` directly, so a task could end FAILED with an empty
    ledger and a persisted projection that did not explain its own state.
    """
    invocations = []
    registry = _registry([_spec("project.op", lambda **kw: (
        invocations.append(1), ToolResult(False, error="ValueError: nope"))[1])])
    provider = _Provider([_response(_call("project.op")) for _ in range(8)])
    agent = _agent(tmp_settings, provider, registry)
    agent.respond("please execute the project op operation now")

    ledger = _ledger_of(agent)
    assert agent._tasks.verification is VerificationState.FAILED
    # The authoritative state is now always explained by a ledger entry.
    assert len(ledger) >= 1
    assert ledger.latest_state is VerificationState.FAILED
    # And the persisted projection agrees with the authoritative scalar.
    lifecycle = _persisted_lifecycle(agent)
    assert lifecycle["verification"] == "failed"
    persisted_states = [e["state"] for e in lifecycle["verification_ledger"]["entries"]]
    assert persisted_states[-1] == "failed"


def test_persistence_failure_still_records_through_the_ledger(tmp_settings, monkeypatch):
    """A task-store write failure is fail-closed and still audited."""
    registry = _registry([_spec("project.ok", lambda **kw: ToolResult(True, output="ok"))])
    provider = _Provider([_response(_call("project.ok")), _response(text="ok")])
    agent = _agent(tmp_settings, provider, registry)

    calls = {"n": 0}
    real_update = agent._task_store.update

    def flaky_update(task_id, /, **fields):
        calls["n"] += 1
        if calls["n"] > 2:
            raise OSError("disk on fire")
        return real_update(task_id, **fields)

    monkeypatch.setattr(agent._task_store, "update", flaky_update)
    agent.respond("please execute the project ok operation now")

    assert agent._tasks.verification is VerificationState.FAILED
    assert _ledger_of(agent).latest_state is VerificationState.FAILED
    assert any(
        entry.state is VerificationState.FAILED for entry in _ledger_of(agent).entries
    )


# ==========================================================================
# 4. verification failure semantics
# ==========================================================================


def test_capability_verification_failure_stops_the_task_and_is_recorded(tmp_settings):
    vfail = VerificationResult(VerificationState.FAILED, "render missing", "render_file", {"exists": False})
    registry = _registry([_spec("project.vf", lambda **kw: ToolResult(True, output="ok", verification=vfail))])
    provider = _Provider([_response(_call("project.vf")), _response(text="ok")])
    agent = _agent(tmp_settings, provider, registry)

    reply = agent.respond("please execute the project vf operation now")

    assert "verification failed" in reply
    assert agent._tasks.state.value == "failed"
    assert agent._tasks.verification is VerificationState.FAILED
    entry = _ledger_of(agent).latest
    assert entry.state is VerificationState.FAILED
    assert entry.legacy_kind == "render_file"
    # A FAILED verdict is never converted into PASSED, in either direction.
    assert entry.requested_state is VerificationState.FAILED
    assert entry.state is not VerificationState.PASSED


def test_verification_failure_never_ends_the_task_as_passed(tmp_settings):
    """A FAILED verdict is terminal for the request; it cannot become PASSED."""
    vfail = VerificationResult(VerificationState.FAILED, "gone", "render_file", {"exists": False})
    registry = _registry([_spec("project.vv", lambda **kw: ToolResult(True, output="x", verification=vfail))])
    provider = _Provider([_response(_call("project.vv")), _response(text="ok")])
    agent = _agent(tmp_settings, provider, registry)
    agent.respond("please execute the project vv operation now")

    assert agent._tasks.verification is VerificationState.FAILED
    assert agent._tasks.state.value == "failed"
    assert _ledger_of(agent).latest_state is VerificationState.FAILED
    # The recorded request was FAILED too: no silent upgrade anywhere.
    assert _ledger_of(agent).latest.requested_state is VerificationState.FAILED


def test_rerun_verify_is_reachable_only_from_the_debug_loop(tmp_settings):
    """Documents the traced asymmetry rather than asserting a guess.

    RERUN_VERIFY requires a VERIFICATION_FAILED category, which is only
    classified for a FAILED/BLOCKED/CANCELLED step. In ``_llm_loop`` a failed
    verification short-circuits to "Task stopped because verification failed"
    *before* the recovery branch, so the policy is never consulted and the
    action is unreachable there. DebugLoop, which has no durable plan, is the
    only caller that can produce it.
    """
    policy = RecoveryPolicy(max_retries=3, max_rerun_verify=1)
    decision = policy.decide(FailureObservation(
        category=FailureCategory.VERIFICATION_FAILED,
        verification_state=VerificationState.FAILED,
        verification_evidence={"exists": False},
        step_status=StepStatus.FAILED,
    ))
    assert decision.action is RecoveryAction.RERUN_VERIFY

    # And the _llm_loop short-circuit is real: a capability verification failure
    # ends the task without ever reaching recovery.
    vfail = VerificationResult(VerificationState.FAILED, "missing", "render_file", {"exists": False})
    registry = _registry([_spec("project.vf", lambda **kw: ToolResult(True, output="ok", verification=vfail))])
    provider = _Provider([_response(_call("project.vf")), _response(text="ok")])
    agent = _agent(tmp_settings, provider, registry)
    agent.respond("please execute the project vf operation now")
    # No recovery attempt was made for a verification failure.
    assert agent._tasks.recovery_attempts == 0


# ==========================================================================
# 5. escalation -> router
# ==========================================================================


def test_exhausted_retry_budget_escalates_through_the_existing_router_only(tmp_settings):
    invocations = []
    registry = _registry([_spec("project.op", lambda **kw: (
        invocations.append(1), ToolResult(False, error="ValueError: nope"))[1])])
    provider = _Provider([_response(_call("project.op")) for _ in range(8)])
    agent = _agent(tmp_settings, provider, registry)

    reply = agent.respond("please execute the project op operation now")

    assert "stronger model" in reply
    # Escalation is a signal to the router, never a model choice of its own.
    assert agent._router_verify_ok is False
    assert agent._last_model is not None
    # The policy does not mint escalation counts; only the router does.
    assert agent._escalations_used == 0


def test_no_second_router_is_introduced_by_recovery(tmp_settings):
    registry = _registry([_spec("project.ok", lambda **kw: ToolResult(True, output="ok"))])
    agent = _agent(tmp_settings, _Provider([_response(_call("project.ok"))]), registry)
    # Exactly one router exists and it is the pre-existing one.
    assert hasattr(agent._router, "choose")
    assert agent._router.__class__.__module__.startswith("ai.")
    # RecoveryPolicy exposes no model-selection surface at all.
    policy = RecoveryPolicy()
    assert not any("model" in name.lower() for name in dir(policy) if not name.startswith("__"))


# ==========================================================================
# 6. cancellation -> no recovery
# ==========================================================================


class _Cancelled:
    def is_cancelled(self):
        return True


def test_cancel_before_execution_never_dispatches_and_never_recovers(tmp_settings):
    invocations = []
    registry = _registry([_spec("project.c", lambda **kw: (
        invocations.append(1), ToolResult(True, output="ok"))[1])])
    provider = _Provider([_response(_call("project.c")), _response(text="x")])
    agent = _agent(tmp_settings, provider, registry)

    reply = agent.respond("please execute the project c operation now", cancellation_token=_Cancelled())

    assert reply == "Task cancelled."
    assert agent._tasks.state.value == "cancelled"
    assert invocations == []
    assert agent._tasks.recovery_attempts == 0
    assert _ledger_of(agent).latest_state in (VerificationState.NOT_RUN, VerificationState.UNVERIFIED)


def test_cancel_during_execution_never_retries(tmp_settings):
    invocations = []
    registry = _registry([_spec("project.d", lambda **kw: (
        invocations.append(1), ToolResult(False, cancelled=True))[1])])
    provider = _Provider([_response(_call("project.d")) for _ in range(5)])
    agent = _agent(tmp_settings, provider, registry)

    reply = agent.respond("please execute the project d operation now")

    assert reply == "Task cancelled."
    assert agent._tasks.state.value == "cancelled"
    assert len(invocations) == 1, "cancellation must not be retried"
    assert agent._tasks.recovery_attempts == 0
    assert agent._active_plan.steps[0].status is StepStatus.CANCELLED


def test_cancellation_dominates_every_recovery_action(tmp_settings):
    policy = RecoveryPolicy(max_retries=99, max_escalations=99, max_rerun_verify=99, allow_skip=True)
    for category in FailureCategory:
        for status in (StepStatus.FAILED, StepStatus.RUNNING, StepStatus.CANCELLED):
            decision = policy.decide(FailureObservation(
                category=category, tool="t", cancelled=True, step_status=status,
                recovery_attempts=0, escalations_used=0,
                verification_state=VerificationState.FAILED,
                verification_evidence={"exists": False},
            ))
            assert decision.action is RecoveryAction.NONE, (category, status)


def test_cancellation_after_failure_before_recovery_does_not_retry(tmp_settings):
    """Cancel arriving mid-request must stop before any retry is dispatched."""
    state = {"calls": 0}

    def flaky(**kwargs):
        state["calls"] += 1
        return ToolResult(True, output="late")

    class _CancelsAfterFirstPoll:
        def __init__(self):
            self.n = 0

        def is_cancelled(self):
            self.n += 1
            return self.n > 1

    token = _CancelsAfterFirstPoll()
    registry = _registry([_spec("project.f", flaky)])
    provider = _Provider([_response(_call("project.f")) for _ in range(5)])
    agent = _agent(tmp_settings, provider, registry)

    reply = agent.respond("please execute the project f operation now", cancellation_token=token)

    assert agent._tasks.state.value == "cancelled"
    # Cancellation is re-checked at dispatch, so at most the already-authorised
    # attempt could have run. Crucially no *retry* was dispatched.
    assert state["calls"] <= 1, "no retry may be dispatched after cancellation"
    assert agent._tasks.recovery_attempts == 0
    assert "cancel" in reply.lower()


# ==========================================================================
# 7. interrupted step -> no replay
# ==========================================================================


@pytest.mark.parametrize("budget", [0, 1, 5, 99])
def test_interrupted_step_is_never_replayable_at_any_budget(budget):
    decision = RecoveryPolicy(
        max_retries=budget, max_escalations=budget, allow_skip=True
    ).decide(FailureObservation(
        category=FailureCategory.EXECUTION_FAILED,
        tool="blender.render",
        step_status=StepStatus.FAILED,
        step_replayable=False,
        interrupted=True,
    ))
    assert decision.action is RecoveryAction.ABORT
    assert "interrupted" in decision.reason


def test_reconciliation_records_interruption_without_replaying(tmp_settings):
    from core.planner import ExecutionPlanRecord, PlanStepRecord
    from core.planner import StepStatus as PlanStepStatus

    registry = _registry([_spec("project.ok", lambda **kw: ToolResult(True, output="ok"))])
    agent = _agent(tmp_settings, _Provider([_response(_call("project.ok"))]), registry)
    agent.respond("please execute the project ok operation now")

    # Simulate a persisted plan whose step was left RUNNING by a process death.
    record = agent._task_store.get(agent._tasks.task_id)
    plan = ExecutionPlanRecord(goal="g")
    live = PlanStepRecord(step_id="s1", tool="project.ok")
    live.status = PlanStepStatus.RUNNING
    plan.steps = [live]
    record.plan = plan
    agent._task_store.update(record.task_id, plan=plan)

    flagged = agent._classify_interrupted_steps(record)
    assert flagged == ["s1"]
    # Status is deliberately left RUNNING: only an explicit resume may resolve it.
    assert plan.steps[0].status is PlanStepStatus.RUNNING
    assert plan.steps[0].interrupted is True
    # And recovery still refuses it, at any budget.
    for budget in (0, 5, 99):
        decision = RecoveryPolicy(max_retries=budget).decide(FailureObservation(
            category=FailureCategory.EXECUTION_FAILED,
            tool=plan.steps[0].tool,
            step_status=StepStatus.FAILED,
            interrupted=plan.steps[0].interrupted,
        ))
        assert decision.action is RecoveryAction.ABORT


def test_swallowed_illegal_transition_cannot_corrupt_the_outcome(tmp_settings):
    """Documents a real, tolerated wrinkle found by tracing.

    After a recovery, a turn that returns no tool calls makes ``_llm_loop``
    attempt ``RECOVERING -> COMPLETED``, which the state machine forbids.
    ``Agent._transition`` deliberately swallows it (it must not raise from
    cleanup paths) and logs a warning; ``_respond`` then re-evaluates from the
    step statuses and reaches the correct terminal state. The durable record
    status is always recomputed from steps, so the swallowed edge cannot
    produce a wrong outcome. This test pins that outcome.
    """
    from core.task_manager import _ALLOWED_TRANSITIONS

    assert TaskState.COMPLETED not in _ALLOWED_TRANSITIONS[TaskState.RECOVERING]

    invocations = []

    def flaky(**kwargs):
        invocations.append(1)
        return ToolResult(False, error="ValueError: transient")

    registry = _registry([_spec("project.f", flaky)])
    provider = _Provider([_response(_call("project.f")), _response(text="giving up")])
    agent = _agent(tmp_settings, provider, registry)

    agent.respond("please execute the project f operation now")

    # A FAILED step exists, so the task must not be reported as completed even
    # though the intermediate COMPLETED edge was attempted and refused.
    assert any(step.status is StepStatus.FAILED for step in agent._tasks.steps)
    assert agent._tasks.state.value == "failed"
    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status == "failed"
    assert _persisted_lifecycle(agent)["state"] == "failed"


# ==========================================================================
# 8/9. persistence: malformed and legacy records
# ==========================================================================


def test_malformed_persisted_verification_degrades_safely(tmp_settings):
    from core.verification_ledger import VerificationLedger

    for payload in (None, "junk", {"entries": [None, 3]}, {"entries": [{"state": "passed", "evidence": {}}]}):
        ledger = VerificationLedger.from_dict(payload)
        assert all(
            entry.state is not VerificationState.PASSED or entry.evidence
            for entry in ledger.entries
        )


def test_legacy_task_record_without_a_ledger_still_loads(tmp_settings):
    from core.task_store import TaskRecord

    legacy = {
        "task_id": "legacy-1",
        "project_name": "p",
        "project_root": "",
        "title": "t",
        "goal": "g",
        "created_at": "20260101-000000",
        "status": "completed",
        "metadata": {"lifecycle": {"state": "completed", "verification": "passed"}},
    }
    record = TaskRecord.from_dict(legacy)
    assert record.status == "completed"
    assert record.metadata["lifecycle"]["verification"] == "passed"
    assert "verification_ledger" not in record.metadata["lifecycle"]


def test_unreadable_plan_does_not_become_an_empty_plan(tmp_settings):
    from core.task_store import _coerce_plan

    assert _coerce_plan(None) is None
    assert _coerce_plan("nonsense") is None
    assert _coerce_plan({"steps": "not-a-list"}) is None
    assert _coerce_plan({"steps": [{"no_step_id": True}]}) is None


def test_bounded_layers_stay_bounded(tmp_settings):
    tasks = TaskManager()
    for index in range(40):
        tasks.record_verification(VerificationResult(VerificationState.FAILED, f"d{index}", "execution"))
        tasks.invalidate_verification("invalidated")
    assert len(tasks.verification_history) == 20
    assert len(tasks.verification_ledger_snapshot()["entries"]) == 20
    assert len(tasks.ledger) == 40


# ==========================================================================
# step status contract
# ==========================================================================
# The structured-signal / error-wording contract for step status is pinned in
# tests/test_step_status_contract.py, next to the fix it guards.


# ==========================================================================
# permissions cannot be bypassed by recovery
# ==========================================================================


def test_retry_re_evaluates_permissions_on_every_attempt(tmp_settings):
    """A retry re-enters Executor.run, so the permission check runs again."""
    seen = []
    registry = _registry([_spec("project.p", lambda **kw: ToolResult(False, error="nope"))])
    provider = _Provider([_response(_call("project.p")) for _ in range(6)])
    agent = _agent(tmp_settings, provider, registry)

    original = agent._executor.run

    def counting_run(tool, args, token=None):
        seen.append(tool)
        return original(tool, args, token)

    agent._executor.run = counting_run
    agent.respond("please execute the project p operation now")

    budget = max(0, int(getattr(tmp_settings, "max_recovery_attempts", 3)))
    assert len(seen) == budget + 1
    # Every single attempt went through the executor, permission check included.
    assert set(seen) == {"project.p"}


def test_recovery_cannot_dispatch_anything_itself(tmp_settings):
    """The policy is a decision layer: `decide` is its only operation."""
    policy = RecoveryPolicy()
    public = {name for name in dir(policy) if not name.startswith("_")}
    # Only the decision entry point, plus inert data fields.
    assert public - {
        "max_retries", "max_rerun_verify", "max_escalations", "allow_skip", "decide",
    } == set()
    assert callable(policy.decide)


def test_recovery_decision_is_json_serialisable_for_audit(tmp_settings):
    decision = RecoveryPolicy().decide(FailureObservation(
        category=FailureCategory.EXECUTION_FAILED, tool="t", error_message="boom",
        step_status=StepStatus.FAILED,
    ))
    json.dumps(decision.to_dict())
    json.dumps(FailureObservation(
        category=FailureCategory.EXECUTION_FAILED, tool="t",
    ).to_dict())
