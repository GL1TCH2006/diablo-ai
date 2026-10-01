"""M2.14 Phase 10A - the terminal verdict invariant.

Phase 10A found a contradiction inside a single test file. Two live tests
asserted opposite things about a task that ends ``COMPLETED`` without a passing
verdict:

* ``test_recovery_p10_real_model.py::...unverified_task_is_labelled_unverified``
  treated ``COMPLETED`` + non-PASSED as **permitted and honest**, and
* ``test_recovery_p10_real_model.py::...unrepairable_project_stays_bounded``
  required verification to be ``PASSED`` whenever the task was ``COMPLETED``.

Both could not be authoritative. The second was passing only because its
``if state == "completed"`` guard never fired for the model class it was
written against, so it was not validating anything at all.

This module is the resolution. It pins the invariant that replaces both
assertions, at the level where it is decided:

    ``TaskState`` answers *did the request terminate, and how?*
    ``VerificationState`` answers *was the result independently checked?*

A task may finish without any verifier having existed. ``COMPLETED`` asserts
the first fact and never the second. What is forbidden is the **opposite**
disagreement: a known-failed verdict reported as a finished request, or a
``PASSED`` surviving a later executed failure.

Every test here executes the branch it asserts. There are no guards whose
absence would make an assertion unreachable, which is the specific defect that
made the contradiction survive a full session unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai.provider import AIProvider, AIResponse, ToolCall
from config.permissions import PermissionManager
from core.agent import Agent
from core.context import ProjectContext
from core.events import EventBus
from core.recovery import (
    FailureCategory,
    FailureObservation,
    RecoveryAction,
    RecoveryPolicy,
)
from core.task_manager import (
    COMPLETED_ADMISSIBLE_VERIFICATIONS,
    FAILED_ADMISSIBLE_VERIFICATIONS,
    TASK_TERMINAL_STATES,
    StepStatus,
    TaskManager,
    TaskState,
    TerminalVerdict,
    terminal_invariant_violation,
    terminal_verdict,
)
from core.task_store import TaskRecord, TaskStore
from core.verification import VerificationState
from core.verification_ledger import (
    EvidenceTrust,
    VerificationMethod,
    VerificationSubject,
)
from memory.preferences import PreferencesStore
from tools import build_registry

OBSERVED_PASS = dict(subject_kind=VerificationSubject.TASK,
                     method_kind=VerificationMethod.PROJECT_TESTS,
                     trust=EvidenceTrust.OBSERVED,
                     evidence={"tests_passed": "3", "exit_code": 0})


# ══════════════════════════════════════════ the invariant, as a table


ALL_STATES = [
    TaskState.CREATED, TaskState.PLANNING, TaskState.AWAITING_PERMISSION,
    TaskState.EXECUTING, TaskState.VERIFYING, TaskState.RECOVERING,
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
]
ALL_VERIFICATIONS = list(VerificationState)


class TestTheAdmissiblePairs:
    """The whole contract, as an exhaustive table. No sampling."""

    def test_completed_accepts_every_non_failed_verdict(self):
        for verdict in ALL_VERIFICATIONS:
            if verdict is VerificationState.FAILED:
                continue
            assert terminal_invariant_violation(TaskState.COMPLETED, verdict) == "", (
                f"COMPLETED + {verdict.value} must be admissible")

    def test_completed_rejects_failed_because_that_reports_failure_as_completion(self):
        violation = terminal_invariant_violation(
            TaskState.COMPLETED, VerificationState.FAILED)
        assert violation, (
            "COMPLETED + FAILED is the false-pass this invariant exists to "
            "prevent and must be reported as a violation")

    def test_failed_rejects_passed_because_a_pass_cannot_survive_a_failure(self):
        violation = terminal_invariant_violation(
            TaskState.FAILED, VerificationState.PASSED)
        assert violation, (
            "FAILED + PASSED is a false pass and must be reported as a violation")

    def test_failed_accepts_every_non_passed_verdict(self):
        for verdict in ALL_VERIFICATIONS:
            if verdict is VerificationState.PASSED:
                continue
            assert terminal_invariant_violation(TaskState.FAILED, verdict) == ""

    def test_cancelled_dominates_every_verdict(self):
        """Cancellation is terminal and its verdict is never load-bearing."""
        for verdict in ALL_VERIFICATIONS:
            assert terminal_invariant_violation(TaskState.CANCELLED, verdict) == ""
            assert terminal_verdict(TaskState.CANCELLED, verdict) is TerminalVerdict.CANCELLED

    def test_non_terminal_states_are_never_checked(self):
        """An in-flight task has no terminal verdict to be inconsistent with."""
        for state in ALL_STATES:
            if state in TASK_TERMINAL_STATES:
                continue
            for verdict in ALL_VERIFICATIONS:
                assert terminal_invariant_violation(state, verdict) == ""
                assert terminal_verdict(state, verdict) is TerminalVerdict.IN_FLIGHT

    def test_the_admissible_sets_are_exactly_the_permitted_half(self):
        """The frozensets and the predicate must not drift apart."""
        for verdict in ALL_VERIFICATIONS:
            expected = (verdict in COMPLETED_ADMISSIBLE_VERIFICATIONS
                        and terminal_invariant_violation(TaskState.COMPLETED, verdict) == "")
            assert (verdict in COMPLETED_ADMISSIBLE_VERIFICATIONS) is expected
            expected = (verdict in FAILED_ADMISSIBLE_VERIFICATIONS
                        and terminal_invariant_violation(TaskState.FAILED, verdict) == "")
            assert (verdict in FAILED_ADMISSIBLE_VERIFICATIONS) is expected

    def test_a_raw_string_pair_is_classified_identically(self):
        """A reloaded record arrives as strings, not enums."""
        assert terminal_verdict("completed", "unverified") is TerminalVerdict.UNVERIFIED
        assert terminal_invariant_violation("completed", "failed")
        assert terminal_invariant_violation("nonsense", "failed"), (
            "an unreadable pair must be reported, not silently accepted")


class TestTheTriStateSuccessAnswer:
    """``None`` means 'no verdict'. It is not ``False`` and not ``True``."""

    def test_verified_success_requires_passed(self):
        assert terminal_verdict(TaskState.COMPLETED,
                                VerificationState.PASSED) is TerminalVerdict.VERIFIED
        assert terminal_verdict(TaskState.COMPLETED,
                                VerificationState.PASSED).succeeded is True

    def test_completion_without_a_verdict_is_none_not_false(self):
        """The exact case Phase 10A exists for."""
        for verdict in (VerificationState.UNVERIFIED,
                        VerificationState.NOT_REQUIRED,
                        VerificationState.NOT_RUN):
            outcome = terminal_verdict(TaskState.COMPLETED, verdict)
            assert outcome is TerminalVerdict.UNVERIFIED, verdict
            assert outcome.succeeded is None, (
                f"{verdict.value} must read as 'unknown', never as False")

    def test_failure_is_false_in_both_directions(self):
        assert terminal_verdict(TaskState.FAILED,
                                VerificationState.UNVERIFIED).succeeded is False
        assert terminal_verdict(TaskState.FAILED,
                                VerificationState.FAILED).succeeded is False

    def test_cancellation_is_never_reported_as_success_or_failure(self):
        assert terminal_verdict(TaskState.CANCELLED,
                                VerificationState.UNVERIFIED).succeeded is None

    def test_task_manager_projects_the_same_answer(self):
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        for state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            for verdict in ALL_VERIFICATIONS:
                tasks.state = state
                tasks.verification = verdict
                assert tasks.task_succeeded() is terminal_verdict(state, verdict).succeeded
                assert tasks.terminal_verdict() is terminal_verdict(state, verdict)


# ══════════════════════════════════════════ enforcement, not declaration


class TestTheStateMachineRefusesTheForbiddenPair:
    def test_transition_into_completed_with_a_failed_verdict_is_refused(self):
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        tasks.transition(TaskState.VERIFYING)
        tasks.set_verification(VerificationState.FAILED, "the suite failed")
        with pytest.raises(ValueError, match="terminal verdict invariant"):
            tasks.transition(TaskState.COMPLETED)
        assert tasks.state is TaskState.VERIFYING, (
            "the refused transition must not have half-applied")

    def test_transition_into_completed_with_a_passing_verdict_is_allowed(self):
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        tasks.transition(TaskState.VERIFYING)
        tasks.set_verification(VerificationState.PASSED, "observed", {"exit_code": 0})
        tasks.transition(TaskState.COMPLETED)
        assert tasks.terminal_verdict() is TerminalVerdict.VERIFIED

    def test_a_passed_verdict_cannot_survive_a_later_failure(self):
        """The invalidation guard, pinned on both halves."""
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        tasks.transition(TaskState.VERIFYING)
        tasks.set_verification(VerificationState.PASSED, "observed", {"exit_code": 0})
        tasks.transition(TaskState.COMPLETED)
        tasks.invalidate_verification("a later step failed")
        assert tasks.verification is VerificationState.UNVERIFIED
        assert tasks.terminal_verdict() is TerminalVerdict.UNVERIFIED
        assert tasks.verification_history[-1]["state"] == "passed", (
            "the displaced PASSED must stay auditable")

    def test_a_failed_task_cannot_be_handed_a_passing_verdict(self):
        """``FAILED + PASSED`` is forbidden, so it is reported, not accepted."""
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        tasks.transition(TaskState.FAILED)
        tasks.set_verification(VerificationState.PASSED, "observed", {"exit_code": 0})
        assert tasks.terminal_violation(), (
            "a passing verdict on a failed task is a false pass and must show")
        with pytest.raises(ValueError, match="never survive an execution failure"):
            tasks.assert_terminal_invariant()

    def test_synchronize_verification_closes_a_terminal_not_run(self):
        """NOT_RUN is admissible but transient; settling it is explicit."""
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        tasks.transition(TaskState.VERIFYING)
        tasks.transition(TaskState.COMPLETED)
        assert tasks.verification is VerificationState.NOT_RUN
        assert tasks.synchronize_verification() is VerificationState.NOT_REQUIRED
        assert tasks.terminal_violation() == ""

    def test_synchronize_verification_reports_attempted_work_as_unverified(self):
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        step = tasks.add_step("did work")
        tasks.set_status(step, StepStatus.DONE, "ok")
        tasks.transition(TaskState.VERIFYING)
        tasks.transition(TaskState.COMPLETED)
        assert tasks.synchronize_verification() is VerificationState.UNVERIFIED

    def test_synchronize_verification_is_idempotent(self):
        tasks = TaskManager()
        tasks.begin_task("t", task_id="t1")
        tasks.transition(TaskState.EXECUTING)
        tasks.transition(TaskState.VERIFYING)
        tasks.transition(TaskState.COMPLETED)
        tasks.synchronize_verification()
        first = tasks.verification
        assert tasks.synchronize_verification() is first
        assert tasks.verification is first


# ══════════════════════════════════════════ end to end, through the Agent


BROKEN_MODULE = 'def add(a, b):\n    return a - b\n'
FIXED_MODULE = 'def add(a, b):\n    return a + b\n'
TEST_FILE = ("from src.calc import add\n\n\n"
             "def test_add_positive():\n    assert add(2, 3) == 5\n")


class ScriptedProvider(AIProvider):
    """Stands in for the model only, so the branch under test is reached."""

    name = "scripted"
    model_label = "scripted/local"
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.turns = 0

    def is_available(self):
        return True

    def list_models(self):
        return []

    def chat(self, messages, tools=None, model=None):
        self.turns += 1
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return AIResponse(text="done", tool_calls=[])


def _response(*calls, text=""):
    return AIResponse(text=text, tool_calls=list(calls))


def _call(name, **args):
    return ToolCall(name=name, arguments=args)


def _write_project(root: Path) -> Path:
    project = root / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "calc.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_calc.py").write_text(TEST_FILE, encoding="utf-8")
    return project


def _agent(settings, project: Path, responses):
    prefs = PreferencesStore(settings.memory_dir)
    provider = ScriptedProvider(responses)
    agent = Agent(
        provider=provider,
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, prefs),
        events=EventBus(),
        confirmer=lambda tool, description: "allow",
        project=ProjectContext(name=project.name, path=str(project)),
        memory_preferences=prefs,
        model_name="",
        model_catalog=None,
    )
    return agent, provider


def _suite_passes(project: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(project)],
        capture_output=True, text=True, timeout=300, cwd=str(project))
    return result.returncode == 0


class TestTheAgentEndsOnAdmissiblePairs:
    def test_execution_success_with_verification_passed(
            self, tmp_settings, tmp_path: Path):
        """Scenario 1: the whole conjunction holds."""
        project = _write_project(tmp_path)
        assert not _suite_passes(project), "the fixture must start broken"
        agent, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
            _response(_call("filesystem.write_file",
                            path=str(project / "src" / "calc.py"),
                            content=FIXED_MODULE)),
            _response(_call("project.verify", path=str(project))),
            _response(text="repaired and verified"),
        ])
        agent.respond("repair and verify the project")

        tasks = agent._tasks
        assert tasks.verification is VerificationState.PASSED, (
            f"a repaired, passing project was not verified: {tasks.verification}")
        assert tasks.state is TaskState.COMPLETED
        assert tasks.terminal_verdict() is TerminalVerdict.VERIFIED
        assert tasks.task_succeeded() is True
        assert tasks.terminal_violation() == ""
        entry = tasks.ledger.latest
        assert entry is not None and entry.state is VerificationState.PASSED
        assert entry.trust is EvidenceTrust.OBSERVED
        assert entry.evidence, "PASSED with no evidence"

    def test_execution_success_with_verification_failed(
            self, tmp_settings, tmp_path: Path):
        """Scenario 2: the work ran and the suite still fails. Not a pass."""
        project = _write_project(tmp_path)
        (project / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\n\n"
            "def test_add_positive():\n    assert add(2, 3) == 5\n\n\n"
            "def test_impossible():\n    assert add(1, 1) == 99\n",
            encoding="utf-8")
        agent, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))) for _ in range(6)
        ])
        agent.respond("repair and verify the project")

        tasks = agent._tasks
        assert tasks.verification is VerificationState.FAILED, (
            f"an impossible suite was not adjudicated FAILED: {tasks.verification}")
        assert tasks.terminal_verdict() is TerminalVerdict.FAILED
        assert tasks.task_succeeded() is False
        assert tasks.terminal_violation() == ""
        for entry in tasks.ledger.entries:
            if entry.state is VerificationState.PASSED:
                raise AssertionError(f"an impossible suite produced PASSED: {entry.evidence}")

    def test_execution_success_with_verification_unavailable(
            self, tmp_settings, tmp_path: Path):
        """Scenario 3: work ran, no independent verifier existed."""
        project = _write_project(tmp_path)
        agent, _ = _agent(tmp_settings, project, [
            _response(_call("filesystem.write_file",
                            path=str(project / "README.md"),
                            content="# hello\n")),
            _response(text="wrote a file"),
        ])
        agent.respond("add a readme to the project")

        tasks = agent._tasks
        assert tasks.state is TaskState.COMPLETED, (
            f"a plain completed turn ended {tasks.state.value}")
        assert tasks.verification is VerificationState.UNVERIFIED, (
            f"expected UNVERIFIED for attempted-but-unverified work, got "
            f"{tasks.verification}")
        # The branch Phase 10A is about, reached for real and not by luck.
        assert tasks.terminal_verdict() is TerminalVerdict.UNVERIFIED
        assert tasks.task_succeeded() is None
        assert tasks.terminal_violation() == ""

    def test_execution_success_with_nothing_executed(
            self, tmp_settings, tmp_path: Path):
        """Scenario 4: an inherently unverifiable request, e.g. a greeting."""
        project = _write_project(tmp_path)
        agent, _ = _agent(tmp_settings, project, [_response(text="hello")])
        agent.respond("hello there")

        tasks = agent._tasks
        assert tasks.state is TaskState.COMPLETED
        assert tasks.verification is VerificationState.NOT_REQUIRED, (
            f"nothing was executed, so this should be NOT_REQUIRED, got "
            f"{tasks.verification}")
        assert tasks.task_succeeded() is None
        assert tasks.terminal_violation() == ""

    def test_the_finished_pair_is_always_admissible(self, tmp_settings, tmp_path: Path):
        """Whichever branch this run took, the invariant must hold."""
        project = _write_project(tmp_path)
        agent, _ = _agent(tmp_settings, project, [_response(text="thinking about it")])
        agent.respond("do something about the project")
        tasks = agent._tasks
        assert tasks.state in TASK_TERMINAL_STATES, (
            f"the request did not terminate: {tasks.state}")
        assert tasks.terminal_violation() == "", tasks.terminal_violation()


# ══════════════════════════════════════════ ledger, persistence, restart


class TestLedgerAndDurableStateAgree:
    def test_the_ledger_always_explains_the_final_verdict(self, tmp_settings, tmp_path: Path):
        project = _write_project(tmp_path)
        agent, _ = _agent(tmp_settings, project, [
            _response(_call("filesystem.write_file",
                            path=str(project / "README.md"),
                            content="# hello\n")),
            _response(text="wrote a file"),
        ])
        agent.respond("add a readme to the project")
        tasks = agent._tasks
        assert tasks.ledger.entries, "a finished task explained nothing"
        assert tasks.ledger.latest_state is tasks.verification, (
            "the authoritative scalar and the ledger disagree")

    def test_the_persisted_projection_agrees_with_the_authoritative_pair(
            self, tmp_settings, tmp_path: Path):
        project = _write_project(tmp_path)
        agent, _ = _agent(tmp_settings, project, [
            _response(_call("filesystem.write_file",
                            path=str(project / "README.md"),
                            content="# hello\n")),
            _response(text="wrote a file"),
        ])
        agent.respond("add a readme to the project")
        tasks = agent._tasks
        lifecycle = agent._lifecycle_metadata()
        assert lifecycle["state"] == tasks.state.value
        assert lifecycle["verification"] == tasks.verification.value
        persisted = tasks.verification_ledger_snapshot()
        assert persisted["entries"], "the persisted projection carried no entries"
        assert persisted["entries"][-1]["state"] == tasks.verification.value

    def test_a_durable_record_reloads_into_an_admissible_pair(
            self, tmp_path: Path):
        """Scenario 5: persistence across an interpreter restart."""
        store = TaskStore(tmp_path / "sessions")
        record = TaskRecord(
            task_id="task-persist-1",
            project_name="proj",
            project_root=str(tmp_path / "proj"),
            title="t",
            goal="g",
            created_at="20260101-000000",
        )
        store.create(record)
        store.update(record.task_id, metadata={"lifecycle": {
            "state": "completed",
            "verification": "unverified",
            "verification_ledger": {
                "schema": "verification_ledger/1",
                "entries": [{"seq": 0, "state": "unverified",
                             "detail": "no verifier was available",
                             "evidence": {}, "recorded_at": "20260101-000000"}],
            },
        }})
        store.finish(record.task_id, "wrote a readme", "completed")

        reloaded = store.get(record.task_id)
        assert reloaded is not None
        assert reloaded.status == "completed"
        lifecycle = (reloaded.metadata or {}).get("lifecycle") or {}
        assert lifecycle["state"] == "completed"
        assert lifecycle["verification"] == "unverified"
        # The one authority, applied to the reloaded pair exactly as it is
        # applied to the live task.
        assert terminal_invariant_violation(lifecycle["state"], lifecycle["verification"]) == ""
        assert terminal_verdict(lifecycle["state"], lifecycle["verification"]) is \
            TerminalVerdict.UNVERIFIED

    def test_a_reloaded_record_claiming_a_false_pass_is_refused_on_load(
            self, tmp_path: Path):
        """Scenario: the durable layer must not manufacture a success."""
        from core.verification_ledger import VerificationEntry, VerificationLedger

        # A hand-edited record claiming PASSED with no evidence at all.
        entry = VerificationEntry.from_dict({
            "state": "passed", "subject_kind": "unknown",
            "method_kind": "none", "trust": "untrusted",
            "evidence": {}, "detail": "hand written",
        })
        ledger = VerificationLedger.from_dict({"entries": [entry.to_dict()]})
        assert ledger.latest_state is VerificationState.UNVERIFIED, (
            "a persisted PASSED that fails the invariant must not be trusted on load")
        assert ledger.entries[0].reason, "the refusal must be explained in the record"


class TestRecoveryReadsTheSameInvariant:
    def test_recovery_scoped_to_a_step_never_consults_the_task_state(self):
        """The policy answers a step question; task terminality is not its input."""
        policy = RecoveryPolicy()
        observation = FailureObservation(
            category=FailureCategory.VERIFICATION_FAILED,
            step_id="s1",
            recovery_attempts=0,
            verification_state=VerificationState.FAILED,
            verification_evidence={"exit_code": 1},
        )
        assert policy.decide(observation).action is RecoveryAction.RERUN_VERIFY

    def test_an_unverified_task_is_not_reported_as_a_verification_failure(self):
        """UNVERIFIED must not be mistaken for FAILED by the policy."""
        policy = RecoveryPolicy()
        observation = FailureObservation(
            category=FailureCategory.EXECUTION_FAILED,
            step_id="s1",
            recovery_attempts=0,
            verification_state=VerificationState.UNVERIFIED,
            verification_evidence={"exit_code": 1},
        )
        decision = policy.decide(observation)
        assert decision.action is RecoveryAction.RETRY_STEP, decision.reason
        assert decision.action is not RecoveryAction.RERUN_VERIFY, (
            "re-running verification cannot help when nothing adjudicated it")

    def test_exhaustion_is_deterministic_and_terminal(self):
        """Scenario 10: bounded exhaustion reaches the same answer every time."""
        policy = RecoveryPolicy(max_retries=2, max_escalations=1)
        results = []
        for _ in range(3):
            action = None
            for attempts in range(0, 6):
                action = policy.decide(FailureObservation(
                    category=FailureCategory.EXECUTION_FAILED,
                    step_id="s1",
                    recovery_attempts=attempts,
                    escalations_used=attempts,
                )).action
            results.append(action)
        assert len(set(results)) == 1, f"exhaustion was not deterministic: {results}"
        assert results[0] is RecoveryAction.ABORT
