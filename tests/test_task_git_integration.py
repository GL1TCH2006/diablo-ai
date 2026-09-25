import subprocess
from pathlib import Path
from types import SimpleNamespace

from core.task_store import TaskStore, TaskRecord, CommitInfo
from coding.change_tracker import ChangeTracker
from core.agent import Agent
from tools.git import _git


def _git_init_repo(path: Path) -> Path:
    """Initialize a git repo with an initial commit."""
    repo = path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (["init", "-q"], ["config", "user.email", "a@b.c"], ["config", "user.name", "T"]):
        subprocess.run(["git", *cmd], cwd=str(repo), capture_output=True, check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo), capture_output=True, check=True)
    return repo


def test_task_start_creates_persistent_record(tmp_settings):
    """Task start creates a persistent TaskRecord in TaskStore."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", "/some/project")

    store = TaskStore(tmp_settings.sessions_dir)
    # Manually create record like agent would
    record = TaskRecord(
        task_id=session.id,
        project_name="test-project",
        project_root="/some/project",
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    got = store.get(session.id)
    assert got is not None
    assert got.task_id == session.id


def test_starting_git_commit_captured(tmp_settings):
    """Starting Git commit is captured in TaskRecord."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    # Simulate agent capturing start
    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Now capture git start like agent does
    from tools.git import _git
    code, out, err = _git(str(repo), ["rev-parse", "HEAD"])
    assert code == 0
    starting_commit = out.strip()

    store.update(session.id, git={"starting_commit": starting_commit})

    got = store.get(session.id)
    assert got.git.starting_commit == starting_commit
    assert len(got.git.starting_commit) == 40  # SHA-1


def test_starting_git_branch_captured(tmp_settings):
    """Starting Git branch is captured in TaskRecord."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    from tools.git import _git
    code, out, err = _git(str(repo), ["branch", "--show-current"])
    assert code == 0
    starting_branch = out.strip() or "(detached)"

    store.update(session.id, git={"starting_branch": starting_branch})

    got = store.get(session.id)
    assert got.git.starting_branch == starting_branch
    assert got.git.starting_branch != ""


def test_task_finish_captures_ending_commit(tmp_settings):
    """Task finish captures ending Git commit."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Make a change and commit
    (repo / "b.txt").write_text("new file\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add b.txt"], cwd=str(repo), capture_output=True, check=True)

    from tools.git import _git
    code, out, err = _git(str(repo), ["rev-parse", "HEAD"])
    assert code == 0
    ending_commit = out.strip()

    store.update(session.id, git={"ending_commit": ending_commit})
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert got.git.ending_commit == ending_commit
    assert got.finished_at is not None
    assert got.status == "completed"


def test_task_finish_captures_ending_branch(tmp_settings):
    """Task finish captures ending Git branch."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    from tools.git import _git
    code, out, err = _git(str(repo), ["branch", "--show-current"])
    assert code == 0
    ending_branch = out.strip() or "(detached)"

    store.update(session.id, git={"ending_branch": ending_branch})
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert got.git.ending_branch == ending_branch


def test_final_git_status_captured(tmp_settings):
    """Final Git status is captured in TaskRecord."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Modify a tracked file
    (repo / "a.txt").write_text("modified\n", encoding="utf-8")

    from tools.git import git_status
    result = git_status(str(repo))
    assert result.success

    store.update(session.id, git={"final_status": result.output})
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert "a.txt" in got.git.final_status


def test_net_diff_stat_captured(tmp_settings):
    """Net diff statistics are captured."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Modify a file
    (repo / "a.txt").write_text("modified content\n", encoding="utf-8")

    from tools.git import git_diff
    result = git_diff(str(repo), stat=True)
    assert result.success

    store.update(session.id, git={"net_diff_stat": result.output})
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert "a.txt" in got.git.net_diff_stat


def test_changed_files_captured(tmp_settings):
    """Changed files list is captured from porcelain status."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Modify a.txt and create untracked file
    (repo / "a.txt").write_text("modified\n", encoding="utf-8")
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")

    from tools.git import git_status
    result = git_status(str(repo))
    assert result.success

    # Parse files from status
    files_changed = []
    for line in result.output.splitlines():
        line = line.strip()
        if line and not line.startswith("##"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                files_changed.append(parts[1])

    store.update(session.id, git={"files_changed": files_changed, "final_status": result.output})
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert "a.txt" in got.git.files_changed
    assert "new.txt" in got.git.files_changed


def test_non_git_project_does_not_fail(tmp_settings):
    """Task works when project has no Git repository."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    project_dir = tmp_settings.scratch_dir / "no-git-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "file.txt").write_text("content\n", encoding="utf-8")

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(project_dir))

    record = TaskRecord(
        task_id=session.id,
        project_name="no-git",
        project_root=str(project_dir),
        title="Non-Git Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Should not fail even without .git
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert got.status == "completed"
    assert got.git.starting_commit == ""


def test_git_inspection_failure_does_not_crash(tmp_settings):
    """Git inspection failure doesn't crash task completion."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    # Simulate git inspection failure by setting project_root to non-existent path
    store.update(session.id, project_root="/nonexistent/path")

    # Finish should not raise
    store.finish(session.id, "Done", "completed")

    got = store.get(session.id)
    assert got.status == "completed"


def test_task_record_id_equals_change_tracker_session_id(tmp_settings):
    """TaskRecord.task_id equals ChangeTracker session ID."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", "/some/project")

    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id=session.id,
        project_name="test",
        project_root="/some/project",
        title="Test",
        goal="Test goal",
        created_at=session.started_at,
    )
    store.create(record)

    got = store.get(session.id)
    assert got is not None
    assert got.task_id == session.id


def test_m1_snapshot_behavior_intact(tmp_settings):
    """Existing M1 snapshot/checkpoint behavior remains intact."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    from coding.snapshot_manager import SnapshotManager
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    snap_mgr = SnapshotManager(tmp_settings.sessions_dir)

    # Create snapshot like agent does
    snap = snap_mgr.create_snapshot(
        task_id=session.id,
        label="pre-test",
        project_root=str(repo),
        creation_reason="auto-checkpoint-before-test",
    )

    assert snap.task_id == session.id
    assert snap.git_commit != ""
    assert snap.git_branch != ""

    # Restore should still work (with policy allow)
    tmp_settings.git_restore_policy = "allow"
    # Add and commit a file after snapshot (like test_restore_clean_workspace)
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "add", "new.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add new.txt"], cwd=str(repo), capture_output=True, check=True)
    ok, msg = snap_mgr.restore_snapshot(session.id, snap.snapshot_id, settings=tmp_settings)
    assert ok
    assert not (repo / "new.txt").exists()


def test_failed_task_gets_failed_status(tmp_settings):
    """Failed task gets status='failed'."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    store.finish(session.id, "Something went wrong", "failed")

    got = store.get(session.id)
    assert got.status == "failed"
    assert got.outcome == "Something went wrong"


def test_completed_task_gets_completed_status(tmp_settings):
    """Completed task gets status='completed'."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    store.finish(session.id, "All done", "completed")

    got = store.get(session.id)
    assert got.status == "completed"
    assert got.outcome == "All done"


def test_cancelled_task_gets_cancelled_status(tmp_settings):
    """Cancelled task gets status='cancelled'."""
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)

    store = TaskStore(tmp_settings.sessions_dir)
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", str(repo))

    record = TaskRecord(
        task_id=session.id,
        project_name="test-repo",
        project_root=str(repo),
        title="Test Task",
        goal="Test goal",
        created_at=session.started_at,
        started_at=session.started_at,
        status="active",
    )
    store.create(record)

    store.finish(session.id, "User cancelled", "cancelled")

    got = store.get(session.id)
    assert got.status == "cancelled"
    assert got.outcome == "User cancelled"


def _commit_agent(sessions_dir: Path, project_root: str = "") -> Agent:
    agent = Agent.__new__(Agent)
    agent._task_store = TaskStore(sessions_dir)
    agent._change_tracker = ChangeTracker(sessions_dir)
    agent._project = SimpleNamespace(path=project_root, name=Path(project_root).name) if project_root else None
    agent._pending_goal = ""
    agent._tasks = SimpleNamespace(task_title="")
    return agent


def _commit(repo: Path, filename: str, message: str) -> str:
    (repo / filename).write_text(f"{message}\n", encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(repo), capture_output=True, check=True)
    code, out, _ = _git(str(repo), ["rev-parse", "HEAD"])
    assert code == 0
    return out.strip()


def _track_commit(agent: Agent, repo: Path, filename: str, message: str) -> str:
    outcome = SimpleNamespace(allowed=True, executed=True, success=True, reason="")
    if agent._change_tracker.active is None:
        agent._note_step("terminal.execute", {"root": str(repo), "command": "git status"}, outcome)
    commit_hash = _commit(repo, filename, message)
    agent._note_step("git.commit", {"root": str(repo), "message": message}, outcome)
    return commit_hash


def test_task_with_zero_commits(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))
    outcome = SimpleNamespace(allowed=True, executed=True, success=True, reason="")

    agent._note_step("terminal.execute", {"root": str(repo), "command": "git status"}, outcome)
    task_id = agent._change_tracker.active.id
    agent._capture_task_git_end(task_id, "Done", "completed")

    record = agent._task_store.get(task_id)
    assert record.status == "completed"
    assert record.git.commits_made == []


def test_task_with_one_commit(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))

    commit_hash = _track_commit(agent, repo, "b.txt", "add b.txt")

    record = agent._task_store.get(agent._change_tracker.active.id)
    assert len(record.git.commits_made) == 1
    assert record.git.commits_made[0].hash == commit_hash
    assert record.git.commits_made[0].message == "add b.txt"
    assert record.git.commits_made[0].timestamp
    assert record.git.ending_commit == commit_hash


def test_task_with_multiple_commits_preserves_chronological_order(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))

    hashes = [
        _track_commit(agent, repo, f"{index}.txt", message)
        for index, message in enumerate(("commit one", "commit two", "commit three"), start=1)
    ]

    record = agent._task_store.get(agent._change_tracker.active.id)
    assert [commit.hash for commit in record.git.commits_made] == hashes
    assert [commit.message for commit in record.git.commits_made] == [
        "commit one",
        "commit two",
        "commit three",
    ]
    assert [commit.timestamp for commit in record.git.commits_made] == sorted(
        commit.timestamp for commit in record.git.commits_made
    )


def test_commit_persistence_after_reload(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))
    commit_hash = _track_commit(agent, repo, "persisted.txt", "persist commit")

    record = TaskStore(tmp_settings.sessions_dir).get(agent._change_tracker.active.id)

    assert isinstance(record.git.commits_made[0], CommitInfo)
    assert record.git.commits_made[0].hash == commit_hash
    assert record.git.commits_made[0].message == "persist commit"
    assert record.git.commits_made[0].timestamp
    assert record.git.ending_commit == commit_hash


def test_non_git_task_completes_without_commits(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    project_dir = tmp_settings.scratch_dir / "no-git-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    agent = _commit_agent(tmp_settings.sessions_dir)
    outcome = SimpleNamespace(allowed=True, executed=True, success=True, reason="")

    agent._note_step("terminal.execute", {"root": str(project_dir), "command": "dir"}, outcome)
    task_id = agent._change_tracker.active.id
    agent._capture_task_git_end(task_id, "Done", "completed")

    record = agent._task_store.get(task_id)
    assert record.status == "completed"
    assert record.git.commits_made == []


def test_commit_inspection_failure_does_not_interrupt_lifecycle(tmp_settings, monkeypatch):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))
    first_hash = _track_commit(agent, repo, "first.txt", "first commit")
    task_id = agent._change_tracker.active.id
    monkeypatch.setattr("tools.git._git", lambda *_args, **_kwargs: (1, "", "inspection failed"))
    _commit(repo, "second.txt", "second commit")
    outcome = SimpleNamespace(allowed=True, executed=True, success=True, reason="")

    agent._note_step("git.commit", {"root": str(repo), "message": "second commit"}, outcome)
    agent._capture_task_git_end(task_id, "Done", "completed")

    record = agent._task_store.get(task_id)
    assert record.status == "completed"
    assert [commit.hash for commit in record.git.commits_made] == [first_hash]


def test_commit_uses_change_tracker_active_task_id(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))
    session = agent._change_tracker.start("Track task", str(repo))
    other_id = "unrelated-task"
    for task_id in (session.id, other_id):
        agent._task_store.create(TaskRecord(
            task_id=task_id,
            project_name="repo",
            project_root=str(repo),
            title="Task",
            goal="Track task",
            created_at=session.started_at,
            status="active",
        ))

    commit_hash = _commit(repo, "associated.txt", "associated commit")
    outcome = SimpleNamespace(allowed=True, executed=True, success=True, reason="")
    agent._note_step("git.commit", {"root": str(repo), "message": "associated commit"}, outcome)

    tracked = agent._task_store.get(agent._change_tracker.active.id)
    unrelated = agent._task_store.get(other_id)
    assert tracked.task_id == session.id
    assert [commit.hash for commit in tracked.git.commits_made] == [commit_hash]
    assert unrelated.git.commits_made == []


def test_m22_git_lifecycle_compatibility(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    repo = _git_init_repo(tmp_settings.scratch_dir)
    initial_hash = _git(str(repo), ["rev-parse", "HEAD"])[1].strip()
    agent = _commit_agent(tmp_settings.sessions_dir, str(repo))
    final_hash = _track_commit(agent, repo, "m22.txt", "M2.3 commit")
    task_id = agent._change_tracker.active.id

    agent._capture_task_git_end(task_id, "Done", "completed")

    record = TaskStore(tmp_settings.sessions_dir).get(task_id)
    assert record.status == "completed"
    assert record.git.starting_commit == initial_hash
    assert record.git.starting_branch
    assert record.git.ending_commit == final_hash
    assert record.git.ending_branch
    assert len(record.git.commits_made) == 1


def test_legacy_commit_hash_entries_load_compatibly():
    record = TaskRecord.from_dict({
        "task_id": "legacy-task",
        "project_name": "repo",
        "project_root": "/repo",
        "title": "Legacy",
        "goal": "Load old task",
        "created_at": "20240101-120000",
        "git": {"commits_made": ["abc123", "def456"]},
    })

    assert [commit.hash for commit in record.git.commits_made] == ["abc123", "def456"]
    assert [commit.message for commit in record.git.commits_made] == ["", ""]
    assert [commit.timestamp for commit in record.git.commits_made] == ["", ""]


def _clean_tasks_dir(sessions_dir: Path):
    tasks_dir = sessions_dir / "tasks"
    if tasks_dir.exists():
        for f in tasks_dir.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass