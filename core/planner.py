from __future__ import annotations

import re
from dataclasses import dataclass, field

from config.settings import Settings


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
