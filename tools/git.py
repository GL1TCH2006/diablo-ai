from __future__ import annotations

import subprocess
from pathlib import Path

from core.logging_setup import get_logger

from .registry import ToolResult

log = get_logger("git")

_SAFE_GIT = ("git.status", "git.diff", "git.log", "git.branch")


def _git(root: str, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=str(Path(root)), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"git timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)


def _valid_git_ref(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.startswith("-") and not any(ord(ch) < 32 for ch in text)


def git_status(root: str) -> ToolResult:
    code, out, err = _git(root, ["status", "--porcelain=v1", "--branch"])
    if code == 0 and out:
        return ToolResult(True, output=out)
    return ToolResult(code == 0, output=out, error=err if code else "")


def git_diff(root: str, include_staged: bool = False, stat: bool = False, max_chars: int = 12000) -> ToolResult:
    args = ["diff"]
    if include_staged:
        args.append("--staged")
    if stat:
        args.append("--stat")
    code, out, err = _git(root, args)
    if code != 0:
        return ToolResult(False, error=err or "git diff failed")
    if not out:
        return ToolResult(True, output="(no changes)")
    return ToolResult(True, output=out[:max_chars])


def git_log(root: str, max_entries: int = 20) -> ToolResult:
    code, out, err = _git(root, ["log", "--oneline", f"-{max_entries}"])
    return ToolResult(code == 0, output=out, error=err if code else "")


def git_branch(root: str) -> ToolResult:
    code, out, err = _git(root, ["branch", "--show-current"])
    return ToolResult(code == 0, output=out or "(detached)", error=err if code else "")


def git_branches(root: str) -> ToolResult:
    code, out, err = _git(root, ["branch", "--list"])
    return ToolResult(code == 0, output=out or "(no branches)", error=err if code else "")


def git_branch_create(root: str, name: str, settings=None) -> ToolResult:
    if not name or not name.strip():
        return ToolResult(False, error="empty branch name")
    name = name.strip()
    code, out, err = _git(root, ["check-ref-format", "--branch", name])
    if code != 0:
        return ToolResult(False, error=err or f"invalid branch name: {name}")
    _gate(settings.git_branch_policy if settings else "blocked", "branch create")
    code, out, err = _git(root, ["switch", "-c", name])
    if code != 0:
        return ToolResult(False, output=out, error=err or f"could not create branch {name} (already exists?)")
    return ToolResult(True, output=out or f"created and switched to {name}", error="")


def _gate(policy: str, action: str) -> None:
    from config.permissions import ToolBlockedError
    if policy not in ("allow", "confirm"):
        raise ToolBlockedError(
            f"git {action} is disabled by policy (config [git]). Change git_{action}_policy to 'confirm' to allow."
        )


def _backup_diff(root: str, backup_dir: Path) -> Path | None:
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        code, out, err = _git(root, ["diff", "--no-color", "--binary", "HEAD"])
        if code != 0 or not out.strip():
            return None
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"git-before-{stamp}.patch"
        dest.write_text(out + "\n", encoding="utf-8", errors="replace")
        return dest
    except OSError:
        return None


def git_stage(root: str, paths: list[str] | None = None, all: bool = False) -> ToolResult:
    args = ["add"]
    if all:
        args.append("-A")
    args.append("--")
    if not all:
        args.extend(paths or ["."])
    code, out, err = _git(root, args)
    return ToolResult(code == 0, output=out or f"staged {', '.join(paths) if paths else 'all changes'}", error=err if code else "")


def git_commit(root: str, message: str) -> ToolResult:
    if not message or not message.strip():
        return ToolResult(False, error="empty commit message")
    code, out, err = _git(root, ["commit", "-m", message.strip()])
    if code != 0:
        return ToolResult(False, output=out, error=err or "commit failed (nothing to commit?)")
    return ToolResult(True, output=out or f"committed: {message.strip()[:80]}", error="")


def git_push(root: str, remote: str = "origin", branch: str = "", settings=None) -> ToolResult:
    _gate(settings.git_push_policy if settings else "blocked", "push")
    if not _valid_git_ref(remote):
        return ToolResult(False, error="invalid remote")
    if branch and not _valid_git_ref(branch):
        return ToolResult(False, error="invalid branch")
    args = ["push", remote]
    if branch:
        args.append(branch)
    code, out, err = _git(root, args)
    return ToolResult(code == 0, output=out, error=err if code else "")


def git_restore(root: str, paths: list[str] | None = None, all: bool = False, settings=None) -> ToolResult:
    _gate(settings.git_restore_policy if settings else "blocked", "restore")
    backup_dir = settings.backups_dir if settings else None
    backup = _backup_diff(root, backup_dir) if backup_dir else None
    if paths and not all:
        targets = [p for p in paths if (Path(root) / p).exists() or not p.startswith("-")]
        if not targets:
            return ToolResult(False, error="no target paths provided")
    else:
        targets = ["."]
    args = ["restore", "--worktree", "--"] + targets
    code, out, err = _git(root, args)
    if code != 0:
        return ToolResult(False, output=out, error=err or "git restore failed")
    note = f"backup: {backup}" if backup else "no uncommitted changes to back up"
    return ToolResult(True, output=f"restored {', '.join(targets)} ({note})")


def git_reset(root: str, mode: str = "mixed", target: str = "HEAD", settings=None) -> ToolResult:
    mode = (mode or "mixed").lower()
    if mode not in ("soft", "mixed", "hard"):
        return ToolResult(False, error=f"unsupported reset mode: {mode}")
    if not _valid_git_ref(target):
        return ToolResult(False, error="invalid reset target")
    if mode == "hard":
        _gate(settings.git_restore_policy if settings else "blocked", "reset --hard")
        backup = _backup_diff(root, settings.backups_dir) if settings else None
        note = f"\nbackup: {backup}" if backup else "\n(no backup written - clean tree or no backups dir)"
    else:
        note = ""
    args = ["reset", "--" + mode, target]
    code, out, err = _git(root, args)
    return ToolResult(code == 0, output=(out or "reset done") + note, error=err if code else "")


def git_fetch(root: str, remote: str = "origin") -> ToolResult:
    if not _valid_git_ref(remote):
        return ToolResult(False, error="invalid remote")
    code, out, err = _git(root, ["fetch", remote])
    return ToolResult(code == 0, output=out or "fetched", error=err if code else "")


def git_pull(root: str, remote: str = "origin", branch: str = "") -> ToolResult:
    if remote and not _valid_git_ref(remote):
        return ToolResult(False, error="invalid remote")
    if branch and not _valid_git_ref(branch):
        return ToolResult(False, error="invalid branch")
    args = ["pull"]
    if remote:
        args.append(remote)
    if branch:
        args.append(branch)
    code, out, err = _git(root, args)
    return ToolResult(code == 0, output=out, error=err if code else "")


def git_rebase(root: str, onto: str, settings=None) -> ToolResult:
    _gate(settings.git_restore_policy if settings else "blocked", "rebase")
    if not _valid_git_ref(onto):
        return ToolResult(False, error="invalid rebase target")
    code, out, err = _git(root, ["rebase", onto])
    return ToolResult(code == 0, output=out, error=err if code else "")


def git_branch_list(root: str) -> ToolResult:
    code, out, err = _git(root, ["branch", "--list", "--no-color"])
    if code == 0 and out:
        return ToolResult(True, output=out)
    if code == 0:
        return ToolResult(True, output="(no branches)")
    return ToolResult(False, output=out, error=err if code else "")


def git_branch_create(root: str, name: str, settings=None) -> ToolResult:
    _gate(settings.git_branch_policy if settings else "blocked", "branch create")
    name = (name or "").strip()
    if not name:
        return ToolResult(False, error="empty branch name")
    if not _valid_git_ref(name):
        return ToolResult(False, error="invalid branch name")
    code, out, err = _git(root, ["check-ref-format", "--branch", name])
    if code != 0:
        return ToolResult(False, output=out, error=err or f"invalid branch name: {name}")
    code, out, err = _git(root, ["branch", "--list", "--no-color", name])
    if code == 0 and name in out:
        return ToolResult(True, output=f"branch already exists: {name} (no-op)")
    code, out, err = _git(root, ["switch", "-c", name])
    if code != 0:
        return ToolResult(False, output=out, error=err or f"could not create branch {name}")
    return ToolResult(True, output=out or f"created and switched to branch {name}")


def _git_branch_switch(root: str, name: str) -> ToolResult:
    name = (name or "").strip()
    if not name:
        return ToolResult(False, error="empty branch name")
    if not _valid_git_ref(name):
        return ToolResult(False, error="invalid branch name")
    code, out, err = _git(root, ["switch", name])
    if code != 0:
        return ToolResult(False, output=out, error=err or f"could not switch to branch {name}")
    return ToolResult(True, output=out or f"switched to branch {name}")


def git_branch_switch(root: str, name: str, settings=None) -> ToolResult:
    _gate(settings.git_branch_policy if settings else "blocked", "branch switch")
    return _git_branch_switch(root, name)


def git_snapshot(
    root: str,
    task_id: str,
    label: str,
    settings=None,
    creation_reason: str = "manual",
) -> ToolResult:
    from coding.snapshot_manager import SnapshotManager
    if not task_id or not task_id.strip():
        return ToolResult(False, error="empty task_id")
    if not label or not label.strip():
        return ToolResult(False, error="empty label")
    if not root or not Path(root).is_dir():
        return ToolResult(False, error="invalid project root")

    manager = SnapshotManager(settings.sessions_dir)
    try:
        snapshot = manager.create_snapshot(
            task_id=task_id.strip(),
            label=label.strip(),
            project_root=root,
            creation_reason=creation_reason,
        )
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(False, error=f"Could not create snapshot: {exc}")
    from core.task_store import TaskStore
    TaskStore(settings.sessions_dir).link_snapshot(snapshot.task_id, snapshot.snapshot_id)

    return ToolResult(
        True,
        output=f"snapshot created: {snapshot.snapshot_id}",
        error="",
    )


def git_snapshot_list(task_id: str, settings=None) -> ToolResult:
    from coding.snapshot_manager import SnapshotManager
    if not task_id or not task_id.strip():
        return ToolResult(False, error="empty task_id")

    manager = SnapshotManager(settings.sessions_dir)
    try:
        snapshots = manager.list_snapshots(task_id.strip())
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(False, error=str(exc))

    if not snapshots:
        return ToolResult(True, output="(no snapshots)")

    lines = []
    for s in snapshots:
        status = "restored" if s.restored else "active"
        lines.append(f"{s.snapshot_id} | {s.label} | {s.created_at} | {s.git_branch} | {s.git_commit[:8]} | {status}")
    return ToolResult(True, output="\n".join(lines))


def _same_path(left: str, right: str) -> bool:
    try:
        return str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()
    except OSError:
        return False


def git_snapshot_restore(
    root: str,
    task_id: str,
    snapshot_id: str,
    settings=None,
    confirmer=None,
) -> ToolResult:
    from coding.snapshot_manager import SnapshotManager
    if not task_id or not task_id.strip():
        return ToolResult(False, error="empty task_id")
    if not snapshot_id or not snapshot_id.strip():
        return ToolResult(False, error="empty snapshot_id")

    _gate(settings.git_restore_policy if settings else "blocked", "snapshot restore")

    manager = SnapshotManager(settings.sessions_dir)
    try:
        snapshot = manager.get_snapshot(task_id.strip(), snapshot_id.strip())
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(False, error=str(exc))
    if snapshot is None:
        return ToolResult(False, error=f"Snapshot not found: {snapshot_id.strip()}")
    if not _same_path(root, snapshot.project_root):
        return ToolResult(False, error="Snapshot project root does not match authorized root")
    try:
        success, message = manager.restore_snapshot(
            task_id=task_id.strip(),
            snapshot_id=snapshot_id.strip(),
            settings=settings,
            confirmer=confirmer,
        )
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(False, error=f"Could not restore snapshot: {exc}")

    if success:
        return ToolResult(True, output=message)
    return ToolResult(False, error=message)