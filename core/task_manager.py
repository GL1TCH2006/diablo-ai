from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from core.logging_setup import get_logger
from core.verification import VerificationResult, VerificationState
from core.verification_ledger import VerificationEntry, VerificationLedger

log = get_logger("task_manager")


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SKIPPED = "skipped"

    @property
    def symbol(self) -> str:
        return {
            StepStatus.PENDING: "o",
            StepStatus.RUNNING: "->",
            StepStatus.DONE: "v",
            StepStatus.FAILED: "x",
            StepStatus.CANCELLED: "-",
            StepStatus.BLOCKED: "!",
            StepStatus.SKIPPED: "~",
        }[self]


# M2.12 P2 step state machine. PENDING/RUNNING/DONE/FAILED/CANCELLED/BLOCKED are
# the pre-existing six states and keep their original meanings; SKIPPED is added
# so a step whose dependency reached a terminal non-DONE state can be recorded as
# "never attempted" instead of being misreported as a failure.
#
# DONE, CANCELLED, BLOCKED and SKIPPED are terminal for a given attempt. Only
# FAILED is retryable, and only through the explicit FAILED -> PENDING edge,
# which a caller must gate on its own attempt budget.
_ALLOWED_STEP_TRANSITIONS = {
    StepStatus.PENDING: {
        StepStatus.RUNNING,
        StepStatus.SKIPPED,
        StepStatus.BLOCKED,
        StepStatus.CANCELLED,
    },
    StepStatus.RUNNING: {
        StepStatus.DONE,
        StepStatus.FAILED,
        StepStatus.CANCELLED,
    },
    StepStatus.FAILED: {
        StepStatus.PENDING,
    },
    StepStatus.DONE: set(),
    StepStatus.CANCELLED: set(),
    StepStatus.BLOCKED: set(),
    StepStatus.SKIPPED: set(),
}

#: Terminal states that mean "this step will never run again".
STEP_TERMINAL_STATES = frozenset(
    {StepStatus.DONE, StepStatus.CANCELLED, StepStatus.BLOCKED, StepStatus.SKIPPED}
)

#: Terminal states that satisfy a dependent step's dependency.
STEP_SATISFIED_STATES = frozenset({StepStatus.DONE})

#: Terminal states that leave a dependent step unsatisfiable.
STEP_UNSATISFIABLE_STATES = frozenset(
    {StepStatus.FAILED, StepStatus.CANCELLED, StepStatus.BLOCKED, StepStatus.SKIPPED}
)


class TaskState(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_PERMISSION = "awaiting_permission"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ─────────────────────────────────────────────────────────────────────────────
# The terminal verdict invariant (M2.14 Phase 10A)
# ─────────────────────────────────────────────────────────────────────────────
#
# ``TaskState`` and ``VerificationState`` are two *independent* facts and the
# architecture keeps them apart on purpose. This module is the single place
# that rule is written down, so it cannot drift again the way two Phase 10
# tests did.
#
# THE INVARIANT
#
#   ``TaskState`` answers: did the request terminate, and how?
#   ``VerificationState`` answers: was the result independently checked?
#
#   Therefore ``COMPLETED`` asserts that the request finished without an
#   execution failure. It does NOT assert that the work was verified. Those
#   are different claims about different things, and a task may legitimately
#   finish without any verifier having existed.
#
#   A task can and does enter ``COMPLETED`` with a non-PASSED verdict. That is
#   an honest outcome, not a false one. What is forbidden is the *opposite*
#   disagreement: reporting a known-failed verdict as a completed request, or
#   letting a ``PASSED`` survive a later executed failure.
#
# ALLOWED terminal (TaskState, VerificationState) pairs
#
#   COMPLETED + PASSED           verified completion
#   COMPLETED + UNVERIFIED       finished, never independently checked
#   COMPLETED + NOT_REQUIRED     finished, nothing executed to verify
#   COMPLETED + NOT_RUN          finished before any verification phase; the
#                                only legitimate terminal NOT_RUN, and a
#                                transient one (:meth:`synchronize_verification`
#                                closes it on the next check)
#   FAILED     + FAILED          the ordinary failure
#   FAILED     + UNVERIFIED      failed without an adjudicated verdict
#   FAILED     + NOT_REQUIRED    failed before anything ran
#   FAILED     + NOT_RUN         failed before any verification phase
#   CANCELLED  + any             cancellation dominates; the verdict is
#                                invalidated to UNVERIFIED on the way out and
#                                is never load-bearing
#
# FORBIDDEN pairs, and why each is a safety property rather than tidiness
#
#   COMPLETED + FAILED     a failure reported as a completed request. Already
#                          unreachable by construction: ``Agent._record_verification``
#                          transitions the task to FAILED on a FAILED verdict.
#                          Asserted here so a future writer cannot undo that.
#   FAILED     + PASSED    a passing verdict surviving an execution failure.
#                          Reachable only by bypassing ``invalidate_verification``,
#                          and it is the false-pass this whole architecture exists
#                          to prevent.
#
# HOW SUCCESS IS REPORTED
#
# Success is the conjunction, and it is reported by :meth:`task_succeeded`, which
# is deliberately tri-state. ``None`` means "no verdict was reached", which is
# not the same as ``False`` and must never be reported as either:
#
#   True   COMPLETED and the ledger adjudicated PASSED with observed evidence
#   False  FAILED state, or an adjudicated FAILED verdict
#   None   finished (or cancelled) with no adjudicated verdict
#
# Nothing anywhere infers this from message wording. :func:`terminal_verdict`
# below is the single authority, and both the in-memory task and a reloaded
# durable record are checked against it by the same function.

#: Terminal task states: the request is over and no further edge exists.
TASK_TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})

#: Verdicts a COMPLETED task may legitimately end on.
COMPLETED_ADMISSIBLE_VERIFICATIONS = frozenset({
    VerificationState.PASSED,
    VerificationState.UNVERIFIED,
    VerificationState.NOT_REQUIRED,
    VerificationState.NOT_RUN,
})

#: Verdicts a FAILED task may end on. ``PASSED`` is absent by invariant.
FAILED_ADMISSIBLE_VERIFICATIONS = frozenset({
    VerificationState.FAILED,
    VerificationState.UNVERIFIED,
    VerificationState.NOT_REQUIRED,
    VerificationState.NOT_RUN,
})


class TerminalVerdict(str, Enum):
    """The structured answer to "did this task succeed?".

    Introduced in M2.14 Phase 10A so that no caller has to re-derive the
    conjunction from ``TaskState`` and ``VerificationState`` itself. Reuses the
    existing vocabularies: it adds no task state and no verification state, and
    ``TaskManager.task_succeeded`` keeps returning the same tri-state boolean it
    always did, for backward compatibility.
    """

    #: Finished and independently verified.
    VERIFIED = "verified"
    #: Finished without any adjudicated verdict. Not a failure, not a pass.
    UNVERIFIED = "unverified"
    #: The request terminated in failure, or a verdict adjudicated FAILED.
    FAILED = "failed"
    #: Cancellation dominated. Never reported as success or as failure.
    CANCELLED = "cancelled"
    #: The request has not reached a terminal state yet.
    IN_FLIGHT = "in_flight"

    @property
    def succeeded(self) -> bool | None:
        """Tri-state success, matching :meth:`TaskManager.task_succeeded`."""
        return {
            TerminalVerdict.VERIFIED: True,
            TerminalVerdict.UNVERIFIED: None,
            TerminalVerdict.FAILED: False,
            TerminalVerdict.CANCELLED: None,
            TerminalVerdict.IN_FLIGHT: None,
        }[self]


def terminal_verdict(state, verification) -> TerminalVerdict:
    """Classify one (task state, verification state) pair.

    The single authority for the terminal verdict invariant. Pure, total, and
    tolerant of raw strings so it can be applied to a reloaded durable record
    exactly as it is applied to the live task.
    """
    task_state = state if isinstance(state, TaskState) else TaskState(str(state))
    verdict = (
        verification
        if isinstance(verification, VerificationState)
        else VerificationState(str(verification))
    )
    if task_state not in TASK_TERMINAL_STATES:
        return TerminalVerdict.IN_FLIGHT
    if task_state is TaskState.CANCELLED:
        # Cancellation dominates every other fact.
        return TerminalVerdict.CANCELLED
    if task_state is TaskState.FAILED:
        return TerminalVerdict.FAILED
    # COMPLETED.
    if verdict is VerificationState.PASSED:
        return TerminalVerdict.VERIFIED
    return TerminalVerdict.UNVERIFIED


def terminal_invariant_violation(state, verification) -> str:
    """Return "" when the pair is admissible, else a sentence saying why not.

    Callers that must be tolerant of unreadable input should guard the call; a
    malformed pair is reported by this function rather than raised, because a
    corrupt persisted record must not abort a task load.
    """
    try:
        task_state = state if isinstance(state, TaskState) else TaskState(str(state))
        verdict = (
            verification
            if isinstance(verification, VerificationState)
            else VerificationState(str(verification))
        )
    except ValueError:
        return f"unreadable terminal pair ({state!r}, {verification!r})"

    if task_state is TaskState.COMPLETED and verdict not in COMPLETED_ADMISSIBLE_VERIFICATIONS:
        return (
            f"COMPLETED task carries verification {verdict.value}, which reports a "
            f"failure as a finished request"
        )
    if task_state is TaskState.FAILED and verdict is VerificationState.PASSED:
        return (
            "FAILED task carries a PASSED verdict; a passing verdict must never "
            "survive an execution failure"
        )
    return ""


_ALLOWED_TRANSITIONS = {
    TaskState.CREATED: {TaskState.PLANNING, TaskState.EXECUTING, TaskState.VERIFYING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PLANNING: {
        TaskState.AWAITING_PERMISSION,
        TaskState.EXECUTING,
        TaskState.VERIFYING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.AWAITING_PERMISSION: {
        TaskState.EXECUTING,
        TaskState.VERIFYING,
        TaskState.RECOVERING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.EXECUTING: {
        TaskState.AWAITING_PERMISSION,
        TaskState.VERIFYING,
        TaskState.RECOVERING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.VERIFYING: {
        TaskState.RECOVERING,
        TaskState.EXECUTING,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.RECOVERING: {
        TaskState.PLANNING,
        TaskState.EXECUTING,
        TaskState.VERIFYING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}


@dataclass
class TaskStep:
    name: str
    status: StepStatus = StepStatus.PENDING
    detail: str = ""
    step_id: str = ""


def step_transition_allowed(current: StepStatus | str, target: StepStatus | str) -> bool:
    """Whether ``current -> target`` is a legal step transition."""
    source = current if isinstance(current, StepStatus) else StepStatus(str(current))
    dest = target if isinstance(target, StepStatus) else StepStatus(str(target))
    return dest in _ALLOWED_STEP_TRANSITIONS[source]


@dataclass
class TaskManager:
    task_title: str = ""
    steps: list[TaskStep] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    state: TaskState = TaskState.CREATED
    verification: VerificationState = VerificationState.NOT_RUN
    verification_detail: str = ""
    verification_evidence: dict = field(default_factory=dict)
    verification_history: list[dict] = field(default_factory=list)
    #: Authoritative adjudication ledger (M2.12 P3). Every verification verdict
    #: for this task is appended here first, and the scalar ``verification``
    #: field above is set from the ledger's *adjudicated* state. The ledger is
    #: the boundary that decides whether PASSED is legitimate; these scalars are
    #: the existing durable projection of that decision, not a second authority.
    ledger: VerificationLedger = field(default_factory=VerificationLedger)
    recovery_attempts: int = 0
    #: M2.15.2. True once recovery for this task terminally stopped.
    #:
    #: A counter cannot express this on its own: ``recovery_attempts`` only
    #: implies exhaustion relative to a budget, so a restart that rebuilt the
    #: counter from zero - or a raised budget - would make a spent recovery path
    #: look fresh. This flag is the durable, monotonic statement and is restored
    #: from ``TaskRecord.metadata["lifecycle"]`` on restart. It is only ever set,
    #: never cleared by recovery logic, so exhaustion cannot be undone by a
    #: later failure.
    recovery_exhausted: bool = False
    #: Why recovery stopped terminally, for diagnostics. Read by nothing but the
    #: lifecycle projection; the decision itself lives in ``RecoveryPolicy``.
    recovery_termination: str = ""
    partial: bool = False
    failure_reason: str = ""

    def mark_recovery_exhausted(self, reason: str = "") -> None:
        """Record that recovery for this task is terminally spent.

        Monotonic by construction: a second call updates the reason but never
        clears the flag. Recovery is bounded, so "exhausted" is a terminal fact
        about a task, not a phase it can come back from.
        """
        self.recovery_exhausted = True
        if reason:
            self.recovery_termination = str(reason)

    def begin_task(self, title: str, task_id: str = "") -> None:
        self.task_title = title
        self.steps.clear()
        self.run_id = uuid.uuid4().hex
        self.task_id = task_id or self.run_id
        self.state = TaskState.CREATED
        self.verification = VerificationState.NOT_RUN
        self.verification_detail = ""
        self.verification_evidence = {}
        self.verification_history = []
        self.ledger = VerificationLedger()
        self.recovery_attempts = 0
        self.recovery_exhausted = False
        self.recovery_termination = ""
        self.partial = False
        self.failure_reason = ""

    def bind_task(self, task_id: str) -> None:
        if task_id:
            self.task_id = task_id

    def transition(self, state: TaskState | str) -> None:
        target = state if isinstance(state, TaskState) else TaskState(str(state))
        if target == self.state:
            return
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid task transition: {self.state.value} -> {target.value}")
        if target in TASK_TERMINAL_STATES:
            # M2.14 Phase 10A: refuse to enter a terminal state that would
            # report a known-failed verdict as a finished request. This is a
            # state-machine rule rather than a post-hoc check, so the forbidden
            # pair cannot be constructed even transiently.
            violation = terminal_invariant_violation(target, self.verification)
            if violation:
                raise ValueError(
                    f"terminal verdict invariant violated entering {target.value}: {violation}"
                )
        self.state = target

    def terminal_violation(self) -> str:
        """Check the *current* (state, verification) pair against the invariant.

        Returns "" when the pair is admissible. This is the post-hoc form, for
        asserting a task that was already built by other means rather than
        driven through :meth:`transition` (a reloaded record, a test double).
        """
        return terminal_invariant_violation(self.state, self.verification)

    def assert_terminal_invariant(self) -> None:
        """Raise ``ValueError`` if the current pair violates the invariant."""
        violation = self.terminal_violation()
        if violation:
            raise ValueError(f"terminal verdict invariant violated: {violation}")

    def synchronize_verification(self) -> str:
        """Close a terminal task left with no adjudicated verdict.

        A task that reaches a terminal state before its verification phase ran
        is left at ``NOT_RUN``. ``COMPLETED + NOT_RUN`` is admissible but
        transient: it is the state the request is in for the few instructions
        between the last step and ``_finish_tracking``. Rather than guess, this
        method settles it explicitly, using the rule the rest of the codebase
        already applies: work was attempted, so it is ``UNVERIFIED``; nothing
        ran, so there is nothing to verify.

        Returns the settled verification state. Idempotent: calling it on an
        already-adjudicated task changes nothing.
        """
        if self.state not in TASK_TERMINAL_STATES:
            return self.verification
        if self.verification is not VerificationState.NOT_RUN:
            return self.verification
        settled = (
            VerificationState.UNVERIFIED
            if self.any_executed()
            else VerificationState.NOT_REQUIRED
        )
        self.set_verification(settled, "Settled from NOT_RUN at a terminal task state.")
        return self.verification

    def set_verification(
        self,
        state: VerificationState | str,
        detail: str = "",
        evidence: dict | None = None,
    ) -> None:
        value = state if isinstance(state, VerificationState) else VerificationState(str(state))
        self.verification = value
        self.verification_detail = detail
        if evidence is not None:
            # `verification_evidence` is a mapping by contract, and this value is
            # persisted into the task record. A capability that reports a
            # non-mapping evidence payload (a bare list, tuple or string) must
            # not be able to raise here: dict() on those raises TypeError or
            # ValueError, which would abort task persistence mid-flight.
            # Reject the payload instead of crashing; the ledger is what
            # adjudicates whether such evidence may support a verdict.
            if isinstance(evidence, Mapping):
                self.verification_evidence = dict(evidence)
            else:
                log.warning(
                    "ignored non-mapping verification evidence of type %s",
                    type(evidence).__name__,
                )
                self.verification_evidence = {}

    def record_verification(
        self,
        result: VerificationResult,
        *,
        subject_kind=None,
        method_kind=None,
        trust=None,
        legacy_kind: str | None = None,
        step_id: str = "",
    ) -> VerificationEntry:
        """Adjudicate ``result`` through the ledger and apply the outcome.

        This is the single funnel every verification writer goes through (M2.12
        P3 Phase 2). The ledger is authoritative: a requested ``PASSED`` that
        fails the invariant is downgraded here, and the scalar
        :attr:`verification` / :attr:`verification_evidence` fields are set from
        the *adjudicated* entry, never from the writer's request.

        Unset axes are resolved from the legacy ``kind`` via
        ``LEGACY_KIND_AXES``, so an unmigrated writer keeps its vocabulary and
        gains the invariant for free.

        Returns the appended :class:`VerificationEntry` so the caller can emit
        the downgrade reason.
        """
        entry = self.ledger.record(
            result=result,
            subject_kind=subject_kind,
            method_kind=method_kind,
            trust=trust,
            legacy_kind=legacy_kind,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=step_id,
        )
        self.set_verification(entry.state, entry.detail, dict(entry.evidence))
        return entry

    def verification_ledger_snapshot(self) -> dict:
        """Bounded ledger projection for the persisted task record."""
        return self.ledger.to_persisted_dict()

    def invalidate_verification(self, detail: str) -> None:
        if self.verification != VerificationState.NOT_RUN:
            self.verification_history.append({
                "state": self.verification.value,
                "detail": self.verification_detail,
                "evidence": dict(self.verification_evidence),
            })
            self.verification_history = self.verification_history[-20:]
        self.verification = VerificationState.UNVERIFIED
        self.verification_detail = detail

    def mark_partial(self) -> None:
        self.partial = True

    def record_recovery(self) -> None:
        self.recovery_attempts += 1

    def fail(self, reason: str) -> None:
        self.failure_reason = reason
        if any(step.executed if hasattr(step, "executed") else step.status == StepStatus.DONE for step in self.steps):
            self.mark_partial()
        self.transition(TaskState.FAILED)

    def add_step(self, name: str, step_id: str = "") -> TaskStep:
        step = TaskStep(name=name, status=StepStatus.PENDING, step_id=step_id)
        self.steps.append(step)
        return step

    def set_status(self, step: TaskStep, status: StepStatus, detail: str = "") -> None:
        step.status = status
        step.detail = detail

    def set_step_status_checked(self, step: TaskStep, status: StepStatus, detail: str = "") -> None:
        """Transition a step, rejecting edges the state machine forbids.

        ``set_status`` remains unvalidated for backward compatibility with the
        pre-P2 execution paths; new durable-plan code uses this.
        """
        target = status if isinstance(status, StepStatus) else StepStatus(str(status))
        if not step_transition_allowed(step.status, target):
            raise ValueError(f"invalid step transition: {step.status.value} -> {target.value}")
        self.set_status(step, target, detail)

    def step_by_id(self, step_id: str) -> TaskStep | None:
        if not step_id:
            return None
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def lines(self) -> list[str]:
        out = []
        for step in self.steps:
            suffix = f" - {step.detail}" if step.detail else ""
            out.append(f"{step.status.symbol} {step.name}{suffix}")
        return out

    def any_failed(self) -> bool:
        return any(
            s.status in (StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.SKIPPED)
            for s in self.steps
        )

    def any_executed(self) -> bool:
        return any(s.status in (StepStatus.RUNNING, StepStatus.DONE) for s in self.steps)

    def final_status(self) -> str:
        if self.state == TaskState.CANCELLED:
            return "cancelled"
        if self.state == TaskState.FAILED:
            return "failed"
        if self.state == TaskState.COMPLETED:
            return "completed"
        if any(s.status == StepStatus.CANCELLED for s in self.steps):
            return "cancelled"
        if self.any_failed():
            return "failed"
        return "completed"

    def task_succeeded(self) -> bool | None:
        """Tri-state success. ``None`` means "no verdict was reached".

        Kept as the backward-compatible projection of
        :meth:`terminal_verdict`; the classification itself lives in the one
        authority so it cannot be re-derived inconsistently elsewhere.
        """
        return terminal_verdict(self.state, self.verification).succeeded

    def terminal_verdict(self) -> TerminalVerdict:
        """The structured terminal verdict for this task."""
        return terminal_verdict(self.state, self.verification)
