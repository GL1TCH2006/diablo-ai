from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.logging_setup import get_logger

log = get_logger("project_index")

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "out", "target", ".idea", ".vscode", ".pytest_cache",
    ".tox", ".nox", "data", "logs", "backups", ".next", "coverage",
}

_BINARY_EXT = {
    ".exe", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif",
    ".pyd", ".class", ".jar", ".zip", ".7z", ".gz", ".bin", ".obj",
    ".lib", ".whl", ".pyc", ".dat", ".db", ".ico", ".woff", ".woff2",
}


@dataclass
class FileRecord:
    relative: str
    size: int
    mtime: float
    ext: str

    @property
    def name(self) -> str:
        return self.relative.rsplit("/", 1)[-1]

    @property
    def is_binary_like(self) -> bool:
        return self.ext.lower() in _BINARY_EXT


def index_project(root: Path, max_files: int = 2000, max_depth: int = 12) -> list[FileRecord]:
    """Walk a project and return a flat, capped file map (ignoring vendor dirs)."""
    if not root.is_dir():
        return []
    records: list[FileRecord] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth or len(records) >= max_files:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                if entry.name not in IGNORED_DIRS:
                    walk(entry, depth + 1)
                continue
            if not entry.is_file() or entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            rel = entry.resolve().relative_to(root.resolve()).as_posix()
            records.append(FileRecord(relative=rel, size=stat.st_size, mtime=stat.st_mtime, ext=entry.suffix.lower()))
            if len(records) >= max_files:
                break

    walk(root, 0)
    return records


def index_cached(root: Path, cache_dir: Path | None, max_files: int = 2000) -> list[FileRecord]:
    """index_project with a stale-by-mtime JSON cache under cache_dir."""
    if cache_dir is None:
        return index_project(root, max_files=max_files)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{root.name.replace(' ', '_')}.json"
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            records = [FileRecord(**r) for r in cached.get("records", [])]
            newest = max((r.mtime for r in records), default=0.0)
            if newest <= cache_file.stat().st_mtime:
                return records
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("index cache unavailable: %s", exc)

    records = index_project(root, max_files=max_files)
    newest = max((r.mtime for r in records), default=0.0)
    try:
        cache_file.write_text(
            json.dumps({"newest_mtime": newest, "records": [r.__dict__ for r in records]}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - defensive
        log.debug("cannot write index cache: %s", exc)
    return records


def summarize_files(records: list[FileRecord], max_chars: int = 800) -> str:
    """Compact model-friendly tree summary grouped by top-level directory."""
    by_dir: dict[str, list[FileRecord]] = {}
    for rec in records:
        node = rec.relative.split("/", 1)[0]
        by_dir.setdefault(node, []).append(rec)

    lines = []
    total = len(records)
    for key in sorted(by_dir):
        group = by_dir[key]
        names = ", ".join(r.name + ("*" if r.is_binary_like else "") for r in group[:40])
        lines.append(f"{key}/ ({len(group)}): {names}")
    head = f"{total} file(s):\n" + "\n".join(lines)
    if len(head) > max_chars:
        head = head[:max_chars] + "\n... (truncated)"
    return head