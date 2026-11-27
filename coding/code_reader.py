from __future__ import annotations

from pathlib import Path


def read_text_file(path: str) -> tuple[bool, str, str]:
    target = Path(path)
    if not target.is_file():
        return False, "", f"File not found: {target}"
    if target.stat().st_size > 512 * 1024:
        return False, "", f"File too large to read: {target}"
    try:
        return True, target.read_text(encoding="utf-8"), ""
    except (OSError, UnicodeDecodeError) as exc:
        return False, "", f"Cannot read {target}: {exc}"


def list_tree(root: str, max_depth: int = 3, limit: int = 200) -> tuple[bool, str, str]:
    base = Path(root)
    if not base.is_dir():
        return False, "", f"Not a directory: {base}"
    lines: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if len(lines) >= limit:
            return
        indent = "  " * depth
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith((".", "__pycache__", "node_modules", ".venv", "data", "logs")):
                continue
            if len(lines) >= limit:
                lines.append("... (limit reached)")
                return
            lines.append(f"{indent}{entry.name}/" if entry.is_dir() else f"{indent}{entry.name}")
            if entry.is_dir() and depth < max_depth:
                walk(entry, depth + 1)

    walk(base, 0)
    return True, "\n".join(lines), ""


def find_files(root: str, pattern: str) -> tuple[bool, list[str], str]:
    base = Path(root)
    if not base.is_dir():
        return False, [], f"Not a directory: {base}"
    hits = [str(p.resolve()) for p in base.rglob(pattern) if p.is_file()][:100]
    return True, hits, ""