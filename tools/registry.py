from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from config.permissions import PermissionLevel


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    error: str = ""
    exit_code: int | None = None
    data: dict | None = None
    cancelled: bool = False
    verification: Any = None


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., ToolResult]
    parameters: dict | None = None
    level: PermissionLevel = PermissionLevel.SAFE
    level_for: Callable[[dict], PermissionLevel] | None = None
    required: list[str] = field(default_factory=list)
    describe: Callable[[dict], str] | None = None
    wants_context: bool = False
    validate_args: Callable[[dict], None] | None = None
    persist_data: bool = False
    explicit_confirmation: bool = False
    #: M2.13.0: explicit capability tags. Left empty on almost every tool, in
    #: which case :attr:`capability` is derived from the tool's name namespace.
    #: An explicit tag exists so a tool can declare that it serves a capability
    #: other than the one its name suggests (``capability.execute`` reaches
    #: Blender and Vision without being named after them).
    capabilities: tuple[str, ...] = ()

    @property
    def capability(self) -> str:
        """The single capability this tool belongs to.

        Derived from the name namespace (``git.status`` -> ``git``) unless the
        spec declares one explicitly. The namespace is real, stable structure
        already present in every registered tool name, so classification needs
        no keyword matching against user text.
        """
        if self.capabilities:
            return str(self.capabilities[0])
        namespace = str(self.name or "").split(".", 1)[0]
        return namespace or "unknown"

    def describe_args(self, args: dict) -> str:
        if self.describe is not None:
            try:
                return self.describe(args)
            except Exception:
                pass
        return f"{self.name}({args})"


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    task_id: str | None
    cancellation_token: Any


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def by_capability(self, capability: str) -> list[ToolSpec]:
        """Every spec whose capability matches, in registration order.

        Ordering is registration order, so a capability subset is deterministic.
        """
        wanted = str(capability or "")
        return [spec for spec in self._tools.values() if spec.capability == wanted]