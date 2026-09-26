"""Step status must be derived from structured signals, never from wording.

Regression for a pre-existing violation of the ratified cross-layer rule that
"status must not be inferred from error-message wording".

``Agent._step_status`` used to classify a step as CANCELLED when the reason text
contained the phrase "Denied by user". A real user denial already sets the
structured signals ``cancelled=True`` and ``status="cancelled"`` in
``Executor``, which the earlier branch catches, so the text match was redundant
for production and only reachable through synthetic outcomes. Its actual effect
was harmful: any unrelated blocked operation whose message happened to read
"Denied by user" was reported as CANCELLED, inverting the terminal task state
(``TaskManager.final_status()`` returns "cancelled" instead of "failed").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.agent import Agent
from core.task_manager import StepStatus
from core.verification import VerificationState
from tests.test_p3_composition_audit import _Provider, _agent, _call, _registry, _response, _spec


def _outcome(**overrides):
    base = dict(
        allowed=False, executed=False, success=False, cancelled=False,
        status="blocked", reason="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _bare(settings):
    return _agent(settings, _Provider([]), _registry([]))


def test_real_denial_is_classified_from_the_structured_signal(tmp_settings):
    agent = _bare(tmp_settings)
    assert agent._step_status(_outcome(
        cancelled=True, status="cancelled", reason="Denied by user."
    )) is StepStatus.CANCELLED


def test_status_cancelled_alone_is_enough(tmp_settings):
    agent = _bare(tmp_settings)
    assert agent._step_status(_outcome(status="cancelled")) is StepStatus.CANCELLED


@pytest.mark.parametrize("reason", [
    "Denied by user.",
    "Denied by user. but actually a destructive-command policy block",
    "cancelled by policy",
    "the user was denied earlier",
])
def test_wording_alone_never_promotes_blocked_to_cancelled(tmp_settings, reason):
    agent = _bare(tmp_settings)
    assert agent._step_status(_outcome(reason=reason)) is StepStatus.BLOCKED


def test_genuinely_blocked_operation_stays_blocked(tmp_settings):
    agent = _bare(tmp_settings)
    assert agent._step_status(_outcome(
        status="blocked",
        reason="Blocked by destructive-command policy (blocked phrase: shutdown).",
    )) is StepStatus.BLOCKED


def test_executed_failures_are_unaffected(tmp_settings):
    agent = _bare(tmp_settings)
    assert agent._step_status(_outcome(
        allowed=True, executed=True, success=True, status="success"
    )) is StepStatus.DONE
    assert agent._step_status(_outcome(
        allowed=True, executed=True, success=False, status="error", reason="Denied by user."
    )) is StepStatus.FAILED


def test_real_denial_through_the_executor_still_ends_cancelled(tmp_settings):
    """The end-to-end structured denial path is unchanged by the fix."""
    invocations = []
    risky = _spec("project.risky", lambda **kw: (
        invocations.append(1), SimpleNamespace(success=True, output="ok", error="", exit_code=0,
                                              data=None, cancelled=False)
    )[1])
    risky.explicit_confirmation = True
    asked = []

    agent = _agent(
        tmp_settings,
        _Provider([_response(_call("project.risky"))]),
        _registry([risky]),
        confirmer=lambda tool, description: (asked.append(tool), "deny")[1],
    )

    agent.respond("please execute the project risky operation now")

    assert asked == ["project.risky"], "the confirmer must actually be consulted"
    assert invocations == [], "a denied tool must never execute"
    assert agent._tasks.state.value == "cancelled"
    assert agent._tasks.verification is not VerificationState.PASSED
