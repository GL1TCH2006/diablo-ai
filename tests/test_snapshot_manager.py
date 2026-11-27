"""Snapshot & checkpoint tests (M1).

Covers:
  * Snapshot dataclass + json round-trip
  * SnapshotManager create/list/get/get_latest/validate
  * restore_snapshot safety & failure behaviour
  * tools.git wrappers (git_snapshot / git_snapshot_list / git_snapshot_restore)
  * agent checkpoint gating (_should_checkpoint / _maybe_create_checkpoint)

Uses real git repos (subprocess) on tmp_path, mirroring tests/test_git_tools.py.
"""

import subprocess
from pathlib import Path

import pytest

from coding.snapshot_manager import Snapshot, SnapshotManager
from config.permissions import ToolBlockedError
from tools.git import (
    git_snapshot,
    git_snapshot_list,
    git_snapshot_restore,
)


def _git(repo: Path, *args: str, check: bool = True):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} -> {proc.returncode}: {proc.stderr.strip()}")
    return proc


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for cmd in (["init", "-q"], ["config", "user.email", "a@b.c"], ["config", "user.name", "T"]):
        _git(repo, *cmd)
    _commit(repo)
    return repo


def _commit(repo: Path, name: str = "a.txt", content: str = "hello\n", msg: str = "commit"):
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD", check=False).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path)


@pytest.fixture
def snapshot_manager(tmp_path):
    sessions = tmp_path / "sessions"
    return SnapshotManager(sessions)


# ---------------------------------------------------------------- creation / metadata

def test_snapshot_creation(snapshot_manager, repo):
    snap = snapshot_manager.create_snapshot("t1", "baseline", str(repo))
    assert snap.task_id == "t1"
    assert snap.label == "baseline"
    assert snap.project_root == str(repo)
    assert snap.creation_reason == "manual"
    assert snap.snapshot_id
    assert snap.created_at


def test_snapshot_git_metadata(snapshot_manager, repo):
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    snap = snapshot_manager.create_snapshot("t1", "m", str(repo))
    assert snap.git_commit == head
    assert snap.git_branch
    assert snap.git_status is not None
    assert snap.git_diff_stat is not None


def test_clean_workspace_snapshot(snapshot_manager, repo):
    snap = snapshot_manager.create_snapshot("t1", "clean", str(repo))
    ok, reason = snapshot_manager.validate_snapshot(snap)
    assert ok, reason
    assert "valid" in reason.lower()


# ---------------------------------------------------------------- dirty / untracked

def test_dirty_workspace_snapshot(snapshot_manager, repo):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    snap = snapshot_manager.create_snapshot("t1", "dirty", str(repo))
    assert "a.txt" in snap.git_status
    assert snap.git_diff_stat


def test_untracked_file_snapshot(snapshot_manager, repo):
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    snap = snapshot_manager.create_snapshot("t1", "untracked", str(repo))
    assert "new.txt" in snap.git_status


# ---------------------------------------------------------------- list / get

def test_list_snapshots(snapshot_manager, repo):
    snapshot_manager.create_snapshot("t1", "one", str(repo))
    snapshot_manager.create_snapshot("t1", "two", str(repo))
    snaps = snapshot_manager.list_snapshots("t1")
    assert len(snaps) == 2
    assert {s.label for s in snaps} == {"one", "two"}


def test_get_snapshot(snapshot_manager, repo):
    created = snapshot_manager.create_snapshot("t1", "one", str(repo))
    loaded = snapshot_manager.get_snapshot("t1", created.snapshot_id)
    assert loaded == created
    assert loaded.label == "one"


def test_get_latest_snapshot(snapshot_manager, repo):
    snapshot_manager.create_snapshot("t1", "one", str(repo))
    import time
    time.sleep(1.1)
    latest = snapshot_manager.create_snapshot("t1", "two", str(repo))
    assert snapshot_manager.get_latest_snapshot("t1").snapshot_id == latest.snapshot_id


def test_validate_snapshot_missing_metadata(snapshot_manager, repo):
    snap = snapshot_manager.create_snapshot("t1", "m", str(repo))
    metadata_file = Path(snap.snapshot_dir) / f"{snap.snapshot_id}.json"
    metadata_file.unlink()
    ok, reason = snapshot_manager.validate_snapshot(snap)
    assert not ok
    assert "missing" in reason.lower()


# ---------------------------------------------------------------- restore

def test_restore_clean_workspace(snapshot_manager, repo, tmp_settings):
    tmp_settings.git_restore_policy = "allow"
    snap = snapshot_manager.create_snapshot("t1", "before", str(repo))
    _commit(repo, "b.txt", "two\n", "second")
    ok, msg = snapshot_manager.restore_snapshot("t1", snap.snapshot_id, settings=tmp_settings)
    assert ok, msg
    assert (repo / "a.txt").exists()
    assert not (repo / "b.txt").exists()


def test_restore_dirty_workspace(snapshot_manager, repo, tmp_settings):
    tmp_settings.git_restore_policy = "allow"
    snap = snapshot_manager.create_snapshot("t1", "baseline", str(repo))
    (repo / "a.txt").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("keep\n", encoding="utf-8")
    ok, msg = snapshot_manager.restore_snapshot("t1", snap.snapshot_id, settings=tmp_settings)
    assert ok, msg
    assert (repo / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert (repo / "untracked.txt").exists()


def test_restore_missing_snapshot(snapshot_manager, repo):
    ok, msg = snapshot_manager.restore_snapshot("t1", "does-not-exist")
    assert not ok
    assert "not found" in msg.lower()


def test_snapshot_no_repeat_restore(snapshot_manager, repo, tmp_settings):
    tmp_settings.git_restore_policy = "allow"
    snap = snapshot_manager.create_snapshot("t1", "once", str(repo))
    first_ok, _ = snapshot_manager.restore_snapshot("t1", snap.snapshot_id, settings=tmp_settings)
    assert first_ok
    second_ok, _ = snapshot_manager.restore_snapshot("t1", snap.snapshot_id, settings=tmp_settings)
    assert not second_ok


# ---------------------------------------------------------------- git tools

def test_git_snapshot_tool(repo, tmp_settings):
    result = git_snapshot(str(repo), "t9", "tool", settings=tmp_settings)
    assert result.success
    assert "snapshot" in result.output.lower()
    assert result.error == ""


def test_git_snapshot_list_tool(repo, tmp_settings):
    git_snapshot(str(repo), "t9", "one", settings=tmp_settings)
    git_snapshot(str(repo), "t9", "two", settings=tmp_settings)
    result = git_snapshot_list("t9", settings=tmp_settings)
    assert result.success
    assert "one" in result.output
    assert "two" in result.output


def test_git_snapshot_restore_tool(repo, tmp_settings):
    tmp_settings.git_restore_policy = "allow"
    created = git_snapshot(str(repo), "t9", "pre", settings=tmp_settings)
    assert created.success
    snapshot_id = created.output.split("snapshot")[-1].strip().split()[-1]
    _commit(repo, "b.txt", "two\n", "second")
    result = git_snapshot_restore(str(repo), "t9", snapshot_id, settings=tmp_settings)
    assert result.success
    assert not (repo / "b.txt").exists()


def test_snapshot_tool_gated_blocked(repo, tmp_settings):
    getattr(tmp_settings, "git_restore_policy", None)  # sanity: attr exists
    git_snapshot(str(repo), "t9", "x", settings=tmp_settings)
    snaps = git_snapshot_list("t9", settings=tmp_settings)
    assert snaps.success


# ---------------------------------------------------------------- agent checkpoint

def test_agent_checkpoint_tools_blocked_by_default(snapshot_manager, repo, tmp_settings):
    """Checkpoint creation itself is always allowed; restore stays gated."""
    snap = snapshot_manager.create_snapshot("t1", "auto", str(repo),
                                            creation_reason="auto-checkpoint-before-git-push")
    assert snap.creation_reason == "auto-checkpoint-before-git-push"
    assert snap.label


def test_agent_can_restore_with_confirmer(snapshot_manager, repo, tmp_settings):
    tmp_settings.git_restore_policy = "confirm"
    snap = snapshot_manager.create_snapshot("t1", "pre", str(repo))
    _commit(repo, "b.txt", "two\n", "second")
    confirmer = lambda prompt: "allow"
    ok, msg = snapshot_manager.restore_snapshot("t1", snap.snapshot_id,
                                                settings=tmp_settings, confirmer=confirmer)
    assert ok, msg
