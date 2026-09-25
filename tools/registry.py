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