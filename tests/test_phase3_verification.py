import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.permissions import PermissionLevel, PermissionManager
from core.agent import Agent
from core.context import ProjectContext
from core.events import EventBus
from core.executor import Executor
from core.task_manager import StepStatus, TaskManager
from core.task_store import TaskRecord, TaskStore
from coding.change_tracker import ChangeTracker
from coding.snapshot_manager import SnapshotManager
from memory.storage import save_json
from tools import build_registry
from tools import git as git_tool
from tools.registry import ToolRegistry, ToolResult, ToolSpec


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (("init", "-q"), ("config", "user.email", "a@b.c"), ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=str(path), capture_output=True, check=True)
    (path / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(path), capture_output=True, check=True)
    return path


def _record(task_id="task-1", project_root="/project"):
    return TaskRecord(
        task_id=task_id,
        project_name="project",
        project_root=str(project_root),
        title="Task",
        goal="Goal",
        created_at="20240101-120000",
        status="active",
    )


def _bare_agent(settings, project=None, project_memory=None):
    agent = Agent.__new__(Agent)
    agent._task_store = TaskStore(settings.sessions_dir)
    agent._change_tracker = ChangeTracker(settings.sessions_dir)
    agent._project = project
    agent._project_memory = project_memory
    agent._pending_goal = ""
    agent._tasks = TaskManager()
    agent._settings = SimpleNamespace(memory_write_policy="implicit")
    return agent


def test_task_store_rejects_unsafe_ids_and_invalid_updates(tmp_settings):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record())

    for task_id in ("../outside", "C:outside", "bad/id", "bad\\id", ".", ".."):
        assert store.get(task_id) is None
        assert store.inspect_task(task_id) is None
        assert store.update(task_id, title="changed") is None
        assert store.delete(task_id) is False
        assert store.finish(task_id, "done") is None
        with pytest.raises(ValueError):
            _record(task_id=task_id)

    with pytest.raises(ValueError, match="invalid status"):
        store.update("task-1", status="invalid")
    with pytest.raises(ValueError, match="cannot be changed"):
        store.update("task-1", task_id="task-2")
    assert store.get("task-1").title == "Task"


def test_task_store_isolates_null_and_malformed_records(tmp_settings):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("valid"))
    save_json(tmp_settings.sessions_dir / "tasks" / "null-fields.json", {
        "task_id": "null-fields",
        "project_name": "project",
        "project_root": "/project",
        "title": "Null fields",
        "goal": "Safe defaults",
        "created_at": "20240101-120000",
        "git": None,
        "snapshots": None,
        "change_steps": None,
        "metadata": None,
    })
    save_json(tmp_settings.sessions_dir / "tasks" / "malformed.json", {
        "task_id": "malformed",
        "git": "not-an-object",
    })

    records = store.list_all()
    null_record = store.get("null-fields")

    assert {record.task_id for record in records} == {"valid", "null-fields"}
    assert store.get("malformed") is None
    assert null_record.git.commits_made == []
    assert null_record.snapshots == []
    assert null_record.change_steps == []
    assert null_record.metadata == {}


def test_approved_first_action_starts_task_before_execution(tmp_settings, tmp_path):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    repo = _repo(tmp_path / "repo")
    agent = _bare_agent(tmp_settings, ProjectContext(name="repo", path=str(repo)))
    observed = {}

    def execute(**kwargs):
        session = agent._change_tracker.active
        record = agent._task_store.get(session.id)
        observed["task_id"] = session.id
        observed["starting_commit"] = record.git.starting_commit
        return ToolResult(True, output="ok")

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="git.status",
        description="test",
        fn=execute,
        level=PermissionLevel.SAFE,
    ))
    executor = Executor(
        registry,
        PermissionManager(tmp_settings),
        EventBus(),
        before_execute=agent._before_execute,
    )

    outcome = executor.run("git.status", {"root": str(repo)})

    assert outcome.success is True
    assert observed["task_id"]
    assert observed["starting_commit"]


def test_debug_failures_project_into_task_status(tmp_settings, monkeypatch):
    agent = _bare_agent(tmp_settings)
    agent._provider = SimpleNamespace(chat=None)
    agent._executor = None
    agent._registry = None
    agent._model_name = ""
    agent._router_verify_ok = True

    class FakeDebugLoop:
        def __init__(self, *args, **kwargs):
            pass

        def diagnose(self, user_input, ctx):
            return SimpleNamespace(
                steps=[SimpleNamespace(action="git.status", success=False, detail="failed")],
                verified=False,
                exhausted="",
                to_context=lambda: "debug failed",
            )

    monkeypatch.setattr("core.debug_loop.DebugLoop", FakeDebugLoop)
    agent._run_debug("debug this")

    assert agent._tasks.steps[0].status == StepStatus.FAILED


def test_denied_operation_does_not_start_task_or_inspect_git(tmp_settings, monkeypatch, tmp_path):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    repo = _repo(tmp_path / "repo")
    agent = _bare_agent(tmp_settings)
    outcome = SimpleNamespace(allowed=False, executed=False, success=False, reason="Denied by user.")

    def fail_git(*args, **kwargs):
        raise AssertionError("Git must not be inspected for a denied operation")

    monkeypatch.setattr("tools.git._git", fail_git)
    agent._note_step("git.commit", {"root": str(repo), "message": "blocked"}, outcome)

    assert agent._change_tracker.active is None
    assert list((tmp_settings.sessions_dir / "tasks").glob("*.json")) == []


def test_agent_persists_change_steps_without_copying_to_project_memory(tmp_settings, tmp_path):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    project_root = tmp_path / "non-git-project"
    project_root.mkdir()
    memory = SimpleNamespace(add_history_entry=lambda *args: (_ for _ in ()).throw(AssertionError("task history copied")))
    agent = _bare_agent(tmp_settings, ProjectContext(name="project", path=str(project_root)), memory)
    session = agent._change_tracker.start("Change task", str(project_root))
    agent._change_tracker.step("terminal.execute", {"command": "dir"}, True, True, True)

    agent._finish_tracking("done")

    record = agent._task_store.get(session.id)
    assert record.status == "completed"
    assert [step.tool for step in record.change_steps] == ["terminal.execute"]
    assert agent._change_tracker.active is None


def test_cancelled_agent_task_is_retired_before_next_session(tmp_settings, tmp_path):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    project_root = tmp_path / "project"
    project_root.mkdir()
    agent = _bare_agent(tmp_settings, ProjectContext(name="project", path=str(project_root)))
    first = agent._change_tracker.start("First", str(project_root))
    step = agent._tasks.add_step("terminal.execute")
    agent._tasks.set_status(step, StepStatus.CANCELLED, "Denied")

    agent._finish_tracking("cancelled")

    record = agent._task_store.get(first.id)
    assert record.status == "cancelled"
    assert agent._change_tracker.active is None
    second = agent._change_tracker.start("Second", str(project_root))
    assert second.id != first.id


def test_snapshot_tool_links_reference_to_task_record(tmp_settings, tmp_path):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"
    repo = _repo(tmp_path / "repo")
    store = TaskStore(tmp_settings.sessions_dir)
    store.create(_record("snapshot-task", repo))

    result = git_tool.git_snapshot(str(repo), "snapshot-task", "before", settings=tmp_settings)

    assert result.success
    snapshot_id = result.output.rsplit(":", 1)[-1].strip()
    task = store.inspect_task("snapshot-task")
    assert task["snapshots"] == [snapshot_id]
    assert store.query_tasks(limit=1)[0]["git"]["snapshot_count"] == 1


def test_git_option_inputs_are_rejected_before_execution(tmp_settings, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    tmp_settings.git_push_policy = "allow"
    tmp_settings.git_restore_policy = "allow"
    tmp_settings.git_branch_policy = "allow"
    calls = []
    monkeypatch.setattr("tools.git._git", lambda *args: calls.append(args) or (0, "", ""))

    assert git_tool.git_reset(str(repo), mode="mixed", target="--hard", settings=tmp_settings).success is False
    assert git_tool.git_push(str(repo), branch="--force", settings=tmp_settings).success is False
    assert git_tool.git_branch_switch(str(repo), "--detach", settings=tmp_settings).success is False
    assert calls == []


def test_git_policies_and_directory_listing_permission_levels(tmp_settings, tmp_path):
    repo = _repo(tmp_path / "repo")
    settings = tmp_settings
    settings.git_push_policy = "allow"
    registry = build_registry(settings)
    push = registry.get("git.push")
    assert push.level_for({"root": str(repo)}) == PermissionLevel.NORMAL

    settings.git_push_policy = "confirm"
    registry = build_registry(settings)
    push = registry.get("git.push")
    assert push.level_for({"root": str(repo)}) == PermissionLevel.SENSITIVE

    settings.git_push_policy = "blocked"
    registry = build_registry(settings)
    push = registry.get("git.push")
    assert push.level_for({"root": str(repo)}) == PermissionLevel.BLOCKED
    assert registry.get("filesystem.list_dir").level_for({"path": "C:/outside"}) == PermissionLevel.SENSITIVE


def test_terminal_auto_allow_rejects_chaining_and_honors_deny(tmp_settings):
    settings = tmp_settings
    settings.terminal_auto_allow = ["git status"]
    manager = PermissionManager(settings)
    chained = manager.evaluate("terminal.execute", {"command": "git status; Remove-Item x"}, PermissionLevel.SENSITIVE)
    assert chained.requires_confirmation is True

    preferences = SimpleNamespace(get_permission=lambda tool: "deny" if tool == "terminal.execute" else None)
    denied = PermissionManager(settings, preferences).evaluate(
        "terminal.execute", {"command": "git status"}, PermissionLevel.SENSITIVE
    )
    assert denied.blocked is True


def test_snapshot_identities_and_restore_root_are_bound(tmp_settings, tmp_path):
    tmp_settings.git_restore_policy = "allow"
    sessions = tmp_settings.data_dir / "sessions"
    tmp_settings.sessions_dir = sessions
    manager = SnapshotManager(sessions)
    repo = _repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(ValueError, match="unsafe task_id"):
        manager.create_snapshot("../escape", "bad", str(repo))
    with pytest.raises(ValueError, match="unsafe task_id"):
        manager.list_snapshots("../escape")
    assert manager.get_snapshot("task", "../escape") is None

    snapshot = manager.create_snapshot("task", "safe", str(repo))
    result = git_tool.git_snapshot_restore(
        str(other), "task", snapshot.snapshot_id, settings=tmp_settings
    )
    assert result.success is False
    assert "does not match" in result.error
