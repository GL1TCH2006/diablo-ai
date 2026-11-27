"""M2.12 P3 Phase 3 - FailureObservation and unified RecoveryPolicy.

Phase 1 built the verification ledger and Phase 2 made it the authoritative
adjudication boundary. Phase 3 unifies *recovery*: previously three
independent mechanisms each decided what to do after a failure.

1. ``Agent._llm_loop`` bounded retry via ``max_recovery_attempts``
2. ``Agent._record_execution`` model escalation via
   ``models_max_attempts_before_escalate``
3. ``DebugLoop`` signature-dedup recovery

All three now route their **final** recovery decision through the single
:class:`RecoveryPolicy`. The policy is a decision layer only: it is pure, has no
side effects, cannot recurse, and never dispatches anything. The Executor and
TaskManager remain solely responsible for enforcing state transitions and for
gating permissions.

What a FailureObservation is
---------------------------
One immutable, redacted record of "what went wrong, and what is known about it
at the moment recovery is being considered". It captures recovery *decision
inputs*. It deliberately does **not** duplicate ``TaskRecord`` / ``TaskRecord
.plan``: it carries no goal, no full step list, no git state and no snapshots.
It references the authoritative ids and carries the bounded facts a decision
needs.

It is safe to serialise and log. Every free-text and structured field is passed
through ``core.logging_setup.redact`` on construction, so a token, password or
credential appearing in an error message or in verification evidence is replaced
with ``<redacted>`` before the observation can be stored or logged.

Inputs, not commands
--------------------
``recovery_hint`` and ``expected_result`` are **inputs to** the decision:

* A ``recovery_hint`` saying "retry" never overrides a retry limit, a terminal
  plan state, a non-retryable category or cancellation. It is carried for
  diagnostics and is never parsed for intent.
* An ``expected_result`` is never verification evidence. The ledger
  (:mod:`core.verification_ledger`) is the only thing that adjudicates a
  verdict, and a satisfied expectation does not upgrade a failure.

Recovery precedence
-------------------
Evaluated in this order, first match wins. Derived from the ratified contracts
rather than invented:

1. **Cancellation dominates.** If cancellation is active the answer is always
   :attr:`RecoveryAction.NONE`. No retry, no escalation, no verification rerun,
   no skip into another side effect, no replay of an interrupted
   side-effecting step. This matches the S2.9 rule that CANCELLED is terminal.
2. **Terminal plan state with no legal retry edge.** ``DONE``, ``CANCELLED``,
   ``BLOCKED`` and ``SKIPPED`` are terminal; ``RUNNING`` may only move to
   ``DONE``/``FAILED``/``CANCELLED``. None of them offers a retry edge, so the
   policy answers :attr:`RecoveryAction.NONE` and lets the caller report the
   terminal outcome. ``PENDING`` means nothing has failed yet.
3. **Interrupted step.** A step left ``RUNNING`` by a process death may or may
   not have applied its side effect, and the P2 contract requires an explicit
   resume decision. Checked before the retryable test, so no budget can
   authorise a silent replay. -> :attr:`RecoveryAction.ABORT`.
4. **Non-retryable category.** Permission denial, invalid request and
   cancellation-adjacent categories cannot be fixed by trying again.
   -> :attr:`RecoveryAction.ABORT`.
5. **Verification failure.** When verification actively *failed* and the ledger
   holds evidence, re-running verification is cheaper and safer than re-running
   the step, and the bounded ``max_rerun_verify`` budget is respected.
   -> :attr:`RecoveryAction.RERUN_VERIFY`, else fall through.
6. **Retry.** The failure is retryable, a legal ``FAILED -> PENDING`` edge
   exists and the budget allows. Retry is preferred over skipping and over
   burning an escalation because it is the cheapest correct action. Every retry
   re-enters the normal dispatch path, so the whole safety stack re-applies.
   -> :attr:`RecoveryAction.RETRY_STEP`.
7. **Legal skip.** Only once retries are spent, and only when
   :attr:`RecoveryPolicy.allow_skip` is explicitly enabled. Disabled by
   default: the durable plan cannot know whether a skipped step was required by
   the goal, so a skip is never inferred.
   -> :attr:`RecoveryAction.SKIP_STEP`.
8. **Escalate, then abort.** Once retries are spent, escalate the model while
   the escalation budget lasts, then abort.
   -> :attr:`RecoveryAction.ESCALATE_MODEL` or :attr:`RecoveryAction.ABORT`.

Note what is deliberately *not* a rule: ``PlanStepRecord.is_replayable`` does
not veto an in-request retry. It answers the resume question ("is re-dispatching
free of side effects?"), which is the right question for
``Agent.resume()`` and the wrong one for a step that ran to completion and
reported a definite failure. See :attr:`FailureObservation.step_replayable`.

Nothing bypasses the safety stack. The policy cannot grant permission, cannot
widen workspace roots, cannot touch branch policy or destructive-command
restrictions, and cannot move a step the state machine forbids. It also does not
choose a model: :attr:`RecoveryAction.ESCALATE_MODEL` is a signal consumed by
the existing ``ModelRouter``/``_select_model`` path, which keeps its own
``models_max_escalations`` bound. There is deliberately no second model router.

Known limitation, unchanged by this phase
-----------------------------------------
Some S2.8 workflow calls cannot interrupt an already-running Blender child
because supervision is not bound directly to the task token. Phase 3 does not
fix that. It only guarantees that where interruption *is* possible,
cancellation dominates every recovery action.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging_setup import redact
from core.task_manager import StepStatus, step_transition_allowed
from core.verification import VerificationState

__all__ = [
    "FailureCategory",
    "FailureObservation",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryPolicy",
]


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class FailureCategory(str, Enum):
    """What kind of failure this is, and whether retrying can ever help.

    The vocabulary deliberately reuses the ratified ``CapabilityStatus`` and
    ``StepStatus`` meanings rather than inventing a parallel taxonomy:

    * ``CANCELLED`` / ``PERMISSION_DENIED`` / ``UNAVAILABLE`` /
      ``INVALID_REQUEST`` mirror ``CapabilityStatus`` values.
    * ``NOT_EXECUTED`` mirrors a ``BLOCKED`` step that never ran.
    * ``VERIFICATION_FAILED`` and ``EXECUTION_FAILED`` mirror the verification
      and step-status layers respectively.
    """

    CANCELLED = "cancelled"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    NOT_EXECUTED = "not_executed"
    VERIFICATION_FAILED = "verification_failed"
    EXECUTION_FAILED = "execution_failed"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        """Whether repeating the same operation could plausibly succeed.

        This is a *category-level* prior only. The policy still requires a
        replayable step, a legal state transition and budget headroom, and an
        explicit ``retryable=False`` on the observation always wins.
        """
        return self in {
            FailureCategory.EXECUTION_FAILED,
            FailureCategory.UNAVAILABLE,
            FailureCategory.VERIFICATION_FAILED,
        }

    @property
    def terminal(self) -> bool:
        """Whether this category is an end state rather than a setback."""
        return self in {
            FailureCategory.CANCELLED,
            FailureCategory.PERMISSION_DENIED,
            FailureCategory.INVALID_REQUEST,
        }


class RecoveryAction(str, Enum):
    """The single vocabulary of recovery decisions."""

    NONE = "none"
    RETRY_STEP = "retry_step"
    RERUN_VERIFY = "rerun_verify"
    ESCALATE_MODEL = "escalate_model"
    SKIP_STEP = "skip_step"
    ABORT = "abort"


@dataclass(frozen=True)
class FailureObservation:
    """One immutable, redacted record of a failure, for recovery decisions.

    Carries decision inputs only. It is not a second copy of ``TaskRecord``:
    there is no goal, no plan listing, no git state and no snapshot data here.
    """

    category: FailureCategory = FailureCategory.UNKNOWN
    task_id: str = ""
    run_id: str = ""
    step_id: str = ""
    tool: str = ""
    capability: str = ""
    error_code: str = ""
    error_type: str = ""
    error_message: str = ""
    attempt: int = 0
    recovery_attempts: int = 0
    escalations_used: int = 0
    #: M2.15.2: recovery for this task is already spent, terminally.
    #:
    #: ``recovery_attempts`` and ``escalations_used`` are counters, and counters
    #: alone only imply exhaustion relative to *the budgets they are compared
    #: against*. If a restarted process rebuilt those counters from zero, or if a
    #: raised budget made spent counters look affordable again, an exhausted
    #: recovery path would look fresh. This flag is the durable statement that the
    #: path already terminated, and it dominates every budget comparison below.
    recovery_exhausted: bool = False
    verification_state: VerificationState = VerificationState.NOT_RUN
    verification_evidence: Mapping[str, Any] = field(default_factory=dict)
    #: Explicit override. ``None`` means "use the category prior".
    retryable: bool | None = None
    cancelled: bool = False
    recovery_hint: str = ""
    expected_result: Mapping[str, Any] | None = None
    #: Durable-plan facts. The plan owns this knowledge; the policy only reads it.
    step_status: StepStatus | None = None
    #: ``PlanStepRecord.is_replayable``. Carried for diagnostics and for the
    #: resume path (``Agent.resume()``, a later milestone).
    #:
    #: It is deliberately **not** used to veto an in-request bounded retry. The
    #: two answer different questions: ``is_replayable`` means "re-dispatching
    #: this step is free of side effects", which is the right question when
    #: *resuming* persisted work whose outcome is unknown. An in-request retry
    #: follows a step that ran to completion and reported a definite failure, so
    #: re-dispatching it is a fresh attempt, not a silent replay - and it
    #: re-enters the normal dispatch path, so PermissionManager, workspace
    #: roots, branch policy and destructive-command restrictions all re-apply on
    #: every attempt. What must never be replayed is an *interrupted* step, and
    #: that is checked separately and unconditionally.
    step_replayable: bool = True
    interrupted: bool = False
    observed_at: str = ""

    def __post_init__(self) -> None:
        # Redact on construction so an observation is safe to log the moment it
        # exists. Frozen dataclass: write through object.__setattr__.
        for name in ("error_message", "recovery_hint", "tool", "capability", "error_code", "error_type"):
            object.__setattr__(self, name, str(redact(str(getattr(self, name) or ""))))
        object.__setattr__(self, "verification_evidence", _safe_redacted(self.verification_evidence))
        expected = self.expected_result
        object.__setattr__(
            self,
            "expected_result",
            _safe_redacted(expected) if isinstance(expected, Mapping) else expected,
        )

    # --------------------------------------------------------------- helpers

    @property
    def is_retryable(self) -> bool:
        """Final retryability: an explicit override beats the category prior."""
        if self.retryable is not None:
            return bool(self.retryable)
        return self.category.retryable

    @property
    def has_verification_evidence(self) -> bool:
        return bool(self.verification_evidence)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form. Safe to persist or log."""
        state = self.verification_state
        return {
            "category": self.category.value,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool": self.tool,
            "capability": self.capability,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "attempt": int(self.attempt or 0),
"recovery_attempts": int(self.recovery_attempts or 0),
        "escalations_used": int(self.escalations_used or 0),
        "recovery_exhausted": bool(self.recovery_exhausted),
            "verification_state": state.value if isinstance(state, VerificationState) else str(state),
            "verification_evidence": dict(self.verification_evidence or {}),
            "retryable": self.retryable,
            "is_retryable": self.is_retryable,
            "cancelled": bool(self.cancelled),
            "recovery_hint": self.recovery_hint,
            "expected_result": dict(self.expected_result) if isinstance(self.expected_result, Mapping) else None,
            "step_status": self.step_status.value if isinstance(self.step_status, StepStatus) else self.step_status,
            "step_replayable": bool(self.step_replayable),
            "interrupted": bool(self.interrupted),
            "observed_at": self.observed_at or _now(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


def _safe_redacted(value: Any) -> dict[str, Any]:
    """Redact and coerce a mapping into a JSON-safe dict. Never raises."""
    if not isinstance(value, Mapping):
        return {}
    try:
        redacted = redact(dict(value))
    except Exception:  # pragma: no cover - defensive
        return {}
    try:
        json.dumps(redacted)
    except (TypeError, ValueError):
        return {"__unserialisable__": True}
    return redacted


@dataclass(frozen=True)
class RecoveryDecision:
    """The policy's answer, plus the reason it is the answer."""

    action: RecoveryAction = RecoveryAction.NONE
    reason: str = ""
    #: Retries already spent when the decision was made, for diagnostics.
    retries_used: int = 0
    escalations_used: int = 0

    @property
    def should_retry(self) -> bool:
        return self.action is RecoveryAction.RETRY_STEP

    @property
    def should_abort(self) -> bool:
        return self.action is RecoveryAction.ABORT

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "retries_used": int(self.retries_used or 0),
            "escalations_used": int(self.escalations_used or 0),
        }


@dataclass(frozen=True)
class RecoveryPolicy:
    """The one authoritative recovery decision layer.

    Pure and side-effect free: it inspects a :class:`FailureObservation` and
    returns a :class:`RecoveryDecision`. It dispatches nothing, mutates nothing,
    cannot recurse into itself, and cannot grant permission or move a step the
    durable-plan state machine forbids.
    """

    #: Bounded retries for a single failing step. There is no unbounded path:
    #: once this is spent the policy escalates and then aborts.
    max_retries: int = 3
    #: Bounded verification re-runs. Mirrors the need to re-check a verdict
    #: without re-executing the step.
    max_rerun_verify: int = 1
    #: Bounded model escalations. The ModelRouter keeps its own
    #: ``models_max_escalations`` bound; this is a second, independent ceiling
    #: so recovery cannot drive unbounded escalation.
    max_escalations: int = 2
    #: Skipping is never inferred. The durable plan cannot know whether a
    #: skipped step was required by the goal, so this is off unless enabled.
    allow_skip: bool = False

    def decide(self, observation: FailureObservation) -> RecoveryDecision:
        """Return the single recovery decision for ``observation``."""
        retries_used = int(observation.recovery_attempts or 0)
        escalations_used = int(observation.escalations_used or 0)

        def answer(action: RecoveryAction, reason: str) -> RecoveryDecision:
            return RecoveryDecision(
                action=action,
                reason=reason,
                retries_used=retries_used,
                escalations_used=escalations_used,
            )

        # 1. Cancellation dominates everything, including escalation.
        if observation.cancelled or observation.category is FailureCategory.CANCELLED:
            return answer(
                RecoveryAction.NONE,
                "cancellation is active; cancellation dominates recovery",
            )

        # 1b. M2.15.2: durable exhaustion dominates every budget below. Checked
        # before the counters because a raised ``max_retries`` must not make a
        # terminally-spent recovery path look affordable again after a restart.
        if observation.recovery_exhausted:
            return answer(
                RecoveryAction.ABORT,
                "recovery for this task was already exhausted and exhausted "
                "state was restored from durable task state",
            )

        # 2/3. Plan state. A terminal state, RUNNING, or PENDING offers no
        # legal retry edge, so the policy does not pretend one exists.
        status = observation.step_status
        if status is not None:
            if status is StepStatus.CANCELLED:
                return answer(RecoveryAction.NONE, "step is CANCELLED, which is terminal")
            if status is StepStatus.DONE:
                return answer(RecoveryAction.NONE, "step is DONE, which is terminal")
            if status is StepStatus.SKIPPED:
                return answer(RecoveryAction.NONE, "step is SKIPPED, which is terminal")
            if status is StepStatus.BLOCKED:
                return answer(RecoveryAction.ABORT, "step is BLOCKED and has no legal retry edge")
            if status is StepStatus.RUNNING:
                return answer(RecoveryAction.ABORT, "step is RUNNING and has no legal retry edge")
            if status is StepStatus.PENDING:
                return answer(RecoveryAction.NONE, "step has not run yet; nothing to recover")

        # 3. Interrupted steps are never silently replayed, in any status. The
        # P2 contract is explicit that a step left RUNNING by a process death
        # may have applied a partial effect and only an explicit resume decision
        # may resolve that. This is deliberately checked before the retryable
        # test, so no budget can authorise a silent replay.
        if observation.interrupted:
            return answer(
                RecoveryAction.ABORT,
                "step was interrupted mid-run; replaying it needs an explicit resume decision",
            )

        # 4. Non-retryable categories cannot be fixed by trying again.
        if not observation.is_retryable:
            return answer(
                RecoveryAction.ABORT,
                f"failure category {observation.category.value} is not retryable",
            )

        # A retry additionally needs a legal FAILED -> PENDING edge.
        if status is StepStatus.FAILED and not step_transition_allowed(
            StepStatus.FAILED, StepStatus.PENDING
        ):  # pragma: no cover - guards a future state-machine change
            return answer(RecoveryAction.ABORT, "no legal FAILED -> PENDING retry edge")

        # 6. An active verification failure is cheaper to re-check than to
        # re-execute, provided the ledger actually holds evidence and the
        # verification budget allows.
        if observation.category is FailureCategory.VERIFICATION_FAILED:
            if observation.verification_state is VerificationState.FAILED and observation.has_verification_evidence:
                if retries_used < max(1, int(self.max_rerun_verify)):
                    return answer(
                        RecoveryAction.RERUN_VERIFY,
                        "verification failed with recorded evidence; re-checking is cheaper than re-executing",
                    )
                return answer(
                    RecoveryAction.ABORT,
                    "verification failed and the verification re-run budget is spent",
                )

        # 7. Retry, while the budget allows. This is the cheapest correct action
        # for a retryable, replayable, legally-transitionable failure, so it is
        # preferred over skipping and over burning an escalation.
        if retries_used < max(0, int(self.max_retries)):
            return answer(
                RecoveryAction.RETRY_STEP,
                f"retryable failure with budget remaining ({retries_used}/{self.max_retries})",
            )

        # 8. Legal skip, only when explicitly enabled. Retries are spent by this
        # point, so this is the only remaining way to make progress. Disabled by
        # default: the durable plan cannot know whether a skipped step was
        # required by the goal, so a skip is never inferred.
        if self.allow_skip and status is not None:
            return answer(
                RecoveryAction.SKIP_STEP,
                "retries spent and skip is enabled by policy; the step carries no side effect",
            )

        # 9. Escalate while the escalation budget lasts, then abort. Escalation
        # is a signal to the existing model router, not a model choice here.
        if escalations_used < max(0, int(self.max_escalations)):
            return answer(
                RecoveryAction.ESCALATE_MODEL,
                f"retry budget spent ({retries_used}); escalating to a stronger model",
            )
        return answer(
            RecoveryAction.ABORT,
            f"retry budget spent ({retries_used}) and escalation budget spent ({escalations_used})",
        )
