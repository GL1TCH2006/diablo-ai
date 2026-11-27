"""M2.16.6 desktop workflow orchestration.

Composition, not capability
===========================

This module contains **no native Windows code**. It cannot import ``pywin32``,
``comtypes`` or anything under :mod:`core.windows`, because every native concern
already has exactly one owner and a second route to the desktop is how the wrong
window gets clicked. A workflow step is a *capability request* against the
already-registered ``windows`` capability, dispatched through
:class:`integration.contracts.CapabilityRegistry`.

That is the whole design. The milestones before this one each added a primitive:

===================  ==========================================================
M2.16.1              process and window identity, anchored to creation time
M2.16.2 / .3         anchored pointer and keyboard input
M2.16.4              display topology and bounded capture
M2.16.5              UI Automation discovery, targets and five actions
===================  ==========================================================

What was missing is the layer that *sequences* them - "open Calculator, focus it,
find the button with this automation id, invoke it, read the result back, and
prove the result is right". That is what a workflow is, and it is deliberately the
only new thing here.

Three properties the composition must not lose
=============================================

**Anchoring.** Every element of :class:`DesktopContext` is a *logical* reference:
a process identity (pid **plus** creation time), a window key, a UIA target
recipe, an optional display id. Never a handle. ``WindowIdentity.key`` is the
durable form the M2.16.1 layer already publishes, and re-resolving it is how a
recycled window handle gets caught rather than acted on.

**Verification.** "The Windows API returned" is not "the workflow succeeded".
Every step carries an observation and, where a claim can be checked, a
verification requirement. A step that cannot be observed reports ``UNVERIFIED``
rather than passing - the same discipline M2.16.5 applied to ``Invoke``, which has
no state of its own and is honestly unverifiable without a witness.

**No blind replay.** A desktop mutation may have applied before the process died.
:meth:`DesktopStep.is_replayable` follows the same rule as
:meth:`core.planner.PlanStepRecord.is_replayable`: a side-effecting step that did
not cleanly succeed must not be re-dispatched. Recovery re-anchors a *target*; it
never re-sends an *action*.

Cancellation, permissions and identity are not reimplemented here either. The
workflow borrows the executor's token, routes every primitive through
:class:`config.permissions.PermissionManager`, and takes its ids from the registry
so a model cannot supply one.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable

from core.cancellation import cancellation_requested
from core.logging_setup import get_logger
from integration.contracts import (
    CapabilityCancellationError,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    CoreEventBusPublisher,
)
from integration.protocol import IntegrationResult

log = get_logger("desktop_workflow")

__all__ = [
    "DesktopWorkflow",
    "DesktopContext",
    "DesktopStep",
    "DesktopStepResult",
    "DesktopWorkflowPlan",
    "DesktopWorkflowResult",
    "DesktopWorkflowError",
    "DesktopErrorCode",
    "TargetingMode",
    "WINDOW_CAPABILITY",
    "DESKTOP_OPERATIONS",
    "READ_ONLY_OPERATIONS",
    "MUTATING_OPERATIONS",
    "UIA_ACTION_BY_OPERATION",
]


# ═════════════════════════════════════════════════════════════ vocabulary


class DesktopErrorCode(str, Enum):
    """Why a desktop workflow stopped.

    Deliberately specific. The M2.16.1-.5 layers can each report "not found", but
    the difference between *no such window* and *two windows match* is the whole
    difference between acting and refusing, and collapsing them into one generic
    failure is what makes a workflow quietly guess.

    The codes the planner and the ledger read are the values; the values are the
    vocabulary, so they are spelled once here rather than constructed from strings
    at each raise site.
    """

    APPLICATION_NOT_FOUND = "APPLICATION_NOT_FOUND"
    PROCESS_NOT_FOUND = "PROCESS_NOT_FOUND"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    WINDOW_AMBIGUOUS = "WINDOW_AMBIGUOUS"
    WINDOW_STALE = "WINDOW_STALE"
    FOCUS_FAILED = "FOCUS_FAILED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    TARGET_STALE = "TARGET_STALE"
    ACTION_REJECTED = "ACTION_REJECTED"
    ACTION_FAILED = "ACTION_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_WORKFLOW = "INVALID_WORKFLOW"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"

    @classmethod
    def from_value(cls, value: "DesktopErrorCode | str | None") -> "DesktopErrorCode | None":
        if value is None or value == "":
            return None
        if isinstance(value, cls):
            return value
        text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return cls(text)
        except ValueError:
            return None


class TargetingMode(str, Enum):
    """How an interaction actually reached the desktop.

    Recorded on every action step. A workflow that quietly downgraded a semantic
    UIA invocation into a coordinate click would still "work", and nothing in the
    result would say so - which is precisely the failure this milestone exists to
    prevent. So the mode is evidence, not a comment.
    """

    UIA = "UIA"
    KEYBOARD = "KEYBOARD"
    POINTER = "POINTER"
    NONE = "NONE"

    @classmethod
    def from_value(cls, value: "TargetingMode | str | None") -> "TargetingMode":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().upper()
        for member in cls:
            if member.value == text:
                return member
        return cls.NONE


#: The single capability a desktop workflow composes. There is no second native
#: layer to reach for, and this constant exists so that the dependency is stated
#: once rather than spelled as a string at eight call sites.
WINDOW_CAPABILITY = "windows"

#: Operations that only read. Mutating anything is a change to another
#: application's UI, which is a mutation of the user's machine.
READ_ONLY_OPERATIONS = frozenset({
    "list_windows", "get_window", "get_foreground_window", "get_cursor_pos",
    "list_displays", "primary_display", "virtual_desktop", "get_display",
    "point_to_display",
    "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
    "uia_target_point", "visual_resolve_target",
})

#: Operations that change the desktop. Mirrors, and is kept adjacent to, the
#: M2.16.5 declaration in ``core.windows.capabilities`` - the two must agree, and
#: :func:`_assert_mutating_sets_agree` below is what keeps them from drifting.
MUTATING_OPERATIONS = frozenset({
    "terminate", "window_focus", "window_minimize", "window_restore",
    "window_close",
    "mouse_move", "mouse_left_click", "mouse_right_click", "mouse_double_click",
    "key_press", "key_release", "key_tap", "hotkey", "type_text",
    "capture_screen",
    "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
    "uia_expand_collapse",
})

#: Interaction intent -> (window operation, UIA pattern). This is the UIA-first
#: policy in one table: a caller names the *intent* ("click the OK button") and the
#: workflow chooses the pattern and the primitive, rather than the caller choosing
#: a COM interface or remembering the operation's name.
UIA_ACTION_BY_OPERATION = {
    "invoke": ("uia_invoke", "Invoke"),
    "set_value": ("uia_set_value", "Value"),
    "toggle": ("uia_toggle", "Toggle"),
    "select": ("uia_select", "SelectionItem"),
    "expand_collapse": ("uia_expand_collapse", "ExpandCollapse"),
}

#: The five workflow operations exposed to the agent. Small on purpose: the point
#: is that the model stops composing ``mouse_move`` + ``mouse_left_click`` +
#: ``uia_find_elements`` + ``uia_invoke`` by hand. Every one of them composes the
#: existing primitives; none of them hides them.
DESKTOP_OPERATIONS = (
    "open_application",
    "find_target",
    "interact",
    "read_target",
    "verify_window",
)


def _assert_mutating_sets_agree() -> None:
    """Fail loudly if this module and the Windows layer disagree on what mutates.

    A workflow that believed a read was a mutation would demand confirmation for
    harmless reads; one that believed a mutation was a read would dispatch a button
    press with no permission asked. Both are worse than a startup error, so this is
    checked at import time rather than trusted.
    """
    try:
        from core.windows.capabilities import _MUTATING_OPERATIONS as declared
    except Exception:  # pragma: no cover - non-Windows or partially built host
        return
    if set(declared) != set(MUTATING_OPERATIONS):
        missing = sorted(set(declared) ^ set(MUTATING_OPERATIONS))
        raise RuntimeError(
            "desktop workflow mutating set disagrees with core.windows.capabilities: "
            + ", ".join(missing))


_assert_mutating_sets_agree()


# ═════════════════════════════════════════════════════════════ errors


class DesktopWorkflowError(Exception):
    """A workflow step refused, with the reason kept machine-readable.

    ``code`` is the contract; ``detail`` is for a human. Nothing here collapses a
    refusal into "something went wrong", because the planner's next move depends
    entirely on *which* refusal it was: an ambiguous window needs the user to
    choose, a stale one needs re-acquisition, a permission denial needs nothing
    retried at all.
    """

    def __init__(
        self,
        message: str,
        *,
        code: DesktopErrorCode | str = DesktopErrorCode.ACTION_FAILED,
        stage: str = "",
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        ambiguous: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        self.message = str(message or type(self).__name__)
        self.code = DesktopErrorCode.from_value(code) or DesktopErrorCode.ACTION_FAILED
        self.stage = str(stage or "")
        self.details = dict(details or {})
        self.cause = cause
        #: Populated for ``TARGET_AMBIGUOUS`` / ``WINDOW_AMBIGUOUS`` so the caller
        #: can present the candidates. Safe fields only - ids and titles, never
        #: handles.
        self.ambiguous = tuple(dict(row) for row in ambiguous)
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "stage": self.stage,
            "details": dict(self.details),
        }
        if self.ambiguous:
            payload["ambiguous"] = [dict(row) for row in self.ambiguous]
        if self.cause is not None:
            payload["cause"] = type(self.cause).__name__
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DesktopWorkflowError({self.code.value}: {self.message})"


# ═════════════════════════════════════════════════════════════ context


@dataclass(frozen=True)
class DesktopContext:
    """Logical references to the desktop a workflow is working on.

    Everything here is a *name*, never a handle:

    ``pid`` / ``created_at``
        The process identity pair. The creation time is what makes it durable -
        a bare pid is reused, so a pid alone is exactly the "not a sufficient
        durable identity" the design rules rule out.
    ``window_key``
        The M2.16.1 window identity key, re-verified against the live window
        before every action that depends on it.
    ``uia_target``
        A M2.16.5 element *recipe*. ``runtime_id`` inside it is corroboration
        only; the durable identity is the ``target_id`` digest.
    ``display_id``
        Optional display reference, for the coordinate space a point would land in.

    :meth:`from_dict` refuses a payload carrying a native handle. Not because that
    key would be dangerous on its own, but because it means the caller was handed
    a handle somewhere and the boundary has already been crossed - better to stop
    at the edge than to carry it a few layers deeper.
    """

    pid: int = 0
    created_at: str = ""
    window_key: str = ""
    target_id: str = ""
    uia_target: Mapping[str, Any] | None = None
    display_id: str = ""
    coordinate_space: str = "physical"

    _HANDLE_KEYS = frozenset({"hwnd", "handle", "hmonitor", "hmainwindow",
                              "native_handle", "pyhandle"})

    @property
    def anchored(self) -> bool:
        """Whether this context can be re-verified against a live window."""
        return bool(self.window_key)

    @property
    def process_identity(self) -> tuple[int, str]:
        return (int(self.pid or 0), str(self.created_at or ""))

    def with_window(self, window_key: str) -> "DesktopContext":
        return replace(self, window_key=str(window_key or ""))

    def with_target(self, target_id: str, uia_target: Mapping[str, Any] | None) -> "DesktopContext":
        return replace(self, target_id=str(target_id or ""),
                       uia_target=dict(uia_target) if uia_target else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": int(self.pid or 0),
            "created_at": str(self.created_at or ""),
            "window_key": str(self.window_key or ""),
            "target_id": str(self.target_id or ""),
            "uia_target": dict(self.uia_target) if self.uia_target else None,
            "display_id": str(self.display_id or ""),
            "coordinate_space": str(self.coordinate_space or "physical"),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DesktopContext":
        if isinstance(data, DesktopContext):
            return data
        if not isinstance(data, Mapping):
            raise DesktopWorkflowError(
                "a desktop context must be an object.",
                code=DesktopErrorCode.INVALID_WORKFLOW)
        leaked = sorted(
            str(key) for key in data
            if str(key).lower() in cls._HANDLE_KEYS)
        if leaked:
            raise DesktopWorkflowError(
                "a desktop context must not carry native handles; the handle "
                f"keys {', '.join(leaked)} were supplied. Workflow state names "
                "windows logically and re-resolves them inside the capability.",
                code=DesktopErrorCode.INVALID_WORKFLOW,
                details={"rejected": leaked})
        try:
            return cls(
                pid=int(data.get("pid") or 0),
                created_at=str(data.get("created_at") or ""),
                window_key=str(data.get("window_key") or ""),
                target_id=str(data.get("target_id") or ""),
                uia_target=dict(data["uia_target"]) if data.get("uia_target") else None,
                display_id=str(data.get("display_id") or ""),
                coordinate_space=str(data.get("coordinate_space") or "physical"),
            )
        except (TypeError, ValueError) as exc:
            raise DesktopWorkflowError(
                f"the desktop context is malformed: {exc}",
                code=DesktopErrorCode.INVALID_WORKFLOW) from exc


# ═════════════════════════════════════════════════════════════ steps


@dataclass(frozen=True)
class DesktopStep:
    """One composable unit of desktop work.

    ``step_id`` and ``depends_on`` are what let a caller describe a multi-step
    intent ("find the window, then invoke the button inside it") declaratively,
    and the result reports per-step outcomes against those ids.

    ``expected_observation`` / ``expect`` together are the verification
    requirement. ``expect`` is a name/``observed`` pair the workflow can check
    itself; ``expected_observation`` describes it for a human or a planner. Leaving
    both empty is legal and means "this step is not independently checkable" -
    which the result reports as ``UNVERIFIED`` rather than quietly passing.
    """

    step_id: str
    operation: str
    params: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    targeting_mode: TargetingMode = TargetingMode.NONE
    #: ``uia`` -> prefer a semantic target; ``pointer`` -> only when the caller has
    #: a justified reason; ``keyboard`` -> navigation and text.
    strategy: str = "uia"
    expect: Mapping[str, Any] | None = None
    expected_observation: str = ""
    #: An element whose visible state should change when this step succeeds.
    #: Without one, an ``Invoke`` is honestly ``UNVERIFIED`` - so a plan that wants
    #: to prove it pressed something has to name what proves it.
    observe: Mapping[str, Any] | None = None
    allow_pointer_fallback: bool = False
    fallback_reason: str = ""
    optional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", str(self.step_id or "").strip())
        object.__setattr__(self, "operation", str(self.operation or "").strip())
        object.__setattr__(self, "params", dict(self.params or {}))
        object.__setattr__(self, "depends_on", tuple(
            str(dep) for dep in (self.depends_on or ()) if str(dep)))
        object.__setattr__(self, "targeting_mode",
                           TargetingMode.from_value(self.targeting_mode))
        if not self.step_id:
            raise DesktopWorkflowError(
                "every desktop step needs a step_id.",
                code=DesktopErrorCode.INVALID_WORKFLOW)
        if not self.operation:
            raise DesktopWorkflowError(
                f"desktop step {self.step_id!r} needs an operation.",
                code=DesktopErrorCode.INVALID_WORKFLOW,
                details={"step_id": self.step_id})

    @property
    def mutating(self) -> bool:
        return self.operation in MUTATING_OPERATIONS

    @property
    def requires_focus(self) -> bool:
        """Whether the step delivers input that must not land in another window."""
        return self.operation in MUTATING_OPERATIONS

    def is_replayable(self) -> bool:
        """Whether re-dispatching this step after an interruption is side-effect free.

        Same rule as :meth:`core.planner.PlanStepRecord.is_replayable`, keyed on
        the capability operation rather than a tool name. A desktop step that
        types, clicks or invokes may already have applied before the process died,
        and repeating it is how a form gets submitted twice.
        """
        return self.operation in READ_ONLY_OPERATIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation,
            "params": dict(self.params),
            "depends_on": list(self.depends_on),
            "targeting_mode": self.targeting_mode.value,
            "strategy": self.strategy,
            "mutating": self.mutating,
            "replayable": self.is_replayable(),
            "expect": dict(self.expect) if self.expect else None,
            "expected_observation": self.expected_observation,
            "has_observer": bool(self.observe),
            "allow_pointer_fallback": bool(self.allow_pointer_fallback),
            "fallback_reason": self.fallback_reason,
            "optional": bool(self.optional),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DesktopStep":
        if isinstance(data, DesktopStep):
            return data
        if not isinstance(data, Mapping):
            raise DesktopWorkflowError(
                "a desktop step must be an object.",
                code=DesktopErrorCode.INVALID_WORKFLOW)
        return cls(
            step_id=str(data.get("step_id") or ""),
            operation=str(data.get("operation") or ""),
            params=dict(data.get("params") or {}),
            depends_on=tuple(data.get("depends_on") or ()),
            targeting_mode=TargetingMode.from_value(data.get("targeting_mode")),
            strategy=str(data.get("strategy") or "uia"),
            expect=dict(data["expect"]) if data.get("expect") else None,
            expected_observation=str(data.get("expected_observation") or ""),
            observe=dict(data["observe"]) if data.get("observe") else None,
            allow_pointer_fallback=bool(data.get("allow_pointer_fallback")),
            fallback_reason=str(data.get("fallback_reason") or ""),
            optional=bool(data.get("optional")),
        )


@dataclass(frozen=True)
class DesktopWorkflowPlan:
    """An ordered, dependency-respecting set of desktop steps."""

    steps: tuple[DesktopStep, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(
            step if isinstance(step, DesktopStep) else DesktopStep.from_dict(step)
            for step in (self.steps or ())))
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise DesktopWorkflowError(
                    f"duplicate desktop step id: {step.step_id!r}.",
                    code=DesktopErrorCode.INVALID_WORKFLOW)
            seen.add(step.step_id)
        ids = {step.step_id for step in self.steps}
        for step in self.steps:
            missing = sorted(dep for dep in step.depends_on if dep not in ids)
            if missing:
                # Same reasoning as ``dependencies_unsatisfiable``: a dependency on
                # a step that is not in the plan can never be satisfied, and
                # treating it as "not yet" would hang with no diagnostic.
                raise DesktopWorkflowError(
                    f"desktop step {step.step_id!r} depends on unknown step(s): "
                    f"{', '.join(missing)}.",
                    code=DesktopErrorCode.INVALID_WORKFLOW,
                    details={"step_id": step.step_id, "missing": missing})

    def validate(self) -> "DesktopWorkflowPlan":
        """Check the plan is a DAG. Returns self, so it composes in a call chain."""
        order = self.resolved_order()
        if len(order) != len(self.steps):
            raise DesktopWorkflowError(
                "the desktop workflow plan contains a dependency cycle.",
                code=DesktopErrorCode.INVALID_WORKFLOW)
        return self

    def resolved_order(self) -> list[DesktopStep]:
        """Topological order, or ``[]`` when a cycle makes one impossible.

        Deterministic: dependencies first, and among ready steps the order they were
        declared. A workflow whose step order varies run to run is impossible to
        test and impossible to reason about in a recovery report.
        """
        by_id = {step.step_id: step for step in self.steps}
        done: set[str] = set()
        order: list[DesktopStep] = []
        remaining = list(self.steps)
        while remaining:
            progressed = False
            for step in list(remaining):
                if all(dep in done for dep in step.depends_on):
                    order.append(step)
                    done.add(step.step_id)
                    remaining.remove(step)
                    progressed = True
            if not progressed:
                return []
        _ = by_id
        return order

    def step(self, step_id: str) -> DesktopStep | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    @property
    def mutating(self) -> bool:
        return any(step.mutating for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "mutating": self.mutating,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DesktopWorkflowPlan":
        if isinstance(data, DesktopWorkflowPlan):
            return data
        if isinstance(data, Mapping):
            data = data.get("steps") or ()
        return cls(steps=tuple(
            step if isinstance(step, DesktopStep) else DesktopStep.from_dict(step)
            for step in (data or ())))

    def to_plan_step_records(self, tool_for: Callable[[DesktopStep], str] | None = None) -> list:
        """Project onto the existing durable plan records.

        M2.16.6 must not introduce a second persistent plan format. A desktop step
        is projected onto :class:`core.planner.PlanStepRecord` so write-ahead
        persistence, restart reconciliation and ``is_replayable`` all keep working
        unchanged - the workflow contributes intent, not a new store.

        Note that the projection is deliberately *more* conservative than
        :meth:`DesktopStep.is_replayable`. ``tool_is_read_only`` matches registered
        tool-name prefixes, and ``windows.uia_find_elements`` is a capability
        operation rather than a registered tool, so the planner gives it a side
        effect key and reports every desktop step as non-replayable.

        That asymmetry is intentional and is the safe direction to err in: a
        capability-named step that looks read-only is exactly the kind of step
        whose read-only-ness a future edit could invalidate, and restart
        reconciliation acts on ``PlanStepRecord.is_replayable`` alone. Over-
        refusing a replay costs a re-observation; under-refusing one duplicates a
        button press. The workflow's own opinion is carried as intent in
        ``recovery_hint`` rather than enforced here a second time.
        """
        from core.planner import PlanStepRecord, side_effect_key

        tool_for = tool_for or (lambda step: f"{WINDOW_CAPABILITY}.{step.operation}")
        records = []
        for step in self.steps:
            tool = tool_for(step)
            args = dict(step.params)
            records.append(PlanStepRecord(
                step_id=step.step_id,
                tool=tool,
                args=args,
                depends_on=step.depends_on,
                expected_result=dict(step.expect) if step.expect else None,
                recovery_hint=step.expected_observation,
                side_effect_key=side_effect_key(tool, args),
                capability=WINDOW_CAPABILITY,
            ))
        return records


# ═════════════════════════════════════════════════════════════ results


@dataclass
class DesktopStepResult:
    """What actually happened for one step.

    ``targeting_mode`` and ``fallback_reason`` are the observability that matters
    most here: they answer "did this click the button by its name, or did it click
    a pixel?" without having to read the logs.
    """

    step_id: str
    operation: str
    status: str = "pending"
    targeting_mode: TargetingMode = TargetingMode.NONE
    fallback_used: bool = False
    fallback_reason: str = ""
    observed: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    verification: str = "NOT_RUN"
    error_code: str = ""
    error: str = ""
    detail: str = ""
    capability: str = WINDOW_CAPABILITY
    request_id: str = ""
    elapsed_ms: float = 0.0
    replayable: bool = True

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation,
            "status": self.status,
            "capability": self.capability,
            "targeting_mode": self.targeting_mode.value,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "observed": dict(self.observed),
            "expected": dict(self.expected),
            "verification": self.verification,
            "error_code": self.error_code,
            "error": self.error,
            "detail": self.detail,
            "request_id": self.request_id,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "replayable": self.replayable,
        }


@dataclass
class DesktopWorkflowResult:
    """The outcome of a whole workflow.

    ``verification`` is the adjudicated verdict the caller should believe; the
    per-step verifications are the evidence behind it. A workflow that ran every
    step and verified none reports ``UNVERIFIED`` - never ``PASSED`` - because
    "it ran" and "it worked" are different claims.
    """

    workflow_id: str
    status: str = "pending"
    request_id: str = ""
    task_id: str = ""
    operation: str = ""
    context: DesktopContext = field(default_factory=DesktopContext)
    #: What the workflow saw. Declared with a default rather than assigned only on
    #: success, because a caller reading it on a *failed* run must get an empty
    #: observation rather than an AttributeError - the absence of an observation is
    #: itself the finding.
    observed: dict = field(default_factory=dict)
    steps: list[DesktopStepResult] = field(default_factory=list)
    verification: str = "NOT_RUN"
    verification_detail: str = ""
    error_code: str = ""
    error: str = ""
    cancelled: bool = False
    recovered: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def step(self, step_id: str) -> DesktopStepResult | None:
        for result in self.steps:
            if result.step_id == step_id:
                return result
        return None

    @property
    def targeting_mode(self) -> TargetingMode:
        """The mode of the first real interaction, for a quick summary."""
        for result in self.steps:
            if result.targeting_mode is not TargetingMode.NONE:
                return result.targeting_mode
        return TargetingMode.NONE

    @property
    def fallback_used(self) -> bool:
        return any(result.fallback_used for result in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "operation": self.operation,
            "status": self.status,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "context": self.context.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "verification": self.verification,
            "verification_detail": self.verification_detail,
            "error_code": self.error_code,
            "error": self.error,
            "cancelled": self.cancelled,
            "recovered": [dict(row) for row in self.recovered],
            "targeting_mode": self.targeting_mode.value,
            "fallback_used": self.fallback_used,
            "evidence": dict(self.evidence),
            "elapsed_ms": round(float(self.elapsed_ms), 3),
        }

    def to_json_safe(self) -> dict[str, Any]:
        """Strip application text out of anything a model may read.

        Reached for rather than hand-rolled, because this repository already has one
        definition of what a model is allowed to see and a second one would drift.
        """
        try:
            from vision.protocol import to_json_safe
        except Exception:  # pragma: no cover - vision layer absent
            return self.to_dict()
        return to_json_safe(self.to_dict())


# ═════════════════════════════════════════════════════════════ the workflow


class DesktopWorkflow:
    """Composes existing desktop capabilities into verified workflows.

    It holds no native state. Its whole job is sequencing: resolve an anchored
    target, dispatch a primitive through the registry, observe what happened, and
    decide whether that counts as success.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        permissions: Any = None,
        applications: Any = None,
        events: Any = None,
        settle: Any = None,
        launch_timeout: float = 20.0,
        settle_timeout: float = 2.0,
        max_reanchor_attempts: int = 1,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._applications = applications
        self._events = events
        #: Host-supplied "let my own UI run" hook, forwarded to the UIA layer.
        #:
        #: A UIA action is delivered through the owning application's message loop.
        #: With a real application that loop is already running and this stays
        #: ``None``. It exists for the in-process case - a host that *is* the target
        #: application, where nothing else will pump it.
        #:
        #: It deliberately is not a background thread and cannot be supplied per
        #: call. ``PeekMessage`` with a null hwnd pulls messages system-wide, so a
        #: background pump does not help: it steals the target's messages and
        #: dispatches them on the wrong thread, and the action never appears to take
        #: effect. Pumping has to happen on the thread that owns the window, which is
        #: the thread already inside this call.
        self._settle = settle
        self._launch_timeout = max(0.5, float(launch_timeout))
        self._settle_timeout = max(0.0, float(settle_timeout))
        self._max_reanchor = max(0, int(max_reanchor_attempts))
        self._lock = threading.RLock()
        self._cancelled: dict[tuple[str, str], Any] = {}
        self._tokens: dict[tuple[str, str], Any] = {}
        #: Operations that needed explicit confirmation at the outer gate. Recorded
        #: per workflow so a report can show which steps were sensitive enough to
        #: have required a human, rather than only what eventually happened.
        self._confirmation_required: list[str] = []
        self._last_error: dict | None = None
        self._last_result: DesktopWorkflowResult | None = None
        self._closed = False

    # -- lifecycle ------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def capabilities(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "available": not self._closed,
            "operations": list(DESKTOP_OPERATIONS),
            "uia_first": True,
        }
        windows = self._registry.get(WINDOW_CAPABILITY)
        report["window_capability"] = bool(
            windows is not None and windows.can_execute("list_windows"))
        return report

    def can_execute(self, operation: str) -> bool:
        if self._closed:
            return False
        return str(operation) in DESKTOP_OPERATIONS

    def last_error(self) -> dict | None:
        with self._lock:
            return dict(self._last_error) if self._last_error else None

    def last_result(self) -> DesktopWorkflowResult | None:
        with self._lock:
            return self._last_result

    def confirmation_required_for(self) -> tuple[str, ...]:
        """Operations in this workflow that needed explicit human confirmation."""
        with self._lock:
            return tuple(self._confirmation_required)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._cancelled.clear()
            self._tokens.clear()

    def __enter__(self) -> "DesktopWorkflow":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False

    # -- task binding and cancellation -----------------------------------

    def bind_task(self, task_id: str, token: Any = None, *,
                  request_id: str = "") -> Any:
        """Attach a task's cancellation token to this workflow.

        Mirrors the Blender/Vision workflow's binding so the executor's existing
        cancellation plumbing reaches desktop work with no new mechanism.
        """
        key = (str(task_id or ""), str(request_id or ""))
        with self._lock:
            self._tokens[key] = token
        return token

    def release_task(self, task_id: str = "", *, request_id: str = "") -> None:
        key = (str(task_id or ""), str(request_id or ""))
        with self._lock:
            self._tokens.pop(key, None)
            self._cancelled.pop(key, None)

    def cancel(self, task_id: str = "", *, request_id: str = "",
               reason: str = "cancelled") -> bool:
        key = (str(task_id or ""), str(request_id or ""))
        with self._lock:
            self._cancelled[key] = reason or "cancelled"
            token = self._tokens.get(key)
        if token is None:
            return bool(key in self._cancelled)
        for name in ("request_cancellation", "cancel"):
            caller = getattr(token, name, None)
            if callable(caller):
                try:
                    caller(reason) if name == "request_cancellation" else caller()
                except TypeError:
                    caller()
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.debug("cancellation propagation failed: %s", exc)
                return True
        return True

    def _token_for(self, task_id: str, request_id: str) -> Any:
        with self._lock:
            exact = self._tokens.get((str(task_id or ""), str(request_id or "")))
            if exact is not None:
                return exact
            return self._tokens.get((str(task_id or ""), ""), None)

    def _is_cancelled(self, task_id: str, request_id: str = "") -> bool:
        with self._lock:
            for key in ((str(task_id or ""), str(request_id or "")),
                        (str(task_id or ""), "")):
                if key in self._cancelled:
                    return True
        return cancellation_requested(
            self._token_for(task_id, request_id))

    def _check_cancelled(self, task_id: str, request_id: str = "") -> None:
        if self._is_cancelled(task_id, request_id):
            raise DesktopWorkflowError(
                "the desktop workflow was cancelled.",
                code=DesktopErrorCode.CANCELLATION_REQUESTED,
                details={"cancellation_terminated": True})

    # -- dispatch -------------------------------------------------------

    def _level_for(self, capability: str, operation: str, params: Mapping[str, Any]) -> Any:
        """Ask the existing bridge how sensitive this operation is.

        Reusing ``CapabilityBridge.permission_level`` rather than restating the
        rule here is deliberate: mutation classification then has exactly one
        implementation, and M2.16.5's UIA actions cannot be quietly reclassified
        as reads by a second list here.
        """
        from tools.capability_bridge import CapabilityBridge

        probe = CapabilityBridge(self._registry)
        return probe.permission_level({
            "capability": capability, "operation": operation,
            "params": dict(params or {}),
        })

    def _authorize(self, request: CapabilityRequest) -> Any:
        """The permission ladder, evaluated per primitive.

        A workflow is internal, but "internal" is not "trusted": the step it is
        about to dispatch may still be a button press in the user's foreground
        application. So each dispatch is evaluated against ``PermissionManager``
        with the operation's own declared sensitivity, and the tool-level decision
        the executor already made remains the outer gate. Two gates, not one -
        because the inner one is what makes rule 16 true rather than aspirational.
        """
        if self._permissions is None:
            return True
        args = {
            "capability": request.capability,
            "operation": request.operation,
            "params": dict(request.params or {}),
        }
        try:
            declared = self._level_for(request.capability, request.operation,
                                       request.params)
        except Exception as exc:  # noqa: BLE001 - never widen on a broken probe
            log.warning("permission level probe failed: %s", exc)
            declared = getattr(self._permissions, "SENSITIVE", "SENSITIVE")
        try:
            decision = self._permissions.evaluate(
                "capability.execute", args, declared, None,
                str(getattr(request, "task_id", "") or ""))
        except Exception as exc:  # noqa: BLE001 - permission errors are outcomes
            raise DesktopWorkflowError(
                f"permission evaluation failed: {exc}",
                code=DesktopErrorCode.PERMISSION_DENIED,
                details={"operation": request.operation}) from exc
        if getattr(decision, "blocked", False):
            raise PermissionError(
                f"blocked by policy for {request.operation}: "
                f"{getattr(decision, 'reason', '') or 'blocked'}")
        if not getattr(decision, "allow", False) \
                and not getattr(decision, "requires_confirmation", False):
            raise PermissionError(
                f"permission denied for {request.operation}: "
                f"{getattr(decision, 'reason', '') or 'not allowed'}")
        # ``requires_confirmation`` is *not* a denial here, and treating it as one
        # would make every desktop mutation impossible. Confirmation is an
        # interactive concern owned by the Executor, which already asked for it
        # before this tool was dispatched; the workflow is the inner gate and its
        # job is policy (block lists, session blocks, path rules), not prompting.
        # The requirement is recorded so a report can show which steps were
        # sensitive enough to have needed a human.
        if getattr(decision, "requires_confirmation", False):
            with self._lock:
                self._confirmation_required.append(str(request.operation))
        # Returned as a plain ``True`` rather than the decision: the registry's
        # ``_authorized`` reads ``.allow``, which is False whenever confirmation is
        # pending - so handing it the decision would deny every sensitive desktop
        # step even though the policy permitted it and the Executor already
        # confirmed it. The interesting facts are recorded above.
        return True

    def _dispatch(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        task_id: str = "",
        request_id: str = "",
        capability: str = WINDOW_CAPABILITY,
        settle: Any = None,
    ) -> CapabilityResult:
        """One primitive call, through the registry, with cancellation attached.

        The ``request_id`` on the resulting request is minted by
        ``CapabilityRequest`` itself and is the only id any downstream consumer
        reads, so a ``params`` payload carrying ``request_id`` or ``task_id`` is
        ignored rather than honoured.
        """
        cleaned = self._sanitise_params(params)
        if settle is not None:
            cleaned = {**cleaned, "settle": settle}
        request = CapabilityRequest(
            capability=capability,
            operation=str(operation),
            params=cleaned,
            context={"source": "desktop_workflow"},
            task_id=str(task_id or ""),
            cancellation_token=self._token_for(task_id, request_id),
        )
        publisher = (CoreEventBusPublisher(self._events)
                     if self._events is not None else None)
        return self._registry.execute(
            request, authorize=self._authorize,
            event_publisher=publisher if publisher is not None else None)

    @staticmethod
    def _sanitise_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
        """Drop identity claims a caller tried to smuggle through ``params``.

        ``task_id`` and ``request_id`` are authoritative, and they are set by the
        executor and the registry respectively. A nested payload is data being sent
        to a capability, not a place to restate who the caller is.
        """
        clean = {k: v for k, v in dict(params or {}).items()
                 if k not in ("task_id", "request_id", "trusted_request_id")}
        return clean

    def _publisher(self) -> CoreEventBusPublisher | None:
        return (CoreEventBusPublisher(self._events)
                if self._events is not None else None)

    # -- observation helpers --------------------------------------------

    def _rows(self, result: CapabilityResult, *keys: str) -> list[dict]:
        data = result.data if isinstance(result.data, dict) else {}
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return []

    @staticmethod
    def _observe_text(element: Mapping[str, Any]) -> str:
        """The application's own text, still untrusted data.

        Read through the envelope M2.16.5 established rather than off the top level
        of the payload, because that is the only copy that is *marked* as
        untrusted - and a workflow must not launder it by reading the other one.
        """
        recipe = element.get("recipe") if isinstance(element, dict) else None
        text = recipe.get("text") if isinstance(recipe, dict) else None
        if isinstance(text, dict):
            return str(text.get("name") or "")
        identity = element.get("identity") if isinstance(element, dict) else None
        if isinstance(identity, dict):
            return str(identity.get("name") or "")
        return ""

    @staticmethod
    def _element_identity(element: Mapping[str, Any]) -> dict:
        """The element's identifying recipe.

        Read from the ``recipe`` block, which is the only copy of the label marked
        untrusted. ``target_id`` is merged in from the element itself when the
        recipe does not carry it: in M2.16.5's payload ``target_id`` is the digest of
        the recipe and sits beside it, not inside it, and a missing digest would
        leave the resolved context unable to name what it resolved.
        """
        if not isinstance(element, Mapping):
            return {}
        recipe = element.get("recipe")
        identity: dict[str, Any] = {}
        if isinstance(recipe, dict) and isinstance(recipe.get("identity"), dict):
            identity = dict(recipe["identity"])
        elif isinstance(element.get("identity"), dict):
            identity = dict(element["identity"])
        target_id = str(element.get("target_id") or "")
        if target_id and not identity.get("target_id"):
            identity["target_id"] = target_id
        return identity

    # -- window acquisition ---------------------------------------------

    def find_window(
        self,
        *,
        title_contains: str = "",
        pid: Any = None,
        class_name: str = "",
        window_key: str = "",
        visible_only: bool = True,
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Resolve exactly one window, or refuse.

        ``WINDOW_AMBIGUOUS`` is a real answer, not an error to route around. Two
        windows matching a title is the normal state of a browser or an editor, and
        picking the first one is how input reaches the wrong document.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="find_window",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            if window_key:
                # An explicit key is re-verified, not trusted: this is the same
                # check that refuses a recycled handle.
                dispatched = self._dispatch(
                    "get_window", {"window": window_key},
                    task_id=task_id, request_id=request_id)
                data = self._as_dict(dispatched)
                identity = self._window_identity_of(data)
                if not identity:
                    raise DesktopWorkflowError(
                        "the named window could not be resolved.",
                        code=DesktopErrorCode.WINDOW_NOT_FOUND,
                        details={"window_key": window_key})
                result.context = DesktopContext(
                    pid=int(identity.get("pid") or 0),
                    created_at=str(identity.get("identity_created_at") or ""),
                    window_key=str(identity.get("window_key") or ""),
                    display_id=str(identity.get("display_id") or ""),
                )
                if not result.context.anchored:
                    raise DesktopWorkflowError(
                        "the window resolved without a usable anchor.",
                        code=DesktopErrorCode.WINDOW_STALE,
                        details={"window_key": window_key})
                result.verification = "PASSED"
                # Status and verification are separate facts: the call succeeded
                # whether that means the desktop is correct.
                result.status = "success"
                result.verification_detail = (
                    "The named window was re-resolved and matched its identity.")
            else:
                params: dict[str, Any] = {"visible_only": bool(visible_only)}
                if title_contains:
                    params["title_contains"] = str(title_contains)
                if pid not in (None, ""):
                    params["pid"] = pid
                if class_name:
                    params["class_name"] = str(class_name)
                dispatched = self._dispatch("list_windows", params,
                                            task_id=task_id, request_id=request_id)
                self._raise_for_status(dispatched, "list_windows")
                candidates = self._candidates(dispatched)
                if not candidates:
                    raise DesktopWorkflowError(
                        self._not_found_message(title_contains, pid, class_name),
                        code=DesktopErrorCode.WINDOW_NOT_FOUND,
                        details=self._match_details(title_contains, pid, class_name))
                if len(candidates) > 1:
                    raise DesktopWorkflowError(
                        f"{len(candidates)} windows matched. Refusing to choose "
                        "one: pass a window_key or narrow the match.",
                        code=DesktopErrorCode.WINDOW_AMBIGUOUS,
                        details={"matched": len(candidates)},
                        ambiguous=tuple(
                            {"window_key": row.get("window_key"),
                             "title": row.get("title"),
                             "class_name": row.get("class_name"),
                             "pid": row.get("pid")}
                            for row in candidates[:8]))
                identity = self._window_identity_of(candidates[0])
                result.context = DesktopContext(
                    pid=int(identity.get("pid") or 0),
                    created_at=str(identity.get("identity_created_at") or ""),
                    window_key=str(identity.get("window_key") or ""),
                    display_id=str(identity.get("display_id") or ""),
                )
                if not result.context.anchored:
                    raise DesktopWorkflowError(
                        "the matched window has no durable identity, so it "
                        "cannot be acted on.",
                        code=DesktopErrorCode.WINDOW_STALE)
                result.verification = "PASSED"
                # Status and verification are separate facts: the call succeeded
                # whether that means the desktop is correct.
                result.status = "success"
                result.verification_detail = (
                    "Exactly one window matched and it carries a verified identity.")
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except CapabilityCancellationError as exc:
            self._fail(result, DesktopWorkflowError(
                "cancelled", code=DesktopErrorCode.CANCELLATION_REQUESTED,
                cause=exc))
        except Exception as exc:  # noqa: BLE001 - boundary isolation
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    # -- focus ----------------------------------------------------------

    def focus_window(
        self,
        context: Any = None,
        *,
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Focus the window, then *prove* focus before any input is sent.

        M2.16.2 already refuses to dispatch input to a window that is not
        foreground; the workflow's job is to not send input at all when focus could
        not be established, rather than letting the pointer layer discover it.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="focus_window",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            ctx = self._require_context(context, DesktopErrorCode.WINDOW_NOT_FOUND)
            if not ctx.anchored:
                raise DesktopWorkflowError(
                    "focus requires an anchored window.",
                    code=DesktopErrorCode.WINDOW_NOT_FOUND)
            dispatched = self._dispatch("window_focus", {"window": ctx.window_key},
                                        task_id=task_id, request_id=request_id)
            self._raise_for_status(dispatched, "window_focus")
            foreground = self._dispatch("get_foreground_window", {},
                                        task_id=task_id, request_id=request_id)
            self._raise_for_status(foreground, "get_foreground_window")
            observed_key = self._foreground_key(foreground)
            if not observed_key:
                raise DesktopWorkflowError(
                    "focus could not be confirmed: no foreground window was read.",
                    code=DesktopErrorCode.FOCUS_FAILED,
                    details={"window_key": ctx.window_key})
            if observed_key != ctx.window_key:
                raise DesktopWorkflowError(
                    "focus did not land on the intended window; refusing to send "
                    "input to a different application.",
                    code=DesktopErrorCode.FOCUS_FAILED,
                    details={"expected_window_key": ctx.window_key,
                             "foreground_window_key": observed_key})
            result.context = ctx
            result.observed = {"foreground_window_key": observed_key}
            result.verification = "PASSED"
            # Status and verification are separate facts: the call succeeded
            # whether that means the desktop is correct.
            result.status = "success"
            result.verification_detail = (
                "The window was focused and the foreground window independently "
                "re-read as the same identity.")
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    # -- target resolution ----------------------------------------------

    def find_target(
        self,
        *,
        window_key: str = "",
        automation_id: str = "",
        name: str = "",
        control_type: str = "",
        context: Any = None,
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Resolve a semantic UIA target, or refuse.

        Never falls back to "the first element that looks close". ``TARGET_AMBIGUOUS``
        is returned when a recipe matches several elements, because a click aimed at
        an ambiguous target is a click at a guess.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="find_target",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            ctx = self._context_or_window(
                context, DesktopErrorCode.WINDOW_NOT_FOUND,
                window_key=window_key)
            window_key = str(window_key or ctx.window_key or "")
            if not window_key:
                raise DesktopWorkflowError(
                    "a UIA target needs a window to look in.",
                    code=DesktopErrorCode.WINDOW_NOT_FOUND)
            params: dict[str, Any] = {"window": window_key}
            if automation_id:
                params["automation_id"] = str(automation_id)
            if name:
                params["name"] = str(name)
            if control_type:
                params["control_type"] = str(control_type)
            dispatched = self._dispatch("uia_find_elements", params,
                                        task_id=task_id, request_id=request_id)
            self._raise_for_status(dispatched, "uia_find_elements")
            data = self._as_dict(dispatched)
            elements = self._rows(dispatched, "elements")
            matches = [
                element for element in elements
                if self._matches_recipe(element, automation_id, name, control_type)]
            if not matches:
                raise DesktopWorkflowError(
                    "no element matched the requested recipe.",
                    code=DesktopErrorCode.TARGET_NOT_FOUND,
                    details={"window_key": window_key,
                             "automation_id": automation_id,
                             "name": name, "control_type": control_type,
                             "searched": len(elements)})
            if len(matches) > 1:
                raise DesktopWorkflowError(
                    f"{len(matches)} elements matched the recipe. Refusing to "
                    "choose one; narrow it with an automation id or control type.",
                    code=DesktopErrorCode.TARGET_AMBIGUOUS,
                    details={"window_key": window_key, "matched": len(matches)},
                    ambiguous=tuple(
                        {"target_id": self._element_identity(m).get("target_id"),
                         "automation_id": self._element_identity(m).get("automation_id"),
                         "control_type": self._element_identity(m).get("control_type")}
                        for m in matches[:8]))
            element = matches[0]
            identity = self._element_identity(element)
            result.context = ctx.with_target(
                str(identity.get("target_id") or ""),
                dict(element.get("recipe") or {"identity": identity})
                if isinstance(element.get("recipe"), dict) else {"identity": identity})
            result.observed = {
                "target_id": result.context.target_id,
                "control_type": identity.get("control_type"),
                "automation_id": identity.get("automation_id"),
                "patterns": list(element.get("patterns") or ()),
                "bounds": element.get("bounds"),
                "name_present": bool(identity.get("name")),
            }
            result.verification = "PASSED"
            # Status and verification are separate facts: the call succeeded
            # whether that means the desktop is correct.
            result.status = "success"
            result.verification_detail = (
                "Exactly one element matched and its recipe resolved to a target "
                "anchored in the verified window.")
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    # -- interaction ----------------------------------------------------

    def interact(
        self,
        *,
        action: str = "invoke",
        context: Any = None,
        value: Any = None,
        state: str = "",
        observe: Mapping[str, Any] | None = None,
        target: Mapping[str, Any] | None = None,
        window_key: str = "",
        allow_pointer_fallback: bool = False,
        fallback_reason: str = "",
        step_id: str = "",
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Perform one semantic interaction and verify it.

        UIA first, always: ``action`` names an *intent* and the pattern is chosen
        from :data:`UIA_ACTION_BY_OPERATION`. Pointer input happens only when the
        caller asked for it with a stated reason, and either way the mode is
        recorded on the result so a coordinate click is never mistaken for a
        semantic one.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        step_id = step_id or action
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="interact",
            request_id=request_id, task_id=task_id)
        step = DesktopStepResult(
            step_id=step_id, operation=f"uia_{action}",
            replayable=False)
        result.steps.append(step)
        try:
            self._check_cancelled(task_id, request_id)
            ctx = self._context_or_window(
                context, DesktopErrorCode.TARGET_NOT_FOUND,
                window_key=window_key, uia_target=target)
            if not ctx.anchored:
                raise DesktopWorkflowError(
                    "an interaction needs an anchored window.",
                    code=DesktopErrorCode.WINDOW_NOT_FOUND)
            operation, pattern = self._operation_for(action)
            # Cancellation is checked *again* immediately before the mutation, not
            # only at entry: the expensive part of an interact is the resolution
            # that precedes it.
            self._check_cancelled(task_id, request_id)
            dispatched, mode, fallback_reason = self._perform_action(
                operation, pattern, ctx, value=value, state=state,
                target=target if isinstance(target, Mapping) else ctx.uia_target,
                observe=observe,
                allow_pointer_fallback=allow_pointer_fallback,
                fallback_reason=fallback_reason,
                task_id=task_id, request_id=request_id)
            step.targeting_mode = mode
            # ``fallback_used`` answers "did a pointer click stand in for the
            # semantic action", so it is true whenever the mode is POINTER. Deriving
            # it from the *presence* of a reason instead inverted the flag exactly
            # when a fallback was best justified - a well-reasoned fallback was
            # reported as no fallback at all.
            step.fallback_used = mode is TargetingMode.POINTER
            step.fallback_reason = fallback_reason
            step.capability = WINDOW_CAPABILITY
            data = self._as_dict(dispatched)
            verification = data.get("verification") if isinstance(
                data.get("verification"), dict) else {}
            step.observed = {
                "status": verification.get("status"),
                "observed": verification.get("observed"),
                "observed_value": verification.get("observed_value"),
                "observed_state": verification.get("observed_state"),
                "observed_selected": verification.get("observed_selected"),
                "changed": verification.get("changed"),
            }
            step.request_id = str(getattr(dispatched, "request_id", "") or "")
            result.context = ctx
            result.observed = dict(step.observed)
            if self._confirmation_required:
                result.evidence["confirmation_required_for"] = \
                    list(self._confirmation_required)

            if step.observed.get("status") == "PASSED":
                step.verification = "PASSED"
                result.verification = "PASSED"
                result.verification_detail = (
                    f"{pattern} was applied and its postcondition was observed "
                    "independently.")
            elif step.observed.get("status") == "FAILED":
                # "Applied" is not "worked", and a reported FAILED is not retried
                # through the pointer: the action was dispatched, so it may already
                # have applied and a second attempt is not a repair.
                step.verification = "FAILED"
                result.verification = "FAILED"
                result.verification_detail = str(
                    verification.get("reason")
                    or "the postcondition was not observed")
                raise DesktopWorkflowError(
                    result.verification_detail,
                    code=DesktopErrorCode.VERIFICATION_FAILED,
                    details={"pattern": pattern, "step_id": step_id})
            else:
                # No postcondition was reported at all. The call returned and the
                # desktop may or may not have changed; calling that success or
                # failure would be a guess. This is exactly where M2.16.5's
                # ``Invoke`` lands without a witness, and it has to survive
                # composition rather than being rounded up.
                step.verification = "UNVERIFIED"
                result.verification = "UNVERIFIED"
                result.verification_detail = str(
                    verification.get("reason")
                    or (f"{pattern} was dispatched but nothing observed its "
                        "postcondition, so the desktop state is unproven."))
            # Status and verification are separate facts: the call succeeded whether
            # or not that means the desktop is now correct.
            step.status = "success"
            result.status = "success"
        except DesktopWorkflowError as exc:
            step.error_code = exc.code.value
            step.error = exc.message
            step.status = "cancelled" if exc.code is DesktopErrorCode.CANCELLATION_REQUESTED \
                else "failed"
            self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001
            step.error_code = DesktopErrorCode.ACTION_FAILED.value
            step.error = f"{type(exc).__name__}: {exc}"
            step.status = "failed"
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        step.elapsed_ms = (time.perf_counter() - started) * 1000.0
        result.elapsed_ms = step.elapsed_ms
        self._store(result)
        return result

    def read_target(
        self,
        *,
        context: Any = None,
        target: Mapping[str, Any] | None = None,
        window_key: str = "",
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Read a target's current value or state, without acting on it.

        This is the observation M2.16.5's verification depends on, exposed on its
        own so a caller can check a result without re-asserting it. It is also the
        cheapest reliable observation, which is why nothing here reaches for the
        screen: a UIA property answers "what is in this field" without capturing
        the user's whole display.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="read_target",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            ctx = self._context_or_window(
                context, DesktopErrorCode.TARGET_NOT_FOUND,
                window_key=window_key, uia_target=target)
            if not ctx.anchored:
                raise DesktopWorkflowError(
                    "reading a target needs an anchored window.",
                    code=DesktopErrorCode.WINDOW_NOT_FOUND)
            params = {"window": ctx.window_key}
            recipe = target if isinstance(target, Mapping) else ctx.uia_target
            if isinstance(recipe, Mapping) and recipe:
                params["target"] = self._recipe_params(recipe)
            dispatched = self._dispatch("uia_resolve_target", params,
                                        task_id=task_id, request_id=request_id)
            self._raise_for_status(dispatched, "uia_resolve_target")
            data = self._as_dict(dispatched)
            element = data.get("element") if isinstance(data.get("element"), dict) else {}
            identity = self._element_identity(element)
            observed_text = self._observe_text(element)
            result.context = ctx.with_target(
                str(identity.get("target_id") or ctx.target_id), None)
            result.observed = {
                "target_id": str(identity.get("target_id") or ""),
                "control_type": identity.get("control_type"),
                "value": self._value_of(element),
                "text_present": bool(observed_text),
                "patterns": list(element.get("patterns") or ()),
            }
            # A read observes; it does not assert a postcondition, so it is
            # UNVERIFIED unless the caller compares the value to something. The
            # read itself succeeded either way - status and verification are
            # separate facts, and a caller that supplied no expectation has simply
            # not yet checked anything.
            result.status = "success"
            result.verification = "UNVERIFIED"
            result.verification_detail = (
                "Target state was read. Reading is an observation, not a claim "
                "that anything was achieved.")
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    def verify_window(
        self,
        *,
        window_key: str = "",
        expect_title: str = "",
        context: Any = None,
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Verify a window is still the same live window.

        Called before and after an interaction. The M2.16.1 layer already refuses to
        act on a mismatched identity; this states the check as workflow evidence so
        a report can show *when* staleness was noticed.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="verify_window",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            key = str(window_key or "")
            if key:
                # A named window needs no prior context: verifying it *is* how a
                # context is established or re-established. Requiring one first
                # would make the check that guards anchoring unusable.
                ctx = (DesktopContext.from_dict(context)
                       if isinstance(context, Mapping) else DesktopContext())
            else:
                ctx = self._require_context(context, DesktopErrorCode.WINDOW_NOT_FOUND)
                key = str(ctx.window_key or "")
            if not key:
                raise DesktopWorkflowError(
                    "verification needs a window identity.",
                    code=DesktopErrorCode.WINDOW_NOT_FOUND)
            dispatched = self._dispatch("get_window", {"window": key},
                                        task_id=task_id, request_id=request_id)
            self._raise_for_status(dispatched, "get_window")
            data = self._as_dict(dispatched)
            identity = self._window_identity_of(data)
            if not identity or str(identity.get("window_key") or "") != key:
                raise DesktopWorkflowError(
                    "the window no longer matches its recorded identity.",
                    code=DesktopErrorCode.WINDOW_STALE,
                    details={"window_key": key})
            title = str(data.get("title") or identity.get("title") or "")
            result.context = ctx.with_window(key)
            result.observed = {
                "window_key": key, "title": title,
                "pid": identity.get("pid"),
                "foreground": data.get("foreground"),
                "visible": data.get("visible"),
            }
            if expect_title and expect_title.lower() not in title.lower():
                result.verification = "FAILED"
                result.verification_detail = (
                    f"window title {title!r} does not contain {expect_title!r}")
                raise DesktopWorkflowError(
                    result.verification_detail,
                    code=DesktopErrorCode.VERIFICATION_FAILED,
                    details={"expected_title": expect_title, "title": title})
            result.verification = "PASSED"
            # Status and verification are separate facts: the call succeeded
            # whether that means the desktop is correct.
            result.status = "success"
            result.verification_detail = (
                "The window was re-resolved and matched its recorded identity.")
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    # -- application launch ---------------------------------------------

    def open_application(
        self,
        *,
        app: str = "",
        title_contains: str = "",
        expect_title: str = "",
        settle: float = 1.0,
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
    ) -> DesktopWorkflowResult:
        """Launch an application, then anchor to its window.

        Launching goes through the *existing* ``applications.open`` tool
        (:mod:`tools.applications`), which already handles the Start Menu lookup and
        the process confirmation. This method does not reimplement any of that; its
        contribution is the second half - waiting for the window and refusing to
        anchor to something ambiguous.

        Note what this does **not** treat as an identity: not the pid, not the
        title, not a handle. The anchor is the M2.16.1 window key.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="open_application",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            if not str(app or "").strip():
                raise DesktopWorkflowError(
                    "an application name is required.",
                    code=DesktopErrorCode.INVALID_WORKFLOW)
            launched = self._launch(app, task_id=task_id, request_id=request_id)
            evidence = dict(self._as_dict(launched))
            result.evidence["launch"] = {
                key: evidence.get(key)
                for key in ("application", "process_id", "launch_path", "display")
                if key in evidence}
            if launched.status is CapabilityStatus.PERMISSION_REQUIRED:
                raise DesktopWorkflowError(
                    str(launched.error.get("message") or "launch was not permitted"),
                    code=DesktopErrorCode.PERMISSION_DENIED)
            if not launched.success:
                raise DesktopWorkflowError(
                    str(launched.error.get("message")
                        or f"could not launch {app!r}"),
                    code=DesktopErrorCode.APPLICATION_NOT_FOUND,
                    details={"app": app})
            # A window can take a moment to appear. Bounded, and cancelled between
            # attempts, because an unbounded wait here is how a workflow hangs.
            wanted = str(expect_title or title_contains or evidence.get("display") or app)
            deadline = time.monotonic() + self._launch_timeout
            found: DesktopWorkflowResult | None = None
            while True:
                self._check_cancelled(task_id, request_id)
                attempt = self.find_window(
                    title_contains=wanted, visible_only=True,
                    task_id=task_id, request_id=request_id,
                    workflow_id=workflow_id)
                if attempt.ok:
                    found = attempt
                    break
                if attempt.error_code == DesktopErrorCode.WINDOW_AMBIGUOUS.value:
                    # Ambiguity will not resolve by waiting: two windows are two
                    # windows. Surface it now rather than after the timeout.
                    raise DesktopWorkflowError(
                        attempt.error, code=DesktopErrorCode.WINDOW_AMBIGUOUS,
                        details=attempt.evidence.get("error_details", {}))
                if time.monotonic() >= deadline:
                    raise DesktopWorkflowError(
                        f"{app!r} launched but no single window matching "
                        f"{wanted!r} appeared within "
                        f"{self._launch_timeout:.1f}s.",
                        code=DesktopErrorCode.WINDOW_NOT_FOUND,
                        details={"app": app, "title_contains": wanted,
                                 "launched": bool(launched.success)})
                time.sleep(max(0.05, min(float(settle or 0.25), 1.0)))
            result.context = found.context
            result.verification = "PASSED"
            result.verification_detail = (
                "The application launched and exactly one window was anchored to "
                "a verified identity.")
            result.status = "success"
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except CapabilityCancellationError as exc:
            self._fail(result, DesktopWorkflowError(
                "cancelled", code=DesktopErrorCode.CANCELLATION_REQUESTED, cause=exc))
        except Exception as exc:  # noqa: BLE001
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    def _launch(self, app: str, *, task_id: str, request_id: str) -> CapabilityResult:
        """Call the existing ``applications.open`` tool, permission-checked.

        Routed as a ``capability.execute``-shaped request so the same
        :class:`PermissionManager` gate applies as for any other primitive. If the
        tool is unavailable on this host the workflow reports
        ``CAPABILITY_UNAVAILABLE`` rather than silently doing nothing.
        """
        self._check_cancelled(task_id, request_id)
        if self._applications is not None:
            outcome = self._applications(str(app))
            data: dict[str, Any] = {}
            payload = getattr(outcome, "data", None)
            if isinstance(payload, dict):
                data = dict(payload)
            if not data:
                try:
                    import json

                    data = json.loads(getattr(outcome, "output", "") or "{}")
                except Exception:  # noqa: BLE001 - output shape is best effort
                    data = {}
            data.setdefault("display", str(app))
            status = (CapabilityStatus.SUCCESS
                      if getattr(outcome, "success", False)
                      else CapabilityStatus.FAILURE)
            return CapabilityResult(
                status=status, data=data,
                error={} if status is CapabilityStatus.SUCCESS
                else {"message": str(getattr(outcome, "error", "")
                                     or "launch failed")})
        request = CapabilityRequest(
            capability="capability.execute",
            operation="open",
            params={"app": str(app)},
            context={"source": "desktop_workflow"},
            task_id=str(task_id or ""),
            cancellation_token=self._token_for(task_id, request_id),
        )
        return self._registry.execute(request, authorize=self._authorize,
                                      event_publisher=self._publisher())

    # -- whole plans ----------------------------------------------------

    def run_plan(
        self,
        plan: Any = None,
        *,
        context: Any = None,
        task_id: str = "",
        request_id: str = "",
        workflow_id: str = "",
        event_sink: Callable[[dict], Any] | None = None,
    ) -> DesktopWorkflowResult:
        """Run a :class:`DesktopWorkflowPlan` in dependency order.

        Recovery is bounded and one-directional. A *stale target* is re-acquired,
        because re-resolving an identity is a read. An *uncertain mutation* is
        never repeated: if a step that mutates did not cleanly succeed, the
        workflow stops and says so, leaving reconciliation to fresh observation.
        That asymmetry is the difference between recovery and a duplicate
        submission.
        """
        started = time.perf_counter()
        workflow_id = workflow_id or uuid.uuid4().hex
        result = DesktopWorkflowResult(
            workflow_id=workflow_id, operation="run_plan",
            request_id=request_id, task_id=task_id)
        try:
            self._check_cancelled(task_id, request_id)
            parsed = DesktopWorkflowPlan.from_dict(plan).validate()
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
            result.elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._store(result)
            return result
        try:
            ctx = (context if isinstance(context, DesktopContext)
                   else DesktopContext.from_dict(context) if context
                   else DesktopContext())
            result.context = ctx
            for step in parsed.resolved_order():
                self._check_cancelled(task_id, request_id)
                step_result = self._run_step(
                    step, ctx, task_id=task_id, request_id=request_id,
                    workflow_id=workflow_id)
                if step_result is not None:
                    result.steps.append(step_result.result)
                    if step_result.context_target:
                        ctx = step_result.context_target
                        result.context = ctx
                if event_sink is not None:
                    try:
                        # The wrapper is internal, so the sink gets the serialisable
                        # step result. Passing the wrapper raised AttributeError
                        # *before* the sink ran, which meant a cancellation requested
                        # from the sink never happened - and a silently ignored
                        # cancellation is the worst possible version of this bug.
                        event_sink(step_result.result.to_dict()
                                   if step_result is not None else {})
                    except Exception as exc:  # noqa: BLE001 - sink is advisory
                        log.warning("workflow event sink failed: %s", exc)
                if step_result is not None and not step_result.ok:
                    if step.optional and step_result.error_code in (
                            DesktopErrorCode.TARGET_NOT_FOUND.value,
                            DesktopErrorCode.TARGET_STALE.value):
                        continue
                    result.status = "cancelled" if step_result.status == "cancelled" \
                        else "failed"
                    result.error_code = step_result.error_code
                    result.error = step_result.error
                    result.cancelled = step_result.status == "cancelled"
                    break
            else:
                result.status = "success"
            self._adjudicate(result)
        except DesktopWorkflowError as exc:
            self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(result, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._store(result)
        return result

    def _run_step(
        self,
        step: DesktopStep,
        context: DesktopContext,
        *,
        task_id: str,
        request_id: str,
        workflow_id: str,
    ) -> "_PlanStepOutcome | None":
        params = dict(step.params)
        handler = self._handler_for(step.operation)
        if handler is None:
            raise DesktopWorkflowError(
                f"desktop step {step.step_id!r} names an unsupported operation "
                f"{step.operation!r}.",
                code=DesktopErrorCode.INVALID_WORKFLOW,
                details={"supported": list(DESKTOP_OPERATIONS)})
        outcome = handler(self, step, context, params, task_id, request_id, workflow_id)
        outcome.result.step_id = step.step_id
        outcome.result.replayable = step.is_replayable()
        return outcome

    def _handler_for(self, operation: str) -> Callable[..., "_PlanStepOutcome"] | None:
        table = {
            "find_window": _step_find_window,
            "focus_window": _step_focus_window,
            "find_target": _step_find_target,
            "interact": _step_interact,
            "read_target": _step_read_target,
            "verify_window": _step_verify_window,
            "open_application": _step_open_application,
        }
        return table.get(str(operation))

    # -- internals ------------------------------------------------------

    @staticmethod
    def _as_dict(result: CapabilityResult) -> dict:
        return dict(result.data) if isinstance(result.data, dict) else {}

    @staticmethod
    def _operation_for(action: str) -> tuple[str, str]:
        """Resolve an interaction intent to (window operation, UIA pattern).

        Accepts the bare intent (``invoke``) and the already-qualified operation
        name (``uia_invoke``), because a caller who has just read the capability
        surface should not have to un-map it first.
        """
        name = str(action or "").strip()
        entry = UIA_ACTION_BY_OPERATION.get(name)
        if entry is None and name.startswith("uia_"):
            entry = UIA_ACTION_BY_OPERATION.get(name[4:])
        if entry is None:
            raise DesktopWorkflowError(
                f"unknown desktop interaction {action!r}; expected one of "
                + ", ".join(sorted(UIA_ACTION_BY_OPERATION)),
                code=DesktopErrorCode.INVALID_WORKFLOW,
                details={"supported": sorted(UIA_ACTION_BY_OPERATION)})
        return entry

    def _perform_action(
        self,
        operation: str,
        pattern: str,
        context: DesktopContext,
        *,
        value: Any,
        state: str,
        target: Mapping[str, Any] | None,
        observe: Mapping[str, Any] | None,
        allow_pointer_fallback: bool,
        fallback_reason: str,
        task_id: str,
        request_id: str,
    ) -> tuple[CapabilityResult, TargetingMode, str]:
        """UIA first, then a justified and recorded fallback.

        The fallback is never silent. If UIA cannot express the intent the caller
        has to say so, and the reason is carried on the result - so "the OK button
        was clicked" and "some pixel was clicked" can never be the same claim.
        """
        if pattern == "Value" and value is not None:
            params: dict[str, Any] = {
                "window": context.window_key,
                "target": self._recipe_params(target or context.uia_target or {}),
                "value": str(value),
            }
        elif pattern == "ExpandCollapse":
            params = {
                "window": context.window_key,
                "target": self._recipe_params(target or context.uia_target or {}),
                "state": str(state or "expand"),
            }
        else:
            params = {
                "window": context.window_key,
                "target": self._recipe_params(target or context.uia_target or {}),
            }
        if isinstance(observe, Mapping):
            # Addressed by its *stable* fields only. A witness is re-resolved after
            # the action, and the whole point of a witness is that its visible state
            # changes - so pinning its ``name`` would search for the value it is
            # about to stop having, and the postcondition could never be observed.
            witness = self._resolve_witness(
                context, observe, task_id=task_id, request_id=request_id)
            if witness:
                params["observe"] = witness
        self._check_cancelled(task_id, request_id)
        dispatched = self._dispatch(operation, params,
                                    task_id=task_id, request_id=request_id,
                                    settle=self._settle)
        if dispatched.status is CapabilityStatus.SUCCESS:
            return dispatched, TargetingMode.UIA, ""
        error_text = str(dispatched.error.get("message") or "")
        if not allow_pointer_fallback:
            self._raise_for_status(dispatched, operation)
            return dispatched, TargetingMode.UIA, ""
        # Explicitly requested fallback. Still UIA-shaped reasoning first: only a
        # *target point* is used, never a fabricated coordinate, so the click lands
        # where the semantic target said it does.
        reason = str(fallback_reason or error_text or
                     "semantic UIA interaction was unavailable")
        point = self._target_point(context, target, task_id=task_id,
                                   request_id=request_id)
        if not point:
            raise DesktopWorkflowError(
                f"{pattern} was unavailable and no UIA target point existed to "
                f"fall back to ({reason}).",
                code=DesktopErrorCode.UNSUPPORTED_TARGET,
                details={"pattern": pattern, "fallback_reason": reason})
        clicked = self._dispatch(
            "mouse_left_click", {"target": point},
            task_id=task_id, request_id=request_id)
        if not clicked.success:
            raise DesktopWorkflowError(
                str(clicked.error.get("message")
                    or "pointer fallback click failed"),
                code=DesktopErrorCode.ACTION_FAILED,
                details={"fallback_reason": reason})
        return clicked, TargetingMode.POINTER, reason

    def _resolve_witness(
        self,
        context: DesktopContext,
        observe: Mapping[str, Any],
        *,
        task_id: str,
        request_id: str,
    ) -> dict:
        """Turn a witness description into a complete, stable recipe.

        A plan cannot supply a full recipe, because it does not know the window key
        or the element's class until it has run its own discovery steps. And a
        *partial* recipe is not enough for the UIA layer: its matcher treats an
        absent field as a mismatch rather than as "don't care", so a witness given
        only an automation id would resolve to nothing and the postcondition would
        silently degrade to UNVERIFIED.

        So the witness is resolved here, through the same lenient matching
        :meth:`find_target` uses, and the complete recipe is what gets forwarded. An
        ambiguous witness resolves to nothing rather than to a guess.
        """
        identity = observe.get("identity") if isinstance(
            observe.get("identity"), Mapping) else observe
        wanted = {
            "automation_id": str(identity.get("automation_id") or ""),
            "name": str(identity.get("name") or ""),
            "control_type": str(identity.get("control_type") or ""),
            "class_name": str(identity.get("class_name") or ""),
        }
        dispatched = self._dispatch(
            "uia_find_elements", {"window": context.window_key},
            task_id=task_id, request_id=request_id)
        if not dispatched.success:
            return {}
        elements = self._rows(dispatched, "elements")
        matches = [element for element in elements
                   if self._matches_recipe(element, wanted["automation_id"],
                                           wanted["name"], wanted["control_type"],
                                           class_name=wanted["class_name"])]
        if len(matches) != 1:
            log.info(
                "witness did not resolve to exactly one element (%d matches); "
                "the postcondition will be reported as unverified",
                len(matches))
            return {}
        return self._stable_recipe_params(matches[0])

    def _target_point(
        self,
        context: DesktopContext,
        target: Mapping[str, Any] | None,
        *,
        task_id: str,
        request_id: str,
    ) -> dict:
        dispatched = self._dispatch(
            "uia_target_point",
            {"window": context.window_key,
             "target": self._recipe_params(target or context.uia_target or {})},
            task_id=task_id, request_id=request_id)
        self._raise_for_status(dispatched, "uia_target_point")
        data = self._as_dict(dispatched)
        point = data.get("point") if isinstance(data.get("point"), dict) else {}
        if not point or "x" not in point:
            return {}
        # Anchoring is M2.16.2's rule, not this module's to relax.
        if data.get("anchored") is not True:
            return {}
        return point

    def _raise_for_status(self, result: CapabilityResult, operation: str) -> None:
        """Turn a failed capability result into a typed workflow refusal."""
        if result.status is CapabilityStatus.SUCCESS:
            return
        message = str(result.error.get("message") or
                      result.error.get("error") or
                      f"{operation} did not succeed")
        code = result.error.get("code") or ""
        if result.status is CapabilityStatus.CANCELLED:
            raise DesktopWorkflowError(
                message, code=DesktopErrorCode.CANCELLATION_REQUESTED,
                details={"operation": operation})
        if result.status is CapabilityStatus.PERMISSION_REQUIRED:
            raise DesktopWorkflowError(
                message, code=DesktopErrorCode.PERMISSION_DENIED,
                details={"operation": operation})
        if result.status is CapabilityStatus.UNAVAILABLE:
            raise DesktopWorkflowError(
                message, code=DesktopErrorCode.CAPABILITY_UNAVAILABLE,
                details={"operation": operation})
        mapped = DesktopErrorCode.from_value(code)
        if mapped is not None and mapped is not DesktopErrorCode.ACTION_FAILED:
            raise DesktopWorkflowError(message, code=mapped,
                                       details={"operation": operation})
        # Window problems and element problems are told apart, because the caller's
        # next move differs: a missing window is re-acquired by re-enumerating, a
        # missing element by narrowing the recipe. Reporting both as one code makes
        # both look like a search that needs different arguments.
        family = _WINDOW_OPERATIONS if operation in _WINDOW_OPERATIONS else _ELEMENT_OPERATIONS
        if _is_not_found(message):
            raise DesktopWorkflowError(
                message,
                code=DesktopErrorCode.WINDOW_NOT_FOUND
                if operation in _WINDOW_OPERATIONS
                else DesktopErrorCode.TARGET_NOT_FOUND,
                details={"operation": operation})
        if _is_stale(message):
            raise DesktopWorkflowError(
                message,
                code=DesktopErrorCode.WINDOW_STALE
                if operation in _WINDOW_OPERATIONS
                else DesktopErrorCode.TARGET_STALE,
                details={"operation": operation})
        _ = family
        raise DesktopWorkflowError(message, code=DesktopErrorCode.ACTION_FAILED,
                                   details={"operation": operation})

    def _candidates(self, result: CapabilityResult) -> list[dict]:
        rows = self._rows(result, "windows")
        return [row for row in rows if str(row.get("window_key") or "")]

    def _window_identity_of(self, data: Mapping[str, Any]) -> dict:
        window = data.get("window") if isinstance(data.get("window"), dict) else None
        source: Mapping[str, Any] = window or data
        identity = source.get("identity") if isinstance(
            source.get("identity"), dict) else None
        if isinstance(identity, dict) and identity.get("window_key"):
            merged = dict(identity)
            for key, value in source.items():
                merged.setdefault(key, value)
            merged.setdefault("pid", merged.get("owner_pid"))
            return merged
        if source.get("window_key"):
            return dict(source)
        return {}

    def _foreground_key(self, result: CapabilityResult) -> str:
        data = self._as_dict(result)
        window = data.get("window") if isinstance(data.get("window"), dict) else {}
        identity = window.get("identity") if isinstance(
            window.get("identity"), dict) else {}
        for source in (identity, window, data):
            key = str(source.get("window_key") or "")
            if key:
                return key
        return ""

    @staticmethod
    def _matches_recipe(
        element: Mapping[str, Any],
        automation_id: str,
        name: str,
        control_type: str,
        class_name: str = "",
    ) -> bool:
        """Match on the recipe fields only.

        Name is compared case-insensitively and as a substring, because a human
        names buttons ("OK", "Save") rather than their exact strings. Every
        comparison that could narrow the match is an improvement; none of them
        invents a match - a unique result is still required, or the workflow
        refuses with ``TARGET_AMBIGUOUS``.
        """
        identity = DesktopWorkflow._element_identity(element)
        if automation_id and str(identity.get("automation_id") or "") != str(automation_id):
            return False
        if control_type and str(identity.get("control_type") or "").lower() \
                != str(control_type).lower():
            return False
        if class_name and str(identity.get("class_name") or "") != str(class_name):
            return False
        if name:
            actual = str(identity.get("name") or "").lower()
            if str(name).lower() not in actual:
                return False
        return True

    @staticmethod
    def _identity_of_payload(payload: Any) -> dict:
        """The identity dict, whichever of the three shapes this is.

        A payload may be a full element (as ``uia_find_elements`` returns), a recipe
        wrapper, or a bare identity. Guessing wrong here is silent and expensive: an
        element treated as an identity yields only its top-level fields, so the
        automation id is dropped and the element stops identifying anything - which
        surfaces much later as "identifies nothing" from the UIA layer.
        """
        if not isinstance(payload, Mapping):
            return {}
        if isinstance(payload.get("recipe"), Mapping) or \
                isinstance(payload.get("identity"), Mapping):
            return DesktopWorkflow._element_identity(payload)
        return dict(payload)

    @classmethod
    def _recipe_params(cls, recipe: Any) -> dict:
        """Turn a stored recipe into the params the UIA operations accept.

        The stored form is a full identity (``recipe.identity``); the operations
        want the identifying subset. Passing the whole identity through would work
        today and quietly change meaning if a field were added, so the subset is
        named explicitly.
        """
        identity = cls._identity_of_payload(recipe)
        if not identity:
            return {}
        params: dict[str, Any] = {}
        # ``process_id`` and ``target_id`` are not optional extras here. The UIA
        # layer's own ``anchored`` rule is "has a window key *and* a positive pid",
        # so dropping the pid made every projected recipe come back unanchored and
        # every real interaction refuse with "must be anchored to a verified
        # window" - a message that pointed at the window rather than at the
        # projection that lost the field.
        for key in ("window_key", "process_id", "target_id", "automation_id",
                    "name", "control_type", "class_name", "framework_id",
                    "occurrence"):
            value = identity.get(key)
            if value not in (None, ""):
                params[key] = value
        if identity.get("runtime_id"):
            params["runtime_id"] = list(identity.get("runtime_id") or ())
        return params

    @classmethod
    def _stable_recipe_params(cls, recipe: Any) -> dict:
        """The identifying subset that does not include a changing label.

        Used for witnesses. Everything mutable is dropped - ``name`` above all,
        since a witness is *expected* to change its text - leaving automation id,
        control type, class and occurrence, which are what actually identify an
        element across a state change.
        """
        params = cls._recipe_params(recipe)
        params.pop("name", None)
        params.pop("runtime_id", None)
        return params

    @staticmethod
    def _value_of(element: Mapping[str, Any]) -> Any:
        recipe = element.get("recipe") if isinstance(element, dict) else None
        text = recipe.get("text") if isinstance(recipe, dict) else None
        if isinstance(text, dict) and text.get("text_value"):
            return text.get("text_value")
        if isinstance(element, dict) and element.get("text_value"):
            return element.get("text_value")
        return None

    @staticmethod
    def _not_found_message(title_contains: str, pid: Any,
                           class_name: str) -> str:
        wanted = title_contains or (f"pid {pid}" if pid else "") or class_name
        return f"no window matched {wanted!r}."

    @staticmethod
    def _match_details(title_contains: str, pid: Any, class_name: str) -> dict:
        return {"title_contains": str(title_contains or ""),
                "pid": pid, "class_name": str(class_name or "")}

    @staticmethod
    def _require_context(context: Any, code: DesktopErrorCode) -> DesktopContext:
        if isinstance(context, DesktopContext):
            return context
        if isinstance(context, Mapping):
            return DesktopContext.from_dict(context)
        raise DesktopWorkflowError(
            "this operation needs a desktop context naming a verified window.",
            code=code)

    @classmethod
    def _context_or_window(
        cls,
        context: Any,
        code: DesktopErrorCode,
        *,
        window_key: str = "",
        target_id: str = "",
        uia_target: Mapping[str, Any] | None = None,
    ) -> DesktopContext:
        """Accept a context, or assemble one from the parts the caller supplied.

        A caller who already knows the ``window_key`` does not also have to echo a
        context back, and forcing them to is a papercuff that produces a confusing
        refusal for a request that was perfectly well specified. What is still
        required is *some* way to name a window, because without one there is
        nothing to verify against.
        """
        if context is not None:
            ctx = cls._require_context(context, code)
            if window_key:
                ctx = ctx.with_window(window_key)
            if target_id or uia_target:
                ctx = ctx.with_target(target_id, uia_target)
            return ctx
        if window_key:
            return DesktopContext(window_key=str(window_key),
                                 target_id=str(target_id or ""),
                                 uia_target=dict(uia_target) if uia_target else None)
        raise DesktopWorkflowError(
            "this operation needs a window identity: pass a desktop context or a "
            "window_key.",
            code=code)

    def _adjudicate(self, result: DesktopWorkflowResult) -> None:
        """Roll per-step verification into one workflow verdict.

        Deliberately conservative: a workflow with no mutating step and no explicit
        expectation is ``PASSED`` only if every step did; anything that could not be
        independently checked leaves the verdict ``UNVERIFIED`` rather than
        borrowing confidence from a step that merely ran.

        A workflow that *stopped* still gets a verdict. Leaving it ``NOT_RUN`` would
        hand the ledger a terminal outcome with nothing recorded, which is the one
        state that is always wrong at the end of an execution.
        """
        verifications = [step.verification for step in result.steps]
        failed_steps = [step.step_id for step in result.steps
                        if step.verification == "FAILED"]
        if failed_steps:
            result.verification = "FAILED"
            result.verification_detail = (
                "Step(s) " + ", ".join(failed_steps) + " did not meet their "
                "postcondition.")
            return
        if result.status != "success":
            if result.cancelled:
                result.verification = "UNVERIFIED"
                result.verification_detail = (
                    "The workflow was cancelled before its postconditions could be "
                    "checked.")
            else:
                result.verification = "FAILED"
                result.verification_detail = (
                    result.error or "The workflow did not complete.")
            return
        if not verifications:
            result.verification = "UNVERIFIED"
            result.verification_detail = "The workflow ran no steps to verify."
            return
        if all(state == "PASSED" for state in verifications):
            result.verification = "PASSED"
            result.verification_detail = (
                "Every step's postcondition was independently observed.")
            return
        unverified = [step.step_id for step in result.steps
                      if step.verification != "PASSED"]
        result.verification = "UNVERIFIED"
        result.verification_detail = (
            "Steps " + ", ".join(unverified) + " could not be independently "
            "verified.")

    @staticmethod
    def _fail(result: DesktopWorkflowResult, error: DesktopWorkflowError) -> None:
        result.status = ("cancelled"
                         if error.code is DesktopErrorCode.CANCELLATION_REQUESTED
                         else "failed")
        result.error_code = error.code.value
        result.error = error.message
        result.cancelled = error.code is DesktopErrorCode.CANCELLATION_REQUESTED
        result.evidence["error"] = error.to_dict()
        if error.ambiguous:
            result.evidence["ambiguous"] = [dict(row) for row in error.ambiguous]
        if not result.verification or result.verification == "NOT_RUN":
            result.verification = ("UNVERIFIED"
                                   if error.code is DesktopErrorCode.CANCELLATION_REQUESTED
                                   else "FAILED")
            result.verification_detail = error.message

    def _store(self, result: DesktopWorkflowResult) -> None:
        with self._lock:
            self._last_result = result
            if result.status == "failed":
                self._last_error = {
                    "code": result.error_code,
                    "message": result.error,
                    "workflow_id": result.workflow_id,
                }

    # -- capability entry point -----------------------------------------

    def execute(self, operation: str, params: Mapping[str, Any] | None = None,
                *, task_id: str = "", request_id: str = "") -> DesktopWorkflowResult:
        """Run one workflow operation. The capability adapter calls this."""
        args = dict(params or {})
        token = self._token_for(task_id, request_id)
        name = str(operation)
        try:
            if name == "open_application":
                return self.open_application(
                    app=str(args.get("app") or args.get("application") or ""),
                    title_contains=str(args.get("title_contains") or ""),
                    expect_title=str(args.get("expect_title") or ""),
                    settle=float(args.get("settle") or 0.5),
                    task_id=task_id, request_id=request_id)
            if name == "find_target":
                return self.find_target(
                    window_key=str(args.get("window_key") or ""),
                    automation_id=str(args.get("automation_id") or ""),
                    name=str(args.get("name") or ""),
                    control_type=str(args.get("control_type") or ""),
                    context=args.get("context"), task_id=task_id,
                    request_id=request_id)
            if name == "interact":
                return self.interact(
                    action=str(args.get("action") or "invoke"),
                    context=args.get("context"), value=args.get("value"),
                    state=str(args.get("state") or ""),
                    target=args.get("target") if isinstance(
                        args.get("target"), Mapping) else None,
                    window_key=str(args.get("window_key") or ""),
                    allow_pointer_fallback=bool(
                        args.get("allow_pointer_fallback")),
                    fallback_reason=str(args.get("fallback_reason") or ""),
                    task_id=task_id, request_id=request_id)
            if name == "read_target":
                return self.read_target(
                    context=args.get("context"),
                    target=args.get("target") if isinstance(
                        args.get("target"), Mapping) else None,
                    window_key=str(args.get("window_key") or ""),
                    task_id=task_id, request_id=request_id)
            if name == "verify_window":
                return self.verify_window(
                    window_key=str(args.get("window_key") or ""),
                    expect_title=str(args.get("expect_title") or ""),
                    context=args.get("context"), task_id=task_id,
                    request_id=request_id)
            raise DesktopWorkflowError(
                f"unsupported desktop workflow operation: {name!r}",
                code=DesktopErrorCode.INVALID_WORKFLOW,
                details={"supported": list(DESKTOP_OPERATIONS)})
        except DesktopWorkflowError as exc:
            failed = DesktopWorkflowResult(
                workflow_id=uuid.uuid4().hex, operation=name,
                request_id=request_id, task_id=task_id)
            self._fail(failed, exc)
            self._store(failed)
            return failed
        except Exception as exc:  # noqa: BLE001 - capability boundary isolation
            failed = DesktopWorkflowResult(
                workflow_id=uuid.uuid4().hex, operation=name,
                request_id=request_id, task_id=task_id)
            self._fail(failed, DesktopWorkflowError(
                f"{type(exc).__name__}: {exc}",
                code=DesktopErrorCode.ACTION_FAILED, cause=exc))
            self._store(failed)
            return failed
        finally:
            _ = token


@dataclass
class _PlanStepOutcome:
    """Internal per-step outcome, carrying any context the step re-anchored.

    Proxies the handful of :class:`DesktopStepResult` fields ``run_plan`` needs, so
    the caller reads one object rather than reaching through to ``.result`` at
    every use.
    """

    result: DesktopStepResult
    context_target: DesktopContext | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def status(self) -> str:
        return self.result.status

    @property
    def step_id(self) -> str:
        return self.result.step_id

    @property
    def error(self) -> str:
        return self.result.error

    @property
    def error_code(self) -> str:
        return self.result.error_code

    @property
    def verification(self) -> str:
        return self.result.verification


def _step_result(step: DesktopStep, status: str = "success", *,
                 observed: Mapping[str, Any] | None = None,
                 verification: str = "PASSED",
                 error_code: str = "", error: str = "",
                 targeting: TargetingMode = TargetingMode.NONE) -> DesktopStepResult:
    return DesktopStepResult(
        step_id=step.step_id, operation=step.operation, status=status,
        observed=dict(observed or {}), verification=verification,
        error_code=error_code, error=error,
        targeting_mode=targeting)


def _verify_expectation(step: DesktopStep, observed: Mapping[str, Any],
                        fallback: str) -> str:
    """Check ``expect`` against what was observed, if the step declared one.

    Returns ``UNVERIFIED`` when there is nothing to check: inventing a pass for an
    unchecked step is the failure mode this whole layer is built to avoid.
    """
    if not step.expect:
        return fallback
    field = str(step.expect.get("field") or "value")
    if field not in observed:
        return "FAILED"
    return "PASSED" if observed.get(field) == step.expect.get("equals") else "FAILED"


def _step_find_window(workflow, step, context, params, task_id, request_id,
                      workflow_id):
    found = workflow.find_window(
        title_contains=str(params.get("title_contains") or ""),
        pid=params.get("pid"), class_name=str(params.get("class_name") or ""),
        window_key=str(params.get("window_key") or ""),
        visible_only=bool(params.get("visible_only", True)),
        task_id=task_id, request_id=request_id, workflow_id=workflow_id)
    if not found.ok:
        return _PlanStepOutcome(_step_result(
            step, "failed", verification=found.verification,
            error_code=found.error_code, error=found.error))
    return _PlanStepOutcome(
        _step_result(step, "success", observed=found.context.to_dict(),
                     verification=found.verification),
        context_target=found.context)


def _step_open_application(workflow, step, context, params, task_id, request_id,
                          workflow_id):
    opened = workflow.open_application(
        app=str(params.get("app") or params.get("application") or ""),
        title_contains=str(params.get("title_contains") or ""),
        expect_title=str(params.get("expect_title") or ""),
        task_id=task_id, request_id=request_id, workflow_id=workflow_id)
    if not opened.ok:
        return _PlanStepOutcome(_step_result(
            step, "failed", verification=opened.verification,
            error_code=opened.error_code, error=opened.error))
    return _PlanStepOutcome(
        _step_result(step, "success", observed=opened.context.to_dict(),
                     verification=opened.verification),
        context_target=opened.context)


def _step_focus_window(workflow, step, context, params, task_id, request_id,
                       workflow_id):
    focused = workflow.focus_window(
        params.get("context") or context, task_id=task_id,
        request_id=request_id, workflow_id=workflow_id)
    if not focused.ok:
        return _PlanStepOutcome(_step_result(
            step, "failed", verification=focused.verification,
            error_code=focused.error_code, error=focused.error))
    return _PlanStepOutcome(
        _step_result(step, "success", observed=focused.observed,
                     verification=focused.verification))


def _step_find_target(workflow, step, context, params, task_id, request_id,
                      workflow_id):
    found = workflow.find_target(
        window_key=str(params.get("window_key") or ""),
        automation_id=str(params.get("automation_id") or ""),
        name=str(params.get("name") or ""),
        control_type=str(params.get("control_type") or ""),
        context=params.get("context") or context, task_id=task_id,
        request_id=request_id, workflow_id=workflow_id)
    if not found.ok:
        return _PlanStepOutcome(_step_result(
            step, "failed", verification=found.verification,
            error_code=found.error_code, error=found.error))
    outcome = _step_result(step, "success", observed=found.observed,
                           verification=found.verification)
    return _PlanStepOutcome(outcome, context_target=found.context)


def _step_interact(workflow, step, context, params, task_id, request_id,
                   workflow_id):
    acted = workflow.interact(
        action=str(params.get("action") or "invoke"),
        context=params.get("context") or context, value=params.get("value"),
        state=str(params.get("state") or ""),
        observe=(params.get("observe") if isinstance(
            params.get("observe"), Mapping) else step.observe),
        target=params.get("target") if isinstance(
            params.get("target"), Mapping) else None,
        window_key=str(params.get("window_key") or ""),
        allow_pointer_fallback=step.allow_pointer_fallback,
        fallback_reason=step.fallback_reason,
        step_id=step.step_id, task_id=task_id, request_id=request_id,
        workflow_id=workflow_id)
    step_result = acted.step(step.step_id)
    if step_result is None:
        step_result = _step_result(step, "failed", error=acted.error)
    step_result.operation = f"{step.operation}"
    step_result.verification = _verify_expectation(
        step, acted.observed, acted.verification)
    step_result.replayable = step.is_replayable()
    if step.expect and step_result.verification == "FAILED":
        # Same rule as the read step: a declared expectation that did not hold is a
        # failed step, because the terminal invariant forbids pairing a completed
        # task with a failed verdict.
        step_result.status = "failed"
        step_result.error_code = DesktopErrorCode.VERIFICATION_FAILED.value
        step_result.error = (
            f"the expectation for {step.step_id!r} did not hold: expected "
            f"{dict(step.expect or {}).get('equals')!r}, observed "
            f"{acted.observed.get(str((step.expect or {}).get('field') or 'observed'))!r}")
    return _PlanStepOutcome(step_result)


def _step_read_target(workflow, step, context, params, task_id, request_id,
                      workflow_id):
    read = workflow.read_target(
        context=params.get("context") or context,
        target=params.get("target") if isinstance(
            params.get("target"), Mapping) else None,
        window_key=str(params.get("window_key") or ""),
        task_id=task_id, request_id=request_id, workflow_id=workflow_id)
    if not read.ok:
        return _PlanStepOutcome(_step_result(
            step, "failed", verification=read.verification,
            error_code=read.error_code, error=read.error))
    state = _verify_expectation(step, read.observed, "UNVERIFIED")
    if state == "FAILED":
        # A step whose declared expectation did not hold has *failed*, not
        # "completed but unverified". The terminal-state invariant in
        # ``core.task_manager`` does not admit COMPLETED alongside a FAILED
        # verdict, and it is right not to: something the workflow promised did not
        # happen.
        return _PlanStepOutcome(_step_result(
            step, "failed", observed=read.observed, verification="FAILED",
            error_code=DesktopErrorCode.VERIFICATION_FAILED.value,
            error=("the observed value did not match the expectation: "
                   f"expected {dict(step.expect or {}).get('equals')!r}, "
                   f"observed {read.observed.get(str((step.expect or {}).get('field') or 'value'))!r}")))
    return _PlanStepOutcome(
        _step_result(step, "success", observed=read.observed,
                     verification=state))


def _step_verify_window(workflow, step, context, params, task_id, request_id,
                        workflow_id):
    verified = workflow.verify_window(
        window_key=str(params.get("window_key") or ""),
        expect_title=str(params.get("expect_title") or ""),
        context=params.get("context") or context, task_id=task_id,
        request_id=request_id, workflow_id=workflow_id)
    if not verified.ok:
        return _PlanStepOutcome(_step_result(
            step, "failed", verification=verified.verification,
            error_code=verified.error_code, error=verified.error))
    return _PlanStepOutcome(
        _step_result(step, "success", observed=verified.observed,
                     verification=verified.verification))


#: Operations that address a window rather than an element inside one. A refusal
#: from these is a ``WINDOW_*`` problem, and the caller's recovery differs.
_WINDOW_OPERATIONS = frozenset({
    "list_windows", "get_window", "window_focus", "window_minimize",
    "window_restore", "window_close", "get_foreground_window",
})

#: Operations that address an element within a window.
_ELEMENT_OPERATIONS = frozenset({
    "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
    "uia_target_point", "uia_invoke", "uia_set_value", "uia_toggle",
    "uia_select", "uia_expand_collapse", "visual_resolve_target",
})


def _is_stale(message: str) -> bool:
    text = str(message or "").lower()
    return "stale" in text or "no longer valid" in text or "identity mismatch" in text

def _is_not_found(message: str) -> bool:
    text = str(message or "").lower()
    return "not found" in text or "no window exists" in text or "no element" in text
