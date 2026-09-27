"""M2.14 Phase 10 — the autonomous repair loop, and the boundaries around it.

Phase 9 proved the knowledge graph works through a real model. It left one
question open: can a real model *repair* a genuinely broken project. This module
is the deterministic half of that answer. It pins the machinery the repair loop
depends on, so that the expensive real-model runs in
``test_recovery_p10_real_model.py`` are measuring the model rather than
re-testing the harness every time:

* a controlled project with a deterministic defect, and the fact that the defect
  is real and the test really fails before anyone touches it;
* that a failure becomes a structured ``FailureObservation`` and a
  ``RecoveryPolicy`` decision, classified from fields rather than from wording;
* that recovery is bounded, and that an unrepairable project ends honestly
  rather than being reported as a success;
* that the durable plan records attempts and does not duplicate task identity;
* that a success is independently verified with observed evidence, and that a
  model claiming success without evidence stays UNVERIFIED;
* that cancellation, permissions and the tool-context contract still hold on the
  repair path.

The controlled project is built broken on purpose and **never repaired by this
module**. Repairing it is the model's job. Everything here is either the
fixture or the observation of what the architecture did.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.provider import AIProvider, AIResponse, FailureCategory, ToolCall
from config.permissions import PermissionManager
from core.agent import Agent, _failure_category
from core.context import ProjectContext
from core.events import EventBus
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
)
from core.knowledge_projection import knowledge_dir
from core.knowledge_tools import knowledge_index, knowledge_query
from core.recovery import FailureCategory as RC
from core.recovery import FailureObservation, RecoveryAction, RecoveryPolicy
from core.task_manager import StepStatus
from core.verification import VerificationState
from memory.preferences import PreferencesStore
from tools import build_registry

# ── the controlled project ───────────────────────────────────────────────

BROKEN_MODULE = 'def add(a, b):\n    return a - b\n'
FIXED_MODULE = 'def add(a, b):\n    return a + b\n'
TEST_FILE = (
    "from src.calc import add\n"
    "\n"
    "\n"
    "def test_add_positive():\n"
    "    assert add(2, 3) == 5\n"
)
README = "# Calc\n\nA tiny calculator.\n"
ENTRY = "from src.calc import add\n\nif __name__ == '__main__':\n    print(add(2, 3))\n"


class ScriptedProvider(AIProvider):
    """Stands in for the model only.

    Phase 10's real-model coverage lives in the gated module; this one exists so
    the machinery the repair loop relies on can be asserted deterministically,
    including the cases a real model would take too long to reach reliably.
    """

    name = "scripted"
    model_label = "scripted/local"
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.turns = 0
        self.seen_tools: list = []

    def is_available(self):
        return True

    def list_models(self):
        return []

    def chat(self, messages, tools=None, model=None):
        self.turns += 1
        self.seen_tools.append(list(tools or []))
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                return item(messages, model)
            return item
        return AIResponse(text="done", tool_calls=[])


def _response(*calls, text=""):
    return AIResponse(text=text, tool_calls=list(calls))


def _call(name, **args):
    return ToolCall(name=name, arguments=args)


@pytest.fixture(autouse=True)
def _release_handles():
    yield
    close_shared_managers()


def _write_project(root: Path, module_source: str = BROKEN_MODULE) -> Path:
    """Create the controlled project. Deliberately broken by default.

    The defect is arithmetic and local: ``add`` subtracts instead of adding. It
    needs no network, no clock, no external service and no undefined behaviour,
    and it produces one unambiguous failing assertion.
    """
    project = root / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "calc.py").write_text(module_source, encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_calc.py").write_text(TEST_FILE, encoding="utf-8")
    (project / "README.md").write_text(README, encoding="utf-8")
    (project / "main.py").write_text(ENTRY, encoding="utf-8")
    return project


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return _write_project(tmp_path)


def _run_pytest(project: Path) -> subprocess.CompletedProcess:
    """Run the project's own suite with the interpreter under test."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(project)],
        capture_output=True, text=True, timeout=300, cwd=str(project))


def _agent(settings, project: Path, responses, events: list | None = None,
           confirmer=None, catalog=None):
    prefs = PreferencesStore(settings.memory_dir)
    bus = EventBus()
    sink = events if events is not None else []
    for kind in ("recovery_decided", "recovery_started", "verification_completed",
                 "verification_failed", "verification_downgraded",
                 "tool_context_selected", "task_done",
                 "model_fallback", "model_offline_fallback"):
        bus.subscribe(kind, lambda d, k=kind: sink.append((k, d)))
    provider = ScriptedProvider(responses)
    agent = Agent(
        provider=provider,
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, prefs),
        events=bus,
        confirmer=confirmer or (lambda tool, description: "allow"),
        project=ProjectContext(name=project.name, path=str(project)),
        memory_preferences=prefs,
        model_name="",
        model_catalog=catalog,
    )
    return agent, provider, sink


# ══════════════════════════════════════════ 10B the controlled defect


class TestControlledProject:
    def test_the_project_is_complete_and_runnable(self, project: Path):
        for name in ("src/calc.py", "src/__init__.py", "tests/test_calc.py",
                     "README.md", "main.py"):
            assert (project / name).is_file(), f"missing {name}"
        assert (project / "tests" / "test_calc.py").read_text(encoding="utf-8")

    def test_the_defect_is_real_before_anyone_repairs_it(self, project: Path):
        """The failure must be genuine, or the whole phase proves nothing."""
        result = _run_pytest(project)
        assert result.returncode != 0, (
            "the controlled project passed its own tests, so there is no "
            "failure for a model to find")
        assert "1 failed" in result.stdout, result.stdout[-2000:]

    def test_the_defect_is_local_and_about_arithmetic(self, project: Path):
        """No network, no clock, no external service, no undefined behaviour."""
        source = (project / "src" / "calc.py").read_text(encoding="utf-8")
        assert "return a - b" in source
        for forbidden in ("import ", "open(", "time", "random", "input(",
                          "requests", "socket", "os."):
            assert forbidden not in source, (
                f"the defect fixture reached for {forbidden!r}, which would "
                f"make the failure depend on something outside the project")

    def test_the_repair_actually_fixes_it(self, project: Path):
        """The harness may build the fixture; it may not repair the project.

        This is the *definition* of a repairable defect, checked without the
        harness doing any repairing: if overwriting the module with the correct
        arithmetic does not make the suite pass, then the defect class is wrong
        and a real model could not have succeeded either. The file is restored
        to broken afterwards, so the fixture other tests use is untouched.
        """
        try:
            (project / "src" / "calc.py").write_text(FIXED_MODULE, encoding="utf-8")
            result = _run_pytest(project)
            assert result.returncode == 0, (
                "correcting the arithmetic does not fix the suite, so this "
                "defect is not repairable in the intended way")
            assert "1 passed" in result.stdout, result.stdout[-2000:]
        finally:
            (project / "src" / "calc.py").write_text(BROKEN_MODULE, encoding="utf-8")

    def test_the_entry_point_runs(self, project: Path):
        result = subprocess.run([sys.executable, str(project / "main.py")],
                                capture_output=True, text=True, timeout=120,
                                cwd=str(project))
        assert result.returncode == 0, result.stderr
        # Broken on purpose: the entry point prints the wrong answer.
        assert result.stdout.strip() == "-1"


# ══════════════════════════════════════════ 10E structured observation


class TestStructuredFailureObservation:
    def test_a_failing_verification_is_classified_from_fields(self):
        """Category comes from the verdict, never from the error text.

        Two failures with byte-identical messages classify differently when
        their structured fields differ, which is the whole point: wording is
        not evidence.
        """
        from core.verification import VerificationResult

        failed = SimpleNamespace(
            cancelled=False, executed=True, allowed=True, status="",
            error="assert 5 == -1",  # identical wording in both
            verification=VerificationResult(VerificationState.FAILED, "tests failed",
                                            "project_tests", {"tests_failed": 1}),
        )
        plain = SimpleNamespace(
            cancelled=False, executed=True, allowed=True, status="",
            error="assert 5 == -1",
            verification=None,
        )
        assert _failure_category(failed, StepStatus.FAILED) is RC.VERIFICATION_FAILED
        assert _failure_category(plain, StepStatus.FAILED) is RC.EXECUTION_FAILED

    def test_cancellation_is_not_a_retryable_failure(self):
        cancelled = SimpleNamespace(cancelled=True, executed=False, allowed=False,
                                    status="", error="", verification=None)
        assert _failure_category(cancelled, StepStatus.CANCELLED) is RC.CANCELLED
        assert RC.CANCELLED.retryable is False
        assert RC.CANCELLED.terminal is True

    def test_a_blocked_step_is_not_executed_rather_than_failed(self):
        blocked = SimpleNamespace(cancelled=False, executed=False, allowed=False,
                                  status="blocked", error="", verification=None)
        assert _failure_category(blocked, StepStatus.BLOCKED) is RC.NOT_EXECUTED

    def test_the_observation_scrubs_sensitive_material_at_construction(self):
        """An observation is scrubbed the moment it exists, so it is safe to log.

        The scrubber is keyed on field names and on sensitive *keywords* in
        text, which is the contract it offers. Asserted here precisely: a
        credential-named field and a message naming a credential are both
        removed. It is deliberately not asserted to detect an arbitrary
        high-entropy value, because it does not and is not trying to.
        """
        observation = FailureObservation(
            category=RC.EXECUTION_FAILED, task_id="t", run_id="r", step_id="s",
            tool="terminal.run", capability="", error_code="", error_type="",
            error_message="request failed: invalid api_key supplied",
            attempt=1, recovery_attempts=0, escalations_used=0,
            verification_state=None,
            verification_evidence={"authorization": "Bearer abc123"},
            cancelled=False, recovery_hint="", expected_result="",
            step_status=StepStatus.FAILED, step_replayable=True,
            interrupted=False, observed_at="20240101-000000",
        )
        payload = json.dumps(observation.to_dict(), default=str)
        assert "abc123" not in payload, "a credential value survived redaction"
        assert "api_key" not in payload.lower() or "<redacted>" in payload
        assert "Bearer" not in payload
        # The useful part is still there, or redacting would be worthless.
        assert observation.category is RC.EXECUTION_FAILED
        assert observation.tool == "terminal.run"

    def test_the_observation_carries_no_task_record(self):
        """It is decision input, not a second copy of the task."""
        names = {f.name for f in FailureObservation.__dataclass_fields__.values()}
        for forbidden in ("title", "goal", "plan", "steps", "git", "snapshots"):
            assert forbidden not in names, (
                f"FailureObservation grew a {forbidden!r} field")


# ══════════════════════════════════════════ 10E/10J the policy bounds it


class TestRecoveryPolicyBounds:
    def test_cancellation_dominates_every_other_rule(self):
        policy = RecoveryPolicy()
        for status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.DONE):
            decision = policy.decide(FailureObservation(
                category=RC.CANCELLED, task_id="t", run_id="r", step_id="s",
                tool="terminal.run", capability="", error_code="", error_type="",
                error_message="", attempt=1, recovery_attempts=0,
                escalations_used=0, verification_state=None,
                verification_evidence={}, cancelled=True, recovery_hint="",
                expected_result="", step_status=status, step_replayable=True,
                interrupted=False, observed_at="20240101-000000"))
            assert decision.action is RecoveryAction.NONE, (
                f"cancellation was overridden for step status {status}")

    def test_a_non_retryable_category_aborts_immediately(self):
        for category in (RC.PERMISSION_DENIED, RC.INVALID_REQUEST):
            decision = RecoveryPolicy().decide(FailureObservation(
                category=category, task_id="t", run_id="r", step_id="s",
                tool="x", capability="", error_code="", error_type="",
                error_message="", attempt=1, recovery_attempts=0,
                escalations_used=0, verification_state=None,
                verification_evidence={}, cancelled=False, recovery_hint="",
                expected_result="", step_status=StepStatus.FAILED,
                step_replayable=True, interrupted=False,
                observed_at="20240101-000000"))
            assert decision.action is RecoveryAction.ABORT, category

    def test_retries_are_bounded_and_then_abort(self):
        """Whatever retry-flavoured action is chosen, the budget still bounds it.

        A verification failure with evidence is offered ``RERUN_VERIFY`` first,
        which is a *narrower* budget than the general retry budget, so it is
        exhausted sooner. A plain execution failure is offered ``RETRY_STEP``.
        Both must stop.
        """
        for category, state, evidence in (
            (RC.EXECUTION_FAILED, None, {}),
            (RC.VERIFICATION_FAILED, VerificationState.FAILED, {"tests_failed": 1}),
        ):
            policy = RecoveryPolicy(max_retries=2, max_rerun_verify=1,
                                    max_escalations=0)

            def observe(recovery_attempts: int) -> FailureObservation:
                return FailureObservation(
                    category=category, task_id="t", run_id="r", step_id="s",
                    tool="project.verify", capability="", error_code="",
                    error_type="", error_message="", attempt=1,
                    recovery_attempts=recovery_attempts, escalations_used=0,
                    verification_state=state, verification_evidence=dict(evidence),
                    cancelled=False, recovery_hint="", expected_result="",
                    step_status=StepStatus.FAILED, step_replayable=True,
                    interrupted=False, observed_at="20240101-000000")

            actions = [policy.decide(observe(n)).action for n in range(8)]
            retryish = {RecoveryAction.RETRY_STEP, RecoveryAction.RERUN_VERIFY}
            assert actions[0] in retryish, (category, actions)
            assert RecoveryAction.ABORT in actions, (
                f"{category}: the budget was never exhausted: {actions}")
            assert actions[-1] is RecoveryAction.ABORT, (
                f"{category}: the loop did not end in ABORT: {actions}")
            # Bounded: it never kept handing out a retry once spent.
            tail = actions[actions.index(RecoveryAction.ABORT):]
            assert not (set(tail) & retryish), (
                f"{category}: a retry was authorised after the budget ended: {actions}")

    def test_an_interrupted_step_is_never_replayed(self):
        """Checked before the retryable test, so no budget can authorise a
        silent replay of something whose side effects are unknown."""
        decision = RecoveryPolicy().decide(FailureObservation(
            category=RC.EXECUTION_FAILED, task_id="t", run_id="r", step_id="s",
            tool="filesystem.write_file", capability="", error_code="",
            error_type="", error_message="", attempt=1, recovery_attempts=0,
            escalations_used=0, verification_state=None,
            verification_evidence={}, cancelled=False, recovery_hint="",
            expected_result="", step_status=StepStatus.FAILED,
            step_replayable=True, interrupted=True, observed_at="20240101-000000"))
        assert decision.action is RecoveryAction.ABORT

    def test_skipping_is_off_by_default(self):
        """A failing step must never be quietly marked skipped."""
        policy = RecoveryPolicy()
        assert policy.allow_skip is False


# ══════════════════════════════════════════ 10E/10F the loop end to end


class TestRepairLoopMechanics:
    """The deterministic shape of the loop a real model drives in Phase 10."""

    def _repair_script(self, project: Path):
        return [
            _response(_call("project.verify", path=str(project))),
            _response(_call("filesystem.write_file",
                            path=str(project / "src" / "calc.py"),
                            content=FIXED_MODULE)),
            _response(_call("project.verify", path=str(project))),
        ]

    def test_a_failing_verify_becomes_a_retry_and_the_model_repairs(
            self, tmp_settings, project: Path):
        events: list = []
        agent, provider, sink = _agent(tmp_settings, project,
                                       self._repair_script(project), events)
        reply = agent.respond("complete the project and verify it")

        # Read the step list, not a name-keyed dict: ``project.verify`` runs
        # twice here, once failing and once passing, and collapsing by name
        # would hide the failure this test exists to observe.
        statuses = [s.status for s in agent._tasks.steps]
        names = [s.name for s in agent._tasks.steps]
        assert StepStatus.FAILED in statuses, (
            f"no step failed, so no repair was needed or made: "
            f"{list(zip(names, statuses))}")
        assert StepStatus.DONE in statuses, (
            f"nothing succeeded after the repair: {list(zip(names, statuses))}")
        # And the order is the one that matters: it failed, then something was
        # written, then it passed.
        assert names.index("filesystem.write_file") > names.index("project.verify")
        assert statuses[-1] is StepStatus.DONE

        # The repair was a real file write that actually fixed the project.
        assert (project / "src" / "calc.py").read_text(encoding="utf-8") == FIXED_MODULE
        assert _run_pytest(project).returncode == 0

        # And the model was given more than one turn, which is what makes repair
        # possible at all.
        assert provider.turns >= 3, f"only {provider.turns} model turns"
        assert agent._tasks.verification is VerificationState.PASSED

    def test_the_failure_reached_the_policy_with_a_category(self, tmp_settings,
                                                            project: Path):
        events: list = []
        agent, _, _ = _agent(tmp_settings, project, self._repair_script(project),
                             events)
        agent.respond("complete the project and verify it")

        decisions = [d for kind, d in events if kind == "recovery_decided"]
        assert decisions, "RecoveryPolicy was never consulted"
        for decision in decisions:
            assert decision["action"] in {
                "none", "retry_step", "rerun_verify", "escalate_model",
                "skip_step", "abort"}
            assert decision["category"], "a decision carried no category"
            assert decision["category"] == "verification_failed", (
                f"expected the failing suite to classify as a verification "
                f"failure, got {decision['category']}")
        assert any(d["action"] == "retry_step" for d in decisions), (
            f"no retry was authorised: {decisions}")

    def test_the_retry_budget_was_counted_not_guessed(self, tmp_settings,
                                                      project: Path):
        events: list = []
        agent, _, _ = _agent(tmp_settings, project, self._repair_script(project),
                             events)
        agent.respond("complete the project and verify it")
        decisions = [d for kind, d in events if kind == "recovery_decided"]
        for decision in decisions:
            assert decision["retries_used"] >= 0
            assert decision["retries_used"] <= tmp_settings.max_recovery_attempts
        assert agent._tasks.recovery_attempts <= tmp_settings.max_recovery_attempts

    def test_no_duplicate_task_identity_across_the_repair(self, tmp_settings,
                                                          project: Path):
        agent, _, _ = _agent(tmp_settings, project, self._repair_script(project), [])
        agent.respond("complete the project and verify it")
        # One request, one task id, one run id, however many turns it took.
        assert agent._tasks.task_id
        assert agent._tasks.run_id
        record = agent._task_store.get(agent._tasks.task_id)
        assert record is not None
        assert record.task_id == agent._tasks.task_id

    def test_the_durable_plan_records_attempts_and_keeps_history(self, tmp_settings,
                                                                 project: Path):
        events: list = []
        agent, _, _ = _agent(tmp_settings, project, self._repair_script(project),
                             events)
        agent.respond("complete the project and verify it")

        record = agent._task_store.get(agent._tasks.task_id)
        assert record is not None
        # The plan is persisted on the authoritative task record.
        assert record.plan is not None, "no durable plan was persisted"
        statuses = [s.status for s in record.plan.steps]
        assert any(s is StepStatus.FAILED for s in statuses) or \
            any(s is StepStatus.DONE for s in statuses), statuses
        for step in record.plan.steps:
            assert step.attempts >= 0
        # Plans are versions, so re-planning may add one, but each is a
        # distinct record rather than an overwrite of the same identity.
        keys = [s.step_id for s in record.plan.steps]
        assert len(keys) == len(set(keys)), f"duplicate plan step ids: {keys}"

    def test_the_plan_survives_a_reload_from_disk(self, tmp_settings,
                                                 project: Path):
        agent, _, _ = _agent(tmp_settings, project, self._repair_script(project), [])
        agent.respond("complete the project and verify it")
        task_id = agent._tasks.task_id
        agent._task_store.__init__(tmp_settings.sessions_dir)  # reopen from disk
        record = agent._task_store.get(task_id)
        assert record is not None, "the task did not survive a store reopen"
        assert record.plan is not None


# ══════════════════════════════════════════ 10G verification


class TestVerificationIsIndependent:
    def test_a_repaired_project_is_verified_with_observed_evidence(self, tmp_settings,
                                                                   project: Path):
        events: list = []
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
            _response(_call("filesystem.write_file",
                            path=str(project / "src" / "calc.py"),
                            content=FIXED_MODULE)),
            _response(_call("project.verify", path=str(project))),
        ], events)
        agent.respond("complete the project and verify it")

        assert agent._tasks.verification is VerificationState.PASSED, (
            f"a repaired, passing project was not verified: "
            f"{agent._tasks.verification}")
        entry = agent._tasks.ledger.latest
        assert entry is not None
        assert entry.state is VerificationState.PASSED
        # Observed evidence, not a claim: counters from the actual run.
        assert entry.evidence, "PASSED was recorded with no evidence"
        assert str(entry.evidence.get("tests_passed", "0")) != "0", entry.evidence
        assert entry.trust is not None and entry.trust.value == "observed", (
            f"PASSED was not backed by observed evidence: {entry.trust}")

    def test_a_model_claiming_success_without_evidence_stays_unverified(
            self, tmp_settings, project: Path):
        """The model saying "tests passed" is not verification.

        The model claims completion and never runs anything. The architecture
        must not record a pass on the strength of the claim.
        """
        agent, _, _ = _agent(tmp_settings, project, [
            _response(text="All tests pass, the project is complete."),
        ], [])
        agent.respond("complete the project and verify it")
        assert agent._tasks.verification is not VerificationState.PASSED, (
            "a model claim was accepted as verification evidence")
        entries = agent._tasks.ledger.entries
        for entry in entries:
            if entry.state is VerificationState.PASSED:
                assert entry.evidence, "a PASSED entry carries no evidence"

    def test_an_unrepairable_project_does_not_become_a_pass(self, tmp_settings,
                                                            project: Path):
        """Phase 10J: the test asserts something the code cannot satisfy."""
        (project / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n\n"
            "def test_impossible():\n    assert add(0, 0) == 99\n",
            encoding="utf-8")
        events: list = []
        agent, provider, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
            _response(_call("project.verify", path=str(project))),
            _response(_call("project.verify", path=str(project))),
            _response(_call("project.verify", path=str(project))),
        ], events)
        reply = agent.respond("complete the project and verify it")

        assert agent._tasks.verification is not VerificationState.PASSED, (
            "an impossible test was adjudicated as a pass")
        assert agent._tasks.state.value in {"failed", "completed", "cancelled"}
        # Bounded: the budget was respected and the model was not looped forever.
        assert provider.turns <= 8, f"the model was looped {provider.turns} times"
        assert agent._tasks.recovery_attempts <= tmp_settings.max_recovery_attempts
        for decision in (d for k, d in events if k == "recovery_decided"):
            assert decision["action"] != "retry_step" or \
                decision["retries_used"] < tmp_settings.max_recovery_attempts

    def test_the_budget_terminates_rather_than_looping(self, tmp_settings,
                                                       project: Path):
        """Same impossible project, but the model keeps trying to fix it.

        The repair cannot succeed, so what matters is that the loop stops and
        says so honestly rather than retrying forever or reporting success.

        Counted in *attempts*, not in ``recovery_decided`` events: one failure
        is reported twice, once from the escalation phase and once from the
        loop, so the event count is twice the real number of retries and would
        make a correctly bounded loop look unbounded.
        """
        (project / "src" / "calc.py").write_text(
            'def add(a, b):\n    return a + b\n\n\ndef broken():\n    raise RuntimeError("x")\n',
            encoding="utf-8")
        (project / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n\n"
            "def test_impossible():\n    assert add(1, 1) == 99\n",
            encoding="utf-8")
        events: list = []
        agent, provider, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))) for _ in range(10)
        ], events)
        agent.respond("complete the project and verify it")

        assert provider.turns <= 8, (
            f"the model was given {provider.turns} turns against an "
            f"unrepairable project; the budget is not being enforced")
        assert agent._tasks.verification is not VerificationState.PASSED
        # The authoritative counter, not the event stream.
        assert agent._tasks.recovery_attempts <= tmp_settings.max_recovery_attempts, (
            f"{agent._tasks.recovery_attempts} recovery attempts against a budget "
            f"of {tmp_settings.max_recovery_attempts}")
        # And the loop phase, which is the one that grants the retry, is bounded
        # even counting its doubled reporting.
        loop_retries = [d for k, d in events
                        if k == "recovery_decided" and d["action"] == "retry_step"
                        and d["phase"] == "llm_loop"]
        assert len(loop_retries) <= tmp_settings.max_recovery_attempts, (
            f"the loop authorised {len(loop_retries)} retries against a budget "
            f"of {tmp_settings.max_recovery_attempts}")
        assert not (agent._tasks.state.value == "completed"
                    and agent._tasks.verification is not VerificationState.PASSED), (
            "the task reported completion without a passing verdict")


class TestVerifierIsNotFooledByAStaleBytecodeCache:
    """A repair that is correct must be seen as correct.

    Found in Phase 10: the repair loop reported a correct repair as still
    broken. CPython validates a cached ``.pyc`` against the source's
    modification time in whole seconds *and* its byte size, so an edit that
    keeps the file the same length and lands in the same second as the previous
    run leaves a cache that looks valid. The next test run then imported the
    pre-repair module. For a model that is the worst possible failure mode: it
    is told its correct fix did not work, so it tries something else.
    """

    def _repair_in_place(self, project: Path, attempts: int = 3) -> list[bool]:
        """Compile the broken module, rewrite it identically, re-run the suite.

        Repeated because the collision only happens when the rewrite lands in
        the same second as the cache, so a single pass can pass by luck.
        """
        outcomes: list[bool] = []
        for _ in range(attempts):
            # A first run compiles whatever is on disk right now.
            subprocess.run([sys.executable, "-m", "pytest", "-q", str(project)],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(project))
            # Same byte length as the broken version, so only the clock differs.
            (project / "src" / "calc.py").write_text(FIXED_MODULE, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(project)],
                capture_output=True, text=True, timeout=300, cwd=str(project))
            outcomes.append(result.returncode == 0)
            (project / "src" / "calc.py").write_text(BROKEN_MODULE, encoding="utf-8")
        return outcomes

    def test_the_product_verifier_sees_a_same_size_repair(self, tmp_settings,
                                                         project: Path):
        """Through the product's own runner, which is what the model uses."""
        from coding.test_runner import run_pytest

        for _ in range(3):
            run_pytest(str(project))  # compiles the broken module
            (project / "src" / "calc.py").write_text(FIXED_MODULE, encoding="utf-8")
            ok, output, error = run_pytest(str(project))
            assert ok, (
                "the verifier did not see a correct repair; it is being fooled "
                f"by a stale bytecode cache.\nstdout:\n{output}\nstderr:\n{error}")
            (project / "src" / "calc.py").write_text(BROKEN_MODULE, encoding="utf-8")

    def test_a_repair_of_a_different_length_is_always_seen(self, project: Path):
        """The control: an edit that changes the size is never confused."""
        longer = "def add(a, b):\n    return a + b\n\n\n# repaired\n"
        assert len(longer) != len(BROKEN_MODULE)
        for _ in range(2):
            subprocess.run([sys.executable, "-m", "pytest", "-q", str(project)],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(project))
            (project / "src" / "calc.py").write_text(longer, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(project)],
                capture_output=True, text=True, timeout=300, cwd=str(project))
            assert result.returncode == 0, result.stdout[-1500:]
            (project / "src" / "calc.py").write_text(BROKEN_MODULE, encoding="utf-8")

    def test_the_runner_does_not_delete_anything_in_the_project(self, tmp_settings,
                                                                project: Path):
        """The fix relocates the cache; it must not clean the project out."""
        cache = project / "src" / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "keepme.txt").write_text("intact", encoding="utf-8")
        from coding.test_runner import run_pytest

        run_pytest(str(project))
        assert (cache / "keepme.txt").read_text(encoding="utf-8") == "intact", (
            "the test runner deleted files it did not own")


class TestTheTwoRequestPathsAreBothAccountedFor:
    """Phase 10 observation, kept as a test so it cannot be forgotten.

    ``Planner.coding_intent`` classifies a request from a small verb list, and
    *any* classification ("debug" or "develop") routes it to ``DebugLoop``
    instead of the LLM loop. DebugLoop selects its tools with a hardcoded name
    filter rather than the capability-based selector, and offers none of
    ``project.verify``, ``filesystem.write_file`` or the knowledge tools. It has
    its own auto-verify over the detected test command.

    That is a consistency finding rather than a data-loss one, and it is
    recorded rather than changed:

    * nothing is lost from the graph, because the task and verification are
      projected by ``_finish_tracking`` on every path, and the data/chart tools
      that ``_project_knowledge_tool`` handles do not exist on this path;
    * verification still happens, through ``DebugLoop._run_auto_verify``;
    * the registry stays authoritative, because DebugLoop derives its tool list
      from ``registry.all_specs()`` and only narrows it.

    What it does mean is that a request containing a word like "fix" or
    "create" exercises a different pipeline, so a test has to know which one it
    is on. That is asserted here rather than left to be discovered.
    """

    def _classify(self, text: str) -> str | None:
        from core.planner import Planner

        return Planner(self._settings).coding_intent(text)

    @pytest.fixture(autouse=True)
    def _settings_for_planner(self, tmp_settings):
        self._settings = tmp_settings
        yield

    @pytest.mark.parametrize("text,expected", [
        ("please fix the failing test", "debug"),
        ("this is broken", "debug"),
        ("create a new module", "develop"),
        ("add a helper function", "develop"),
        ("refactor the loader", "develop"),
        ("please complete the project and report the result", None),
        ("what files are in this project", None),
    ])
    def test_the_classifier_is_verb_driven(self, text, expected):
        """Documented, not endorsed: a single word changes the whole pipeline."""
        assert self._classify(text) == expected

    def test_the_llm_loop_offers_a_capability_selected_toolset(self, tmp_settings,
                                                               project: Path):
        events: list = []
        agent, provider, sink = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
        ], events)
        agent.respond("please complete the project and report the result")
        selected = [d for kind, d in events if kind == "tool_context_selected"]
        assert selected, "the LLM loop should record a tool context decision"
        registry_size = len(build_registry(tmp_settings).all_specs())
        assert selected[0]["total_tools"] == registry_size
        assert selected[0]["tool_count"] < registry_size, (
            "the adaptive path did not narrow anything")

    def test_the_debug_path_uses_its_own_narrower_toolset(self, tmp_settings,
                                                          project: Path):
        from core.debug_loop import DebugLoop

        registry = build_registry(tmp_settings)
        all_names = {s.name for s in registry.all_specs()}
        offered = {n for n in all_names if DebugLoop._is_debug_tool(n)}
        assert offered, "the debug path offers no tools at all"
        # It is derived from the full registry, so the registry is still the
        # authority; this path simply narrows by name instead of by capability.
        assert offered < all_names
        # Recorded as observations: these are absent by design of that path.
        assert "project.verify" not in offered, (
            "the debug path cannot verify, so a repair there is verified by "
            "DebugLoop._run_auto_verify instead")
        assert "knowledge.query" not in offered

    def test_the_task_is_projected_on_the_debug_path_too(self, tmp_settings,
                                                         project: Path):
        """The important half: nothing is lost from the graph on that path."""
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("filesystem.read_file",
                            path=str(project / "src" / "calc.py"))),
        ], [])
        reply = agent.respond("please fix the failing test")
        close_shared_managers()
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            graph = mgr.get_graph(canonical_project_key(str(project)))
            assert graph.find_nodes(node_type=NodeType.TASK, limit=10), (
                "a debug-classified request left no task in the graph")
        finally:
            mgr.close_all()
        assert agent._tasks.task_id


# ══════════════════════════════════════════ 10H knowledge projection


class TestRepairIsProjected:
    def _repaired(self, tmp_settings, project: Path):
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
            _response(_call("filesystem.write_file",
                            path=str(project / "src" / "calc.py"),
                            content=FIXED_MODULE)),
            _response(_call("project.verify", path=str(project))),
        ], [])
        agent.respond("complete the project and verify it")
        return agent

    def test_the_graph_knows_the_project_files_and_symbols(self, tmp_settings,
                                                           project: Path):
        self._repaired(tmp_settings, project)
        close_shared_managers()
        knowledge_index(project_path=str(project), settings=tmp_settings)

        files = knowledge_query(query_type="search_files", settings=tmp_settings,
                                project_path=str(project), limit=50)
        assert files.success, files.error
        names = {n["display_name"] for n in files.data["nodes"]}
        assert "calc.py" in names, sorted(names)

        symbols = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                  project_path=str(project),
                                  node_type="code_symbol", limit=50)
        assert symbols.success, symbols.error
        assert "add" in {n["display_name"] for n in symbols.data["nodes"]}

    def test_the_graph_knows_the_task_and_its_verification(self, tmp_settings,
                                                          project: Path):
        agent = self._repaired(tmp_settings, project)
        close_shared_managers()
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            graph = mgr.get_graph(canonical_project_key(str(project)))
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=50)
            verifications = graph.find_nodes(node_type=NodeType.VERIFICATION,
                                            limit=50, include_stale=True)
            assert tasks, "no task reached the graph"
            assert verifications, "no verification reached the graph"
            # The graph mirrors the ledger; it does not invent a verdict.
            assert any(v.metadata.get("state") == "passed" for v in verifications), (
                "the graph holds no passing verdict for a repaired project")
        finally:
            mgr.close_all()

    def test_the_queryable_graph_answers_for_the_repaired_project(self, tmp_settings,
                                                                 project: Path):
        """Phase 10H: read the answer back through the tool, not the internals."""
        self._repaired(tmp_settings, project)
        close_shared_managers()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="search_files", settings=tmp_settings,
                                 project_path=str(project), limit=50)
        assert result.success, result.error
        assert result.data["count"] > 0
        for node in result.data["nodes"]:
            assert node["state"] == "current"

    def test_deleting_the_graph_costs_nothing_authoritative(self, tmp_settings,
                                                            project: Path):
        """The graph is a projection: losing it must lose no real state."""
        agent = self._repaired(tmp_settings, project)
        task_id = agent._tasks.task_id
        close_shared_managers()
        for path in sorted(knowledge_dir(tmp_settings).glob("*")):
            path.unlink()
        record = agent._task_store.get(task_id)
        assert record is not None
        assert record.task_id == task_id
        assert (project / "src" / "calc.py").read_text(encoding="utf-8") == FIXED_MODULE
        assert _run_pytest(project).returncode == 0


# ══════════════════════════════════════════ 10K cancellation


class _Token:
    def __init__(self):
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self.cancelled = True


class TestCancellationOnTheRepairPath:
    def test_a_cancelled_repair_never_becomes_a_pass(self, tmp_settings,
                                                     project: Path):
        events: list = []
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
            _response(_call("filesystem.write_file",
                            path=str(project / "src" / "calc.py"),
                            content=FIXED_MODULE)),
            _response(_call("project.verify", path=str(project))),
        ], events)

        token = _Token()

        def cancel_during_verify(messages, model=None):
            # Cancelled while the failing verify is in flight.
            token.cancel()
            return _response(_call("project.verify", path=str(project)))

        agent._provider.responses = [cancel_during_verify]
        agent.respond("complete the project and verify it", token)

        assert agent._tasks.state.value == "cancelled", (
            f"a cancelled repair ended as {agent._tasks.state.value}")
        assert agent._tasks.verification is not VerificationState.PASSED
        assert agent._tasks.task_succeeded() is not True
        # Cancellation must not be retried.
        retries = [d for k, d in events
                   if k == "recovery_decided" and d["action"] == "retry_step"]
        assert not retries, f"a cancelled step was retried: {retries}"

    def test_cancellation_leaves_the_graph_readable(self, tmp_settings,
                                                    project: Path):
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
        ], [])
        token = _Token()
        token.cancel()
        agent.respond("complete the project and verify it", token)
        close_shared_managers()
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            mgr.get_graph(canonical_project_key(str(project)))
        finally:
            mgr.close_all()


# ══════════════════════════════════════════ 10L security


class TestRepairCannotEscapeItsProject:
    """The authorized root is the boundary, so the targets must be outside it.

    ``make_settings`` authorizes the whole temporary directory, so a sibling of
    the project is *inside* the boundary and legitimately writable. Testing
    there would assert nothing. These targets are genuinely outside, which is
    what makes the refusals mean something.
    """

    def _outside(self, tmp_path: Path) -> Path:
        return tmp_path.parent / "outside-project-target"

    def test_a_traversing_write_path_is_refused(self, tmp_settings, project: Path,
                                                tmp_path: Path):
        outside = self._outside(tmp_path)
        outside.write_text("intact", encoding="utf-8")
        try:
            traversing = project / ".." / ".." / outside.name
            agent, _, _ = _agent(tmp_settings, project, [
                _response(_call("filesystem.write_file", path=str(traversing),
                                content="overwritten")),
                _response(text="done"),
            ], [], confirmer=lambda tool, description: "deny")
            agent.respond("fix the project")
            assert outside.read_text(encoding="utf-8") == "intact", (
                "a traversing path wrote outside the authorized roots")
        finally:
            outside.unlink(missing_ok=True)

    def test_a_sibling_outside_the_roots_is_not_writable(self, tmp_settings,
                                                         project: Path,
                                                         tmp_path: Path):
        sibling = self._outside(tmp_path)
        sibling.write_text("intact", encoding="utf-8")
        try:
            agent, _, _ = _agent(tmp_settings, project, [
                _response(_call("filesystem.write_file", path=str(sibling),
                                content="overwritten")),
                _response(text="done"),
            ], [], confirmer=lambda tool, description: "deny")
            agent.respond("fix the project")
            assert sibling.read_text(encoding="utf-8") == "intact", (
                "a file outside the authorized roots was overwritten")
        finally:
            sibling.unlink(missing_ok=True)

    def test_a_denial_outside_the_roots_ends_cancelled(self, tmp_settings,
                                                       project: Path,
                                                       tmp_path: Path):
        sibling = self._outside(tmp_path)
        sibling.write_text("intact", encoding="utf-8")
        events: list = []
        try:
            agent, provider, _ = _agent(tmp_settings, project, [
                _response(_call("filesystem.write_file", path=str(sibling),
                                content="overwritten")),
                _response(text="done"),
            ], events, confirmer=lambda tool, description: "deny")
            agent.respond("fix the project")
            # A refused write must not be presented as a success, and must not
            # be retried into a loop.
            assert agent._tasks.verification is not VerificationState.PASSED
            retries = [d for k, d in events
                       if k == "recovery_decided" and d["action"] == "retry_step"]
            assert not retries, f"a denied step was retried: {retries}"
        finally:
            sibling.unlink(missing_ok=True)

    def test_a_write_inside_the_project_is_allowed(self, tmp_settings,
                                                   project: Path):
        """The boundary is the root, not the project directory.

        A repair necessarily writes inside the project, so refusing that would
        make the loop impossible. This asserts the complementary half: the
        refusal above is about location, not about writing.
        """
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("filesystem.write_file",
                            path=str(project / "src" / "calc.py"),
                            content=FIXED_MODULE)),
            _response(text="done"),
        ], [], confirmer=lambda tool, description: "deny")
        agent.respond("fix the project")
        assert (project / "src" / "calc.py").read_text(encoding="utf-8") == FIXED_MODULE, (
            "a write inside the project was refused, so no repair is possible")

    def test_a_secret_outside_the_roots_is_not_readable(self, tmp_settings,
                                                        project: Path):
        agent, provider, _ = _agent(tmp_settings, project, [
            _response(_call("filesystem.read_file",
                            path="C:/Windows/System32/drivers/etc/hosts")),
            _response(text="done"),
        ], [])
        reply = agent.respond("read that file")
        surfaced = " ".join(
            str(getattr(s, "detail", "") or "") for s in agent._tasks.steps)
        assert "127.0.0.1" not in surfaced and "127.0.0.1" not in (reply or ""), (
            "a file outside the authorized roots was read")

    def test_a_verification_outside_the_roots_is_not_a_pass(self, tmp_settings,
                                                             project: Path):
        agent, _, _ = _agent(tmp_settings, project, [
            _response(_call("project.verify", path="C:/Windows/System32")),
            _response(text="done"),
        ], [])
        agent.respond("verify the project")
        assert agent._tasks.verification is not VerificationState.PASSED


# ══════════════════════════════════════════ 10M/10N context and provider


class TestContextAndProviderOnTheRepairPath:
    def test_the_repair_tools_are_offered(self, tmp_settings, project: Path):
        agent, provider, sink = _agent(tmp_settings, project, [
            _response(_call("project.verify", path=str(project))),
        ], [])
        agent.respond("complete the project and verify it")

        selected = [d for kind, d in sink if kind == "tool_context_selected"]
        assert selected, "no tool context decision was recorded"
        decision = selected[0]
        registry_size = len(build_registry(tmp_settings).all_specs())
        assert decision["total_tools"] == registry_size
        assert 0 < decision["tool_count"] <= registry_size

        # project.verify is what the whole loop depends on.
        offered = {t.name for t in provider.seen_tools[0]}
        assert "project.verify" in offered, (
            "project.verify was not offered, so the repair loop cannot run")
        # And the full registry is untouched by the narrowing.
        assert len(build_registry(tmp_settings).all_specs()) == registry_size

    def test_a_failing_provider_is_recorded_and_degrades(self, tmp_settings,
                                                         project: Path):
        from ai.provider import AIProviderError

        events: list = []
        agent, _, _ = _agent(tmp_settings, project, [
            AIProviderError("connection refused", category=FailureCategory.CONNECTION,
                            model="m", latency=0.5, retryable=True),
        ], events)
        agent.respond("complete the project and verify it")

        failures = agent.provider_failures
        assert failures, "a provider failure was not recorded"
        detail = failures[0]
        for field in ("category", "model", "latency", "retryable"):
            assert field in detail, f"{field} missing from {detail}"
        assert detail["category"] == "connection"
        assert detail["model"] == "m"
        assert detail["latency"] == pytest.approx(0.5)
        assert detail["retryable"] is True
        # A degradation is a real event, not just a recorded list entry.
        kinds = {kind for kind, _ in events}
        assert kinds & {"model_fallback", "model_offline_fallback"}, (
            f"no degradation event was emitted; saw {sorted(kinds)}")
