from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from config.settings import Settings
from core.task_manager import (
    STEP_SATISFIED_STATES,
    STEP_TERMINAL_STATES,
    STEP_UNSATISFIABLE_STATES,
    StepStatus,
)

#: Schema version for the durable plan record. Bump on any incompatible change
#: to PlanStepRecord / ExecutionPlanRecord. Records written by an older version
#: are migrated by :func:`migrate_plan_record`.
PLAN_SCHEMA_VERSION = 1

#: Tools that only read. A plan step using one of these is safe to replay, so it
#: carries no ``side_effect_key`` and restart reconciliation may re-dispatch it.
READ_ONLY_TOOL_PREFIXES = (
    "filesystem.read_file",
    "filesystem.list_dir",
    "project.info",
    "project.files",
    "project.grep",
    "system.info",
    "git.status",
    "git.diff",
    "git.log",
    "memory.search",
    "memory.read",
    "rules.list",
    "task.info",
    "task.list",
    "applications.list",
)


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def tool_is_read_only(tool: str) -> bool:
    name = str(tool or "")
    return any(name == p or name.startswith(p) for p in READ_ONLY_TOOL_PREFIXES)


def side_effect_key(tool: str, args: dict) -> str:
    """Stable fingerprint of a step's side effect.

    This is a *replay fingerprint*, not a semantic idempotency key: it lets
    restart reconciliation tell "this exact effect was already attempted" from
    "never attempted". It cannot prove an effect did not partially apply.
    """
    if tool_is_read_only(tool):
        return ""
    try:
        canonical = json.dumps(args or {}, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = str(args)
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{tool}:{digest}"


class PlanStepError(ValueError):
    pass


class PlanValidationError(ValueError):
    pass


@dataclass
class Intent:
    name: str
    steps: list[tuple[str, dict]] = field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    tool: str
    args: object
    depends_on: tuple[str, ...] = ()
    expected_result: dict | None = None
    recovery_hint: str = ""


@dataclass(frozen=True)
class ExecutionPlan:
    goal: str
    steps: tuple[PlanStep, ...]

    @classmethod
    def from_intent(cls, intent: Intent, goal: str = "") -> "ExecutionPlan":
        steps = []
        previous = ""
        for index, raw in enumerate(intent.steps, start=1):
            step_id = f"step-{index}"
            tool = raw[0] if isinstance(raw, (tuple, list)) and len(raw) == 2 else None
            args = raw[1] if isinstance(raw, (tuple, list)) and len(raw) == 2 else None
            steps.append(PlanStep(
                step_id=step_id,
                tool=tool,
                args=args,
                depends_on=(previous,) if previous else (),
            ))
            previous = step_id
        return cls(goal=goal or intent.summary or intent.name, steps=tuple(steps))

    @classmethod
    def from_tool_calls(cls, calls: list, goal: str = "") -> "ExecutionPlan":
        steps = []
        previous = ""
        used_ids: set[str] = set()
        for index, call in enumerate(calls, start=1):
            call_id = str(getattr(call, "id", "") or "").strip()
            step_id = call_id or f"step-{index}"
            if step_id in used_ids:
                step_id = f"{step_id}-{index}"
            used_ids.add(step_id)
            steps.append(PlanStep(
                step_id=step_id,
                tool=getattr(call, "name", None),
                args=getattr(call, "arguments", None),
                depends_on=(previous,) if previous else (),
            ))
            previous = step_id
        return cls(goal=goal, steps=tuple(steps))

    def validate(self, registry) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise PlanValidationError("plan goal must be a non-empty string")
        if not self.steps:
            raise PlanValidationError("plan must contain at least one step")
        known: set[str] = set()
        for index, step in enumerate(self.steps):
            if not isinstance(step.step_id, str) or not step.step_id.strip():
                raise PlanValidationError(f"step {index + 1} has an invalid id")
            if step.step_id in known:
                raise PlanValidationError(f"duplicate plan step id: {step.step_id}")
            known.add(step.step_id)
            if not isinstance(step.tool, str) or not step.tool.strip():
                raise PlanValidationError(f"step {step.step_id} has an invalid tool")
            if not isinstance(step.args, dict):
                raise PlanValidationError(f"step {step.step_id} arguments must be an object")
            spec = registry.get(step.tool)
            if spec is None:
                raise PlanValidationError(f"step {step.step_id} references unknown tool {step.tool}")
            missing = [name for name in (spec.required or []) if name not in step.args]
            if missing:
                raise PlanValidationError(f"step {step.step_id} is missing required arguments: {', '.join(missing)}")
            properties = (spec.parameters or {}).get("properties", {}) or {}
            for name, value in step.args.items():
                schema = properties.get(name)
                if isinstance(schema, dict) and "type" in schema and not _matches_type(value, schema["type"]):
                    raise PlanValidationError(f"step {step.step_id} argument {name!r} must be {schema['type']}")
            if callable(spec.validate_args):
                try:
                    spec.validate_args(step.args)
                except PlanValidationError:
                    raise
                except (TypeError, ValueError) as exc:
                    raise PlanValidationError(f"step {step.step_id} arguments are invalid: {exc}") from exc
            dependencies = step.depends_on or ()
            if not isinstance(dependencies, (tuple, list)):
                raise PlanValidationError(f"step {step.step_id} dependencies must be a sequence")
            for dependency in dependencies:
                if dependency == step.step_id:
                    raise PlanValidationError(f"step {step.step_id} cannot depend on itself")
                if dependency not in known:
                    raise PlanValidationError(f"step {step.step_id} has unknown dependency {dependency}")


@dataclass
class PlanStepRecord:
    """Durable, mutable state for exactly one step of one task's plan.

    Deliberately *not* frozen: this is the live execution record that
    write-ahead persistence updates. ``ExecutionPlan`` stays frozen and remains
    the immutable validated intent.
    """

    step_id: str
    tool: str
    args: dict = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    expected_result: dict | None = None
    recovery_hint: str = ""
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: str = ""
    finished_at: str = ""
    detail: str = ""
    result_summary: str = ""
    checkpoint_id: str = ""
    side_effect_key: str = ""
    interrupted: bool = False
    interrupted_at: str = ""
    verification_state: str = ""
    capability: str = ""

    @classmethod
    def from_plan_step(cls, step: "PlanStep") -> "PlanStepRecord":
        args = step.args if isinstance(step.args, dict) else {}
        return cls(
            step_id=str(step.step_id),
            tool=str(step.tool or ""),
            args=dict(args),
            depends_on=tuple(step.depends_on or ()),
            expected_result=dict(step.expected_result) if isinstance(step.expected_result, dict) else None,
            recovery_hint=str(step.recovery_hint or ""),
            side_effect_key=side_effect_key(step.tool, args),
        )

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "args": dict(self.args or {}),
            "depends_on": list(self.depends_on or ()),
            "expected_result": dict(self.expected_result) if isinstance(self.expected_result, dict) else None,
            "recovery_hint": self.recovery_hint,
            "status": self.status.value if isinstance(self.status, StepStatus) else str(self.status),
            "attempts": int(self.attempts or 0),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
            "result_summary": self.result_summary,
            "checkpoint_id": self.checkpoint_id,
            "side_effect_key": self.side_effect_key,
            "interrupted": bool(self.interrupted),
            "interrupted_at": self.interrupted_at,
            "verification_state": self.verification_state,
            "capability": self.capability,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStepRecord":
        if not isinstance(data, dict):
            raise PlanStepError("plan step must be an object")
        step_id = str(data.get("step_id") or "")
        if not step_id:
            raise PlanStepError("plan step requires a step_id")
        raw_status = str(data.get("status") or StepStatus.PENDING.value)
        try:
            status = StepStatus(raw_status)
        except ValueError as exc:
            raise PlanStepError(f"unknown plan step status: {raw_status!r}") from exc
        depends = data.get("depends_on")
        if depends is None:
            depends_tuple: tuple[str, ...] = ()
        elif isinstance(depends, (list, tuple)):
            depends_tuple = tuple(str(item) for item in depends)
        else:
            raise PlanStepError("depends_on must be a list")
        expected = data.get("expected_result")
        args = data.get("args")
        return cls(
            step_id=step_id,
            tool=str(data.get("tool") or ""),
            args=dict(args) if isinstance(args, dict) else {},
            depends_on=depends_tuple,
            expected_result=dict(expected) if isinstance(expected, dict) else None,
            recovery_hint=str(data.get("recovery_hint") or ""),
            status=status,
            attempts=max(0, int(data.get("attempts") or 0)),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            detail=str(data.get("detail") or ""),
            result_summary=str(data.get("result_summary") or ""),
            checkpoint_id=str(data.get("checkpoint_id") or ""),
            side_effect_key=str(data.get("side_effect_key") or ""),
            interrupted=bool(data.get("interrupted", False)),
            interrupted_at=str(data.get("interrupted_at") or ""),
            verification_state=str(data.get("verification_state") or ""),
            capability=str(data.get("capability") or ""),
        )

    # -- state machine ---------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in STEP_TERMINAL_STATES

    @property
    def is_satisfied(self) -> bool:
        return self.status in STEP_SATISFIED_STATES

    @property
    def is_unsatisfiable(self) -> bool:
        return self.status in STEP_UNSATISFIABLE_STATES

    @property
    def is_replayable(self) -> bool:
        """Whether re-dispatching this step is free of side effects.

        Read-only steps are always safe. A side-effecting step that already
        succeeded is not replayed either. Anything else may have applied a
        partial effect and must not be silently repeated.
        """
        if self.status is StepStatus.DONE:
            return False
        if self.side_effect_key:
            return False
        return tool_is_read_only(self.tool)

    def dependencies_satisfied(self, by_id: dict[str, "PlanStepRecord"]) -> bool:
        return all(
            dep in by_id and by_id[dep].status in STEP_SATISFIED_STATES
            for dep in (self.depends_on or ())
        )

    def dependencies_unsatisfiable(self, by_id: dict[str, "PlanStepRecord"]) -> bool:
        """True when a dependency can never reach DONE.

        A dependency id that is absent from the plan counts as unsatisfiable:
        it can never be satisfied, and treating it as merely "not yet" would
        deadlock the plan with no diagnostic.
        """
        for dep in self.depends_on or ():
            if dep not in by_id:
                return True
            if by_id[dep].status in STEP_UNSATISFIABLE_STATES:
                return True
        return False

    # -- transitions -----------------------------------------------------

    def begin_attempt(self, now: str = "") -> None:
        """Write-ahead: mark RUNNING *before* the side effect is dispatched."""
        self.status = StepStatus.RUNNING
        self.attempts += 1
        self.started_at = now or _now()
        self.finished_at = ""
        self.interrupted = False
        self.interrupted_at = ""

    def finish(
        self,
        status: StepStatus,
        detail: str = "",
        result_summary: str = "",
        now: str = "",
    ) -> None:
        target = status if isinstance(status, StepStatus) else StepStatus(str(status))
        if target not in (
            StepStatus.DONE,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
            StepStatus.BLOCKED,
            StepStatus.SKIPPED,
        ):
            raise PlanStepError(f"cannot finish a step as {target.value}")
        self.status = target
        self.detail = str(detail or "")
        self.result_summary = str(result_summary or "")
        self.finished_at = now or _now()
        self.interrupted = False
        self.interrupted_at = ""

    def mark_interrupted(self, now: str = "") -> None:
        """Classify a step left RUNNING by a process death.

        The status is deliberately left as RUNNING: the step may or may not have
        applied its side effect, and only an explicit resume decision may
        resolve that. The flag is what makes the ambiguity queryable.
        """
        self.interrupted = True
        self.interrupted_at = now or _now()
        self.detail = self.detail or "Interrupted: process exited while this step was running."

    def to_public_dict(self, include_args: bool = False) -> dict:
        """Inspection shape. Raw args are withheld unless explicitly requested."""
        data = self.to_dict()
        if not include_args:
            data["args"] = {"__withheld__": True, "arg_count": len(self.args or {})}
        return data


@dataclass
class ExecutionPlanRecord:
    """Durable plan for exactly one task.

    Owned by ``TaskRecord.plan``. Holds no task identity: ``task_id`` and
    ``run_id`` stay authoritative on ``TaskManager`` / ``TaskRecord``.

    ``superseded`` keeps a bounded summary of plans this one replaced. The LLM
    loop re-plans on every reasoning turn, so the live record describes the
    work currently in flight; without this the persisted plan would only ever
    describe the final turn and earlier steps would vanish from the record.
    """

    goal: str = ""
    created_at: str = ""
    plan_version: int = PLAN_SCHEMA_VERSION
    steps: list[PlanStepRecord] = field(default_factory=list)
    superseded: list[dict] = field(default_factory=list)

    #: Upper bound on retained superseded plans.
    MAX_SUPERSEDED = 20

    @classmethod
    def from_plan(cls, plan: "ExecutionPlan", created_at: str = "") -> "ExecutionPlanRecord":
        return cls(
            goal=str(plan.goal or ""),
            created_at=created_at or _now(),
            plan_version=PLAN_SCHEMA_VERSION,
            steps=[PlanStepRecord.from_plan_step(step) for step in plan.steps],
        )

    def absorb(self, previous: "ExecutionPlanRecord | None") -> None:
        """Retire ``previous`` into ``superseded`` before replacing it."""
        if previous is None or not previous.steps:
            return
        self.superseded.append(previous.summary())
        del self.superseded[: max(0, len(self.superseded) - self.MAX_SUPERSEDED)]

    def summary(self) -> dict:
        return {
            "goal": self.goal,
            "created_at": self.created_at,
            "counts": self.counts(),
            "steps": [
                {
                    "step_id": item.step_id,
                    "tool": item.tool,
                    "status": item.status.value,
                    "attempts": item.attempts,
                    "side_effect_key": item.side_effect_key,
                    "result_summary": item.result_summary,
                }
                for item in self.steps
            ],
        }

    def to_dict(self) -> dict:
        return {
            "plan_version": int(self.plan_version or PLAN_SCHEMA_VERSION),
            "goal": self.goal,
            "created_at": self.created_at,
            "steps": [step.to_dict() for step in self.steps],
            "superseded": [dict(item) for item in self.superseded],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionPlanRecord":
        if not isinstance(data, dict):
            raise PlanStepError("plan must be an object")
        raw_steps = data.get("steps")
        if raw_steps is None:
            raw_steps = []
        if not isinstance(raw_steps, list):
            raise PlanStepError("plan steps must be a list")
        raw_superseded = data.get("superseded")
        if raw_superseded is None:
            raw_superseded = []
        if not isinstance(raw_superseded, list):
            raise PlanStepError("superseded must be a list")
        try:
            version = int(data.get("plan_version") or PLAN_SCHEMA_VERSION)
        except (TypeError, ValueError):
            version = PLAN_SCHEMA_VERSION
        return cls(
            goal=str(data.get("goal") or ""),
            created_at=str(data.get("created_at") or ""),
            plan_version=version,
            steps=[PlanStepRecord.from_dict(item) for item in raw_steps],
            superseded=[dict(item) for item in raw_superseded if isinstance(item, dict)],
        )

    # -- lookup / dependency graph ---------------------------------------

    def step(self, step_id: str) -> PlanStepRecord | None:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        return None

    def by_id(self) -> dict[str, PlanStepRecord]:
        return {item.step_id: item for item in self.steps}

    def check_cycles(self) -> list[str]:
        """Return step ids participating in a dependency cycle, if any."""
        index = self.by_id()
        colour: dict[str, int] = {}
        cycles: list[str] = []

        def visit(node: str, trail: list[str]) -> None:
            colour[node] = 1
            for dep in index[node].depends_on if node in index else ():
                if dep not in index:
                    continue
                state = colour.get(dep, 0)
                if state == 1:
                    cycles.extend(trail[trail.index(dep):] if dep in trail else [dep])
                elif state == 0:
                    visit(dep, trail + [dep])
            colour[node] = 2

        for step_id in index:
            if colour.get(step_id, 0) == 0:
                visit(step_id, [step_id])
        return sorted(set(cycles))

    def propagate_skipped(self) -> list[str]:
        """Mark PENDING steps SKIPPED when a dependency can never be satisfied.

        Runs to a fixed point so a skip cascades through the whole downstream
        cone. Returns the ids newly marked.
        """
        newly: list[str] = []
        changed = True
        while changed:
            changed = False
            index = self.by_id()
            for item in self.steps:
                if item.status is not StepStatus.PENDING:
                    continue
                if item.dependencies_unsatisfiable(index):
                    item.finish(StepStatus.SKIPPED, detail="Skipped: an unmet dependency can no longer succeed.")
                    newly.append(item.step_id)
                    changed = True
        return newly

    def next_executable(self) -> list[PlanStepRecord]:
        """Dependency-gated runnable steps, in plan order.

        A step is runnable only when it is PENDING, not interrupted, and every
        dependency is DONE. With request concurrency fixed at 1 the executor
        takes the first entry; the full list is what a future scheduler would
        consume.
        """
        self.propagate_skipped()
        index = self.by_id()
        return [
            item
            for item in self.steps
            if item.status is StepStatus.PENDING
            and not item.interrupted
            and item.dependencies_satisfied(index)
        ]

    def next_step(self) -> PlanStepRecord | None:
        runnable = self.next_executable()
        return runnable[0] if runnable else None

    def counts(self) -> dict[str, int]:
        tally = {status.value: 0 for status in StepStatus}
        for item in self.steps:
            tally[item.status.value] = tally.get(item.status.value, 0) + 1
        return tally

    @property
    def is_complete(self) -> bool:
        return bool(self.steps) and all(item.status in STEP_TERMINAL_STATES for item in self.steps)

    def interrupted_steps(self) -> list[PlanStepRecord]:
        return [item for item in self.steps if item.interrupted]

    def to_public_dict(self, include_args: bool = False) -> dict:
        return {
            "plan_version": int(self.plan_version or PLAN_SCHEMA_VERSION),
            "goal": self.goal,
            "created_at": self.created_at,
            "counts": self.counts(),
            "is_complete": self.is_complete,
            "superseded_plan_count": len(self.superseded),
            "interrupted_step_ids": [item.step_id for item in self.interrupted_steps()],
            "next_executable_step_ids": [item.step_id for item in self.next_executable()],
            "steps": [item.to_public_dict(include_args=include_args) for item in self.steps],
        }


def migrate_plan_record(data: dict) -> dict:
    """Upgrade a persisted plan dict to :data:`PLAN_SCHEMA_VERSION`.

    Total over unknown input: anything unrecognised is preserved rather than
    dropped, and a record that cannot be understood raises so the caller can
    treat the plan as absent instead of resuming from nonsense.
    """
    if not isinstance(data, dict):
        raise PlanStepError("plan must be an object")
    try:
        version = int(data.get("plan_version") or 0)
    except (TypeError, ValueError):
        version = 0
    migrated = dict(data)
    if version < 1:
        # v0 was the unversioned shape: steps carried only the intent fields.
        steps = []
        for raw in migrated.get("steps") or []:
            if not isinstance(raw, dict):
                raise PlanStepError("plan step must be an object")
            step = dict(raw)
            step.setdefault("status", StepStatus.PENDING.value)
            step.setdefault("attempts", 0)
            step.setdefault("interrupted", False)
            step.setdefault("side_effect_key", side_effect_key(step.get("tool", ""), step.get("args") or {}))
            steps.append(step)
        migrated["steps"] = steps
        migrated["plan_version"] = 1
    return migrated


def _matches_type(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _clean_app_name(name: str) -> str:
    name = re.sub(r"^(the|please|kindly)\s+", "", name).strip().rstrip(".")
    return name.strip("\"'`.,!?")


class Planner:
    OPEN_RE = re.compile(r"^(?:open|launch|start)\s+(.+)$", re.I)
    HELLO_RE = re.compile(r"(?:hello|world)\b", re.I)

    def __init__(self, settings: Settings):
        self._settings = settings

    def fast_intent(self, text: str) -> Intent | None:
        stripped = text.strip()
        if not stripped:
            return None

        match = self.OPEN_RE.match(stripped)
        if match:
            app = _clean_app_name(match.group(1))
            if app.lower() not in ("ai", "openai", "source"):
                return Intent(
                    name="open_app",
                    steps=[("applications.open", {"app": app})],
                    summary=f"Opening {app}.",
                )

        lowered = text.lower()
        if "hello" in lowered and ("python" in lowered or "world" in lowered):
            target = self._settings.scratch_dir / "hello_world.py"
            return Intent(
                name="create_hello_python",
                steps=[
                    ("filesystem.write_file", {"path": str(target), "content": 'print("Hello World")\n'}),
                    ("python.run_script", {"script": str(target)}),
                ],
                summary="Creating a Python file that prints Hello World, running it, and verifying the output.",
            )

        if re.search(r"\b(?:cpu|memory|ram|system\s*info(?:rmation)?|systeminfo)\b", lowered):
            if " " in stripped:
                return Intent(
                    name="system_info",
                    steps=[("system.info", {"components": ["cpu", "memory", "os"]})],
                    summary="Reading system information.",
                )
        return None

    @staticmethod
    def matches_greeting(text: str) -> bool:
        return bool(re.fullmatch(r"[\s,]*(diablo[,\s]*)?(hello|hi|hey)[\s!.]*", text.strip(), re.I))

    _DEBUG_VERBS = ("debug", "fix", "broken", "failing", "traceback", "exception", "error in", "fails", "crash", "bug")
    _DEV_VERBS = ("add a", "add new", "implement", "refactor", "write a", "build", "create", "update")

    def coding_intent(self, text: str) -> str | None:
        lowered = (text or "").lower()
        if any(v in lowered for v in self._DEBUG_VERBS):
            return "debug"
        if any(v in lowered for v in self._DEV_VERBS):
            return "develop"
        return None
