"""M2.17 - the durable Goal: an ongoing, user-authorised objective.

A Goal is not a Task
====================

This is the distinction the whole milestone rests on, so it is worth being precise
about why it exists rather than just asserting it.

``TaskManager`` models **one attempt at one request**: it has a plan, steps that
move PENDING -> RUNNING -> DONE, a verification verdict and a recovery budget, and it
reaches a terminal state. It is written so a single turn can be reasoned about
exactly.

A Goal models **an objective that outlives any single attempt**. "Create the
project and make the tests pass" may need three tasks: scaffold, implement, run
tests. Each of those is a Task with its own plan and verdict. None of them is the
Goal. The Goal is the thing that knows all three are required, knows which have
evidence behind them, and survives the process exiting between them.

Consequences that fall out of keeping them separate:

* A Goal may produce many Tasks over time; a Task belongs to at most one Goal.
* A Goal is completed only by *accumulated verified evidence*, never because its
  most recent Task completed. A Task completing is not a Goal completing.
* Goal state cannot be expressed by widening ``TaskState``. That enum is frozen and
  heavily tested; overloading it would make "this task is done" and "this objective
  is achieved" indistinguishable, which is precisely the conflation that lets an
  agent claim success it did not earn.

Authority
=========

A Goal records *intent and authorisation*. It cannot execute anything. Every piece
of work a Goal implies becomes an ordinary authoritative Task through the existing
TaskManager, and therefore through the existing Executor, PermissionManager and
VerificationLedger. There is no Goal-side execution path, and
``tests/test_m217_persistent_autonomy.py`` asserts that at the import level.

Security
========

A persisted Goal is **data, never policy** (§38).

* ``objective`` and every free-text field are redacted on write with the same
  rules the companion uses, so a credential typed into a goal cannot come back out
  of the store.
* Nothing a Goal stores is ever promoted to a system instruction. Goals are read
  back as task *input*, through the normal request lifecycle, and the security
  rules that apply to any other user text apply to them unchanged.
* ``authorized`` is set by the caller that creates the Goal, never by model output,
  and a Goal with ``authorized=False`` is never scheduled.

Durability
==========

One JSON file per Goal under ``<sessions_dir>/goals/<goal_id>.json``, written with
:func:`memory.storage.save_json` - the repository's single atomic-write helper
(temp file in the same directory, then ``os.replace``). The store refuses unsafe
identifiers, versions its records, and treats an unreadable record as *absent*
rather than as an empty Goal, because "no work ever ran" and "we cannot tell what
ran" must not collapse into the same answer.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from memory.storage import load_json, save_json

__all__ = [
    "GoalState",
    "Goal",
    "GoalStore",
    "GoalError",
    "create_goal",
    "GOAL_SCHEMA_VERSION",
    "TERMINAL_GOAL_STATES",
    "ACTIVE_GOAL_STATES",
    "sanitize_goal_text",
]

#: Bumped when the persisted shape changes incompatibly. ``GoalStore`` migrates
#: older records forward rather than discarding them.
GOAL_SCHEMA_VERSION = 1


class GoalError(Exception):
    """A goal operation that could not be honoured, with a stable code."""

    def __init__(self, message: str, *, code: str = "goal_error") -> None:
        super().__init__(message)
        self.code = code


class GoalState(str, Enum):
    """The goal lifecycle.

    Not a superset of ``TaskState`` and deliberately not interchangeable with it.
    ``RESUMABLE`` is the state that makes a restart meaningful: work that was
    interrupted in a *safe* way and may be picked up again.
    """

    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    RESUMABLE = "resumable"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def from_value(cls, value: "GoalState | str") -> "GoalState":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower().replace("-", "_")
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(f"unknown goal state: {value!r}") from exc

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_GOAL_STATES


TERMINAL_GOAL_STATES = frozenset({
    GoalState.COMPLETED, GoalState.FAILED, GoalState.CANCELLED,
})
#: States in which a goal may still produce new tasks.
ACTIVE_GOAL_STATES = frozenset({
    GoalState.CREATED, GoalState.ACTIVE, GoalState.PAUSED, GoalState.RESUMABLE,
})

#: Declared transitions. Anything not listed is refused, so a stale write cannot
#: walk a finished goal backwards into looking live.
_GOAL_TRANSITIONS: dict[GoalState, frozenset[GoalState]] = {
    GoalState.CREATED: frozenset({
        GoalState.ACTIVE, GoalState.PAUSED, GoalState.CANCELLED, GoalState.FAILED}),
    GoalState.ACTIVE: frozenset({
        GoalState.PAUSED, GoalState.RESUMABLE, GoalState.COMPLETED,
        GoalState.FAILED, GoalState.CANCELLED}),
    GoalState.PAUSED: frozenset({
        GoalState.ACTIVE, GoalState.RESUMABLE, GoalState.CANCELLED,
        GoalState.FAILED}),
    GoalState.RESUMABLE: frozenset({
        GoalState.ACTIVE, GoalState.PAUSED, GoalState.COMPLETED,
        GoalState.FAILED, GoalState.CANCELLED}),
    GoalState.COMPLETED: frozenset(),
    GoalState.FAILED: frozenset({GoalState.ACTIVE, GoalState.RESUMABLE}),
    GoalState.CANCELLED: frozenset(),
}

# ── redaction ────────────────────────────────────────────────────────────
# Reused rather than reinvented: a Goal is persisted, so anything that reaches it
# will be read back from disk. The companion already solved this problem for the
# UI boundary and the same rules apply here.

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_\- ]?key|apikey|access[_\- ]?key|secret|token|password|passwd|"
    r"passphrase|credential|bearer|authorization|auth[_\- ]?token|session[_\- ]?key)\b"
    r"(\s*[:=]\s*|\s+(?:is|was|are|were)\s+|\s+)"
    r"((?:bearer\s+)?\S+)"
)
_BARE_SECRET = re.compile(
    r"\b(?:sk|rk|pk|ghp|gho|ghu|ghs|ghr|glpat|xox[baprs])[_\-][A-Za-z0-9._-]{8,}")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_HANDLE_VALUE = re.compile(
    r"(?i)\b(hwnd|handle|runtime[_\-]?id|process[_\-]?id|thread[_\-]?id|pid)\b"
    r"(\s*[:=]\s*|\s+)(0x[0-9a-f]+|\d{3,})"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")

#: A goal objective is short prose. Anything longer is a document, not an objective,
#: and storing it wholesale would put an unbounded blob into the durable record.
MAX_OBJECTIVE_CHARS = 2000
MAX_SUMMARY_CHARS = 400


def sanitize_goal_text(value: Any, *, limit: int = MAX_OBJECTIVE_CHARS) -> str:
    """Redact and bound one piece of goal text.

    Identical policy to the companion's ``sanitize_text`` and for the same reason:
    both are boundaries that persisted text crosses. Keeping them separate would
    mean two redaction policies to keep in step, which is how one of them ends up
    forgotten.
    """
    try:
        text = value if isinstance(value, str) else ("" if value is None else str(value))
    except Exception:  # noqa: BLE001 - a broken __str__ must not break persistence
        text = ""
    if not text:
        return ""
    text = _CONTROL.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)} [redacted]", text)
    text = _BEARER_TOKEN.sub("Bearer [redacted]", text)
    text = _BARE_SECRET.sub("[redacted]", text)
    text = _HANDLE_VALUE.sub(lambda m: f"{m.group(1)} [handle]", text)
    cap = max(1, int(limit))
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "\u2026"
    return text


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: ``metadata`` is persisted verbatim by ``to_dict`` unless it is bounded. Two limits,
#: both needed: a recursion depth cap so a self-referential structure cannot loop, and
#: an entry cap so a caller cannot write an unbounded blob into the durable record.
MAX_METADATA_DEPTH = 6
MAX_METADATA_ENTRIES = 200


def _sanitize_metadata(value: Any, *, _depth: int = 0) -> Any:
    """Make an arbitrary caller-supplied structure safe to persist.

    ``objective`` and the summaries go through :func:`sanitize_goal_text`, but
    ``metadata`` is a free-form mapping supplied by the caller and previously
    persisted with ``dict(self.metadata)``. That was a hole in the redaction claim
    and not a theoretical one: anything that reaches a goal's metadata - a nested
    ``{"request": {"headers": {"Authorization": "Bearer ..."}}}``, say - would be
    written to disk in the clear while the audit claimed goals redact on write.

    Scalars are redacted, containers are walked, and anything past the depth or
    entry cap is replaced rather than dropped, so the shape of the record stays
    legible to whoever has to diagnose it.
    """
    if _depth >= MAX_METADATA_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return sanitize_goal_text(value, limit=MAX_SUMMARY_CHARS)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_METADATA_ENTRIES:
                out["[truncated]"] = True
                break
            safe_key = sanitize_goal_text(key, limit=64) or "key"
            out[safe_key] = _sanitize_metadata(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        out_list: list[Any] = []
        for index, item in enumerate(list(value)):
            if index >= MAX_METADATA_ENTRIES:
                out_list.append("[truncated]")
                break
            out_list.append(_sanitize_metadata(item, _depth=_depth + 1))
        return out_list
    # An object with a hostile __str__ must not be able to break persistence.
    return sanitize_goal_text(type(value).__name__, limit=64)


def _valid_goal_id(goal_id: Any) -> bool:
    """Reject anything that could escape the goals directory or name a temp file."""
    text = str(goal_id or "")
    if not text or not _VALID_ID.match(text):
        return False
    return text not in {".", ".."} and ".." not in text


# ═════════════════════════════════════════════════════════════ the goal


@dataclass
class Goal:
    """One ongoing, user-authorised objective. Durable and self-describing."""

    goal_id: str = ""
    #: The user's objective, verbatim apart from redaction. Never policy.
    objective: str = ""
    state: GoalState = GoalState.CREATED
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    #: Task ids that belong to this goal, oldest first. A Goal owns many Tasks.
    task_ids: list[str] = field(default_factory=list)
    #: The task currently believed to be doing the work.
    current_task_id: str = ""

    #: Running, human-readable progress. Redacted on write like everything else.
    progress_summary: str = ""
    #: What the goal would do next, if it were resumed. Descriptive, not executable.
    next_action: str = ""

    #: Set by whoever created the goal. A goal created without authorisation is
    #: never scheduled, and this flag is never set from model output.
    authorized: bool = True
    #: Durable. A cancelled goal must not be resurrected by a restart.
    cancel_requested: bool = False

    #: Whether the goal is waiting on a permission decision. Never auto-confirmed.
    awaiting_permission: bool = False
    pending_authorization: str = ""

    #: Recovery bookkeeping, mirroring TaskManager's so exhaustion is visible.
    recovery_attempts: int = 0
    recovery_exhausted: bool = False

    #: Durable schedule intent. ``interval_seconds`` of 0 means "not scheduled".
    next_run_at: str = ""
    interval_seconds: int = 0
    #: Set once a due run has been started for a given ``next_run_at`` tick, so a
    #: repeated scheduler pass cannot start the same tick twice.
    last_started_tick: str = ""

    #: How many verified task results the goal requires before it may complete.
    #: Defaults to 1, but "create the project and make the tests pass" should set 3.
    required_evidence: int = 1

    schema_version: int = GOAL_SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- derived -------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def verified_task_ids(self) -> list[str]:
        """Task ids recorded in metadata as independently verified."""
        verified = self.metadata.get("verified_task_ids")
        if not isinstance(verified, list):
            return []
        return [str(item) for item in verified if str(item)]

    @property
    def has_required_evidence(self) -> bool:
        """Whether enough verified evidence exists to *consider* completion.

        This is a precondition, not a decision. ``mark_completed`` refuses without
        it; nothing sets a goal complete merely because its last task did.
        """
        return len(self.verified_task_ids) >= max(1, int(self.required_evidence))

    # -- lifecycle -----------------------------------------------------

    def transition(self, target: GoalState) -> bool:
        """Move to ``target`` if the table allows it. Never raises."""
        target = GoalState.from_value(target)
        if target is self.state:
            return True
        if target not in _GOAL_TRANSITIONS[self.state]:
            return False
        self.state = target
        self.updated_at = _now()
        return True

    def record_verified_task(self, task_id: str) -> bool:
        """Record that a task under this goal produced verified evidence.

        Idempotent: the same task recorded twice counts once, so a repeated
        completion event cannot inflate the evidence count toward completion.
        """
        ident = str(task_id or "").strip()
        if not ident:
            return False
        verified = self.verified_task_ids
        if ident in verified:
            return False
        verified.append(ident)
        self.metadata["verified_task_ids"] = verified
        self.updated_at = _now()
        return True

    def attach_task(self, task_id: str) -> bool:
        """Associate a task with this goal. Idempotent."""
        ident = str(task_id or "").strip()
        if not ident:
            return False
        if ident not in self.task_ids:
            self.task_ids.append(ident)
            self.current_task_id = ident
            self.updated_at = _now()
        return True

    def mark_completed(self) -> bool:
        """Complete the goal - only with sufficient verified evidence.

        This is the rule §35 exists to state: a Goal is not complete because a Task
        completed. Refusing here is what stops an unverified chain from reporting
        success.
        """
        if not self.has_required_evidence:
            return False
        return self.transition(GoalState.COMPLETED)

    def request_cancel(self) -> bool:
        """Durably cancel the goal. Idempotent and irreversible."""
        self.cancel_requested = True
        self.updated_at = _now()
        if self.state is GoalState.CANCELLED:
            return True
        return self.transition(GoalState.CANCELLED)

    def is_schedulable(self, *, now: str | None = None) -> bool:
        """Whether the scheduler may start work for this goal right now.

        Every condition is checked here rather than at the call site, so a second
        scheduler cannot reach a different conclusion about the same goal.
        """
        if self.cancel_requested or self.state is GoalState.CANCELLED:
            return False
        if not self.authorized:
            return False
        if self.awaiting_permission:
            return False
        if self.recovery_exhausted:
            return False
        if self.state not in ACTIVE_GOAL_STATES:
            return False
        if self.interval_seconds <= 0:
            return False
        if not self.next_run_at:
            return False
        stamp = now or _now()
        return self.next_run_at <= stamp

    # -- serialisation -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "objective": sanitize_goal_text(self.objective),
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "task_ids": list(self.task_ids),
            "current_task_id": self.current_task_id,
            "progress_summary": sanitize_goal_text(
                self.progress_summary, limit=MAX_SUMMARY_CHARS),
            "next_action": sanitize_goal_text(
                self.next_action, limit=MAX_SUMMARY_CHARS),
            "authorized": bool(self.authorized),
            "cancel_requested": bool(self.cancel_requested),
            "awaiting_permission": bool(self.awaiting_permission),
            "pending_authorization": sanitize_goal_text(self.pending_authorization, limit=200),
            "recovery_attempts": int(self.recovery_attempts),
            "recovery_exhausted": bool(self.recovery_exhausted),
            "next_run_at": self.next_run_at,
            "interval_seconds": int(self.interval_seconds),
            "last_started_tick": self.last_started_tick,
            "required_evidence": int(self.required_evidence),
            "schema_version": int(self.schema_version),
            # Bounded and redacted recursively. A free-form caller mapping is
            # persisted user data exactly like ``objective`` is, and treating it
            # differently would have made the redaction guarantee untrue.
            "metadata": _sanitize_metadata(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Goal":
        """Rebuild from a persisted record, tolerating an older or partial shape.

        Unknown keys are preserved in ``metadata`` rather than dropped, so a future
        field written by a newer build is not silently lost by an older one.
        """
        known = {
            "goal_id", "objective", "state", "created_at", "updated_at",
            "task_ids", "current_task_id", "progress_summary", "next_action",
            "authorized", "cancel_requested", "awaiting_permission",
            "pending_authorization", "recovery_attempts", "recovery_exhausted",
            "next_run_at", "interval_seconds", "last_started_tick",
            "required_evidence", "schema_version", "metadata",
        }
        raw_metadata = data.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        for key, value in data.items():
            if key not in known:
                metadata.setdefault(key, value)
        try:
            state = GoalState.from_value(data.get("state") or GoalState.CREATED)
        except ValueError:
            # An unreadable state is not a reason to invent a live goal.
            state = GoalState.CREATED
        task_ids = data.get("task_ids")
        return cls(
            goal_id=str(data.get("goal_id") or ""),
            objective=str(data.get("objective") or ""),
            state=state,
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
            task_ids=[str(t) for t in task_ids] if isinstance(task_ids, list) else [],
            current_task_id=str(data.get("current_task_id") or ""),
            progress_summary=str(data.get("progress_summary") or ""),
            next_action=str(data.get("next_action") or ""),
            authorized=bool(data.get("authorized", True)),
            cancel_requested=bool(data.get("cancel_requested", False)),
            awaiting_permission=bool(data.get("awaiting_permission", False)),
            pending_authorization=str(data.get("pending_authorization") or ""),
            recovery_attempts=int(data.get("recovery_attempts", 0) or 0),
            recovery_exhausted=bool(data.get("recovery_exhausted", False)),
            next_run_at=str(data.get("next_run_at") or ""),
            interval_seconds=int(data.get("interval_seconds", 0) or 0),
            last_started_tick=str(data.get("last_started_tick") or ""),
            required_evidence=max(1, int(data.get("required_evidence", 1) or 1)),
            schema_version=int(data.get("schema_version", GOAL_SCHEMA_VERSION) or 1),
            metadata=metadata,
        )


def create_goal(objective: str, *, goal_id: str = "", authorized: bool = True,
                required_evidence: int = 1, interval_seconds: int = 0,
                next_run_at: str = "", metadata: Mapping[str, Any] | None = None,
                ) -> Goal:
    """Build a new goal with a durable identity.

    Refuses an empty objective rather than creating a goal that can never be
    worked, and refuses an unsafe identifier rather than sanitising one into
    something the caller did not ask for.
    """
    clean = sanitize_goal_text(objective)
    if not clean:
        raise GoalError("a goal needs an objective", code="empty_objective")
    ident = str(goal_id or "").strip() or uuid.uuid4().hex
    if not _valid_goal_id(ident):
        raise GoalError(f"unsafe goal_id: {goal_id!r}", code="unsafe_goal_id")
    stamp = _now()
    return Goal(
        goal_id=ident,
        objective=clean,
        state=GoalState.CREATED,
        created_at=stamp,
        updated_at=stamp,
        authorized=bool(authorized),
        required_evidence=max(1, int(required_evidence)),
        interval_seconds=max(0, int(interval_seconds)),
        next_run_at=str(next_run_at or ""),
        metadata=_sanitize_metadata(metadata or {}),
    )


# ═════════════════════════════════════════════════════════════ the store


class GoalStore:
    """One JSON file per goal, written atomically.

    Deliberately shaped like ``TaskStore`` - same directory-under-sessions
    convention, same ``memory.storage`` helpers, same refuse-unsafe-identifier rule -
    so an operator has one persistence idiom to reason about. It is a *different
    store* serving a *different* entity; it is not a second TaskStore, and it never
    holds task or plan state.
    """

    #: Name of the directory under ``sessions_dir``.
    DIRECTORY = "goals"

    def __init__(self, sessions_dir: Any) -> None:
        from pathlib import Path

        self.sessions_dir = Path(sessions_dir)
        self.goals_dir = self.sessions_dir / self.DIRECTORY
        self.goals_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, goal_id: str) -> Any:
        if not _valid_goal_id(goal_id):
            raise GoalError(f"unsafe goal_id: {goal_id!r}", code="unsafe_goal_id")
        return self.goals_dir / f"{goal_id}.json"

    # -- writes --------------------------------------------------------

    def create(self, goal: Goal) -> Goal:
        """Persist a new goal. Refuses to overwrite an existing one."""
        path = self._path(goal.goal_id)
        if path.exists():
            raise GoalError(
                f"goal {goal.goal_id} already exists", code="goal_exists")
        save_json(path, goal.to_dict())
        return goal

    def save(self, goal: Goal) -> Goal:
        """Persist a goal, creating it if absent.

        Used by the scheduler and by resume. Writes the whole record atomically, so a
        crash mid-write leaves the previous record intact rather than a partial one.
        """
        save_json(self._path(goal.goal_id), goal.to_dict())
        return goal

    def update(self, goal_id: str, **fields: Any) -> Goal | None:
        """Read-modify-write. Returns the saved goal, or None if it is gone.

        Read-modify-write is acceptable here because the store is only ever written
        by the single owning scheduler or resume path, both of which hold the
        authoritative in-memory goal for the duration of their tick.
        """
        goal = self.get(goal_id)
        if goal is None:
            return None
        for key, value in fields.items():
            if hasattr(goal, key):
                setattr(goal, key, value)
            else:
                goal.metadata[key] = value
        goal.updated_at = _now()
        return self.save(goal)

    # -- reads ---------------------------------------------------------

    def get(self, goal_id: str) -> Goal | None:
        """Load one goal. An unreadable record reads as *absent*, not as empty."""
        if not _valid_goal_id(goal_id):
            return None
        path = self.goals_dir / f"{goal_id}.json"
        if not path.exists():
            return None
        data = load_json(path, None)
        if not isinstance(data, Mapping) or not data:
            return None
        try:
            goal = Goal.from_dict(data)
        except Exception:  # noqa: BLE001 - a hostile record must not crash a caller
            return None
        # Never trust a persisted goal_id over the filename it was read from.
        goal.goal_id = str(goal_id)
        return goal

    def list_all(self) -> list[Goal]:
        """Every readable goal. Unreadable files are skipped, never guessed at."""
        goals: list[Goal] = []
        try:
            files = sorted(self.goals_dir.glob("*.json"))
        except OSError:
            return goals
        for path in files:
            goal = self.get(path.stem)
            if goal is not None:
                goals.append(goal)
        return goals

    def active(self) -> list[Goal]:
        """Goals that are not in a terminal state."""
        return [g for g in self.list_all() if not g.is_terminal]

    def schedulable(self, *, now: str | None = None) -> list[Goal]:
        """Goals the scheduler may act on right now."""
        return [g for g in self.list_all() if g.is_schedulable(now=now)]

    def delete(self, goal_id: str) -> bool:
        if not _valid_goal_id(goal_id):
            return False
        path = self.goals_dir / f"{goal_id}.json"
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False