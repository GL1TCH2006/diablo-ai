"""M2.17 - bounded scheduling and startup reconciliation for persistent goals.

What this module is allowed to do
=================================

Exactly one thing: decide *whether a due, authorised goal should have a Task
started right now*, and record that decision durably.

What it is not allowed to do
============================

Call a tool. Touch a capability. Run a plan step. Decide that work succeeded.

The scheduler submits through an injected ``submit_task`` callback, and in
production that callback is the Agent's normal request path. So every piece of
autonomous work becomes an ordinary Task, goes through the ordinary TaskManager,
Executor, PermissionManager and VerificationLedger, and is cancellable and
observable in exactly the way a user-initiated task is. There is no autonomous
execution path, because there is no autonomous executor.

Why the idempotency is durable rather than in-memory
====================================================

A scheduler that remembers what it already started in a Python set works until the
process restarts, and then it starts the same goal's work a second time. Since the
whole point of this milestone is surviving restarts, "already started" has to be a
fact on disk.

Each goal carries ``last_started_tick``. A tick is the goal's own ``next_run_at``
value - the instant it was due - rather than a wall-clock timestamp of the check.
That makes the guard exactly idempotent: re-running a tick, restarting mid-tick, or
two schedulers racing on the same goal all compare the same string and all decline.
A wall-clock timestamp would not do: two checks in the same second would produce
different strings.

Cancellation and duplicates
===========================

Both are checked here, durably, and neither is advisory:

* ``cancel_requested`` is persisted the moment the user cancels, so a restart cannot
  resurrect a cancelled goal.
* ``last_started_tick`` is persisted before the submit callback is invoked, so a
  crash *during* submission still leaves the goal marked as started. The cost of
  that ordering is that a crash can skip one scheduled run; the alternative is
  running it twice, which is worse.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.goals import Goal, GoalState, GoalStore

__all__ = [
    "SchedulerReport",
    "SchedulerLimits",
    "AutonomyScheduler",
    "ReconciliationReport",
    "ResumeReport",
    "reconcile_on_startup",
    "summarise_goal",
    "DEFAULT_SCHEDULER_LIMITS",
]


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


@dataclass(frozen=True)
class SchedulerLimits:
    """Bounds on autonomous work.

    Every one of these is a ceiling on *how much may happen*, never a permission.
    Defaults are deliberately small: a companion process that quietly starts
    dozens of tasks is worse than one that visibly does a few.
    """

    max_active_goals: int = 3
    max_autonomous_tasks: int = 32
    max_concurrent_autonomous: int = 1
    min_interval_seconds: int = 60

    @classmethod
    def from_settings(cls, settings: Any) -> "SchedulerLimits":
        def _int(name: str, default: int) -> int:
            try:
                return max(0, int(getattr(settings, name, default)))
            except (TypeError, ValueError):
                return default
        return cls(
            max_active_goals=_int("autonomy_max_active_goals", 3),
            max_autonomous_tasks=_int("autonomy_max_tasks", 32),
            max_concurrent_autonomous=_int("autonomy_max_concurrent", 1),
            min_interval_seconds=_int("autonomy_min_interval_seconds", 60),
        )


DEFAULT_SCHEDULER_LIMITS = SchedulerLimits()


@dataclass
class ResumeReport:
    """The outcome of an ``Agent.resume``.

    A resume that cannot proceed is a *reported outcome*, not an exception: the
    usual caller is a scheduler that must keep running regardless of what one goal
    decided. The four actions are deliberately distinguishable, because
    "nothing to do" and "refused because unsafe" deserve different handling by a
    caller and must never be reported the same way to a user.
    """

    NOOP = "noop"
    RESUMED = "resumed"
    BLOCKED = "blocked"
    REFUSED = "refused"

    goal_id: str = ""
    task_id: str = ""
    action: str = NOOP
    reason: str = ""
    resumable_steps: list[str] = field(default_factory=list)
    refused_steps: list[str] = field(default_factory=list)
    #: How many verification entries were restored from durable state.
    ledger_entries: int = 0
    #: Whether exhausted-recovery state was restored for this task.
    recovery_restored: bool = False
    #: The terminal verdict read from durable state, via terminal_verdict.
    prior_verdict: str = ""

    @property
    def is_resumed(self) -> bool:
        return self.action == self.RESUMED

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "action": self.action,
            "reason": self.reason,
            "resumable_steps": list(self.resumable_steps),
            "refused_steps": list(self.refused_steps),
            "ledger_entries": self.ledger_entries,
            "recovery_restored": self.recovery_restored,
            "prior_verdict": self.prior_verdict,
        }


@dataclass
class SchedulerReport:
    """What one tick did, and what it deliberately did not do."""

    tick_at: str = ""
    due: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    limits_reached: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def started_count(self) -> int:
        return len(self.started)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick_at": self.tick_at,
            "due": list(self.due),
            "started": list(self.started),
            "skipped": dict(self.skipped),
            "limits_reached": list(self.limits_reached),
            "errors": list(self.errors),
        }


class AutonomyScheduler:
    """Decides which due goals may start a Task. Executes nothing itself."""

    def __init__(
        self,
        goal_store: GoalStore,
        *,
        submit_task: Callable[[Goal], str],
        limits: SchedulerLimits | None = None,
        clock: Callable[[], str] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._goals = goal_store
        self._submit = submit_task
        self._limits = limits or SchedulerLimits()
        self._clock = clock or _now
        self._on_event = on_event
        self._lock = threading.Lock()
        self._in_flight = 0
        self._tasks_started = 0
        self._stopped = False

    # -- introspection -------------------------------------------------

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    @property
    def tasks_started(self) -> int:
        """How many tasks this scheduler process has started, for its own bound."""
        return self._tasks_started

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _emit(self, event: str, **payload: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, dict(payload))
        except Exception:  # noqa: BLE001 - a sink must not break scheduling
            pass

    def stop(self) -> None:
        self._stopped = True

    # -- the tick ------------------------------------------------------

    def tick(self) -> SchedulerReport:
        """One scheduling pass. Never raises.

        Order matters and is part of the contract: durability first (mark started),
        then submission. A crash between the two skips a run; the reverse order
        would duplicate one.
        """
        report = SchedulerReport(tick_at=self._clock())
        if self._stopped:
            report.errors.append("scheduler stopped")
            return report

        goals = self._goals.list_all()
        active = [g for g in goals if g.state in
                  (GoalState.CREATED, GoalState.ACTIVE, GoalState.RESUMABLE)]
        due = [g for g in goals if g.is_schedulable(now=report.tick_at)]

        for goal in due:
            report.due.append(goal.goal_id)

        if len(active) > self._limits.max_active_goals:
            report.limits_reached.append(
                f"active_goals {len(active)} > {self._limits.max_active_goals}")
        if self._tasks_started >= self._limits.max_autonomous_tasks:
            report.limits_reached.append(
                f"autonomous_tasks {self._tasks_started} >= "
                f"{self._limits.max_autonomous_tasks}")
        if self.in_flight >= self._limits.max_concurrent_autonomous:
            report.limits_reached.append(
                f"concurrent {self.in_flight} >= "
                f"{self._limits.max_concurrent_autonomous}")

        due_ids = {g.goal_id for g in due}
        for goal in goals:
            goal_id = goal.goal_id
            reason = self._decline_reason(goal, report)
            if not reason and goal_id not in due_ids:
                reason = "not due"
            if reason:
                # Every non-started goal is still reported, with its reason. Silently
                # omitting it would make a goal that never runs indistinguishable
                # from a goal that does not exist, which is exactly the failure a
                # user cannot diagnose.
                report.skipped.setdefault(goal_id, reason)
                self._emit("autonomy_goal_skipped", goal_id=goal_id, reason=reason)

        for goal in due:
            goal_id = goal.goal_id
            reason = self._decline_reason(goal, report)
            if reason:
                report.skipped[goal_id] = reason
                self._emit("autonomy_goal_skipped", goal_id=goal_id, reason=reason)
                continue
            if len(active) > self._limits.max_active_goals:
                report.skipped[goal_id] = "active goal limit reached"
                continue
            if self._tasks_started >= self._limits.max_autonomous_tasks:
                report.skipped[goal_id] = "autonomous task limit reached"
                continue

            tick_key = goal.next_run_at or report.tick_at
            if goal.last_started_tick == tick_key:
                # The durable idempotency guard. This is what makes a repeated
                # tick, a restart mid-tick, or two schedulers racing harmless.
                report.skipped[goal_id] = "already started for this tick"
                self._emit("autonomy_goal_skipped", goal_id=goal_id,
                           reason="duplicate tick")
                continue

            with self._lock:
                if self._in_flight >= self._limits.max_concurrent_autonomous:
                    report.skipped[goal_id] = "concurrency limit reached"
                    continue
                self._in_flight += 1
            try:
                # Durability BEFORE submission.
                goal.last_started_tick = tick_key
                goal.transition(GoalState.ACTIVE)
                # Advance the schedule *before* the save, so the persisted record
                # already carries the next due time. Saving first and advancing
                # afterwards left the new schedule only in memory, so the next
                # process saw a stale due time and could re-run the tick.
                self._advance_schedule(goal, report.tick_at)
                self._goals.save(goal)
                task_ref = self._submit(goal)
                self._tasks_started += 1
                report.started.append(goal_id)
                self._emit("autonomy_task_started", goal_id=goal_id,
                           task_ref=str(task_ref)[:120],
                           tick=tick_key)
            except Exception as exc:  # noqa: BLE001 - one goal must not stop the tick
                report.errors.append(f"{goal_id}: {type(exc).__name__}: {exc}")
                self._emit("autonomy_goal_failed", goal_id=goal_id,
                           error=type(exc).__name__)
            finally:
                with self._lock:
                    self._in_flight = max(0, self._in_flight - 1)
        return report

    def _decline_reason(self, goal: Goal, report: SchedulerReport) -> str:
        """Why this goal must not start, or "" to proceed."""
        if goal.cancel_requested or goal.state is GoalState.CANCELLED:
            return "cancelled"
        if not goal.authorized:
            return "not authorised"
        if goal.awaiting_permission:
            # Autonomy does not imply permission. A goal parked on a confirmation
            # stays parked; nothing here auto-confirms it.
            return "awaiting permission"
        if goal.recovery_exhausted:
            return "recovery exhausted"
        if goal.is_terminal:
            return f"terminal ({goal.state.value})"
        interval = max(0, int(goal.interval_seconds))
        if interval < self._limits.min_interval_seconds:
            return (f"interval {interval}s below minimum "
                    f"{self._limits.min_interval_seconds}s")
        return ""

    def _advance_schedule(self, goal: Goal, now: str) -> None:
        """Move the goal to its next due time.

        Uses local time arithmetic rather than a wall-clock timestamp of "now" so a
        goal rescheduled at 09:00:00 with a 60s interval becomes due at 09:01:00
        rather than immediately.
        """
        interval = max(0, int(goal.interval_seconds))
        if interval <= 0:
            goal.next_run_at = ""
            return
        try:
            base = time.strptime(now or goal.next_run_at, "%Y%m%d-%H%M%S")
        except (ValueError, TypeError):
            base = time.localtime()
        goal.next_run_at = time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(time.mktime(base) + interval))


# ═════════════════════════════════════════════════════════ reconciliation


@dataclass
class ReconciliationReport:
    """What startup reconciliation found and decided.

    Deterministic by construction: it reads durable state, writes durable state, and
    makes no decision that depends on anything but those records. Running it twice
    produces the same report.
    """

    resumed: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    blocked_on_permission: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (len(self.resumed) + len(self.paused) + len(self.failed)
                + len(self.cancelled) + len(self.blocked_on_permission)
                + len(self.exhausted))

    def to_dict(self) -> dict[str, Any]:
        return {
            "resumed": list(self.resumed),
            "paused": list(self.paused),
            "failed": list(self.failed),
            "cancelled": list(self.cancelled),
            "blocked_on_permission": list(self.blocked_on_permission),
            "exhausted": list(self.exhausted),
            "notes": list(self.notes),
        }


def reconcile_on_startup(goal_store: GoalStore,
                         task_store: Any = None) -> ReconciliationReport:
    """Bring durable goal state into a decidable condition after a restart.

    Deliberately conservative, and the conservatism is the feature:

    * A cancelled goal is left cancelled. Restart must not resurrect it.
    * A goal waiting on permission stays waiting. Autonomy never auto-confirms.
    * A goal whose recovery is exhausted is surfaced, not retried.
    * A goal whose current task is still legitimately in flight is marked
      ``RESUMABLE`` - it *may* be picked up, not that it will be.
    * A goal with no current task becomes ``ACTIVE`` if authorised, otherwise
      ``PAUSED``.

    What this function never does is decide that work succeeded, or mark a step
    safe to replay. Uncertain side effects are refused by
    ``Agent.resume``, which is the only thing allowed to make that call.
    """
    report = ReconciliationReport()
    if task_store is None:
        report.notes.append("no task store supplied; goal-only reconciliation")

    for goal in goal_store.list_all():
        goal_id = goal.goal_id

        if goal.cancel_requested or goal.state is GoalState.CANCELLED:
            if goal.state is not GoalState.CANCELLED:
                goal.transition(GoalState.CANCELLED)
                goal_store.save(goal)
            report.cancelled.append(goal_id)
            continue

        if goal.awaiting_permission:
            goal.transition(GoalState.PAUSED)
            goal_store.save(goal)
            report.blocked_on_permission.append(goal_id)
            continue

        if goal.recovery_exhausted:
            goal.transition(GoalState.FAILED)
            goal_store.save(goal)
            report.exhausted.append(goal_id)
            continue

        if not goal.authorized:
            goal.transition(GoalState.PAUSED)
            goal_store.save(goal)
            report.paused.append(goal_id)
            continue

        task_state = ""
        if task_store is not None and goal.current_task_id:
            record = task_store.get(goal.current_task_id)
            if record is None:
                report.notes.append(
                    f"{goal_id}: current task {goal.current_task_id} is gone")
                task_state = "missing"
            else:
                task_state = str(getattr(record, "status", "") or "")

        if task_state in ("completed", "failed", "cancelled"):
            # The task finished while the process was down. The goal is not thereby
            # complete: its evidence is checked by the goal, not the task.
            goal.transition(GoalState.ACTIVE)
            goal_store.save(goal)
            report.resumed.append(goal_id)
            report.notes.append(
                f"{goal_id}: last task {task_state}; awaiting further evidence")
            continue

        if task_state in ("pending", "active") or task_state == "missing":
            goal.transition(GoalState.RESUMABLE)
            goal_store.save(goal)
            report.resumed.append(goal_id)
            continue

        # No current task at all.
        goal.transition(GoalState.ACTIVE)
        goal_store.save(goal)
        report.resumed.append(goal_id)
    return report


def summarise_goal(goal: Goal) -> dict[str, Any]:
    """A display-safe projection of a goal, for the companion and for logs.

    Bounded and redacted, because a goal's text is user data crossing into a
    presentation surface.
    """
    from core.goals import sanitize_goal_text

    return {
        "goal_id": goal.goal_id,
        "state": goal.state.value,
        "objective": sanitize_goal_text(goal.objective, limit=160),
        "progress_summary": sanitize_goal_text(goal.progress_summary, limit=160),
        "next_action": sanitize_goal_text(goal.next_action, limit=160),
        "task_count": len(goal.task_ids),
        "current_task_id": goal.current_task_id,
        "verified_tasks": len(goal.verified_task_ids),
        "required_evidence": goal.required_evidence,
        "has_required_evidence": goal.has_required_evidence,
        "awaiting_permission": goal.awaiting_permission,
        "recovery_exhausted": goal.recovery_exhausted,
        "cancel_requested": goal.cancel_requested,
    }


def goal_from_mapping(data: Mapping[str, Any]) -> Goal:
    """Rebuild a goal from a mapping. Exposed for tests and for importers."""
    return Goal.from_dict(data)