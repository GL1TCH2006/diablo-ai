"""M2.16.6 high-level desktop workflow tools.

Why these exist
===============

Before this milestone a model wanting to press a button had to hand-compose
``capability.execute`` calls against ``windows``: enumerate processes, enumerate
windows, pick a window, focus it, ``uia_find_elements``, ``uia_resolve_target``,
``uia_invoke``, then read something back to check. Every one of those steps is a
place to get it wrong, and most of the mistakes are silent - a window handle picked
from the wrong application still clicks something.

Five tools replace that with intent-level operations:

============================  ============================================
``desktop.open``              launch an application and anchor to its window
``desktop.find``              resolve one window or one semantic target
``desktop.interact``          act on a target, UIA-first, verified
``desktop.read``              read a target's current value or state
``desktop.verify``            confirm a window is still the same window
============================  ============================================

They hide no machinery. Each is a thin schema over the ``desktop_workflow``
capability, which is itself a thin schema over the existing ``windows`` capability.
``capability.execute`` remains registered and reachable, so the architecture is
still inspectable from the model side; these tools exist so that composing it by
hand is *optional*, not so that it is impossible.

Permission is not bypassed. Every tool's ``level_for`` delegates to
:meth:`CapabilityBridge.permission_level` against the underlying operation, so
``desktop.interact`` is SENSITIVE for the same reason ``windows.uia_invoke`` is -
not because this module re-decided it.
"""

from __future__ import annotations

from typing import Any

from config.permissions import PermissionLevel
from core.logging_setup import get_logger
from tools.capability_bridge import CapabilityBridge
from tools.registry import ToolResult, ToolSpec

log = get_logger("desktop_tools")

__all__ = ["build_desktop_tools"]

#: Workflow operation -> the tool that exposes it.
_OPERATION_FOR_TOOL = {
    "desktop.open": "open_application",
    "desktop.find": "find_target",
    "desktop.interact": "interact",
    "desktop.read": "read_target",
    "desktop.verify": "verify_window",
}

#: Operations that mutate, so a tool wrapping one must never be judged SAFE.
#: Kept as data rather than a heuristic because "interact" reads like a read.
_MUTATING_WORKFLOW_OPERATIONS = frozenset({
    "open_application",   # launching changes the desktop
    "interact",           # the whole point: this one clicks or types
})

DESKTOP_CAPABILITY = "desktop_workflow"


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_CONTEXT_PROPERTY = {
    "type": "object",
    "description": (
        "A desktop context returned by an earlier call, naming a verified "
        "window. Contains logical references only - a window key and a UIA "
        "target recipe - never a native handle. Reuse it so later steps resolve "
        "against the same window this one anchored to."),
}


def build_desktop_tools(
    registry: Any,
    *,
    events: Any = None,
) -> list[ToolSpec]:
    """Build the five desktop tools against a capability registry.

    Returns an empty list when the ``desktop_workflow`` capability is absent, so a
    host without the Windows layer simply has no desktop tools instead of tools
    that fail.
    """
    capability = registry.get(DESKTOP_CAPABILITY) if registry is not None else None
    if capability is None:
        log.info("desktop tools not registered: %s unavailable", DESKTOP_CAPABILITY)
        return []
    bridge = CapabilityBridge(registry, events)

    def _level_for(tool_name: str):
        operation = _OPERATION_FOR_TOOL[tool_name]

        def level_for(args: dict) -> PermissionLevel:
            # Delegate to the bridge so there is one implementation of "is this
            # mutating". The probe operation is the workflow operation itself,
            # which is what the executor is about to run.
            try:
                return bridge.permission_level({
                    "capability": DESKTOP_CAPABILITY,
                    "operation": operation,
                    "params": dict(args or {}),
                })
            except Exception:  # noqa: BLE001 - never widen on a broken probe
                return PermissionLevel.SENSITIVE

        return level_for

    def _call(operation: str, params: dict, tool_context: Any) -> ToolResult:
        request = {"capability": DESKTOP_CAPABILITY,
                   "operation": operation, "params": params}
        try:
            bridge.validate_args(request)
        except ValueError as exc:
            return ToolResult(False, error=str(exc))
        return bridge.execute(request, tool_context)

    def _make(tool_name: str, operation: str, description: str,
              schema: dict, describe: Any) -> ToolSpec:
        # ``required`` lives inside the JSON schema, which is where a tool
        # consumer reads it; taking it as a separate argument as well would be two
        # sources of truth for one list.
        required = list(schema.get("required") or ())

        def fn(**kwargs: Any) -> ToolResult:
            tool_context = kwargs.pop("_tool_context", None)
            params = {k: v for k, v in kwargs.items() if v is not None}
            return _call(operation, params, tool_context)

        return ToolSpec(
            name=tool_name,
            description=description,
            parameters=schema,
            required=required,
            level=PermissionLevel.SENSITIVE
            if operation in _MUTATING_WORKFLOW_OPERATIONS
            else PermissionLevel.SAFE,
            level_for=_level_for(tool_name),
            fn=fn,
            describe=describe,
            wants_context=True,
            validate_args=lambda args: bridge.validate_args(
                {"capability": DESKTOP_CAPABILITY,
                 "operation": operation,
                 "params": {k: v for k, v in (args or {}).items()
                            if k != "_tool_context"}}),
            persist_data=True,
            capabilities=("desktop",),
        )

    tools = [
        _make(
            "desktop.open", "open_application",
            "LAUNCH a desktop application AND anchor to its window, so later "
            "steps can act on it. Prefer this over applications.open whenever the "
            "request involves doing anything with the app after starting it, "
            "because it returns a context naming a verified window. Operates on "
            "the window, not on any control inside it. Waits for the window to "
            "appear; refuses rather than guessing if more than one window matches.",
            _schema({
                "app": {"type": "string",
                        "description": "Application name, e.g. 'notepad'."},
                "title_contains": {"type": "string"},
                "expect_title": {"type": "string",
                                 "description": "Substring the window title must "
                                                "contain once it appears."},
                "settle": {"type": "number"},
            }, ["app"]),
            lambda args: f"Open application {args.get('app', '')!r} and anchor to its window",
        ),
        _make(
            "desktop.find", "find_target",
            "DISCOVER one desktop window or one named UI element, semantically. "
            "Use this first whenever you need to act on something: it is how a "
            "control is located before it can be pressed, and how a window is "
            "located before it can be interacted with. Prefers UI Automation - an "
            "element is named by its automation id, visible name or control "
            "type, never by screen coordinates. Operates on windows and on "
            "controls inside them, and needs nothing but a hint of what to look "
            "for. Returns a context or target recipe. Refuses when the match is "
            "not unique, so a successful result really is the element you meant.",
            _schema({
                "window_key": {"type": "string",
                               "description": "Anchors to a specific window "
                                              "instead of searching."},
                "automation_id": {"type": "string"},
                "name": {"type": "string",
                         "description": "Visible label, matched "
                                        "case-insensitively."},
                "control_type": {"type": "string",
                                 "description": "e.g. Button, Edit, CheckBox."},
                "context": _CONTEXT_PROPERTY,
            }, []),
            lambda args: (
                "Find UI element"
                if any(args.get(k) for k in ("automation_id", "name", "control_type"))
                else "Find desktop window"),
        ),
        _make(
            "desktop.interact", "interact",
            "ACT on a desktop target - click a button, set a field, toggle a "
            "checkbox, select an item, expand a control, or send keys and typed "
            "text to the focused control. This is the tool that performs UI "
            "actions; use it for clicks, typing and key presses on an already-"
            "discovered target. Requires a target, normally from desktop.find, "
            "passed as 'target' or via 'context'. Operates on controls inside a "
            "window, not on the window itself. Uses UI Automation and verifies the "
            "result by re-reading the control's state; an action with no "
            "observable effect is reported as UNVERIFIED rather than done.",
            _schema({
                "action": {"type": "string",
                           "description": "invoke | set_value | toggle | select "
                                          "| expand_collapse"},
                "context": _CONTEXT_PROPERTY,
                "value": {"type": "string",
                          "description": "Value to set, for set_value."},
                "state": {"type": "string",
                          "description": "expand or collapse."},
                "target": {"type": "object",
                           "description": "A target recipe from desktop.find."},
                "allow_pointer_fallback": {
                    "type": "boolean",
                    "description": "Permit a pointer click at the target's own "
                                   "point if the semantic pattern is unavailable. "
                                   "The fallback is recorded, never silent."},
                "fallback_reason": {"type": "string"},
            }, ["action"]),
            lambda args: f"Interact with desktop target using {args.get('action', '')!r}",
        ),
        _make(
            "desktop.read", "read_target",
            "READ a desktop target's current value or state without acting on it. "
            "Use this to CHECK a result rather than assume one - after an action, "
            "or to decide what to do next. Operates on windows and on controls, "
            "and needs a target from desktop.find. Safe: it changes nothing.",
            _schema({
                "context": _CONTEXT_PROPERTY,
                "target": {"type": "object",
                           "description": "A target recipe from desktop.find."},
            }, []),
            lambda args: "Read desktop target state",
        ),
        _make(
            "desktop.verify", "verify_window",
            "VERIFY that a desktop window is still the same verified window it "
            "was, optionally checking its title. Use before trusting an action "
            "that happened some time ago, and before acting on a window found by "
            "an earlier step. Operates on windows only - not on controls. Refuses "
            "if the window has been replaced or closed rather than continuing "
            "against whatever now occupies that title.",
            _schema({
                "window_key": {"type": "string"},
                "expect_title": {"type": "string"},
                "context": _CONTEXT_PROPERTY,
            }, []),
            lambda args: "Verify desktop window identity",
        ),
    ]
    log.info("registered desktop tools: %s", ", ".join(t.name for t in tools))
    return tools
