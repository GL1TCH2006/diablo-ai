from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_setup import get_logger

log = get_logger("patch")

_ACTIONS = re.compile(r"^(replace|insert_after|insert_before|delete) (.*)$")
_SECTION = re.compile(r"^=+\s*(.+?)\s*=+\s*$")
_LINE_TOKEN = re.compile(r"^([+\- ])(.*)$")
_AMBIGUOUS = -8  # anchor matched more than one line


@dataclass
class Hunk:
    action: str
    anchor: str
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""


@dataclass
class PatchBlock:
    path: Path
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class PatchPlan:
    blocks: list[PatchBlock] = field(default_factory=list)
    parse_errors: list[Hunk] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return all(h.ok for b in self.blocks for h in b.hunks) and not self.parse_errors


def parse_patch(patch_text: str) -> PatchPlan:
    """Parse Diablo's anchored patch syntax into a PatchPlan.

    Syntax::
        === src/app.py ===        <- target file (absolute or relative to workspace)
        replace <anchor line>     <- action + exact existing line to locate
        -<line to remove>         <- prefixed lines you expect to find (optional, validated)
        +<new line>               <- prefixed lines to insert (default behaviour)
    """
    plan = PatchPlan()
    current: PatchBlock | None = None
    hunk: Hunk | None = None

    for raw in patch_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        section = _SECTION.match(line.strip())
        if section:
            current = PatchBlock(path=Path(section.group(1).strip()))
            plan.blocks.append(current)
            hunk = None
            continue
        if current is None:
            plan.parse_errors.append(Hunk("", "", error="content before first '=== path ===' section"))
            continue
        if hunk is None:
            m = _ACTIONS.match(line.strip())
            if not m:
                hunk = Hunk("", "")
                hunk.ok = False
                hunk.error = f"expected 'replace/insert_after/insert_before/delete <anchor>', got: {line.strip()[:60]}"
                plan.parse_errors.append(hunk)
                current.hunks.append(hunk)
                continue
            hunk = Hunk(action=m.group(1), anchor=m.group(2).rstrip())
            current.hunks.append(hunk)
            continue
        tok = _LINE_TOKEN.match(line)
        if not tok:
            hunk.ok = False
            hunk.error = f"line must start with +, - or space: {line[:60]}"
            plan.parse_errors.append(hunk)
            continue
        marker, content = tok.group(1), tok.group(2)
        if marker == "+":
            hunk.add.append(content)
        elif marker == "-":
            hunk.remove.append(content)
        else:
            hunk.context.append(content)
    return plan


def _index_of(lines: list[str], needle: str) -> int:
    target = needle.rstrip()
    exact = [i for i, line in enumerate(lines) if line.rstrip() == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return _AMBIGUOUS
    fuzzy = [i for i, line in enumerate(lines) if target.strip().lower() in line.strip().lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        return _AMBIGUOUS
    return -1  # missing


def apply_block(block: PatchBlock, workspace: Path, dry_run: bool = True, backup_dir: Path | None = None) -> tuple[bool, str]:
    path = block.path
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    if not path.is_file():
        return False, f"file does not exist: {path}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"could not read {path}: {exc}"

    new_lines = list(lines)
    applied = 0
    for hunk in block.hunks:
        if not hunk.ok or hunk.error:
            return False, f"invalid hunk in {block.path}: {hunk.error}"
        idx = _index_of(new_lines, hunk.anchor)
        if idx == -1:
            return False, f"anchor not found in {block.path}: {hunk.anchor[:80]}"
        if idx == _AMBIGUOUS:
            return False, f"anchor is ambiguous in {block.path} (more than one match): {hunk.anchor[:80]}"

        existing_window = new_lines[idx:idx + len(hunk.remove)]
        if [ln.rstrip() for ln in existing_window] != hunk.remove:
            soft_ok = bool(hunk.remove) and all(
                expected.strip().lower() in actual.strip().lower()
                for actual, expected in zip(existing_window, hunk.remove)
            )
            if not soft_ok:
                return False, (
                    f"safety check failed in {block.path} after '{hunk.anchor[:40]}': "
                    f"lines do not match ({existing_window[0] if existing_window else '<blank>'} != {hunk.remove[0] if hunk.remove else '<blank>'})"
                )

        if hunk.action == "replace":
            removal_len = max(len(hunk.remove), 1)
            new_lines[idx:idx + removal_len] = hunk.add
        elif hunk.action == "delete":
            del new_lines[idx:idx + max(len(hunk.remove), 1)]
        elif hunk.action == "insert_after":
            insertion = list(hunk.add)
            new_lines[idx + 1:idx + 1] = insertion
        elif hunk.action == "insert_before":
            new_lines[idx:idx] = list(hunk.add)
        else:
            return False, f"unknown action: {hunk.action}"
        applied += 1

    new_text = "\n".join(new_lines)
    if new_text == "\n".join(lines):
        return True, f"{path}: no net change ({applied} hunk(s) no-op)"
    if dry_run:
        return True, f"{path}: would apply {applied} hunk(s) (dry run, no file written)"
    backup_path = _backup(path, backup_dir)
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return (backup_path is None), f"{path}: write failed: {exc}"
    return True, f"{path}: applied {applied} hunk(s); backup at {backup_path}"


def apply_plan(plan: PatchPlan, workspace: Path, dry_run: bool = True, backup_dir: Path | None = None) -> list[tuple[Path, bool, str]]:
    results = []
    for block in plan.blocks:
        ok, message = apply_block(block, workspace, dry_run=dry_run, backup_dir=backup_dir)
        results.append((block.path, ok, message))
    return results


def _join_replacement(add_lines: list[str]) -> str:
    return "\n".join(add_lines)


def _backup(path: Path, backup_dir: Path | None = None) -> Path | None:
    """Copy file to backups dir with a timestamp. Returns the backup path or None."""
    base = backup_dir or (path.parent / ".diablo-backups")
    try:
        base.mkdir(parents=True, exist_ok=True)
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        dest = base / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, dest)
        return dest
    except OSError as exc:
        log.warning("backup failed for %s: %s", path, exc)
        return None