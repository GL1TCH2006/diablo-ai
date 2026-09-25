from __future__ import annotations

from config.settings import Settings

from .registry import ToolResult


def _store(settings: Settings):
    from memory.memory_store import MemoryStore
    return MemoryStore(settings.memory_dir / "memory.json")


def memory_remember(key: str, value: str, category: str = "notes", note: str = "", settings: Settings | None = None) -> ToolResult:
    try:
        entry = _store(settings).remember(key, value, category, note)
    except ValueError as exc:
        return ToolResult(False, error=str(exc))
    return ToolResult(True, output=f"remembered [{entry['category']}] {entry['key']}")


def memory_get(key: str, settings: Settings | None = None) -> ToolResult:
    entry = _store(settings).get(key)
    if entry is None:
        return ToolResult(True, output=f"(no memory under key: {key})")
    return ToolResult(True, output=f"[{entry['category']}] {entry['key']}:\n{entry['value']}")


def memory_search(query: str, category: str = "", limit: int = 15, settings: Settings | None = None) -> ToolResult:
    hits = _store(settings).search(query, category, limit)
    if not hits:
        return ToolResult(True, output="(no matches)")
    lines = [f"[{e['category']}] {e['key']} -> {e['value'][:200]}" for e in hits]
    return ToolResult(True, output="\n".join(lines))


def memory_list(category: str = "", settings: Settings | None = None) -> ToolResult:
    entries = _store(settings).list(category)
    if not entries:
        return ToolResult(True, output="(empty)")
    lines = [f"[{e['category']}] {e['key']}" for e in entries]
    return ToolResult(True, output="\n".join(lines))


def memory_update(key: str, value: str, settings: Settings | None = None) -> ToolResult:
    entry = _store(settings).update(key, value)
    if entry is None:
        return ToolResult(False, error=f"no memory under key: {key}")
    return ToolResult(True, output=f"updated [{entry['category']}] {entry['key']}")


def memory_delete(key: str, settings: Settings | None = None) -> ToolResult:
    removed = _store(settings).delete(key)
    if not removed:
        return ToolResult(False, error=f"no memory under key: {key}")
    return ToolResult(True, output=f"forgot: {key}")


def memory_categories(settings: Settings | None = None) -> ToolResult:
    cats = _store(settings).categories()
    return ToolResult(True, output="\n".join(f"{name}: {desc}" for name, desc in cats.items()))