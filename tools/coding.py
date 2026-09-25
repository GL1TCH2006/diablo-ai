from __future__ import annotations

import re
from pathlib import Path

from config.settings import Settings
from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("coding")

_MAX_GREP_RESULTS = 100
_MAX_GREP_FILE_BYTES = 256 * 1024


def project_info(path: str, settings: Settings | None = None) -> ToolResult:
    from coding.profile import build_profile

    cache = (settings.sessions_dir / "project-index") if settings else None
    try:
        profile = build_profile(Path(path), cache_dir=cache)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Could not analyse project: {exc}")
    return ToolResult(True, output=profile.to_context())


def project_files(root: str, pattern: str = "", folder: str = "", settings: Settings | None = None) -> ToolResult:
    base = Path(root)
    if not base.is_dir():
        return ToolResult(False, error=f"Not a directory: {base}")
    try:
        cache = (settings.sessions_dir / "project-index") if settings else None
        from coding.project_index import index_cached
        records = index_cached(base, cache_dir=cache)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Could not index project: {exc}")

    pat = pattern.strip().lower()
    folder = folder.strip().lower()
    hits = []
    for rec in sorted(records, key=lambda r: r.relative):
        if folder and not rec.relative.lower().startswith(folder):
            continue
        if pat and pat not in rec.name.lower() and pat not in rec.relative.lower():
            continue
        hits.append(rec.relative)
        if len(hits) >= 200:
            hits.append("... (limit reached)")
            break
    if not hits:
        return ToolResult(True, output="(no matching files)")
    return ToolResult(True, output="\n".join(hits))


def project_grep(root: str, pattern: str, settings: Settings | None = None, limit: int = _MAX_GREP_RESULTS) -> ToolResult:
    base = Path(root)
    if not base.is_dir():
        return ToolResult(False, error=f"Not a directory: {base}")
    if not pattern:
        return ToolResult(False, error="Empty search pattern.")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(False, error=f"Invalid regex: {exc}")

    try:
        cache = (settings.sessions_dir / "project-index") if settings else None
        from coding.project_index import index_cached
        records = index_cached(base, cache_dir=cache)
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Could not index project: {exc}")

    matches: list[str] = []
    try:
        for rec in sorted(records, key=lambda r: r.relative):
            if rec.is_binary_like or rec.size > _MAX_GREP_FILE_BYTES:
                continue
            try:
                path = base / rec.relative
                if not path.is_file() or path.stat().st_size > _MAX_GREP_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rec.relative}:{idx}: {line.strip()[:220]}")
                    if len(matches) >= limit:
                        matches.append("... (limit reached)")
                        return ToolResult(True, output="\n".join(matches))
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult(False, error=f"Grep failed: {exc}")
    if not matches:
        return ToolResult(True, output="(no matches)")
    return ToolResult(True, output="\n".join(matches))