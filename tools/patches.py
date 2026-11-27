from __future__ import annotations

from pathlib import Path

from config.settings import Settings
from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("patches")


def patch_file(patch_text: str, dry_run: bool = False, workspace: str = "", settings: Settings | None = None) -> ToolResult:
    from coding.patch import apply_plan, parse_patch

    if not patch_text or not patch_text.strip():
        return ToolResult(False, error="Empty patch.")

    workdir = Path(workspace) if workspace else (settings.base_dir if settings else Path.cwd())
    plan = parse_patch(patch_text)
    if not plan.valid:
        first_bad = next((h.error for b in plan.blocks for h in b.hunks if h.error), "parse error")
        return ToolResult(False, error=f"Patch rejected before touching any file: {first_bad}")
    resolved_root = workdir.resolve()
    for block in plan.blocks:
        candidate = Path(block.path)
        if candidate.is_absolute() or not (resolved_root / candidate).resolve().is_relative_to(resolved_root):
            return ToolResult(False, error=f"Patch path escapes workspace: {block.path}")

    results = apply_plan(plan, workdir, dry_run=dry_run, backup_dir=settings.backups_dir if settings else None)
    failures = [r for r in results if not r[1]]
    ok = not failures
    if not ok:
        lines = [f"[failed] {r[0]}: {r[2]}" for r in failures]
        ok_lines = [f"[committed] {r[0]}: {r[2]}" for r in results if r[1]]
        return ToolResult(False, error="\n".join(lines + ok_lines))
    return ToolResult(
        True,
        output="\n".join(f"[{'dry-run' if dry_run else 'done'}] {r[0]}: {r[2]}" for r in results),
    )