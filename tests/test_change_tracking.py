import json

import pytest

from coding.change_tracker import ChangeTracker, CodingSession, _scrub_args
from tools.git import git_diff, git_log, git_status


def test_change_tracker_does_not_start_without_steps(tmp_settings):
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    assert tracker.active is None
    assert tracker.summary() == "No active coding session."


def test_change_tracker_start_step_finish(tmp_settings):
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Fix the failing parser", "C:\\proj\\mypakage")
    assert session.id.startswith("20")
    assert session.status == "active"

    tracker.step("git.diff", {"root": "C:\\proj\\mypakage"}, True, True, True, "")
    tracker.step("terminal.execute", {"command": "pytest -q"}, True, True, True, "")
    tracker.step("terminal.execute", {"command": "git status"}, True, True, False, "denied")
    assert len(tracker.active.steps) == 3

    tracker.finish("Fixed.")
    assert tracker.active.status == "completed"
    assert "Fixed." in tracker.active.mission_block()


def test_change_tracker_mission_block_shape(tmp_settings):
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    tracker.start("Add endpoint", "C:\\proj")
    tracker.step("project.info", {"path": "C:\\proj"}, True, True, True)
    block = tracker.active.mission_block()
    assert block.startswith("Mission: Add endpoint")
    assert "Status: active" in block


def test_change_tracker_persists_session_file(tmp_settings):
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    tracker.start("Persist me", "C:\\proj\\x")
    tracker.step("git.diff", {"root": "C:\\proj\\x"}, True, True, True)
    tracker.finish("done")
    files = list(tmp_settings.sessions_dir.glob("*.json"))
    assert files
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    assert data["goal"] == "Persist me"
    assert data["steps"][0]["tool"] == "git.diff"


def test_change_tracker_load_latest(tmp_settings):
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    tracker.start("Loaded?", "C:\\proj\\y")
    tracker.step("terminal.execute", {"command": "pytest"}, True, True, True)
    tracker.finish("ok")
    loaded = ChangeTracker.load_latest(tmp_settings.sessions_dir)
    assert loaded is not None
    assert loaded.goal == "Loaded?"
    assert loaded.steps[0].tool == "terminal.execute"


def test_scrub_args_shortens():
    assert len(_scrub_args({"command": "x" * 500})) <= 170


def test_git_tools_read_without_repo(tmp_path):
    result = git_status(str(tmp_path))
    assert not result.success or result.output
    assert git_log(str(tmp_path)) is not None
    assert git_log(str(tmp_path)).success is False


def _git_repo(tmp_path, tracked: str | None = "a.txt"):
    import subprocess
    if tracked:
        (tmp_path / tracked).write_text("hello\n", encoding="utf-8")
    for cmd in ("init -q", "config user.email a@b.c", "config user.name T", "add ."):
        subprocess.run(["git"] + cmd.split(), cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    return tmp_path


def test_git_diff_readonly(tmp_path):
    repo = _git_repo(tmp_path, tracked="a.txt")
    (repo / "a.txt").write_text("hello\nchanged\n", encoding="utf-8")
    result = git_diff(str(repo))
    assert result.success
    assert "a.txt" in result.output


def test_git_status_branch_line(tmp_path):
    repo = _git_repo(tmp_path)
    result = git_status(str(repo))
    assert result.success
    assert "master" in result.output or "main" in result.output or "HEAD" in result.output