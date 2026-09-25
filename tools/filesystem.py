from __future__ import annotations

import shutil
from pathlib import Path

from config.permissions import path_is_within
from config.settings import Settings
from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("filesystem")


def read_file(path: str) -> ToolResult:
    target = Path(path)
    if not target.exists():
        return ToolResult(False, error=f"File not found: {target}")
    if not target.is_file():
        return ToolResult(False, error=f"Not a file: {target}")
    if target.stat().st_size > 512 * 1024:
        return ToolResult(False, error=f"File too large to read ({target.stat().st_size} bytes).")
    try:
        content = target.read_text(encoding="utf-8")
        return ToolResult(True, output=content)
    except (UnicodeDecodeError, OSError) as exc:
        return ToolResult(False, error=f"Cannot read {target}: {exc}")


def write_file(path: str, content: str, settings: Settings | None = None) -> ToolResult:
    target = Path(path)
    try:
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = _create_backup(target, settings)
        target.write_text(content, encoding="utf-8")
        extra = f" (backup: {backup.name})" if backup else ""
        return ToolResult(True, output=f"Wrote {target} ({target.stat().st_size} bytes){extra}")
    except OSError as exc:
        return ToolResult(False, error=f"Cannot write {target}: {exc}")


def list_dir(path: str) -> ToolResult:
    target = Path(path)
    if not target.exists():
        return ToolResult(False, error=f"Path not found: {target}")
    if not target.is_dir():
        return ToolResult(False, error=f"Not a directory: {target}")
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return ToolResult(True, output="\n".join(names) if names else "(empty)")


def delete_file(path: str, settings: Settings | None = None) -> ToolResult:
    target = Path(path)
    if not target.exists():
        return ToolResult(False, error=f"File not found: {target}")
    if target.is_dir():
        return ToolResult(False, error="Refusing to delete a directory via this tool. Use an explicit file path.")
    try:
        target.unlink()
        return ToolResult(True, output=f"Deleted {target}")
    except OSError as exc:
        return ToolResult(False, error=f"Cannot delete {target}: {exc}")


def _create_backup(target: Path, settings: Settings | None) -> Path | None:
    if not target.exists():
        return None
    backups = (settings.backups_dir if settings else Path("data/backups"))
    backups.mkdir(parents=True, exist_ok=True)
    import time
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backups / f"{target.stem}.{stamp}{''.join(target.suffixes)}.bak"
    shutil.copy2(target, backup)
    log.info("backup created: %s -> %s", target, backup)
    return backup


def within_allowed(path: str, settings: Settings) -> bool:
    return path_is_within(Path(path), settings.allowed_roots)