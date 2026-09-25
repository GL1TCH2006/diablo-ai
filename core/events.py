from __future__ import annotations

from collections import defaultdict
from typing import Callable


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, kind: str, fn: Callable) -> None:
        if not isinstance(kind, str) or not kind.strip() or not callable(fn):
            return
        self._listeners[kind].append(fn)

    def emit(self, kind: str, data: dict | None = None) -> None:
        if not isinstance(kind, str) or not kind.strip() or not isinstance(data, dict):
            return
        for fn in list(self._listeners.get(kind, [])):
            try:
                fn(dict(data))
            except Exception:
                continue

    def emit_tool(self, tool: str, status: str, detail: str = "") -> None:
        self.emit("tool", {"tool": tool, "status": status, "detail": detail})

    def emit_status(self, key: str, value: str) -> None:
        self.emit("status", {"key": key, "value": value})


class NullBus(EventBus):
    def emit(self, kind: str, data: dict | None = None) -> None:
        return

    def emit_tool(self, tool: str, status: str, detail: str = "") -> None:
        return

    def emit_status(self, key: str, value: str) -> None:
        return
