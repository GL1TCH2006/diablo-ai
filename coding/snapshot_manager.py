from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from core.logging_setup import get_logger
from tools.git import git_status, git_diff, git_branch, git_log, _git, git_reset, _git_branch_switch

log = get_logger("snapshot_manager")


def _valid_component(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in (".", "..")
        and "/" not in value
        and "\\" not in value
        and ":" not in value
        and "\x00" not in value
    )


def _same_path(left: Path, right: Path) -> bool:
    try:
        return str(left.resolve()).casefold() == str(right.resolve()).casefold()
    except OSError:
        return False


@dataclass
class Snapshot:
    snapshot_id: str
    task_id: str
    project_root: str
    label: str
    created_at: str
    git_commit: str
    git_branch: str
    git_status: str
    git_diff_stat: str
    snapshot_dir: str
    creation_reason: str = ""
    restored: bool = False
    restored_at: str = ""
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str) -> "Snapshot":
        return cls(**json.loads(data))


class SnapshotManager:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)
        self.snapshots_root = self.sessions_dir / "snapshots"
        self.snapshots_root.mkdir(parents=True, exist_ok=True)

    def _task_snapshot_dir(self, task_id: str, create: bool = True) -> Path:
        if not _valid_component(task_id):
            raise ValueError("unsafe task_id")
        task_dir = self.snapshots_root / task_id
        if create:
            task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def _get_git_commit(self, root: str) -> str:
        code, out, err = _git(root, ["rev-parse", "HEAD"])
        if code == 0 and out:
            return out.strip()
        return ""

    def _get_git_branch(self, root: str) -> str:
        code, out, err = _git(root, ["branch", "--show-current"])
        if code == 0 and out:
            return out.strip()
        return "(detached)"

    def _get_git_status(self, root: str) -> str:
        result = git_status(root)
        return result.output if result.success else result.error or ""

    def _get_git_diff_stat(self, root: str) -> str:
        result = git_diff(root, stat=True)
        return result.output if result.success else result.error or ""

    def _capture_workspace_state(self, root: str, snapshot_dir: Path) -> None:
        state_dir = snapshot_dir / "workspace_state"
        state_dir.mkdir(parents=True, exist_ok=True)

        status_result = git_status(root)
        if status_result.success:
            (state_dir / "git_status.txt").write_text(status_result.output or "", encoding="utf-8")

        diff_result = git_diff(root, include_staged=False, stat=False)
        if diff_result.success and diff_result.output:
            (state_dir / "git_diff.patch").write_text(diff_result.output, encoding="utf-8")

        log_result = git_log(root, max_entries=5)
        if log_result.success and log_result.output:
            (state_dir / "git_recent_log.txt").write_text(log_result.output, encoding="utf-8")

    def create_snapshot(
        self,
        task_id: str,
        label: str,
        project_root: str,
        creation_reason: str = "manual",
    ) -> Snapshot:
        if not _valid_component(task_id):
            raise ValueError("unsafe task_id")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("empty label")
        root_path = Path(project_root)
        if not root_path.is_dir():
            raise ValueError("invalid project root")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        snapshot_dir = self._task_snapshot_dir(task_id) / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        git_commit = self._get_git_commit(project_root)
        git_branch = self._get_git_branch(project_root)
        git_status_out = self._get_git_status(project_root)
        git_diff_stat = self._get_git_diff_stat(project_root)

        self._capture_workspace_state(project_root, snapshot_dir)

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            task_id=task_id,
            project_root=project_root,
            label=label,
            created_at=stamp,
            git_commit=git_commit,
            git_branch=git_branch,
            git_status=git_status_out,
            git_diff_stat=git_diff_stat,
            snapshot_dir=str(snapshot_dir),
            creation_reason=creation_reason,
        )

        snapshot_path = snapshot_dir / f"{snapshot_id}.json"
        snapshot_path.write_text(snapshot.to_json(), encoding="utf-8")

        log.info("snapshot created: %s (task=%s, label=%s)", snapshot_id, task_id, label)
        return snapshot

    def list_snapshots(self, task_id: str) -> list[Snapshot]:
        task_dir = self._task_snapshot_dir(task_id, create=False)
        snapshots = []
        if task_dir.is_dir():
            for snap_file in task_dir.glob("*/*.json"):
                try:
                    data = snap_file.read_text(encoding="utf-8")
                    snapshot = Snapshot.from_json(data)
                    if snapshot.task_id == task_id and _valid_component(snapshot.snapshot_id):
                        snapshots.append(snapshot)
                except (OSError, json.JSONDecodeError, TypeError) as exc:
                    log.warning("failed to load snapshot %s: %s", snap_file, exc)
        snapshots.sort(key=lambda s: s.created_at)
        return snapshots

    def get_snapshot(self, task_id: str, snapshot_id: str) -> Optional[Snapshot]:
        if not _valid_component(snapshot_id):
            return None
        task_dir = self._task_snapshot_dir(task_id, create=False)
        snapshot_file = task_dir / snapshot_id / f"{snapshot_id}.json"
        if not snapshot_file.is_file():
            return None
        try:
            data = snapshot_file.read_text(encoding="utf-8")
            snapshot = Snapshot.from_json(data)
            if snapshot.task_id != task_id or snapshot.snapshot_id != snapshot_id:
                return None
            return snapshot
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            log.warning("failed to load snapshot %s: %s", snapshot_file, exc)
            return None

    def validate_snapshot(self, snapshot: Snapshot) -> tuple[bool, str]:
        if not _valid_component(snapshot.task_id) or not _valid_component(snapshot.snapshot_id):
            return False, "unsafe snapshot identity"
        project_root = Path(snapshot.project_root)
        if not project_root.is_dir():
            return False, f"project root no longer exists: {snapshot.project_root}"

        snapshot_dir = Path(snapshot.snapshot_dir)
        expected_dir = self.snapshots_root / snapshot.task_id / snapshot.snapshot_id
        if not _same_path(snapshot_dir, expected_dir):
            return False, "snapshot directory does not match task identity"
        if not snapshot_dir.is_dir():
            return False, f"snapshot directory missing: {snapshot.snapshot_dir}"

        snap_file = snapshot_dir / f"{snapshot.snapshot_id}.json"
        if not snap_file.is_file():
            return False, f"snapshot metadata missing: {snap_file}"

        return True, "valid"

    def _create_safety_backup(self, root: str, snapshot: Snapshot, settings) -> Optional[Path]:
        from tools.git import _git
        if settings and hasattr(settings, "backups_dir"):
            backup_dir = Path(settings.backups_dir)
        else:
            backup_dir = Path(snapshot.snapshot_dir).parent / "pre_restore_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)

        # Capture full state: tracked changes (staged + unstaged), untracked files
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_data = {}

        # Full diff including staged changes
        code, out, err = _git(root, ["diff", "--no-color", "--binary", "HEAD"])
        if code == 0 and out.strip():
            backup_data["diff_HEAD"] = out

        # Staged changes
        code, out, err = _git(root, ["diff", "--no-color", "--binary", "--staged"])
        if code == 0 and out.strip():
            backup_data["diff_staged"] = out

        # Untracked files
        code, out, err = _git(root, ["ls-files", "--others", "--exclude-standard"])
        if code == 0 and out.strip():
            backup_data["untracked"] = out.strip().splitlines()

        # Deleted files (in index but not in working tree)
        code, out, err = _git(root, ["ls-files", "--deleted"])
        if code == 0 and out.strip():
            backup_data["deleted"] = out.strip().splitlines()

        if not backup_data:
            return None

        dest = backup_dir / f"pre-restore-{stamp}.json"
        try:
            dest.write_text(json.dumps(backup_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return dest
        except OSError:
            return None

    def restore_snapshot(self, task_id: str, snapshot_id: str, settings=None, confirmer=None) -> tuple[bool, str]:
        snapshot = self.get_snapshot(task_id, snapshot_id)
        if snapshot is None:
            return False, f"snapshot not found: {snapshot_id} (task={task_id})"

        valid, reason = self.validate_snapshot(snapshot)
        if not valid:
            return False, f"invalid snapshot: {reason}"

        if snapshot.restored:
            return False, f"snapshot already restored at {snapshot.restored_at}"

        restore_policy = settings.git_restore_policy if settings else "blocked"
        if restore_policy == "blocked":
            return False, "snapshot restore is disabled by policy (git_restore_policy=blocked)"
        if restore_policy == "confirm":
            if confirmer is None:
                return False, "snapshot restore requires confirmation but no confirmer provided"
            choice = confirmer(f"Restore snapshot {snapshot_id}?")
            if not choice:
                return False, "snapshot restore denied by confirmer"

        project_root = snapshot.project_root
        root_path = Path(project_root)

        current_branch = self._get_git_branch(project_root)
        branch_mismatch = current_branch != snapshot.git_branch and snapshot.git_branch != "(detached)"

        backup = self._create_safety_backup(project_root, snapshot, settings)
        if backup:
            log.info("safety backup created: %s", backup)

        switched = False
        if branch_mismatch:
            log.warning("restoring snapshot from different branch: snapshot=%s, current=%s", snapshot.git_branch, current_branch)
            switch_result = _git_branch_switch(project_root, snapshot.git_branch)
            if not switch_result.success:
                return False, f"branch switch failed: {switch_result.error}"
            switched = True

        commit_result = git_reset(project_root, mode="hard", target=snapshot.git_commit, settings=settings)
        if not commit_result.success:
            if switched:
                _git_branch_switch(project_root, current_branch)
            return False, f"git reset failed: {commit_result.error or commit_result.reason}"

        snapshot.restored = True
        snapshot.restored_at = time.strftime("%Y%m%d-%H%M%S")

        snapshot_dir = Path(snapshot.snapshot_dir)
        snapshot_path = snapshot_dir / f"{snapshot.snapshot_id}.json"
        snapshot_path.write_text(snapshot.to_json(), encoding="utf-8")

        verify_commit = self._get_git_commit(project_root)
        verify_branch = self._get_git_branch(project_root)
        
        if verify_commit != snapshot.git_commit:
            log.warning("restored commit mismatch: expected=%s, actual=%s", snapshot.git_commit, verify_commit)
        
        if snapshot.git_branch != "(detached)" and verify_branch != snapshot.git_branch:
            log.warning("restored branch mismatch: expected=%s, actual=%s", snapshot.git_branch, verify_branch)

        log.info("snapshot restored: %s (task=%s, commit=%s)", snapshot_id, task_id, snapshot.git_commit[:8])

        return True, f"restored snapshot {snapshot_id} (commit {snapshot.git_commit[:8]}, branch {verify_branch})"

    def get_latest_snapshot(self, task_id: str) -> Optional[Snapshot]:
        snapshots = self.list_snapshots(task_id)
        return snapshots[-1] if snapshots else None


def _backup_diff(root: str, backup_dir: Path) -> Optional[Path]:
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        from tools.git import _git
        code, out, err = _git(root, ["diff", "--no-color", "--binary", "HEAD"])
        if code != 0 or not out.strip():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"pre-restore-{stamp}.patch"
        dest.write_text(out + "\n", encoding="utf-8", errors="replace")
        return dest
    except OSError:
        return None


def git_reset(root: str, mode: str = "mixed", target: str = "HEAD", settings=None):
    from tools.git import git_reset as _git_reset
    return _git_reset(root, mode, target, settings)


def git_branch_switch(root: str, name: str, settings=None):
    from tools.git import git_branch_switch as _git_branch_switch
    return _git_branch_switch(root, name, settings)


def git_status(root: str):
    from tools.git import git_status as _git_status
    return _git_status(root)


def git_diff(root: str, include_staged: bool = False, stat: bool = False):
    from tools.git import git_diff as _git_diff
    return _git_diff(root, include_staged, stat)


def git_log(root: str, max_entries: int = 20):
    from tools.git import git_log as _git_log
    return _git_log(root, max_entries)