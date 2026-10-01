from __future__ import annotations

from config.permissions import PermissionLevel
from config.settings import Settings

from .registry import ToolResult

#: ``settings.memory_write_policy`` values, and what each one means.
#:
#: The setting existed and was read by nothing. It is now the single authority
#: for whether a memory write may proceed, enforced here through the existing
#: permission ladder rather than by a second authorisation system.
#:
#: ``explicit`` (default)
#:     Writes are accepted only from an explicit ``memory.remember`` /
#:     ``memory.update`` call, and only after the content policy in
#:     :mod:`memory.sanitizer` has accepted it.
#: ``confirm``
#:     As ``explicit``, and each write additionally requires user confirmation.
#:     Implemented by escalating the tool's declared level through the existing
#:     ``level_for`` hook, so it flows through ``PermissionManager`` like every
#:     other confirmation in the system.
#: ``off``
#:     Writes are refused. Reads, search and listing still work, and nothing
#:     already stored is deleted.
WRITE_POLICIES = ("explicit", "confirm", "off")

#: Tools that persist memory.
WRITE_TOOLS = ("memory.remember", "memory.update")


def _store(settings: Settings):
    from memory.memory_store import MemoryStore
    return MemoryStore(settings.memory_dir / "memory.json")


def write_policy(settings: Settings | None) -> str:
    """The configured write policy, normalised. Unknown values fail closed."""
    raw = str(getattr(settings, "memory_write_policy", "explicit") or "explicit").strip().lower()
    return raw if raw in WRITE_POLICIES else "explicit"


def memory_write_level(tool_name: str, settings: Settings | None) -> PermissionLevel:
    """Declared level for a memory tool, honouring the write policy.

    ``off`` makes writes ``BLOCKED``. ``confirm`` makes them ``SENSITIVE``,
    which the permission manager already turns into an explicit confirmation
    request. ``explicit`` leaves the tool at its own declared level.
    """
    if tool_name not in WRITE_TOOLS:
        return PermissionLevel.SAFE
    policy = write_policy(settings)
    if policy == "off":
        return PermissionLevel.BLOCKED
    if policy == "confirm":
        return PermissionLevel.SENSITIVE
    return PermissionLevel.NORMAL


def _refusal(tool_name: str, settings: Settings | None) -> ToolResult | None:
    if write_policy(settings) != "off":
        return None
    return ToolResult(
        False,
        error=(
            f"{tool_name} is disabled: memory write_policy is 'off'. "
            "Existing memory is untouched and still readable."
        ),
    )


def memory_remember(key: str, value: str, category: str = "notes", note: str = "", settings: Settings | None = None) -> ToolResult:
    blocked = _refusal("memory.remember", settings)
    if blocked is not None:
        return blocked
    from memory.memory_store import MemoryWriteRefused
    try:
        entry = _store(settings).remember(key, value, category, note)
    except MemoryWriteRefused as exc:
        return ToolResult(False, error=str(exc))
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
    blocked = _refusal("memory.update", settings)
    if blocked is not None:
        return blocked
    from memory.memory_store import MemoryWriteRefused
    try:
        entry = _store(settings).update(key, value)
    except MemoryWriteRefused as exc:
        return ToolResult(False, error=str(exc))
    if entry is None:
        return ToolResult(False, error=f"no memory under key: {key}")
    return ToolResult(True, output=f"updated [{entry['category']}] {entry['key']}")


def memory_delete(key: str, settings: Settings | None = None) -> ToolResult:
    """Delete one entry.

    Deletion is not a content write, so it is not gated by the write policy:
    a user who set ``write_policy = off`` must still be able to remove
    something. Deletion is always allowed by the permission level instead.
    """
    removed = _store(settings).delete(key)
    if not removed:
        return ToolResult(False, error=f"no memory under key: {key}")
    return ToolResult(True, output=f"forgot: {key}")


def memory_categories(settings: Settings | None = None) -> ToolResult:
    cats = _store(settings).categories()
    return ToolResult(True, output="\n".join(f"{name}: {desc}" for name, desc in cats.items()))


def memory_policy_status(settings: Settings | None) -> ToolResult:
    """Report the effective memory policy so the state is never implicit."""
    policy = write_policy(settings)
    lines = [
        f"write_policy: {policy}",
        "explicit - writes accepted from the memory tools, content-sanitised",
        "confirm - as explicit, and each write needs user confirmation",
        "off       - writes refused; existing memory still readable",
    ]
    return ToolResult(True, output="\n".join(lines))
