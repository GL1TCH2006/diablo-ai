import json

import pytest

from config.permissions import PermissionLevel, PermissionManager
from core.events import EventBus
from core.executor import Executor
from core.task_store import CommitInfo, GitTaskState, TaskChangeStep, TaskRecord, TaskStore
from memory.storage import save_json
from tools import build_registry
from tools import tasks as tasks_tool


def _record(
    task_id,
    project="alpha",
    status="pending",
    created_at="20240101-120000",
    started_at=None,
    finished_at=None,
    outcome=None,
    git=None,
    snapshots=None,
    change_steps=None,
):
    return TaskRecord(
        task_id=task_id,
        project_name=project,
        project_root=f"/projects/{project}",
        title=f"Title {task_id}",
        goal=f"Goal {task_id}",
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        outcome=outcome,
        git=git or GitTaskState(),
        snapshots=snapshots or [],
        change_steps=change_steps or [],
    )


def _clean(sessions_dir):
    tasks_dir = sessions_dir / "tasks"
    for path in tasks_dir.glob("*.json"):
        path.unlink()


def test_task_info_returns_existing_task(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("task-info", project="inspector"))

    task = store.inspect_task("task-info")

    assert task["task_id"] == "task-info"
    assert task["project"] == {"name": "inspector", "root": "/projects/inspector"}
    assert task["title"] == "Title task-info"
    assert task["goal"] == "Goal task-info"
    assert "change_steps" in task
    assert "git" in task
    assert "snapshots" in task


def test_task_info_missing_behaves_correctly(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)

    assert store.inspect_task("missing") is None
    assert store.inspect_task("../outside") is None
    assert store.inspect_task("C:outside") is None
    result = tasks_tool.task_info("missing", tmp_settings)
    assert result.success is False
    assert "not found" in result.error.lower()


def test_task_list_returns_stored_tasks(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    for task_id in ("task-a", "task-b", "task-c"):
        store.create(_record(task_id, created_at=f"2024010{task_id[-1]}-120000"))

    tasks = store.query_tasks()

    assert [task["task_id"] for task in tasks] == ["task-c", "task-b", "task-a"]
    assert all("change_steps" not in task for task in tasks)
    assert all("commits_made" not in task["git"] for task in tasks)


def test_task_list_ordering_is_deterministic(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("task-old", created_at="20240101-120000"))
    store.create(_record("task-a", created_at="20240103-120000"))
    store.create(_record("task-b", created_at="20240103-120000"))

    first = store.query_tasks(limit=None)
    second = store.query_tasks(limit=None)

    assert [task["task_id"] for task in first] == ["task-b", "task-a", "task-old"]
    assert first == second


def test_task_list_filters_by_project(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("alpha-one", project="alpha"))
    store.create(_record("beta-one", project="beta"))
    store.create(_record("alpha-two", project="alpha"))

    tasks = store.query_tasks(project="alpha")

    assert {task["task_id"] for task in tasks} == {"alpha-one", "alpha-two"}
    assert all(task["project_name"] == "alpha" for task in tasks)


def test_task_list_filters_by_status(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("pending-task", status="pending"))
    store.create(_record("completed-task", status="completed"))
    store.create(_record("failed-task", status="failed"))

    tasks = store.query_tasks(status=" COMPLETED ")

    assert [task["task_id"] for task in tasks] == ["completed-task"]


def test_task_list_limit_returns_recent_tasks(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    for day in (1, 2, 3):
        store.create(_record(f"task-{day}", created_at=f"2024010{day}-120000"))

    assert [task["task_id"] for task in store.query_tasks(limit=2)] == ["task-3", "task-2"]
    assert store.query_tasks(limit=0) == []
    assert len(store.query_tasks(limit=None)) == 3


def test_task_git_summary_is_exposed(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    git = GitTaskState(
        project_root="/repo",
        starting_commit="abc123",
        starting_branch="main",
        ending_commit="def456",
        ending_branch="feature/history",
        commits_made=[
            CommitInfo(hash="abc123", message="first", timestamp="20240101-120001"),
            CommitInfo(hash="def456", message="second", timestamp="20240101-120002"),
        ],
        branches_created=["feature/history"],
        branches_switched=["main", "feature/history"],
        files_changed=["a.py", "b.py"],
        net_diff_stat="2 files changed, 3 insertions(+)",
        final_status=" M a.py",
    )
    store.create(_record("git-task", git=git, snapshots=["snapshot-1", "snapshot-2"]))

    task = store.inspect_task("git-task")

    assert task["git"]["starting_commit"] == "abc123"
    assert task["git"]["ending_commit"] == "def456"
    assert task["git"]["starting_branch"] == "main"
    assert task["git"]["ending_branch"] == "feature/history"
    assert [commit["hash"] for commit in task["git"]["commits_made"]] == ["abc123", "def456"]
    assert task["git"]["files_changed"] == ["a.py", "b.py"]
    assert task["git"]["net_diff_stat"] == "2 files changed, 3 insertions(+)"
    assert task["snapshots"] == ["snapshot-1", "snapshot-2"]


def test_task_outcome_and_status_are_exposed(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record(
        "finished-task",
        status="completed",
        created_at="20240101-120000",
        started_at="20240101-120100",
        finished_at="20240101-120500",
        outcome="Implemented and verified",
    ))

    task = store.inspect_task("finished-task")

    assert task["created_at"] == "20240101-120000"
    assert task["started_at"] == "20240101-120100"
    assert task["finished_at"] == "20240101-120500"
    assert task["status"] == "completed"
    assert task["outcome"] == "Implemented and verified"


def test_task_change_steps_are_exposed(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    steps = [
        TaskChangeStep(
            tool="filesystem.write_file",
            args={"path": "a.py"},
            allowed=True,
            executed=True,
            success=True,
            reason="ok",
            ts="12:00:01",
        ),
        TaskChangeStep(tool="git.commit", success=False, reason="nothing to commit"),
    ]
    store.create(_record("history-task", change_steps=steps))

    task = store.inspect_task("history-task")

    assert [step["tool"] for step in task["change_steps"]] == ["filesystem.write_file", "git.commit"]
    assert task["change_steps"][0]["args"] == {"path": "a.py"}
    assert task["change_steps"][0]["success"] is True
    assert task["change_steps"][1]["reason"] == "nothing to commit"


def test_empty_task_store_inspection(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)

    assert store.inspect_task("missing") is None
    assert store.query_tasks() == []
    result = tasks_tool.task_list(settings=tmp_settings)
    assert result.success is True
    assert json.loads(result.output) == {"tasks": [], "count": 0}


def test_task_inspection_persists_across_store_instances(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    first_store = TaskStore(tmp_settings.sessions_dir)
    first_store.create(_record("persistent-task", outcome="done", status="completed"))

    second_store = TaskStore(tmp_settings.sessions_dir)
    task = second_store.inspect_task("persistent-task")
    listed = second_store.query_tasks(project="alpha")

    assert task["outcome"] == "done"
    assert listed[0]["task_id"] == "persistent-task"


def test_legacy_task_record_remains_queryable(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    path = tmp_settings.sessions_dir / "tasks" / "legacy-task.json"
    save_json(path, {
        "task_id": "legacy-task",
        "project_name": "legacy-project",
        "project_root": "/legacy",
        "title": "Legacy task",
        "goal": "Load old data",
        "created_at": "20240101-120000",
        "git": {"commits_made": ["abc123"]},
    })

    store = TaskStore(tmp_settings.sessions_dir)
    task = store.inspect_task("legacy-task")
    listed = store.query_tasks(project="legacy-project")

    assert task["status"] == "pending"
    assert task["git"]["commits_made"] == [
        {"hash": "abc123", "message": "", "timestamp": ""}
    ]
    assert task["snapshots"] == []
    assert task["change_steps"] == []
    assert listed[0]["git"]["commits_made_count"] == 1


def test_task_inspection_apis_and_tools_are_read_only(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("read-only"))
    path = tmp_settings.sessions_dir / "tasks" / "read-only.json"
    before = path.read_bytes()
    registry = build_registry(tmp_settings)

    store.inspect_task("read-only")
    store.query_tasks()
    tasks_tool.task_info("read-only", tmp_settings)
    tasks_tool.task_list(settings=tmp_settings)
    registry.get("task.info").fn(task_id="read-only")
    registry.get("task.list").fn()

    assert path.read_bytes() == before


def test_task_tools_register_and_dispatch(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    TaskStore(tmp_settings.sessions_dir).create(_record("dispatch-task"))
    registry = build_registry(tmp_settings)
    executor = Executor(registry, PermissionManager(tmp_settings), EventBus())

    info_outcome = executor.run("task.info", {"task_id": "dispatch-task"})
    list_outcome = executor.run("task.list", {"project": "alpha", "limit": 5})

    assert info_outcome.success is True
    assert json.loads(info_outcome.output)["task_id"] == "dispatch-task"
    assert list_outcome.success is True
    assert json.loads(list_outcome.output)["count"] == 1


def test_task_tools_are_safe_but_respect_permission_controls(tmp_settings):
    registry = build_registry(tmp_settings)
    info_spec = registry.get("task.info")
    list_spec = registry.get("task.list")
    manager = PermissionManager(tmp_settings)

    assert info_spec.level == PermissionLevel.SAFE
    assert list_spec.level == PermissionLevel.SAFE
    decision = manager.evaluate("task.info", {"task_id": "safe"}, info_spec.level)
    assert decision.allow is True
    assert decision.requires_confirmation is False

    manager.session_block("task.list")
    blocked = manager.evaluate("task.list", {}, list_spec.level)
    assert blocked.blocked is True


def test_query_tasks_rejects_invalid_filters(tmp_settings):
    _clean(tmp_settings.sessions_dir)
    store = TaskStore(tmp_settings.sessions_dir)

    with pytest.raises(ValueError, match="invalid status"):
        store.query_tasks(status="unknown")
    with pytest.raises(ValueError, match="limit"):
        store.query_tasks(limit=-1)
