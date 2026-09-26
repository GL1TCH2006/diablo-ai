from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.verification import VerificationState


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
    recovery_attempts: int = 0
    partial: bool = False
    failure_reason: str = ""

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
        self.recovery_attempts = 0
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
        self.state = target

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
            self.verification_evidence = dict(evidence)

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
        if self.state == TaskState.FAILED:
            return False
        if self.verification == VerificationState.PASSED:
            return True
        if self.verification == VerificationState.FAILED:
            return False
        return None
