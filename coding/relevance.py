from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .project_index import FileRecord, index_cached

_TEXT_EXT = {".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".c", ".h", ".cpp", ".cc", ".hpp",
             ".java", ".html", ".css", ".sql", ".json", ".yaml", ".yml", ".md", ".toml",
             ".ini", ".cfg", ".txt", ".xml", ".sh", ".ps1", ".bat"}


@dataclass
class RelevantFile:
    path: str
    excerpt: str

    def to_context(self) -> str:
        rendered = f"--- {self.path} ---\n{self.excerpt}"
        return rendered


def _lex(text: str) -> set[str]:
    words = []
    for token in text.lower().split():
        cleaned = "".join(ch for ch in token if ch.isalnum() or ch in "_/.")
        if cleaned:
            words.append(cleaned)
    return set(words)


def pick_relevant(
    root: Path,
    query: str,
    records: list[FileRecord] | None = None,
    cache_dir: Path | None = None,
    max_files: int = 8,
    max_lines: int = 200,
) -> list[RelevantFile]:
    """Select and excerpt the files most relevant to query. Never reads whole project."""
    records = records or index_cached(root, cache_dir)
    tokens = _lex(query)
    candidates: list[tuple[float, FileRecord]] = []

    for rec in records:
        if rec.ext.lower() not in _TEXT_EXT or rec.is_binary_like or rec.size > 512 * 1024:
            continue
        name_tokens = _lex(rec.name) | _lex(rec.relative.rsplit("/", 1)[-1])
        overlap = len(tokens & name_tokens)
        score = float(overlap)
        lowercase_path = rec.relative.lower()
        for token in tokens:
            if token and token in lowercase_path:
                score += 0.5
        if score > 0:
            candidates.append((score, rec))

    selected = [rec for _, rec in sorted(candidates, key=lambda pair: (-pair[0], pair[1].relative))[:max_files]]

    results: list[RelevantFile] = []
    for rec in selected:
        path = root / rec.relative
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = content.splitlines()
        if len(lines) > max_lines:
            excerpt = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
        else:
            excerpt = content
        results.append(RelevantFile(path=str(path.resolve()), excerpt=excerpt))
    return results


def small_files_excerpt(root: Path, records: list[FileRecord], limit: int = 200) -> str:
    """Direct excerpt of small config/docs files that matter regardless of query."""
    wanted = {"readme.md", "requirements.txt", "pyproject.toml", "package.json", ".env.example",
              "manage.py", "app.py", "main.py", "settings.py", "setup.py"}
    parts = []
    for rec in records:
        base = rec.name.lower()
        if base not in wanted or rec.size > 512 * 1024:
            continue
        try:
            text = (root / rec.relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        teaser = "\n".join(text.splitlines()[:40])
        parts.append(f"--- {rec.relative} ---\n{teaser}")
        if len(parts) >= 4:
            break
    return "\n\n".join(parts)