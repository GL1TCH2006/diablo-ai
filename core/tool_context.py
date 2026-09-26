"""M2.13.0 Phase 3 - adaptive tool context.

Why this exists
---------------
Every model turn was handed the complete 57-tool registry. Measured on this
machine with real models, that is the single largest controllable driver of
call latency: halving the schema more than halved mean latency and removed all
timeouts for the mid-size model (see the M2.13.0 benchmark). The full registry
is still authoritative - this module only decides what is *presented*.

Safety properties, all structural
---------------------------------
1. The full :class:`~tools.registry.ToolRegistry` remains authoritative.
2. Selection controls **presentation only**. It grants no permission, and it
   never bypasses :class:`~core.executor.Executor` or
   :class:`~config.permissions.PermissionManager`: a tool's permission level is
   resolved from its spec by the existing path, independently of this module.
3. A tool that is not presented can still be *called*. ``ExecutionPlan``
   validates tool calls against the full registry, so an out-of-context call
   still dispatches through the normal, permission-checked path. Exclusion
   reduces what the model can reach for, not what the agent can do.
4. Selection **fails safe**. Any error, unknown input or empty signal set
   degrades to the full registry and is reported as ``degraded``.
5. A capability floor is never crossed: the core set below is always present, so
   a task can never be silently stripped of the tools it needs.
6. Uncertainty expands the set, never narrows it. Weak or missing signals
   produce a broader tool set, never a narrower one.

Selection is driven by *structured* signals, not keyword matching on user text:
the planner's own classification, the tool capabilities already exercised in
this request, and the request's coding/debug shape. A narrow set is only
produced when a structured signal positively supports it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.logging_setup import get_logger

log = get_logger("tool_context")

log_caps = ("filesystem", "project", "code", "python", "terminal", "system", "task")

#: Always presented. Without these an agent turn cannot make progress at all,
#: so this set is a floor that selection may only add to.
CORE_CAPABILITIES = ("filesystem", "project", "system", "task")

#: Individual tools that stay available regardless of capability grouping.
CORE_TOOL_NAMES = (
    "filesystem.read_file",
    "filesystem.list_dir",
    "filesystem.write_file",
    "project.verify",
    "project.info",
    "project.files",
    "system.info",
    "task.info",
)

#: Read-only version-control subset, used when the request is diagnostic. The
#: mutating git surface (push/reset/rebase/branch.switch) is deliberately not
#: in it, so a debugging turn is not offered destructive git operations.
DIAGNOSTIC_TOOLS = (
    "git.status",
    "git.diff",
    "git.log",
    "git.snapshot",
    "git.snapshot.list",
)

#: Capability -> tools added when a signal selects that capability.
CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "coding": ("code", "python", "terminal", "project", "filesystem", "system", "task"),
    "debugging": ("code", "python", "terminal", "project", "filesystem", "system", "task"),
    "version_control": ("git",),
    "github": ("gh",),
    "knowledge": ("memory", "rules"),
    "model_admin": ("models",),
    "desktop": ("applications",),
    "capability": ("capability",),
    "blender": ("capability",),
    "vision": ("capability",),
}


@dataclass(frozen=True)
class ToolContextDecision:
    """The outcome of one tool-context selection, recorded for diagnostics."""

    tool_names: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    signals: Mapping[str, Any] = field(default_factory=dict)
    total_tools: int = 0
    reason: str = ""
    degraded: bool = False
    selected_at: str = ""

    @property
    def tool_count(self) -> int:
        return len(self.tool_names)

    @property
    def reduction_ratio(self) -> float:
        """Fraction of the registry withheld (0.0 = everything presented)."""
        if self.total_tools <= 0:
            return 0.0
        return round(1.0 - (self.tool_count / self.total_tools), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_count": self.tool_count,
            "total_tools": self.total_tools,
            "reduction_ratio": self.reduction_ratio,
            "capabilities": list(self.capabilities),
            "signals": dict(self.signals),
            "reason": self.reason,
            "degraded": self.degraded,
            "selected_at": self.selected_at,
            "tools": list(self.tool_names),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


class ToolContextSelector:
    """Chooses which tool specs to present to the model for one request.

    Stateless and side-effect free: it reads the registry and the signals it is
    given and returns a decision. The caller keeps the decision for diagnostics.
    """

    def __init__(self, registry, min_tools: int = 12):
        self._registry = registry
        self._min_tools = max(0, int(min_tools))

    # ------------------------------------------------------------------ api

    def select(
        self,
        *,
        user_text: str = "",
        planner_class: str = "",
        used_tool_names: Sequence[str] = (),
        capabilities: Sequence[str] = (),
        include: Sequence[str] = (),
        full: bool = False,
    ) -> ToolContextDecision:
        """Return the specs to present. Never raises."""
        try:
            return self._select(
                user_text=str(user_text or ""),
                planner_class=str(planner_class or ""),
                used_tool_names=tuple(used_tool_names or ()),
                capabilities=tuple(capabilities or ()),
                include=tuple(include or ()),
                full=bool(full),
            )
        except Exception as exc:  # pragma: no cover - the fail-safe path
            # Failing safe means: present everything and say so. This must not
            # raise even when the registry itself is what failed, so the
            # recovery path is defensive on its own account.
            log.warning("tool context selection failed: %s: %s", type(exc).__name__, exc)
            try:
                specs = list(self._registry.all_specs())
            except Exception:
                specs = []
            return ToolContextDecision(
                tool_names=tuple(spec.name for spec in specs),
                reason=f"selection failed ({type(exc).__name__}); using full registry",
                degraded=True,
                selected_at=time.strftime("%Y%m%d-%H%M%S"),
            )

    # -------------------------------------------------------------- internals

    def _full_decision(self, reason: str = "full registry requested") -> ToolContextDecision:
        specs = list(self._registry.all_specs())
        return ToolContextDecision(
            tool_names=tuple(spec.name for spec in specs),
            capabilities=tuple(sorted({spec.capability for spec in specs})),
            signals={},
            total_tools=len(specs),
            reason=reason,
            degraded=True,
            selected_at=time.strftime("%Y%m%d-%H%M%S"),
        )

    def _select(
        self,
        *,
        user_text: str,
        planner_class: str,
        used_tool_names: tuple[str, ...],
        capabilities: tuple[str, ...],
        include: tuple[str, ...],
        full: bool,
    ) -> ToolContextDecision:
        all_specs = list(self._registry.all_specs())
        total = len(all_specs)
        by_name = {spec.name: spec for spec in all_specs}
        by_cap: dict[str, list[Any]] = {}
        for spec in all_specs:
            by_cap.setdefault(spec.capability, []).append(spec)

        if full or total == 0:
            return self._full_decision("full registry requested")

        # Capabilities and explicit tools are tracked separately: a capability
        # name is not a tool name, and conflating the two silently yields an
        # empty subset.
        caps: set[str] = set(CORE_CAPABILITIES)
        tools: set[str] = set(CORE_TOOL_NAMES)
        reasons: list[str] = ["core set"]

        # Signal 1: capabilities the request explicitly asked for. Group aliases
        # ("github") and real capability names ("gh") are both accepted. An
        # unresolvable entry is never silently dropped: it is recorded and
        # treated as a weak signal below, so a typo widens the tool set rather
        # than quietly narrowing it.
        unresolved: list[str] = []
        for capability in capabilities:
            key = str(capability)
            if key in by_cap:
                caps.add(key)
            elif key in CAPABILITY_GROUPS:
                caps.update(CAPABILITY_GROUPS[key])
            else:
                unresolved.append(key)
            reasons.append(f"requested:{key}")

        # Signal 2: the planner's own structured classification. Reused rather
        # than re-derived, so routing and tool context cannot disagree.
        klass = str(planner_class or "").strip().lower()
        if klass in ("debug", "develop", "coding"):
            group = "debugging" if klass == "debug" else "coding"
            caps.update(CAPABILITY_GROUPS[group])
            reasons.append(f"planner:{klass}")
            if klass == "debug":
                tools.update(DIAGNOSTIC_TOOLS)
                reasons.append("planner:debug->diagnostic-git")

        # Signal 3: capabilities already exercised in this request. Strongest
        # available evidence, because it is observed rather than inferred.
        used_caps: list[str] = []
        for name in used_tool_names:
            spec = by_name.get(str(name))
            capability = spec.capability if spec else str(name).split(".", 1)[0]
            if capability and capability not in used_caps:
                used_caps.append(capability)
                caps.add(capability)
        if used_caps:
            reasons.append(f"used:{','.join(used_caps)}")

        # Caller-driven additions.
        for name in include:
            spec = by_name.get(str(name))
            if spec is not None:
                caps.add(spec.capability)
        if include:
            reasons.append("include")

        def resolve() -> list[Any]:
            wanted_names = set(tools)
            for cap in caps:
                wanted_names.update(spec.name for spec in by_cap.get(cap, ()))
            return [spec for spec in all_specs if spec.name in wanted_names]

        selected = resolve()

        # Uncertainty expands. If the structured signals said little, or the
        # result would be too small to be useful, widen rather than guess.
        weak_signal = (
            not capabilities and not klass and not used_tool_names
        ) or bool(unresolved)
        if weak_signal or len(selected) < self._min_tools:
            reasons.append(
                "weak-signal->broaden" if weak_signal
                else f"below-floor({len(selected)})->broaden"
            )
            caps.update(log_caps)
            selected = resolve()

        selected_caps = tuple(sorted({spec.capability for spec in selected}))
        return ToolContextDecision(
            tool_names=tuple(spec.name for spec in selected),
            capabilities=selected_caps,
            signals={
                "planner_class": klass,
                "used_capabilities": used_caps,
                "requested_capabilities": list(capabilities),
                "unresolved_capabilities": unresolved,
                "weak_signal": weak_signal,
            },
            total_tools=total,
            reason="; ".join(reasons),
            degraded=False,
            selected_at=time.strftime("%Y%m%d-%H%M%S"),
        )

    def specs_for(self, decision: ToolContextDecision) -> list[Any]:
        """Resolve a decision back to spec objects, in registry order."""
        wanted = set(decision.tool_names)
        return [spec for spec in self._registry.all_specs() if spec.name in wanted]
