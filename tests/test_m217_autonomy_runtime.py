"""M2.17 - the production runtime: schedule, submit, observe, project, cancel.

The goal layer shipped correct and unreachable. Nothing outside the tests imported
``core.goals`` or ``core.autonomy``, so a durable goal could be created and scheduled
by hand but never by the running application. ``AutonomyRuntime`` is the piece that
made it operational, and these tests are mostly about the ways that wiring could have
gone wrong:

* it could have become a second execution path instead of an observer;
* it could have completed a goal because a task finished, rather than because
  evidence accumulated;
* cancelling the goal could have left the live task running;
* the scheduler thread could have outlived ``stop()``.

Everything here is deterministic and headless. The fake Agent implements only the
two members the runtime is allowed to touch - ``respond`` and the cancellation entry
point - which is itself the point: if the runtime needed more than that, it would be
reaching around the ordinary request lifecycle.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.autonomy import AutonomyRuntime, GoalOutcome, SchedulerLimits
from core.goals import GoalState, GoalStore, create_goal


# â”€â”€ doubles â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class FakeTaskStore:
    """The read surface the runtime uses: ``get(task_id)``."""

    def __init__(self) -> None:
        self.records: dict[str, Any] = {}
        self.reads = 0

    def get(self, task_id: str) -> Any:
        self.reads += 1
        return self.records.get(str(task_id))

    def put(self, task_id: str, *, status: str, verification: str) -> Any:
        record = SimpleNamespace(
            task_id=task_id,
            status=status,
            metadata={"lifecycle": {"state": status, "verification": verification}},
        )
        self.records[task_id] = record
        return record


class FakeContext:
    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id


class FakeAgent:
    """Stands in for ``core.agent.Agent`` at exactly two entry points."""

    def __init__(self, *, status: str = "completed",
                 verification: str = "passed",
                 raises: Exception | None = None,
                 produces_task: bool = True) -> None:
        self._task_store = FakeTaskStore()
        self.request_context = FakeContext("")
        self.calls: list[str] = []
        self.cancels = 0
        self._status = status
        self._verification = verification
        self._raises = raises
        self._produces_task = produces_task
        self._counter = 0

    def respond(self, text: str) -> str:
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        if not self._produces_task:
            return "I did not need a task for that."
        self._counter += 1
        task_id = f"t{self._counter}"
        self.request_context = FakeContext(task_id)
        self._task_store.put(task_id, status=self._status,
                             verification=self._verification)
        return "ok"

    def cancel_active(self) -> None:
        self.cancels += 1


def _runtime(tmp_path: Path, agent: FakeAgent, **kwargs: Any) -> AutonomyRuntime:
    return AutonomyRuntime(agent, GoalStore(tmp_path),
                           task_store=agent._task_store, **kwargs)


def _due_goal(store: GoalStore, *, interval: int = 60, evidence: int = 1):
    goal = create_goal("create the project", interval_seconds=interval,
                       next_run_at="20200101-000000", required_evidence=evidence)
    goal.objective = "create the project"
    store.create(goal)
    return goal


# â”€â”€ submit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSubmitUsesTheOrdinaryRequestPath:
    def test_submit_calls_respond_with_the_goal_text(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        task_ref = runtime.submit_goal(goal)
        outcome = runtime.outcome(goal.goal_id)

        assert len(agent.calls) == 1
        assert "create the project" in agent.calls[0]
        assert task_ref == "t1"
        assert outcome.verdict == "verified"

    def test_next_action_is_appended_because_it_is_the_authors_hint(
            self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)
        goal.next_action = "run the tests afterwards"
        runtime.goals.save(goal)

        runtime.submit_goal(goal)

        assert "run the tests afterwards" in agent.calls[0]

    def test_runtime_executes_nothing_itself(self, tmp_path: Path) -> None:
        """The Agent's request is the whole execution path; the runtime observes."""
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.submit_goal(goal)

        # Exactly one Agent call, and no tool dispatch surface of its own.
        assert len(agent.calls) == 1
        source = Path(__file__).resolve().parents[1] / "core" / "autonomy.py"
        text = source.read_text(encoding="utf-8")
        for forbidden in ("core.executor", "core.windows", "tools.registry",
                          "ctypes", "SendInput"):
            assert forbidden not in text

    def test_no_task_produced_is_reported_not_guessed(self, tmp_path: Path) -> None:
        agent = FakeAgent(produces_task=False)
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.submit_goal(goal)
        outcome = runtime.outcome(goal.goal_id)

        assert outcome.verdict == "no_task"
        assert runtime.goals.get(goal.goal_id).verified_task_ids == []

    def test_agent_failure_fails_the_goal_and_does_not_raise(
            self, tmp_path: Path) -> None:
        agent = FakeAgent(raises=RuntimeError("provider down"))
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.submit_goal(goal)
        outcome = runtime.outcome(goal.goal_id)

        assert outcome.verdict == "error"
        assert runtime.goals.get(goal.goal_id).state is GoalState.FAILED


# â”€â”€ evidence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestEvidenceComesFromTheTaskVerdict:
    def test_verified_task_completes_a_single_evidence_goal(
            self, tmp_path: Path) -> None:
        agent = FakeAgent(status="completed", verification="passed")
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.submit_goal(goal)
        outcome = runtime.outcome(goal.goal_id)
        stored = runtime.goals.get(goal.goal_id)

        assert outcome.evidence_recorded is True
        assert outcome.goal_completed is True
        assert stored.state is GoalState.COMPLETED
        assert stored.verified_task_ids == ["t1"]

    def test_unverified_task_records_nothing(self, tmp_path: Path) -> None:
        """A finished-but-unverified task is not evidence. This is the Â§35 rule."""
        agent = FakeAgent(status="completed", verification="unverified")
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.submit_goal(goal)
        outcome = runtime.outcome(goal.goal_id)
        stored = runtime.goals.get(goal.goal_id)

        assert outcome.verdict == "unverified"
        assert outcome.evidence_recorded is False
        assert outcome.goal_completed is False
        assert stored.state is GoalState.ACTIVE
        assert stored.verified_task_ids == []

    def test_failed_verification_is_not_evidence(self, tmp_path: Path) -> None:
        agent = FakeAgent(status="completed", verification="failed")
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.submit_goal(goal)
        outcome = runtime.outcome(goal.goal_id)

        assert outcome.verdict == "unverified"
        assert runtime.goals.get(goal.goal_id).state is GoalState.ACTIVE

    def test_multi_evidence_goal_needs_every_task(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals, evidence=3)

        for _ in range(2):
            runtime.submit_goal(goal)
            goal = runtime.goals.get(goal.goal_id)
        assert goal.state is GoalState.ACTIVE
        assert len(goal.verified_task_ids) == 2

        runtime.submit_goal(goal)
        assert runtime.goals.get(goal.goal_id).state is GoalState.COMPLETED

    def test_missing_task_record_degrades_to_unknown(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        outcome = runtime.observe(goal, task_id="never-existed")

        assert outcome.verdict == "unknown"
        assert runtime.goals.get(goal.goal_id).verified_task_ids == []

    def test_in_flight_task_is_not_evidence(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)
        agent._task_store.put("t9", status="active", verification="not_run")

        outcome = runtime.observe(goal, task_id="t9")

        assert outcome.verdict == "in_flight"
        assert runtime.goals.get(goal.goal_id).verified_task_ids == []

    def test_corrupt_verification_value_degrades_rather_than_raises(
            self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)
        agent._task_store.records["t1"] = SimpleNamespace(
            task_id="t1", status="completed",
            metadata={"lifecycle": {"state": "completed", "verification": "banana"}})

        outcome = runtime.observe(goal, task_id="t1")

        assert outcome.verdict == "unknown"
        assert runtime.goals.get(goal.goal_id).state is GoalState.ACTIVE

    def test_outcome_is_idempotent_for_the_same_task(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals, evidence=2)

        runtime.submit_goal(goal)
        goal = runtime.goals.get(goal.goal_id)
        runtime.observe(goal, task_id="t1")

        assert runtime.goals.get(goal.goal_id).verified_task_ids == ["t1"]


# â”€â”€ cancellation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestCancellationConvergesBothHalves:
    def test_cancel_stops_the_live_task_as_well_as_the_goal(
            self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals, evidence=2)
        runtime.submit_goal(goal)

        outcome = runtime.cancel(goal.goal_id)

        assert agent.cancels == 1
        assert outcome.verdict == "cancelled"
        assert runtime.goals.get(goal.goal_id).state is GoalState.CANCELLED

    def test_cancelled_goal_survives_a_restart(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)
        runtime.cancel(goal.goal_id)

        # A second runtime over the same directory is a restart, in miniature.
        reborn = _runtime(tmp_path, FakeAgent())
        report = reborn.tick()

        assert reborn.goals.get(goal.goal_id).state is GoalState.CANCELLED
        assert report.started == []
        assert report.skipped.get(goal.goal_id) == "cancelled"

    def test_cancel_of_an_unknown_goal_reports_instead_of_raising(
            self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent())
        outcome = runtime.cancel("nope")
        assert "no such goal" in outcome.reason

    def test_cancel_with_no_task_still_cancels_the_goal(
            self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)

        runtime.cancel(goal.goal_id)

        assert agent.cancels == 0
        assert runtime.goals.get(goal.goal_id).state is GoalState.CANCELLED


# â”€â”€ scheduler lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSchedulerLifecycle:
    def test_tick_starts_a_due_goal_through_the_agent(
            self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        _due_goal(runtime.goals)

        report = runtime.tick()

        assert len(report.started) == 1
        assert len(agent.calls) == 1

    def test_start_and_stop_join_the_thread(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent(), poll_seconds=1.0)
        try:
            assert runtime.start() is True
            assert runtime.running is True
            assert runtime.start() is False  # idempotent
        finally:
            stopped = runtime.stop(timeout=5.0)
        assert stopped is True
        assert runtime.running is False

    def test_thread_is_named_and_is_a_daemon(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent(), poll_seconds=1.0)
        runtime.start()
        try:
            found = [t for t in threading.enumerate()
                     if t.name == AutonomyRuntime.THREAD_NAME]
            assert len(found) == 1
            assert found[0].daemon is True
        finally:
            runtime.stop(timeout=5.0)

    def test_stop_without_start_is_a_no_op(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent())
        assert runtime.stop() is True

    def test_the_scheduler_stops_after_runtime_stop(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent(), poll_seconds=1.0)
        runtime.start()
        runtime.stop(timeout=5.0)
        assert runtime.scheduler.stopped is True

    def test_concurrency_ceiling_is_not_decorative(self, tmp_path: Path) -> None:
        """Several goals may start in one tick, but never more than one at a time.

        The ceiling bounds work *simultaneously in flight*, not the number started.
        Submitting asynchronously would make ``max_concurrent_autonomous``
        decorative, so this records what the Agent saw from inside its own call.
        """
        agent = FakeAgent()
        original_respond = agent.respond
        in_flight = 0
        peak = 0

        def counting_respond(text: str) -> str:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                return original_respond(text)
            finally:
                in_flight -= 1

        agent.respond = counting_respond  # type: ignore[method-assign]
        runtime = _runtime(tmp_path, agent, limits=SchedulerLimits(
            max_active_goals=5, max_autonomous_tasks=5,
            max_concurrent_autonomous=1, min_interval_seconds=60))
        for _ in range(3):
            _due_goal(runtime.goals)

        report = runtime.tick()

        assert len(report.started) == 3
        assert peak == 1


# â”€â”€ presentation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestSnapshotIsBoundedAndRedacted:
    def test_snapshot_is_bounded_and_excludes_terminal_goals(
            self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent())
        for index in range(6):
            _due_goal(runtime.goals, interval=60)
            goal = runtime.goals.list_all()[-1]
            goal.objective = f"goal number {index} with token=sk-abcdef1234567890"
            if index == 0:
                goal.request_cancel()
            runtime.goals.save(goal)

        rows = runtime.snapshot(limit=3)

        assert len(rows) == 3
        assert all(r["state"] != "cancelled" for r in rows)
        assert "sk-abcdef1234567890" not in str(rows)

    def test_snapshot_carries_no_authority(self, tmp_path: Path) -> None:
        """A projection is data. Nothing in it can schedule, complete or cancel."""
        runtime = _runtime(tmp_path, FakeAgent())
        _due_goal(runtime.goals)
        row = runtime.snapshot(limit=1)[0]
        for forbidden in ("create", "save", "cancel", "request_cancel",
                          "mark_completed", "transition", "submit_goal", "tick"):
            assert forbidden not in row

    def test_summary_line_is_bounded(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent())
        goal = _due_goal(runtime.goals)
        goal.objective = "x" * 5000
        runtime.goals.save(goal)
        assert len(runtime.summary_line()) <= 300

    def test_snapshot_survives_a_broken_store(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent())

        class Broken:
            def active(self):
                raise OSError("disk gone")

        runtime._goals = Broken()
        assert runtime.snapshot() == []


# â”€â”€ reconciliation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class TestRuntimeReconciliation:
    def test_reconcile_uses_the_agents_task_store(self, tmp_path: Path) -> None:
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)
        agent._task_store.put("t1", status="completed", verification="passed")
        goal.attach_task("t1")
        runtime.goals.save(goal)

        report = runtime.reconcile()

        assert report.changed >= 1
        # A completed task is not a completed goal.
        assert runtime.goals.get(goal.goal_id).state is GoalState.ACTIVE
        assert runtime.goals.get(goal.goal_id).verified_task_ids == []

    def test_reconcile_is_deterministic_and_stops_changing_state(
            self, tmp_path: Path) -> None:
        """Two properties, not one: same report twice, and no change the second time."""
        runtime = _runtime(tmp_path, FakeAgent())
        _due_goal(runtime.goals)

        first = runtime.reconcile()
        second = runtime.reconcile()

        decisions = ("resumed", "paused", "failed", "cancelled",
                     "blocked_on_permission", "exhausted")
        first_data, second_data = first.to_dict(), second.to_dict()
        assert {k: first_data[k] for k in decisions} == {
            k: second_data[k] for k in decisions}
        # The decision is reported again - the goal is still live - but the second
        # pass moved nothing, so ``changed`` is honest about that.
        assert first.changed == 1
        assert second.changed == 0


# â”€â”€ outcome type â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_goal_outcome_is_serialisable() -> None:
    outcome = GoalOutcome(goal_id="g", task_id="t", verdict="verified")
    data = outcome.to_dict()
    assert data["goal_id"] == "g"
    assert data["goal_completed"] is False




class TestOneGateNotTwo:
    """"Resource limits via the existing gate, no second system."

    An earlier scheduler kept its own ``_in_flight`` counter, which meant two
    concurrency limiters sat in front of one Agent. These tests pin the single-gate
    arrangement so it cannot quietly grow back into two.
    """

    def test_scheduler_has_no_private_in_flight_counter(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "core" / "autonomy.py")
        text = source.read_text(encoding="utf-8")
        assert "self._in_flight" not in text, (
            "the scheduler keeps a second concurrency counter; it must read "
            "BoundedRequestGate instead")

    def test_admission_is_read_from_the_agents_own_gate(self, tmp_path: Path) -> None:
        class Gate:
            def __init__(self) -> None:
                self.in_flight = 0

        agent = FakeAgent()
        agent.request_gate = Gate()
        runtime = _runtime(tmp_path, agent)
        assert runtime.scheduler.in_flight == 0
        agent.request_gate.in_flight = 1
        assert runtime.scheduler.in_flight == 1
        agent.request_gate.in_flight = 4
        assert runtime.scheduler.in_flight == 4

    def test_an_agent_without_a_gate_degrades_to_zero(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, FakeAgent())
        assert runtime.scheduler.in_flight == 0

    def test_a_broken_gate_probe_does_not_stop_a_tick(self, tmp_path: Path) -> None:
        class Hostile:
            @property
            def in_flight(self):
                raise RuntimeError("no")

        agent = FakeAgent()
        agent.request_gate = Hostile()
        runtime = _runtime(tmp_path, agent)
        goal = _due_goal(runtime.goals)
        report = runtime.tick()
        assert report.started == [goal.goal_id]

    def test_the_ceiling_is_a_policy_limit_not_a_lock(self, tmp_path: Path) -> None:
        """At a ceiling of one, a busy Agent declines rather than queueing."""
        agent = FakeAgent()
        runtime = _runtime(tmp_path, agent)
        _due_goal(runtime.goals)
        agent.request_gate = type("G", (), {"in_flight": 1})()
        runtime.scheduler._limits = SchedulerLimits(
            max_active_goals=3, max_autonomous_tasks=8,
            max_concurrent_autonomous=1, min_interval_seconds=60)
        report = runtime.tick()
        assert report.started == []
        assert len(agent.calls) == 0

