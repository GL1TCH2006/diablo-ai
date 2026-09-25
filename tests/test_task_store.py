import json
import pytest
from pathlib import Path

from core.task_store import TaskRecord, TaskStore, GitTaskState, TaskChangeStep, CommitInfo


def _clean_tasks_dir(sessions_dir: Path):
    tasks_dir = sessions_dir / "tasks"
    if tasks_dir.exists():
        for f in tasks_dir.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass


def test_task_record_serialization():
    record = TaskRecord(
        task_id="test-123",
        project_name="my-project",
        project_root="/home/user/my-project",
        title="Test Task",
        goal="Do something useful",
        created_at="20240101-120000",
    )
    data = record.to_dict()
    assert data["task_id"] == "test-123"
    assert data["status"] == "pending"
    restored = TaskRecord.from_dict(data)
    assert restored.task_id == "test-123"
    assert restored.project_name == "my-project"
    assert restored.status == "pending"


def test_git_task_state_serializes():
    git = GitTaskState(
        project_root="/repo",
        starting_commit="abc123",
        starting_branch="main",
        ending_commit="def456",
        ending_branch="feature",
        commits_made=[
            CommitInfo(hash="abc123", message="First commit", timestamp="20240101-120000"),
            CommitInfo(hash="def456", message="Second commit", timestamp="20240101-120001"),
        ],
        branches_created=["feature"],
        branches_switched=["main", "feature"],
        files_changed=["file1.py", "file2.py"],
        net_diff_stat="2 files changed, 10 insertions(+), 5 deletions(-)",
        final_status=" M file1.py\n?? file2.py",
    )
    record = TaskRecord(
        task_id="git-test",
        project_name="git-project",
        project_root="/repo",
        title="Git Task",
        goal="Test git state",
        created_at="20240101-120000",
        git=git,
    )
    data = record.to_dict()
    restored = TaskRecord.from_dict(data)
    assert restored.git.starting_commit == "abc123"
    assert len(restored.git.commits_made) == 2
    assert restored.git.commits_made[0].hash == "abc123"
    assert restored.git.commits_made[0].message == "First commit"
    assert restored.git.commits_made[1].hash == "def456"
    assert restored.git.files_changed == ["file1.py", "file2.py"]


def test_task_with_empty_git_state_serializes():
    record = TaskRecord(
        task_id="no-git",
        project_name="no-git-project",
        project_root="/some/path",
        title="No Git Task",
        goal="Test without git",
        created_at="20240101-120000",
    )
    data = record.to_dict()
    restored = TaskRecord.from_dict(data)
    assert restored.git.project_root == ""
    assert restored.git.starting_commit == ""
    assert restored.git.commits_made == []


def test_task_change_steps_round_trip():
    step = TaskChangeStep(
        tool="filesystem.write_file",
        args={"path": "/tmp/test.py", "content": "print('hi')"},
        allowed=True,
        executed=True,
        success=True,
        reason="ok",
        ts="12:00:00",
    )
    record = TaskRecord(
        task_id="steps-test",
        project_name="p",
        project_root="/p",
        title="Steps",
        goal="Test steps",
        created_at="20240101-120000",
        change_steps=[step],
    )
    data = record.to_dict()
    restored = TaskRecord.from_dict(data)
    assert len(restored.change_steps) == 1
    s = restored.change_steps[0]
    assert s.tool == "filesystem.write_file"
    assert s.success is True
    assert s.reason == "ok"


def test_task_store_create_and_get(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id="tst-create-001",
        project_name="proj",
        project_root="/proj",
        title="First Task",
        goal="Create something",
        created_at="20240101-120000",
    )
    created = store.create(record)
    assert created.task_id == "tst-create-001"
    got = store.get("tst-create-001")
    assert got is not None
    assert got.task_id == "tst-create-001"
    assert got.title == "First Task"


def test_duplicate_task_rejected(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id="tst-dup-001",
        project_name="p",
        project_root="/p",
        title="Task",
        goal="Test",
        created_at="20240101-120000",
    )
    store.create(record)
    with pytest.raises(ValueError, match="already exists"):
        store.create(record)


def test_task_store_update(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id="tst-upd-001",
        project_name="p",
        project_root="/p",
        title="Original",
        goal="Test",
        created_at="20240101-120000",
        status="pending",
    )
    store.create(record)
    updated = store.update("tst-upd-001", title="Updated", status="active")
    assert updated is not None
    assert updated.title == "Updated"
    assert updated.status == "active"
    got = store.get("tst-upd-001")
    assert got.title == "Updated"
    assert got.status == "active"


def test_task_store_delete(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id="tst-del-001",
        project_name="p",
        project_root="/p",
        title="To Delete",
        goal="Test",
        created_at="20240101-120000",
    )
    store.create(record)
    assert store.get("tst-del-001") is not None
    deleted = store.delete("tst-del-001")
    assert deleted is True
    assert store.get("tst-del-001") is None
    assert store.delete("tst-del-001") is False


def test_list_all(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    for i in range(3):
        store.create(TaskRecord(
            task_id=f"tst-list-{i:03d}",
            project_name="proj" if i < 2 else "other",
            project_root=f"/proj{i}",
            title=f"Task {i}",
            goal=f"Goal {i}",
            created_at=f"20240101-12000{i}",
        ))
    all_records = store.list_all()
    assert len(all_records) == 3
    ids = sorted([r.task_id for r in all_records])
    assert ids == ["tst-list-000", "tst-list-001", "tst-list-002"]


def test_list_by_project(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(TaskRecord(
        task_id="tst-proj-a",
        project_name="alpha",
        project_root="/alpha",
        title="A",
        goal="A",
        created_at="20240101-120000",
    ))
    store.create(TaskRecord(
        task_id="tst-proj-b",
        project_name="beta",
        project_root="/beta",
        title="B",
        goal="B",
        created_at="20240101-120001",
    ))
    store.create(TaskRecord(
        task_id="tst-proj-c",
        project_name="alpha",
        project_root="/alpha2",
        title="C",
        goal="C",
        created_at="20240101-120002",
    ))
    alpha = store.list_by_project("alpha")
    assert len(alpha) == 2
    assert {r.task_id for r in alpha} == {"tst-proj-a", "tst-proj-c"}
    beta = store.list_by_project("beta")
    assert len(beta) == 1
    assert beta[0].task_id == "tst-proj-b"
    empty = store.list_by_project("gamma")
    assert empty == []


def test_finish_task(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id="tst-fin-001",
        project_name="p",
        project_root="/p",
        title="Finish Me",
        goal="Test",
        created_at="20240101-120000",
        status="active",
    )
    store.create(record)
    finished = store.finish("tst-fin-001", outcome="All done!", status="completed")
    assert finished is not None
    assert finished.finished_at is not None
    assert finished.outcome == "All done!"
    assert finished.status == "completed"
    got = store.get("tst-fin-001")
    assert got.finished_at is not None
    assert got.status == "completed"


def test_persistence_across_store_instances(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store1 = TaskStore(tmp_settings.sessions_dir)
    store1.create(TaskRecord(
        task_id="tst-persist-001",
        project_name="p",
        project_root="/p",
        title="Persist",
        goal="Test",
        created_at="20240101-120000",
    ))
    store2 = TaskStore(tmp_settings.sessions_dir)
    got = store2.get("tst-persist-001")
    assert got is not None
    assert got.title == "Persist"


def test_missing_task(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    assert store.get("nonexistent") is None
    assert store.update("nonexistent", title="X") is None
    assert store.delete("nonexistent") is False
    assert store.finish("nonexistent", "oops") is None


def test_invalid_status_rejected(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    with pytest.raises(ValueError, match="invalid status"):
        TaskRecord(
            task_id="tst-bad",
            project_name="p",
            project_root="/p",
            title="Bad",
            goal="Test",
            created_at="20240101-120000",
            status="invalid_status",
        )
    record = TaskRecord(
        task_id="tst-ok-001",
        project_name="p",
        project_root="/p",
        title="OK",
        goal="Test",
        created_at="20240101-120000",
    )
    store.create(record)
    with pytest.raises(ValueError, match="invalid status"):
        store.finish("tst-ok-001", "done", status="bad_status")


def test_identity_compatibility_with_change_tracker(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    from coding.change_tracker import ChangeTracker
    tracker = ChangeTracker(tmp_settings.sessions_dir)
    session = tracker.start("Test goal", "/some/project")
    assert session.id is not None
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id=session.id,
        project_name="test-project",
        project_root="/some/project",
        title="From ChangeTracker",
        goal="Test goal",
        created_at=session.started_at,
    )
    store.create(record)
    got = store.get(session.id)
    assert got is not None
    assert got.task_id == session.id


def test_atomic_persistence_behavior(tmp_settings):
    _clean_tasks_dir(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    record = TaskRecord(
        task_id="tst-atomic-001",
        project_name="p",
        project_root="/p",
        title="Atomic",
        goal="Test",
        created_at="20240101-120000",
    )
    store.create(record)
    path = tmp_settings.sessions_dir / "tasks" / "tst-atomic-001.json"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "tst-atomic-001" in content
    assert "Atomic" in content