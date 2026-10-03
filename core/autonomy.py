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
    "AutonomyRuntime",
    "GoalOutcome",
    "ReconciliationReport",
    "ResumeReport",
    "reconcile_on_startup",
    "summarise_goal",
    "goal_from_mapping",
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
    #: Goal ids whose persisted state actually moved during this pass. Tracked
    #: separately from the lists above, because those describe what reconciliation
    #: *decided* for every goal it examined while this describes what it *changed*.
    #: Conflating them made every startup report "N goals changed" for goals that
    #: were already exactly where reconciliation wanted them.
    changed_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.changed_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resumed": list(self.resumed),
            "paused": list(self.paused),
            "failed": list(self.failed),
            "cancelled": list(self.cancelled),
            "blocked_on_permission": list(self.blocked_on_permission),
            "exhausted": list(self.exhausted),
            "notes": list(self.notes),
            "changed_ids": list(self.changed_ids),
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
        before = goal.state

        def _persist() -> None:
            goal_store.save(goal)
            if goal.state is not before and goal_id not in report.changed_ids:
                report.changed_ids.append(goal_id)

        if goal.cancel_requested or goal.state is GoalState.CANCELLED:
            if goal.state is not GoalState.CANCELLED:
                goal.transition(GoalState.CANCELLED)
                _persist()
            report.cancelled.append(goal_id)
            continue

        if goal.awaiting_permission:
            goal.transition(GoalState.PAUSED)
            _persist()
            report.blocked_on_permission.append(goal_id)
            continue

        if goal.recovery_exhausted:
            goal.transition(GoalState.FAILED)
            _persist()
            report.exhausted.append(goal_id)
            continue

        if not goal.authorized:
            goal.transition(GoalState.PAUSED)
            _persist()
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
            _persist()
            report.resumed.append(goal_id)
            report.notes.append(
                f"{goal_id}: last task {task_state}; awaiting further evidence")
            continue

        if task_state in ("pending", "active") or task_state == "missing":
            goal.transition(GoalState.RESUMABLE)
            _persist()
            report.resumed.append(goal_id)
            continue

        # No current task at all.
        goal.transition(GoalState.ACTIVE)
        _persist()
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


# ═══════════════════════════════════════════════════════ the production runtime


#: ``TaskRecord.status`` uses the record vocabulary; ``TaskState`` uses the state
#: machine's. Translations are listed explicitly rather than derived, so a new
#: record status cannot be silently mistaken for a task state.
_RECORD_STATUS_TO_TASK_STATE = {
    "pending": "created",
    "active": "executing",
    "executing": "executing",
    "planning": "planning",
    "verifying": "verifying",
    "recovering": "recovering",
    "awaiting_permission": "awaiting_permission",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


@dataclass
class GoalOutcome:
    """What one goal's task produced, as observed from authoritative task state.

    The runtime never decides that autonomous work succeeded. It reads the verdict
    the existing TaskManager/VerificationLedger already produced and copies it onto
    the goal, which is the only way evidence enters a goal at all.
    """

    goal_id: str = ""
    task_id: str = ""
    verdict: str = ""
    evidence_recorded: bool = False
    goal_completed: bool = False
    goal_failed: bool = False
    awaiting_permission: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "verdict": self.verdict,
            "evidence_recorded": self.evidence_recorded,
            "goal_completed": self.goal_completed,
            "goal_failed": self.goal_failed,
            "awaiting_permission": self.awaiting_permission,
            "reason": self.reason,
        }


class AutonomyRuntime:
    """Owns the goal lifecycle in production: schedule, submit, observe, project.

    This exists because the scheduler, the goal store and ``Agent.resume`` were all
    correct and none of them was reachable: nothing imported ``core.goals`` or
    ``core.autonomy`` except tests, so a goal could be created and scheduled by hand
    but never by the application. A durable feature nobody can start is a feature
    that does not exist, so this is the piece that makes the milestone operational.

    What it is allowed to do
    ========================
    Create a Task by calling the Agent's ordinary ``respond`` - the same entry point a
    user turn uses - and then *observe* the authoritative task record that call
    produced. Every permission decision, executor dispatch, cancellation and
    verification between those two points belongs to the existing components.

    What it is not allowed to do
    ============================
    Execute a tool, mint a task id, auto-confirm a permission, mark a goal complete
    on its own judgement, or resurrect a cancelled goal. ``goal_completed`` is True
    only because ``Goal.mark_completed`` accepted accumulated *verified* evidence.
    """

    #: Name of the background thread, so it is identifiable in a stack dump.
    THREAD_NAME = "diablo-autonomy-scheduler"

    def __init__(
        self,
        agent: Any,
        goal_store: GoalStore,
        *,
        task_store: Any = None,
        limits: SchedulerLimits | None = None,
        clock: Callable[[], str] | None = None,
        poll_seconds: float = 30.0,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._agent = agent
        self._goals = goal_store
        self._task_store = task_store if task_store is not None else getattr(
            agent, "_task_store", None)
        self._limits = limits or SchedulerLimits()
        self._clock = clock or _now
        self._poll = max(1.0, float(poll_seconds))
        self._on_event = on_event
        self._scheduler = AutonomyScheduler(
            goal_store,
            submit_task=self.submit_goal,
            limits=self._limits,
            clock=self._clock,
            on_event=on_event,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._outcomes: dict[str, GoalOutcome] = {}

    # -- introspection -------------------------------------------------

    @property
    def scheduler(self) -> AutonomyScheduler:
        return self._scheduler

    @property
    def goals(self) -> GoalStore:
        return self._goals

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def outcome(self, goal_id: str) -> GoalOutcome | None:
        with self._lock:
            return self._outcomes.get(str(goal_id or ""))

    # -- lifecycle -----------------------------------------------------

    def reconcile(self) -> ReconciliationReport:
        """Startup reconciliation, against the Agent's own task store when it has one."""
        return reconcile_on_startup(self._goals, self._task_store)

    def tick(self) -> SchedulerReport:
        """One pass, announced. Never raises."""
        report = self._scheduler.tick()
        # A heartbeat, because a scheduler that silently declines everything is
        # indistinguishable from a scheduler that is not running at all - and
        # "not running at all" is exactly the failure that is invisible from the
        # goal files, since declining a goal writes nothing.
        self._emit("autonomy_tick", started=report.started_count,
                   due=len(report.due), skipped=len(report.skipped),
                   errors=len(report.errors))
        return report

    def start(self) -> bool:
        """Begin ticking in the background. Idempotent, and off by default at the CLI."""
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=self.THREAD_NAME, daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop ticking and join. Returns whether the thread finished."""
        self._stop.set()
        self._scheduler.stop()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a scheduler must outlive one bad tick
                self._emit("autonomy_tick_failed", error="tick raised")

    def _emit(self, event: str, **payload: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, dict(payload))
        except Exception:  # noqa: BLE001 - a sink must not break autonomy
            pass

    # -- the submit callback the scheduler already owns ---------------

    def submit_goal(self, goal: Goal) -> str:
        """Turn a due goal into an ordinary Task through the Agent's request path.

        Synchronous on purpose: the scheduler's ``max_concurrent_autonomous`` bound is
        only meaningful if a start does not return before the task it started has
        finished. Submitting asynchronously would make that limit decorative.
        """
        goal_id = goal.goal_id
        text = self.goal_request_text(goal)
        if not text:
            outcome = GoalOutcome(goal_id=goal_id, reason="nothing to submit")
            self._record_outcome(outcome)
            return ""

        try:
            reply = self._agent.respond(text)
        except Exception as exc:  # noqa: BLE001 - one goal must not stop the tick
            outcome = GoalOutcome(
                goal_id=goal_id, verdict="error",
                reason=f"{type(exc).__name__}: {exc}"[:200])
            self._fail_goal(goal, outcome)
            self._record_outcome(outcome)
            self._emit("autonomy_submit_failed", goal_id=goal_id,
                       error=type(exc).__name__)
            return ""

        task_id = self._current_task_id()
        outcome = self.observe(goal, task_id=task_id, reply=str(reply or ""))
        self._record_outcome(outcome)
        return task_id

    @staticmethod
    def goal_request_text(goal: Goal) -> str:
        """The goal, expressed as one ordinary request. Descriptive, never policy.

        ``next_action`` is appended because it is the human-authored hint about what
        to do next; both fields are user data and arrive through the same request
        lifecycle as any other user text, so the security rules that apply to a typed
        request apply here unchanged.
        """
        parts = [goal.objective.strip()]
        if goal.next_action.strip():
            parts.append(goal.next_action.strip())
        return " ".join(p for p in parts if p)

    def _current_task_id(self) -> str:
        """The task the Agent just created, read back from its own context."""
        context = getattr(self._agent, "request_context", None)
        try:
            return str(getattr(context, "task_id", "") or "")
        except Exception:  # noqa: BLE001 - introspection must not break a tick
            return ""

    # -- observing authoritative task state ----------------------------

    def observe(self, goal: Goal, *, task_id: str = "", reply: str = "") -> GoalOutcome:
        """Copy the task's own verdict onto the goal. Never decides success itself."""
        goal_id = goal.goal_id
        outcome = GoalOutcome(goal_id=goal_id, task_id=task_id)

        if not task_id:
            outcome.verdict = "no_task"
            outcome.reason = "the agent produced no task id"
            self._goals.save(goal)
            return outcome

        fresh = self._goals.get(goal_id)
        if fresh is not None:
            goal = fresh
        # The goal is working now, so it must be in a live state. The scheduler
        # already transitions before submitting, but a direct call - the real-model
        # probe, an operator script, a test - arrives with the goal still CREATED,
        # and CREATED cannot reach COMPLETED through the transition table. Leaving
        # that to the caller made the runtime silently unable to complete the goals
        # it was asked to run.
        if goal.state in (GoalState.CREATED, GoalState.PAUSED,
                          GoalState.RESUMABLE):
            goal.transition(GoalState.ACTIVE)
        goal.attach_task(task_id)

        verdict = self._task_verdict(task_id)
        outcome.verdict = verdict
        if verdict == "verified":
            outcome.evidence_recorded = goal.record_verified_task(task_id)
        elif verdict == "unverified":
            outcome.reason = "task finished without a verified verdict"
        elif verdict == "cancelled":
            outcome.reason = "task was cancelled"
        elif verdict == "in_flight":
            outcome.reason = "task has not reached a terminal state"
        else:
            outcome.reason = f"task verdict {verdict}"

        outcome.evidence_recorded = outcome.evidence_recorded or bool(
            task_id in goal.verified_task_ids and verdict == "verified")

        if goal.has_required_evidence and not goal.cancel_requested:
            outcome.goal_completed = goal.mark_completed()

        if not outcome.goal_completed and goal.state is not GoalState.CANCELLED:
            goal.recovery_attempts += 1
            goal.progress_summary = (
                f"task {task_id}: {verdict}" if not reply
                else f"task {task_id}: {verdict}")
            if goal.recovery_attempts >= max(
                    1, self._limits.max_autonomous_tasks):
                goal.recovery_exhausted = True
                goal.transition(GoalState.FAILED)

        self._goals.save(goal)
        self._emit("autonomy_goal_observed", goal_id=goal_id, task_id=task_id,
                   verdict=verdict,
                   goal_completed=outcome.goal_completed)
        return outcome

    def _task_verdict(self, task_id: str) -> str:
        """The authoritative terminal verdict for a task, read from durable state.

        Read through ``terminal_verdict`` on the persisted lifecycle metadata, exactly
        as ``Agent.resume`` reads it. Reading ``status == "completed"`` instead would
        treat an unverified completion as evidence.

        ``TaskRecord.status`` and ``TaskState`` are deliberately different
        vocabularies - a record says "pending"/"active" where the state machine says
        CREATED/EXECUTING - so the record's own status is translated first. Passing
        "active" straight to ``terminal_verdict`` raises, and swallowing that into an
        "unknown" verdict silently turned every in-flight task into an unreadable one.
        """
        if self._task_store is None:
            return "unknown"
        try:
            record = self._task_store.get(task_id)
        except Exception:  # noqa: BLE001
            return "unknown"
        if record is None:
            return "unknown"
        lifecycle = getattr(record, "metadata", {}) or {}
        if not isinstance(lifecycle, Mapping):
            lifecycle = {}
        lifecycle = lifecycle.get("lifecycle") or {}
        if not isinstance(lifecycle, Mapping):
            lifecycle = {}
        raw_state = str(lifecycle.get("state") or getattr(record, "status", "") or "")
        verification = str(lifecycle.get("verification") or "")
        state = _RECORD_STATUS_TO_TASK_STATE.get(raw_state, raw_state)
        from core.task_manager import terminal_verdict

        try:
            verdict = terminal_verdict(state, verification)
        except Exception:  # noqa: BLE001 - a corrupt record degrades, never crashes
            return "unknown"
        return str(getattr(verdict, "value", verdict))

    def _fail_goal(self, goal: Goal, outcome: GoalOutcome) -> None:
        outcome.goal_failed = True
        try:
            fresh = self._goals.get(goal.goal_id) or goal
            fresh.recovery_attempts += 1
            fresh.transition(GoalState.FAILED)
            self._goals.save(fresh)
            goal.state = fresh.state
            goal.recovery_attempts = fresh.recovery_attempts
        except Exception:  # noqa: BLE001
            pass

    def _record_outcome(self, outcome: GoalOutcome) -> None:
        with self._lock:
            self._outcomes[outcome.goal_id] = outcome

    # -- cancellation ---------------------------------------------------

    def cancel(self, goal_id: str) -> GoalOutcome:
        """Durably cancel a goal and converge its in-flight task, if any.

        Both halves are needed and neither implies the other: the durable flag stops a
        restart resurrecting the goal, and cancelling the live task stops work that is
        already running. Marking only the goal would leave a task executing against a
        cancelled objective.
        """
        outcome = GoalOutcome(goal_id=str(goal_id or ""))
        goal = self._goals.get(goal_id)
        if goal is None:
            outcome.reason = "no such goal"
            self._record_outcome(outcome)
            return outcome
        goal.request_cancel()
        self._goals.save(goal)
        task_id = goal.current_task_id
        if task_id:
            cancelled = self._cancel_task(task_id)
            outcome.reason = ("task cancelled" if cancelled
                              else "goal cancelled; no live task to stop")
        else:
            outcome.reason = "goal cancelled"
        outcome.verdict = "cancelled"
        self._record_outcome(outcome)
        self._emit("autonomy_goal_cancelled", goal_id=goal.goal_id)
        return outcome

    def _cancel_task(self, task_id: str) -> bool:
        """Ask the Agent to cancel a task, using the one cancellation protocol."""
        for attr in ("cancel_active", "_cancel_active", "cancel_current"):
            method = getattr(self._agent, attr, None)
            if callable(method):
                try:
                    method()
                    return True
                except Exception:  # noqa: BLE001
                    return False
        token = getattr(self._agent, "_active_cancellation_token", None)
        if token is not None and hasattr(token, "cancel"):
            try:
                token.cancel()
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    # -- presentation --------------------------------------------------

    def snapshot(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """A bounded, redacted projection for the companion.

        The companion receives summaries and nothing else. It has no reference to the
        store, the scheduler or the goal model, so it cannot schedule, complete or
        cancel a goal even by accident.
        """
        rows: list[dict[str, Any]] = []
        try:
            goals = self._goals.active()
        except Exception:  # noqa: BLE001
            return rows
        for goal in sorted(goals, key=lambda g: g.updated_at, reverse=True):
            if len(rows) >= max(1, int(limit)):
                break
            row = summarise_goal(goal)
            outcome = self.outcome(goal.goal_id)
            if outcome is not None:
                row["last_verdict"] = outcome.verdict
            rows.append(row)
        return rows

    def summary_line(self, *, limit: int = 3) -> str:
        """One line, for a log or a console at startup."""
        rows = self.snapshot(limit=limit)
        if not rows:
            return "no active goals"
        parts = [f"{r['state']}: {r['objective'][:60]}" for r in rows]
        return " | ".join(parts)[:300]


def goal_from_mapping(data: Mapping[str, Any]) -> Goal:
    """Rebuild a goal from a mapping. Exposed for tests and for importers."""
    return Goal.from_dict(data)