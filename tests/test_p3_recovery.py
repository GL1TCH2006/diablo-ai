"""M2.12 P3 Phase 3 - FailureObservation and unified RecoveryPolicy.

Covers the recovery decision layer in isolation (it is pure), the safety
properties that must hold no matter what the inputs say, and the three call
sites that were unified: ``_llm_loop``, ``_record_execution`` escalation and
``DebugLoop``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.recovery import (
    FailureCategory,
    FailureObservation,
    RecoveryAction,
    RecoveryDecision,
    RecoveryPolicy,
)
from core.task_manager import StepStatus
from core.verification import VerificationState


def _obs(**overrides) -> FailureObservation:
    """A plainly retryable in-request failure on a FAILED read-only step."""
    kwargs = {
        "category": FailureCategory.EXECUTION_FAILED,
        "task_id": "task-1",
        "run_id": "run-1",
        "step_id": "step-1",
        "tool": "python.run_script",
        "error_code": "error",
        "error_message": "ValueError: broken",
        "attempt": 1,
        "step_status": StepStatus.FAILED,
        "step_replayable": True,
    }
    kwargs.update(overrides)
    return FailureObservation(**kwargs)


# ==========================================================================
# FailureObservation: construction, optional fields, serialisation, secrets
# ==========================================================================


def test_observation_defaults_are_optional_and_safe():
    obs = FailureObservation()
    assert obs.category is FailureCategory.UNKNOWN
    assert obs.task_id == "" and obs.run_id == "" and obs.step_id == ""
    assert obs.step_status is None
    assert obs.cancelled is False
    assert obs.interrupted is False
    assert obs.verification_evidence == {}
    assert obs.expected_result is None
    # An unknown category is not retryable, so nothing defaults into recovery.
    assert obs.is_retryable is False


def test_observation_serialises_to_json():
    obs = _obs(
        verification_state=VerificationState.FAILED,
        verification_evidence={"exists": False},
        expected_result={"type": "output_contains", "value": "ok"},
        recovery_hint="previous attempt failed",
    )
    payload = obs.to_dict()
    json.dumps(payload)
    assert payload["category"] == "execution_failed"
    assert payload["step_status"] == "failed"
    assert payload["verification_state"] == "failed"
    assert payload["expected_result"]["value"] == "ok"
    assert payload["is_retryable"] is True
    json.loads(obs.to_json())


def test_observation_does_not_duplicate_task_record():
    """It carries decision inputs only, not the plan or git state."""
    payload = _obs().to_dict()
    for forbidden in ("goal", "steps", "plan", "commits", "snapshots", "diff", "changes"):
        assert forbidden not in payload


@pytest.mark.parametrize("secret_key", [
    "password", "passwd", "secret", "token", "apikey",
    "api_key", "authorization", "credential", "private_key",
])
def test_observation_redacts_secrets_in_evidence(secret_key):
    obs = _obs(verification_evidence={secret_key: "hunter2", "path": "C:/a.png"})
    assert obs.verification_evidence[secret_key] == "<redacted>"
    assert obs.verification_evidence["path"] == "C:/a.png"
    assert "hunter2" not in obs.to_json()


def test_observation_redacts_secrets_in_free_text():
    obs = _obs(error_message="failed with api_key=abcd1234", recovery_hint="token: shhh")
    assert "abcd1234" not in obs.error_message
    assert "shhh" not in obs.recovery_hint
    assert "abcd1234" not in obs.to_json()
    assert "shhh" not in obs.to_json()


def test_observation_redacts_nested_secrets():
    obs = _obs(verification_evidence={"outer": {"password": "p", "keep": "visible"}})
    assert obs.verification_evidence["outer"]["password"] == "<redacted>"
    assert obs.verification_evidence["outer"]["keep"] == "visible"


def test_observation_tolerates_unserialisable_and_non_mapping_evidence():
    assert _obs(verification_evidence=None).verification_evidence == {}
    assert _obs(verification_evidence=[1, 2]).verification_evidence == {}
    assert _obs(verification_evidence={"blob": object()}).verification_evidence == {
        "__unserialisable__": True
    }
    assert _obs(expected_result="not-a-mapping").expected_result == "not-a-mapping"
    json.dumps(_obs(verification_evidence={"blob": object()}).to_dict())


def test_explicit_retryable_overrides_the_category_prior():
    assert _obs(category=FailureCategory.PERMISSION_DENIED, retryable=True).is_retryable is True
    assert _obs(category=FailureCategory.EXECUTION_FAILED, retryable=False).is_retryable is False


# ==========================================================================
# RecoveryPolicy: retry, limits, escalation, skip, abort
# ==========================================================================


def test_retryable_failure_with_budget_retries():
    decision = RecoveryPolicy(max_retries=3).decide(_obs())
    assert decision.action is RecoveryAction.RETRY_STEP
    assert decision.should_retry is True
    assert "budget remaining" in decision.reason


def test_retry_stops_at_the_limit_and_never_exceeds_it():
    policy = RecoveryPolicy(max_retries=2, max_escalations=0)
    assert policy.decide(_obs(recovery_attempts=0)).action is RecoveryAction.RETRY_STEP
    assert policy.decide(_obs(recovery_attempts=1)).action is RecoveryAction.RETRY_STEP
    assert policy.decide(_obs(recovery_attempts=2)).action is RecoveryAction.ABORT
    # Far beyond the limit: still abort, never an unbounded retry.
    assert policy.decide(_obs(recovery_attempts=99)).action is RecoveryAction.ABORT


def test_recovery_is_bounded_across_every_action():
    """No input can produce an unbounded or escalating-forever decision."""
    policy = RecoveryPolicy(max_retries=1, max_escalations=1)
    seen = []
    for attempts in range(10):
        for escalations in range(5):
            action = policy.decide(
                _obs(recovery_attempts=attempts, escalations_used=escalations)
            ).action
            seen.append(action)
    # Retry depends only on the retry budget, escalation only on its own.
    # Both are strictly bounded regardless of how large the other counter grows.
    assert seen.count(RecoveryAction.RETRY_STEP) == 5      # attempts == 0
    assert seen.count(RecoveryAction.ESCALATE_MODEL) == 9  # attempts >= 1, esc == 0
    assert seen.count(RecoveryAction.ABORT) == 36           # attempts >= 1, esc >= 1
    assert len(seen) == 50


def test_escalation_is_offered_once_retries_are_spent():
    policy = RecoveryPolicy(max_retries=2, max_escalations=2)
    decision = policy.decide(_obs(recovery_attempts=2, escalations_used=0))
    assert decision.action is RecoveryAction.ESCALATE_MODEL
    assert decision.escalations_used == 0
    assert policy.decide(_obs(recovery_attempts=2, escalations_used=1)).action is (
        RecoveryAction.ESCALATE_MODEL
    )
    assert policy.decide(_obs(recovery_attempts=2, escalations_used=2)).action is RecoveryAction.ABORT


def test_escalation_budget_of_zero_never_escalates():
    policy = RecoveryPolicy(max_retries=0, max_escalations=0)
    assert policy.decide(_obs(recovery_attempts=0)).action is RecoveryAction.ABORT


def test_rerun_verify_when_verification_failed_with_evidence():
    policy = RecoveryPolicy(max_retries=3, max_rerun_verify=1)
    decision = policy.decide(_obs(
        category=FailureCategory.VERIFICATION_FAILED,
        verification_state=VerificationState.FAILED,
        verification_evidence={"exists": False},
    ))
    assert decision.action is RecoveryAction.RERUN_VERIFY


def test_rerun_verify_budget_is_bounded():
    policy = RecoveryPolicy(max_retries=3, max_rerun_verify=1)
    decision = policy.decide(_obs(
        category=FailureCategory.VERIFICATION_FAILED,
        verification_state=VerificationState.FAILED,
        verification_evidence={"exists": False},
        recovery_attempts=1,
    ))
    assert decision.action is RecoveryAction.ABORT
    assert "verification re-run budget" in decision.reason


def test_rerun_verify_requires_recorded_evidence():
    policy = RecoveryPolicy(max_retries=3, max_rerun_verify=1)
    decision = policy.decide(_obs(
        category=FailureCategory.VERIFICATION_FAILED,
        verification_state=VerificationState.FAILED,
        verification_evidence={},
    ))
    # No evidence to re-check, so it falls through to the normal retry path.
    assert decision.action is RecoveryAction.RETRY_STEP


def test_skip_is_disabled_by_default():
    policy = RecoveryPolicy(max_retries=0, max_escalations=0)
    assert policy.decide(_obs(recovery_attempts=5)).action is RecoveryAction.ABORT


def test_skip_only_applies_once_retries_are_spent():
    policy = RecoveryPolicy(max_retries=3, allow_skip=True)
    # With budget, retry is preferred over skipping.
    assert policy.decide(_obs(recovery_attempts=0)).action is RecoveryAction.RETRY_STEP
    assert policy.decide(_obs(recovery_attempts=3)).action is RecoveryAction.SKIP_STEP


def test_non_retryable_category_aborts():
    for category in (
        FailureCategory.PERMISSION_DENIED,
        FailureCategory.INVALID_REQUEST,
        FailureCategory.NOT_EXECUTED,
    ):
        decision = RecoveryPolicy(max_retries=5).decide(_obs(category=category))
        assert decision.action is RecoveryAction.ABORT
        assert "not retryable" in decision.reason


def test_policy_is_pure_and_repeatable():
    policy = RecoveryPolicy(max_retries=2)
    obs = _obs()
    first = policy.decide(obs)
    second = policy.decide(obs)
    assert first == second
    # The observation is frozen: deciding does not mutate it.
    assert obs.recovery_attempts == 0
    with pytest.raises(Exception):
        obs.category = FailureCategory.UNKNOWN


# ==========================================================================
# cancellation dominates
# ==========================================================================


def test_cancellation_dominates_every_other_signal():
    """Even a perfectly retryable failure with budget does not recover."""
    policy = RecoveryPolicy(max_retries=5, max_escalations=5, allow_skip=True)
    decision = policy.decide(_obs(
        cancelled=True,
        recovery_attempts=0,
        step_status=StepStatus.FAILED,
    ))
    assert decision.action is RecoveryAction.NONE
    assert "cancellation" in decision.reason


def test_cancellation_category_alone_dominates():
    policy = RecoveryPolicy(max_retries=5)
    decision = policy.decide(_obs(category=FailureCategory.CANCELLED))
    assert decision.action is RecoveryAction.NONE


def test_cancellation_prevents_escalation_even_when_retries_are_spent():
    policy = RecoveryPolicy(max_retries=1, max_escalations=5)
    assert policy.decide(_obs(cancelled=True, recovery_attempts=9)).action is (
        RecoveryAction.NONE
    )


def test_cancellation_prevents_skip():
    policy = RecoveryPolicy(max_retries=0, allow_skip=True, max_escalations=5)
    assert policy.decide(_obs(cancelled=True, recovery_attempts=9)).action is (
        RecoveryAction.NONE
    )


# ==========================================================================
# durable plan integration
# ==========================================================================


def test_done_is_terminal():
    assert RecoveryPolicy().decide(_obs(step_status=StepStatus.DONE)).action is (
        RecoveryAction.NONE
    )


def test_cancelled_step_is_terminal():
    assert RecoveryPolicy().decide(_obs(step_status=StepStatus.CANCELLED)).action is (
        RecoveryAction.NONE
    )


def test_skipped_step_is_terminal():
    assert RecoveryPolicy().decide(_obs(step_status=StepStatus.SKIPPED)).action is (
        RecoveryAction.NONE
    )


def test_blocked_step_aborts_with_no_retry_edge():
    decision = RecoveryPolicy(max_retries=5).decide(_obs(step_status=StepStatus.BLOCKED))
    assert decision.action is RecoveryAction.ABORT
    assert "no legal retry edge" in decision.reason


def test_running_step_is_not_replayed():
    decision = RecoveryPolicy(max_retries=5).decide(_obs(step_status=StepStatus.RUNNING))
    assert decision.action is RecoveryAction.ABORT


def test_pending_step_has_nothing_to_recover():
    assert RecoveryPolicy().decide(_obs(step_status=StepStatus.PENDING)).action is (
        RecoveryAction.NONE
    )


def test_interrupted_step_is_never_silently_replayed():
    """The P2 contract, enforced regardless of budget or category."""
    for attempts in range(5):
        for category in FailureCategory:
            if category is FailureCategory.CANCELLED:
                continue
            decision = RecoveryPolicy(max_retries=9, max_escalations=9).decide(
                _obs(
                    category=category,
                    interrupted=True,
                    step_status=StepStatus.FAILED,
                    recovery_attempts=attempts,
                )
            )
            assert decision.action is RecoveryAction.ABORT
            assert "interrupted" in decision.reason


def test_interrupted_running_step_is_never_replayed():
    decision = RecoveryPolicy().decide(_obs(
        step_status=StepStatus.RUNNING, interrupted=True
    ))
    assert decision.action is RecoveryAction.ABORT


def test_retry_uses_the_legal_failed_to_pending_edge():
    from core.task_manager import step_transition_allowed

    assert step_transition_allowed(StepStatus.FAILED, StepStatus.PENDING) is True
    policy = RecoveryPolicy(max_retries=3)
    assert policy.decide(_obs(step_status=StepStatus.FAILED)).action is (
        RecoveryAction.RETRY_STEP
    )


def test_policy_does_not_invent_a_second_state_machine():
    """Every action it can return is expressible in the existing vocabulary."""
    assert {a.value for a in RecoveryAction} == {
        "none", "retry_step", "rerun_verify", "escalate_model", "skip_step", "abort",
    }
    # The policy never returns a step status; the plan owns transitions.
    assert not hasattr(RecoveryPolicy(), "step_status")


# ==========================================================================
# recovery_hint and expected_result are inputs, not commands
# ==========================================================================


def test_recovery_hint_saying_retry_does_not_override_limits():
    policy = RecoveryPolicy(max_retries=1, max_escalations=0)
    assert policy.decide(_obs(recovery_attempts=0, recovery_hint="retry")).action is (
        RecoveryAction.RETRY_STEP
    )
    decision = policy.decide(_obs(recovery_attempts=5, recovery_hint="retry again please"))
    assert decision.action is RecoveryAction.ABORT


def test_recovery_hint_cannot_overrule_cancellation():
    policy = RecoveryPolicy(max_retries=5)
    decision = policy.decide(_obs(cancelled=True, recovery_hint="retry immediately"))
    assert decision.action is RecoveryAction.NONE


def test_recovery_hint_cannot_overrule_a_non_retryable_category():
    policy = RecoveryPolicy(max_retries=5)
    decision = policy.decide(_obs(
        category=FailureCategory.PERMISSION_DENIED, recovery_hint="just retry"
    ))
    assert decision.action is RecoveryAction.ABORT


def test_recovery_hint_cannot_replay_an_interrupted_step():
    decision = RecoveryPolicy(max_retries=9).decide(_obs(
        interrupted=True, recovery_hint="retry"
    ))
    assert decision.action is RecoveryAction.ABORT


def test_expected_result_is_never_verification_evidence():
    """A satisfied expectation does not upgrade anything."""
    policy = RecoveryPolicy(max_retries=3)
    observation = _obs(
        expected_result={"type": "output_contains", "value": "Hello World"},
    )
    assert observation.expected_result is not None
    # The policy does not treat it as evidence...
    assert observation.has_verification_evidence is False
    # ...and a verification failure with only an expected_result still aborts
    # rather than claiming a re-check is possible.
    decision = policy.decide(_obs(
        category=FailureCategory.VERIFICATION_FAILED,
        verification_state=VerificationState.FAILED,
        verification_evidence={},
        expected_result={"type": "output_contains", "value": "Hello World"},
    ))
    assert decision.action is RecoveryAction.RETRY_STEP


# ==========================================================================
# integration: the three unified call sites
# ==========================================================================


def _agent_stub():
    agent = SimpleNamespace()
    agent._settings = SimpleNamespace(
        max_recovery_attempts=2,
        models_max_escalations=3,
        models_max_attempts_before_escalate=1,
    )
    return agent


def _agent_harness():
    """A minimal object that runs the *real* Agent recovery methods.

    Uses the genuine unbound implementations rather than reimplementing them,
    so these tests exercise production code, not a copy.
    """
    from core.agent import Agent
    from core.task_manager import TaskManager

    class Harness:
        _recovery_policy = Agent._recovery_policy
        _failure_observation = Agent._failure_observation
        _apply_recovery_policy = Agent._apply_recovery_policy

        def __init__(self):
            self._settings = SimpleNamespace(
                max_recovery_attempts=2,
                models_max_escalations=3,
                models_max_attempts_before_escalate=1,
            )
            self._tasks = TaskManager()
            self._tasks.recovery_attempts = 9
            self._escalations_used = 0
            self._router_verify_ok = True
            self._emit_lifecycle = lambda *a, **k: None

    return Harness()


def test_agent_policy_reuses_existing_settings_and_stays_within_router_ceiling():
    from core.agent import Agent

    agent = _agent_stub()
    policy = Agent._recovery_policy(agent)
    assert policy.max_retries == 2
    # Escalation ceiling is the stricter of the recovery and router budgets.
    assert policy.max_escalations == 2


def test_agent_policy_tolerates_missing_settings():
    from core.agent import Agent

    agent = SimpleNamespace(_settings=SimpleNamespace())
    policy = Agent._recovery_policy(agent)
    assert policy.max_retries >= 0
    assert policy.max_escalations >= 0


def test_escalation_decision_flips_router_verify_ok_only():
    agent = _agent_harness()
    agent._router_verify_ok = True

    outcome = SimpleNamespace(
        success=False, executed=True, allowed=True, cancelled=False,
        error="boom", reason="failed", status="error", exit_code=1, data=None,
        verification=None,
    )
    decision = agent._apply_recovery_policy(
        "python.run_script", outcome, StepStatus.FAILED, None, phase="test"
    )
    assert decision.action is RecoveryAction.ESCALATE_MODEL
    # Only the router's existing input changed; the policy chose no model.
    assert agent._router_verify_ok is False


def test_cancelled_outcome_never_escalates_through_the_policy():
    agent = _agent_harness()
    agent._router_verify_ok = True

    outcome = SimpleNamespace(
        success=False, executed=True, allowed=True, cancelled=True,
        error="", reason="cancelled", status="cancelled", exit_code=None, data=None,
        verification=None,
    )
    decision = agent._apply_recovery_policy(
        "python.run_script", outcome, StepStatus.CANCELLED, None, phase="test"
    )
    assert decision.action is RecoveryAction.NONE
    assert agent._router_verify_ok is True


def test_policy_decision_is_emitted_as_a_lifecycle_event():
    agent = _agent_harness()
    agent._tasks.recovery_attempts = 0
    events = []
    agent._emit_lifecycle = lambda kind, **kw: events.append((kind, kw))

    outcome = SimpleNamespace(
        success=False, executed=True, allowed=True, cancelled=False,
        error="boom", reason="failed", status="error", exit_code=1, data=None,
        verification=None,
    )
    agent._apply_recovery_policy(
        "python.run_script", outcome, StepStatus.FAILED, None, phase="test"
    )
    kinds = [kind for kind, _ in events]
    assert "recovery_decided" in kinds
    payload = dict(events)["recovery_decided"]
    assert payload["action"] == RecoveryAction.RETRY_STEP.value
    assert payload["phase"] == "test"
    assert payload["tool"] == "python.run_script"


def test_failure_category_classification_uses_status_not_error_wording():
    from core.agent import _failure_category

    def outcome(**kw):
        base = dict(
            success=False, executed=True, allowed=True, cancelled=False,
            error="", reason="", status="", exit_code=1, data=None, verification=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    assert _failure_category(outcome(cancelled=True), StepStatus.CANCELLED) is (
        FailureCategory.CANCELLED
    )
    assert _failure_category(outcome(executed=False), StepStatus.BLOCKED) is (
        FailureCategory.NOT_EXECUTED
    )
    assert _failure_category(outcome(allowed=False), StepStatus.FAILED) is (
        FailureCategory.PERMISSION_DENIED
    )
    assert _failure_category(outcome(status="invalid_request"), StepStatus.FAILED) is (
        FailureCategory.INVALID_REQUEST
    )
    assert _failure_category(outcome(status="unavailable"), StepStatus.FAILED) is (
        FailureCategory.UNAVAILABLE
    )
    assert _failure_category(outcome(), StepStatus.FAILED) is (
        FailureCategory.EXECUTION_FAILED
    )


def test_failure_category_never_infers_status_from_error_text():
    """An error message that merely *mentions* a state must not change it."""
    from core.agent import _failure_category

    outcome = SimpleNamespace(
        success=False, executed=True, allowed=True, cancelled=False,
        # Wording that would fool a text-matching classifier:
        error="Permission denied: invalid_request detected in output",
        reason="AssertionError", status="error", exit_code=1, data=None,
        verification=None,
    )
    assert _failure_category(outcome, StepStatus.FAILED) is FailureCategory.EXECUTION_FAILED


def test_debug_loop_recovery_is_bounded_by_the_policy(tmp_settings):
    from core.debug_loop import DebugLoop

    loop = DebugLoop(lambda *a, **k: None, None, tmp_settings)
    policy = loop._recovery_policy()
    # DebugLoop keeps its own stricter default and owns no escalation.
    assert policy.max_escalations == 0
    assert policy.allow_skip is False


def test_debug_loop_stops_recovering_when_cancelled(tmp_settings):
    from core.debug_loop import DebugLoop

    loop = DebugLoop(lambda *a, **k: None, None, tmp_settings)
    report = SimpleNamespace(
        cancelled=True,
        verification_state="failed",
        final_message="Verification cancelled.",
        verification_evidence={"command": "pytest -q", "cancelled": True},
    )
    observation = loop._recovery_observation(report, 0)
    assert observation.cancelled is True
    assert loop._recovery_policy().decide(observation).action is RecoveryAction.NONE


def test_debug_loop_observation_carries_auto_verify_evidence(tmp_settings):
    from core.debug_loop import DebugLoop

    loop = DebugLoop(lambda *a, **k: None, None, tmp_settings)
    report = SimpleNamespace(
        cancelled=False,
        verification_state="failed",
        final_message="still failing",
        verification_evidence={"command": "pytest -q", "exit_code": 1},
    )
    observation = loop._recovery_observation(report, 0)
    assert observation.category is FailureCategory.VERIFICATION_FAILED
    assert observation.verification_state is VerificationState.FAILED
    assert observation.has_verification_evidence is True
    # DebugLoop keeps no durable plan, so it gates on no step state.
    assert observation.step_status is None


def test_debug_loop_report_carries_verification_evidence():
    from core.debug_loop import DebugReport

    report = DebugReport(issue="x")
    assert report.verification_evidence == {}
    # to_context must not leak the evidence payload into the model prompt.
    assert "verification_evidence" not in report.to_context()


def test_recovery_decision_serialises():
    decision = RecoveryDecision(
        action=RecoveryAction.ESCALATE_MODEL, reason="why", retries_used=2, escalations_used=1
    )
    json.dumps(decision.to_dict())
    assert decision.to_dict()["action"] == "escalate_model"
    assert decision.should_abort is False
    assert RecoveryDecision(action=RecoveryAction.ABORT).should_abort is True
