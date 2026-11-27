"""M2.15.2 - recovery exhaustion must survive a restart.

A counter cannot express exhaustion on its own. ``recovery_attempts`` only
*implies* it relative to a budget, and ``TaskManager.begin_task`` zeroes the
counter for every new task. Restarting the process therefore handed an already
exhausted task a full budget, so a task that had burned every retry and every
escalation would retry again - once per process lifetime, indefinitely, with
nothing in the durable record saying the path had already closed.

These tests assert three separate things, because they are separately falsifiable:

* an ``ABORT`` marks exhaustion terminally and durably;
* the mark is restored from ``TaskRecord.metadata["lifecycle"]`` after a restart;
* a restored, exhausted task is never handed budget again - including when the
  budgets are *raised*, which is the case a counter-only design would get wrong.
"""

from __future__ import annotations

import pytest

from core.recovery import (
    FailureCategory,
    FailureObservation,
    RecoveryAction,
    RecoveryPolicy,
)
from core.task_manager import StepStatus, TaskManager
from core.task_store import TaskRecord, TaskStore


def _observation(**overrides) -> FailureObservation:
    """A retryable, replayable failure - the shape recovery actually retries."""
    fields = dict(
        category=FailureCategory.EXECUTION_FAILED,
        step_status=StepStatus.FAILED,
        retryable=True,
    )
    fields.update(overrides)
    return FailureObservation(**fields)


# ══════════════════════════════════════════════════════ 1. bounded semantics preserved


class TestBoundedRecoveryIsUnchanged:
    def test_budgets_are_still_the_documented_ones(self):
        policy = RecoveryPolicy()
        assert policy.max_retries == 3
        assert policy.max_escalations == 2
        assert policy.max_rerun_verify == 1
        assert policy.allow_skip is False

    def test_a_retryable_failure_with_budget_still_retries(self):
        """The negative control for everything below.

        Without it, a change that made every failure abort would satisfy every
        exhaustion test while destroying recovery entirely.
        """
        assert RecoveryPolicy().decide(_observation()).action is RecoveryAction.RETRY_STEP

    def test_retries_then_escalations_then_abort(self):
        """Both budgets drain independently and the policy terminates.

        ``escalations_used`` is a separate counter from ``recovery_attempts``, so
        a caller that keeps retrying past its budget escalates without ever
        exhausting the escalation ceiling - the abort only arrives once *both*
        are spent, which is the point of having two ceilings.
        """
        policy = RecoveryPolicy(max_retries=3, max_escalations=2)

        # Retries drain first.
        assert [policy.decide(_observation(recovery_attempts=used)).action
                for used in range(3)] == [RecoveryAction.RETRY_STEP] * 3

        # Retry budget spent, escalation budget untouched: escalate.
        assert policy.decide(
            _observation(recovery_attempts=3)).action is RecoveryAction.ESCALATE_MODEL

        # Both budgets spent: abort, terminally.
        assert policy.decide(
            _observation(recovery_attempts=3,
                         escalations_used=2)).action is RecoveryAction.ABORT

    def test_escalation_alone_terminates(self):
        """A caller that never retries still cannot escalate forever."""
        policy = RecoveryPolicy(max_retries=3, max_escalations=2)
        assert policy.decide(_observation(
            recovery_attempts=3, escalations_used=1)).action is RecoveryAction.ESCALATE_MODEL
        assert policy.decide(_observation(
            recovery_attempts=3, escalations_used=2)).action is RecoveryAction.ABORT

    def test_there_is_still_no_unbounded_path(self):
        """Even a very high counter must terminate."""
        decision = RecoveryPolicy().decide(
            _observation(recovery_attempts=10_000, escalations_used=10_000))
        assert decision.action is RecoveryAction.ABORT

    def test_cancellation_still_dominates_exhaustion(self):
        """A cancelled failure is never retried, exhausted or not."""
        decision = RecoveryPolicy().decide(
            _observation(recovery_exhausted=True, cancelled=True))
        assert decision.action is RecoveryAction.NONE, (
            "cancellation lost its dominance over the exhaustion check")


# ══════════════════════════════════════════════════════ 2. exhaustion is terminal


class TestExhaustionDominatesEveryBudget:
    def test_an_exhausted_observation_aborts(self):
        decision = RecoveryPolicy().decide(_observation(recovery_exhausted=True))
        assert decision.action is RecoveryAction.ABORT
        assert "exhausted" in decision.reason.lower()

    def test_zero_counters_plus_the_flag_still_abort(self):
        """The point of the flag.

        Counters alone would read ``0/3`` - a full budget - and hand back a
        retry. Exhaustion has to survive being recorded independently of how much
        was counted.
        """
        decision = RecoveryPolicy().decide(
            _observation(recovery_attempts=0, escalations_used=0,
                         recovery_exhausted=True))
        assert decision.action is RecoveryAction.ABORT, (
            "an exhausted path with zeroed counters was given a fresh budget")

    def test_a_raised_budget_does_not_re_open_an_exhausted_path(self):
        """A counter-only design fails exactly here.

        Raising ``max_retries`` would make previously-spent counters look
        affordable again, so a raised budget would quietly re-open a closed
        recovery path. The durable flag must dominate.
        """
        generous = RecoveryPolicy(max_retries=1000, max_escalations=1000)
        decision = generous.decide(_observation(recovery_exhausted=True))
        assert decision.action is RecoveryAction.ABORT, (
            "raising the budgets re-opened a terminally exhausted recovery path")

    def test_the_default_flag_preserves_existing_behaviour(self):
        assert FailureObservation().recovery_exhausted is False
        assert RecoveryPolicy().decide(_observation()).action is RecoveryAction.RETRY_STEP

    def test_the_flag_survives_serialisation(self):
        payload = _observation(recovery_exhausted=True).to_dict()
        assert payload["recovery_exhausted"] is True


# ══════════════════════════════════════════════════════ 3. TaskManager state


class TestTaskManagerExhaustion:
    def test_marking_sets_the_flag_and_keeps_the_reason(self):
        tasks = TaskManager()
        tasks.mark_recovery_exhausted("retry budget spent (3/3)")
        assert tasks.recovery_exhausted is True
        assert "3/3" in tasks.recovery_termination

    def test_exhaustion_is_monotonic(self):
        """A later failure must not quietly re-open recovery."""
        tasks = TaskManager()
        tasks.mark_recovery_exhausted("first")
        tasks.mark_recovery_exhausted("second")
        assert tasks.recovery_exhausted is True

    def test_a_new_task_starts_unexhausted(self):
        tasks = TaskManager()
        tasks.mark_recovery_exhausted("spent")
        tasks.begin_task("new work")
        assert tasks.recovery_exhausted is False
        assert tasks.recovery_termination == ""

    def test_exhaustion_does_not_disturb_verification_state(self):
        """Recovery state and the ledger are separate authorities; marking one
        must not look like adjudicating the other."""
        tasks = TaskManager()
        before_state = tasks.verification
        tasks.mark_recovery_exhausted("spent")
        assert tasks.verification is before_state


# ══════════════════════════════════════════════════════ 4. durable projection


class TestExhaustionIsPersistedThroughTaskStore:
    def test_the_lifecycle_projection_carries_exhaustion(self, tmp_path):
        from core.agent import Agent

        tasks = TaskManager()
        tasks.mark_recovery_exhausted("retry budget spent (3/3)")
        tasks.recovery_attempts = 3

        agent = Agent.__new__(Agent)
        agent._tasks = tasks
        agent._escalations_used = 2

        lifecycle = agent._lifecycle_metadata()

        assert lifecycle["recovery_exhausted"] is True
        assert lifecycle["recovery_attempts"] == 3
        assert lifecycle["escalations_used"] == 2
        assert "3/3" in lifecycle["recovery_termination"]

    def test_exhaustion_survives_a_write_and_reload(self, tmp_path):
        store = TaskStore(tmp_path / "sessions")
        store.create(TaskRecord(
            task_id="t1", project_name="p", project_root="r", title="t",
            goal="g", created_at="now",
            metadata={"lifecycle": {
                "recovery_attempts": 3,
                "escalations_used": 2,
                "recovery_exhausted": True,
                "recovery_termination": "retry budget spent (3/3)",
            }}))

        reloaded = TaskStore(tmp_path / "sessions").get("t1")

        lifecycle = reloaded.metadata["lifecycle"]
        assert lifecycle["recovery_exhausted"] is True, (
            "exhaustion did not survive persistence")
        assert lifecycle["recovery_attempts"] == 3

    def test_no_second_persistence_system_was_created(self, tmp_path):
        """Recovery state rides the existing TaskRecord metadata projection."""
        from pathlib import Path

        store = TaskStore(tmp_path / "sessions")
        store.create(TaskRecord(
            task_id="t1", project_name="p", project_root="r", title="t",
            goal="g", created_at="now",
            metadata={"lifecycle": {"recovery_exhausted": True}}))
        assert (Path(tmp_path) / "sessions").is_dir()
        names = {p.name for p in (Path(tmp_path) / "sessions").iterdir()}
        assert not any("recovery" in name for name in names), (
            f"a parallel recovery store was created: {names}")


# ══════════════════════════════════════════════════════ 5. restart restoration


def _agent_with_restored_state(tmp_path, lifecycle: dict, task_id: str = "t1"):
    """An Agent that has just reconciled a restarted task store.

    An existing record with the same id is updated rather than re-created, which
    is what a real restart sees: the previous process wrote it, and ``create``
    refuses to overwrite it.
    """
    from core.agent import Agent

    store = TaskStore(tmp_path / "sessions")
    record = TaskRecord(
        task_id=task_id, project_name="p", project_root="r", title="t",
        goal="g", created_at="now", metadata={"lifecycle": lifecycle})
    try:
        store.create(record)
    except ValueError:
        store.update(task_id, metadata={"lifecycle": lifecycle})

    agent = Agent.__new__(Agent)
    agent._task_store = store
    agent._change_tracker = _NoTracker()
    agent._tasks = TaskManager()
    agent._tasks.bind_task("t1")
    agent._escalations_used = 0
    agent._exhausted_task_ids = {}
    agent._recovered_task_ids = []
    return agent


class _NoTracker:
    """Stands in for ChangeTracker; reconciliation only needs load_task."""

    sessions_dir = None

    def load_task(self, sessions_dir, task_id):
        return None


class TestRestartRestoresExhaustion:
    def test_exhaustion_is_restored_onto_the_live_task(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3,
            "escalations_used": 2,
            "recovery_exhausted": True,
            "recovery_termination": "retry budget spent (3/3)",
        })

        agent._restore_recovery_state(agent._task_store.get("t1"))

        assert agent._tasks.recovery_exhausted is True, (
            "an exhausted task came back from restart unexhausted")

    def test_spent_attempts_are_restored(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3, "escalations_used": 1,
            "recovery_exhausted": False,
        })

        agent._restore_recovery_state(agent._task_store.get("t1"))

        assert agent._tasks.recovery_attempts == 3

    def test_a_new_task_still_gets_a_full_budget(self, tmp_path):
        """Restoration must not make every task look spent."""
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 0, "recovery_exhausted": False,
        })

        agent._restore_recovery_state(agent._task_store.get("t1"))

        assert agent._tasks.recovery_attempts == 0
        assert agent._tasks.recovery_exhausted is False

    def test_restoration_never_lowers_a_higher_live_count(self, tmp_path):
        """This lifetime spent more than the record holds; restoring the smaller
        number upward would hand back budget that was already used."""
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 1, "recovery_exhausted": False,
        })
        agent._tasks.recovery_attempts = 7

        agent._restore_recovery_state(agent._task_store.get("t1"))

        assert agent._tasks.recovery_attempts == 7, (
            "restoration lowered the live attempt count and returned budget")

    def test_restoring_twice_does_not_double_count(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3, "recovery_exhausted": True,
        })
        record = agent._task_store.get("t1")

        agent._restore_recovery_state(record)
        agent._restore_recovery_state(record)

        assert agent._tasks.recovery_attempts == 3

    def test_a_corrupt_lifecycle_projection_does_not_crash(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {"recovery_attempts": 3})
        agent._task_store.get("t1").metadata["lifecycle"] = "not a dict"

        agent._restore_recovery_state(agent._task_store.get("t1"))

        assert agent._tasks.recovery_exhausted is False

    def test_a_record_with_no_lifecycle_is_tolerated(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {})
        agent._task_store.get("t1").metadata.pop("lifecycle")

        assert agent._restore_recovery_state(agent._task_store.get("t1")) is False


class TestARestartedExhaustedTaskIsNotRetried:
    def test_begining_the_task_applies_the_restored_exhaustion(self, tmp_path):
        """The end-to-end property.

        ``begin_task`` zeroes the counters. The restored state must be applied
        afterwards, or the restart silently hands back a full budget.
        """
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3,
            "recovery_exhausted": True,
            "recovery_termination": "retry budget spent (3/3)",
        })
        agent._restore_recovery_state(agent._task_store.get("t1"))

        agent._tasks.begin_task("resume the same task", task_id="t1")
        agent._apply_restored_recovery_state()

        assert agent._tasks.recovery_exhausted is True, (
            "begin_task reset the restored exhaustion; a restart hands the task "
            "a fresh retry budget")
        assert agent._tasks.recovery_attempts == 3

    def test_the_restored_task_would_not_retry(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3, "recovery_exhausted": True,
        })
        agent._restore_recovery_state(agent._task_store.get("t1"))
        agent._tasks.begin_task("resume", task_id="t1")
        agent._apply_restored_recovery_state()

        decision = RecoveryPolicy().decide(_observation(
            recovery_attempts=agent._tasks.recovery_attempts,
            recovery_exhausted=agent._tasks.recovery_exhausted))

        assert decision.action is RecoveryAction.ABORT, (
            "a task exhausted before the restart would be retried after it")

    def test_no_duplicate_recovery_attempt_after_restart(self, tmp_path):
        """The retry budget must not grow across restarts.

        Each loop is a genuine restart: the durable record is written back by the
        exhausted process, then read by a fresh one. The naive failure mode is a
        counter that keeps growing because each new process contributes its own
        view, or a budget that reappears because the state was consumed only
        within one lifetime.
        """
        for _ in range(3):
            # A restart: fresh Agent, fresh read of the durable record.
            agent = _agent_with_restored_state(tmp_path, {
                "recovery_attempts": 3,
                "recovery_exhausted": True,
                "recovery_termination": "retry budget spent (3/3)",
            })
            agent._restore_recovery_state(agent._task_store.get("t1"))

            agent._tasks.begin_task("resume the same task", task_id="t1")
            agent._apply_restored_recovery_state()

            assert agent._tasks.recovery_attempts == 3, (
                "the recovery budget grew across a restart")
            assert agent._tasks.recovery_exhausted is True
            assert RecoveryPolicy().decide(_observation(
                recovery_attempts=agent._tasks.recovery_attempts,
                recovery_exhausted=agent._tasks.recovery_exhausted,
            )).action is RecoveryAction.ABORT, (
                "a restarted exhausted task would be retried again")

    def test_repeated_restarts_do_not_accumulate_attempts(self, tmp_path):
        """The stored count is authoritative, not a running total."""
        counts = []
        for _ in range(3):
            agent = _agent_with_restored_state(tmp_path, {
                "recovery_attempts": 3, "recovery_exhausted": True})
            agent._restore_recovery_state(agent._task_store.get("t1"))
            agent._tasks.begin_task("resume", task_id="t1")
            agent._apply_restored_recovery_state()
            counts.append(agent._tasks.recovery_attempts)
        assert counts == [3, 3, 3], f"the budget drifted across restarts: {counts}"

    def test_the_restored_state_is_consumed_once(self, tmp_path):
        """A different task that later reuses the id must not inherit it, and the
        entry must not linger."""
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3, "recovery_exhausted": True,
        })
        agent._restore_recovery_state(agent._task_store.get("t1"))

        agent._tasks.begin_task("t1", task_id="t1")
        agent._apply_restored_recovery_state()
        assert "t1" not in agent._exhausted_task_ids

        agent._tasks.begin_task("unrelated", task_id="t2")
        agent._apply_restored_recovery_state()
        assert agent._tasks.recovery_exhausted is False, (
            "restored exhaustion leaked onto an unrelated task")

    def test_an_unrelated_task_keeps_its_full_budget(self, tmp_path):
        agent = _agent_with_restored_state(tmp_path, {
            "recovery_attempts": 3, "recovery_exhausted": True,
        })
        agent._restore_recovery_state(agent._task_store.get("t1"))

        agent._tasks.begin_task("brand new work", task_id="t-new")
        agent._apply_restored_recovery_state()

        assert agent._tasks.recovery_attempts == 0
        assert agent._tasks.recovery_exhausted is False


# ══════════════════════════════════════════════════════ 6. agent abort marks it


class TestAnAgentAbortIsPersisted:
    @pytest.fixture
    def agent(self, tmp_path):
        from core.agent import Agent

        instance = Agent.__new__(Agent)
        instance._tasks = TaskManager()
        instance._escalations_used = 0
        instance._router_verify_ok = True
        instance._task_store = None          # no store: metadata sync is a no-op
        instance._change_tracker = None
        instance._exhausted_task_ids = {}
        instance._recovered_task_ids = []
        instance._emit_lifecycle = lambda *a, **k: None
        return instance

    def test_an_abort_marks_the_task_exhausted(self, agent):
        from core.agent import Agent

        agent._recovery_policy = lambda: RecoveryPolicy(
            max_retries=0, max_escalations=0)
        agent._failure_observation = lambda *a, **k: _observation()

        Agent._apply_recovery_policy(agent, "some.tool", _Outcome(), StepStatus.FAILED)

        assert agent._tasks.recovery_exhausted is True, (
            "an ABORT did not mark recovery terminally spent")

    def test_a_retry_does_not_mark_exhaustion(self, agent):
        """The negative control: exhaustion means spent, not merely failed."""
        from core.agent import Agent

        agent._recovery_policy = lambda: RecoveryPolicy()
        agent._failure_observation = lambda *a, **k: _observation()

        Agent._apply_recovery_policy(agent, "some.tool", _Outcome(), StepStatus.FAILED)

        assert agent._tasks.recovery_exhausted is False
        assert agent._router_verify_ok is True


class _Outcome:
    status = "error"
    reason = "it failed"
    error = "boom"
    cancelled = False