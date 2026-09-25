import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai.offline_provider import OfflineProvider
from config.permissions import PermissionManager
from core.agent import Agent
from core.context import ProjectContext
from core.events import EventBus
from core.project_task_history import ProjectTaskHistory
from core.task_store import CommitInfo, GitTaskState, TaskChangeStep, TaskRecord, TaskStore
from memory.project_memory import ProjectMemory
from memory.storage import save_json
from tools import build_registry


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_settings):
    tmp_settings.sessions_dir = tmp_settings.data_dir / "sessions"


def _record(
    task_id,
    project_name,
    project_root,
    status="pending",
    created_at="20240101-120000",
    started_at=None,
    finished_at=None,
    outcome=None,
    git=None,
    change_steps=None,
):
    return TaskRecord(
        task_id=task_id,
        project_name=project_name,
        project_root=str(project_root),
        title=f"Title {task_id}",
        goal=f"Goal {task_id}",
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        outcome=outcome,
        git=git or GitTaskState(),
        change_steps=change_steps or [],
    )


def _project_memory(tmp_settings, store=None, project_name="alpha", attach=True):
    project_root = tmp_settings.workspace_root / project_name
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    history = ProjectTaskHistory(store, tmp_settings.workspace_root) if attach else None
    memory = ProjectMemory(tmp_settings.memory_dir, tmp_settings.workspace_root, history)
    memory.discover()
    return memory, project_root


def _seed_history(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    project_root = tmp_settings.workspace_root / "alpha"
    records = [
        _record(
            "completed-old",
            "alpha",
            project_root,
            status="completed",
            created_at="20240101-120000",
            started_at="20240101-120100",
            finished_at="20240101-120200",
            outcome="Older completed work",
        ),
        _record(
            "failed-task",
            "alpha",
            project_root,
            status="failed",
            created_at="20240102-120000",
            started_at="20240102-120100",
            finished_at="20240102-120200",
            outcome="Migration failed",
        ),
        _record(
            "completed-new",
            "alpha",
            project_root,
            status="completed",
            created_at="20240103-120000",
            started_at="20240103-120100",
            finished_at="20240103-120200",
            outcome="Authentication fixed",
        ),
        _record(
            "active-task",
            "alpha",
            project_root,
            status="active",
            created_at="20240104-120000",
            started_at="20240104-120100",
        ),
    ]
    for record in records:
        store.create(record)
    memory, project_root = _project_memory(tmp_settings, store)
    return store, memory, project_root


def test_project_memory_retrieves_task_history(tmp_settings):
    _seed_history(tmp_settings)
    store = TaskStore(tmp_settings.sessions_dir)
    memory, _ = _project_memory(tmp_settings, store)

    history = memory.get_task_history("alpha", limit=None)

    assert {task["task_id"] for task in history} == {
        "completed-old",
        "failed-task",
        "completed-new",
        "active-task",
    }
    assert all(task["project_name"] == "alpha" for task in history)


def test_recent_task_history_is_deterministic(tmp_settings):
    _seed_history(tmp_settings)
    store = TaskStore(tmp_settings.sessions_dir)
    memory, _ = _project_memory(tmp_settings, store)

    first = memory.get_recent_tasks("alpha", limit=3)
    second = memory.get_recent_tasks("alpha", limit=3)

    assert [task["task_id"] for task in first] == ["active-task", "completed-new", "failed-task"]
    assert first == second


def test_completed_failed_and_current_states(tmp_settings):
    _seed_history(tmp_settings)
    store = TaskStore(tmp_settings.sessions_dir)
    memory, _ = _project_memory(tmp_settings, store)

    current = memory.get_current_task("alpha")
    completed = memory.get_most_recent_completed_task("alpha")
    failed = memory.get_failed_tasks("alpha")

    assert current["task_id"] == "active-task"
    assert current["status"] == "active"
    assert completed["task_id"] == "completed-new"
    assert completed["status"] == "completed"
    assert [task["task_id"] for task in failed] == ["failed-task"]


def test_task_store_remains_history_source_of_truth(tmp_settings):
    store, memory, _ = _seed_history(tmp_settings)
    store.update("completed-new", outcome="Updated authoritative outcome")

    reloaded = ProjectTaskHistory(TaskStore(tmp_settings.sessions_dir), tmp_settings.workspace_root)

    assert memory.get_task_summary("alpha", "completed-new")["outcome"] == "Updated authoritative outcome"
    assert reloaded.get("alpha", str(tmp_settings.workspace_root / "alpha"), "completed-new")["outcome"] == "Updated authoritative outcome"


def test_project_memory_does_not_duplicate_task_records(tmp_settings):
    store, memory, _ = _seed_history(tmp_settings)
    memory.get_task_history("alpha", limit=None)

    persisted = json.loads(memory.path.read_text(encoding="utf-8"))
    text = json.dumps(persisted)

    assert "completed-new" not in text
    assert "Authentication fixed" not in text
    assert "change_steps" not in text
    assert "commits_made" not in text


def test_project_name_and_root_mismatch_is_safe(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, project_root = _project_memory(tmp_settings, store, project_name="friendly-name")
    other_root = tmp_settings.workspace_root / "other-root"
    other_root.mkdir()
    store.create(_record("matching-root", "directory-name", project_root, status="completed"))
    store.create(_record("same-name-other-root", "friendly-name", other_root, status="completed"))

    history = memory.get_task_history("friendly-name", limit=None)

    assert [task["task_id"] for task in history] == ["matching-root"]
    assert memory.get_task_summary("friendly-name", "same-name-other-root") is None


def test_empty_project_history(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, _ = _project_memory(tmp_settings, store)

    assert memory.get_task_history("alpha") == []
    assert memory.get_current_task("alpha") is None
    assert memory.get_most_recent_completed_task("alpha") is None
    assert memory.get_failed_tasks("alpha") == []
    assert memory.render_task_history_context("alpha") == ""


def test_history_persists_across_task_store_and_memory_reloads(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, project_root = _project_memory(tmp_settings, store)
    store.create(_record("persistent", "alpha", project_root, status="completed", outcome="Done"))
    memory.set_section("alpha", "architecture", "Layered")

    reloaded_store = TaskStore(tmp_settings.sessions_dir)
    reloaded_memory = ProjectMemory(
        tmp_settings.memory_dir,
        tmp_settings.workspace_root,
        ProjectTaskHistory(reloaded_store, tmp_settings.workspace_root),
    )

    assert reloaded_memory.get_task_summary("alpha", "persistent")["outcome"] == "Done"
    assert reloaded_memory.get_section("alpha", "architecture") == "Layered"


def test_existing_project_memory_behavior_remains_intact(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, project_root = _project_memory(tmp_settings, store)
    store.create(_record("history", "alpha", project_root, status="completed"))
    memory.set_section("alpha", "architecture", "three-tier")
    memory.set_preferred_model("alpha", "qwen3:14b")
    memory.add_history_entry("alpha", "Explicit project fact")

    context = memory.render_context("alpha")

    assert "three-tier" in context
    assert "Explicit project fact" in context
    assert memory.get_preferred_model("alpha") == "qwen3:14b"
    assert memory.get_task_history("alpha")[0]["task_id"] == "history"


def test_existing_task_store_behavior_remains_intact(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    project_root = tmp_settings.workspace_root / "direct"
    git = GitTaskState(
        starting_commit="abc",
        ending_commit="def",
        commits_made=[CommitInfo(hash="def", message="change", timestamp="20240101-120001")],
        files_changed=["app.py"],
    )
    record = _record(
        "source-record",
        "direct",
        project_root,
        status="completed",
        git=git,
        change_steps=[TaskChangeStep(tool="git.commit", success=True)],
    )
    store.create(record)

    loaded = store.get("source-record")

    assert loaded.git.commits_made[0].hash == "def"
    assert loaded.git.files_changed == ["app.py"]
    assert loaded.change_steps[0].tool == "git.commit"


def test_legacy_task_records_remain_available_to_project_memory(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, project_root = _project_memory(tmp_settings, store, project_name="legacy-friendly")
    save_json(tmp_settings.sessions_dir / "tasks" / "legacy-task.json", {
        "task_id": "legacy-task",
        "project_name": "legacy-directory",
        "project_root": str(project_root),
        "title": "Legacy task",
        "goal": "Load old task",
        "created_at": "20240101-120000",
        "status": "completed",
    })

    history = memory.get_task_history("legacy-friendly")

    assert [task["task_id"] for task in history] == ["legacy-task"]
    assert history[0]["outcome"] is None


def test_missing_project_memory_does_not_break_task_store(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    record = _record("independent", "alpha", tmp_settings.workspace_root / "alpha")

    store.create(record)
    store.update("independent", status="completed", outcome="Stored")
    loaded = TaskStore(tmp_settings.sessions_dir).get("independent")

    assert loaded.status == "completed"
    assert loaded.outcome == "Stored"


def test_missing_task_store_does_not_corrupt_project_memory(tmp_settings):
    memory, _ = _project_memory(tmp_settings, store=None, attach=True)
    memory.set_section("alpha", "decisions", "Keep project memory")

    assert memory.get_task_history("alpha") == []
    assert memory.get_current_task("alpha") is None

    reloaded = ProjectMemory(
        tmp_settings.memory_dir,
        tmp_settings.workspace_root,
        ProjectTaskHistory(None, tmp_settings.workspace_root),
    )
    assert reloaded.get_section("alpha", "decisions") == "Keep project memory"


def test_broken_task_history_source_fails_safely(tmp_settings):
    memory, _ = _project_memory(tmp_settings, store=None, attach=False)

    class BrokenHistory:
        def list(self, *args, **kwargs):
            raise OSError("TaskStore unavailable")

    memory.attach_task_history(BrokenHistory())

    assert memory.get_task_history("alpha") == []
    assert memory.render_task_history_context("alpha") == ""


def test_compact_task_summaries_have_intended_fields(tmp_settings):
    store, memory, project_root = _seed_history(tmp_settings)
    summary = memory.get_task_summary("alpha", "completed-new")

    assert set(summary) == {
        "task_id",
        "project_name",
        "project_root",
        "title",
        "goal",
        "created_at",
        "started_at",
        "finished_at",
        "status",
        "outcome",
        "git",
    }
    assert set(summary["git"]) == {
        "starting_commit",
        "starting_branch",
        "ending_commit",
        "ending_branch",
        "commits_made_count",
        "files_changed_count",
        "snapshot_count",
    }
    assert "change_steps" not in summary
    assert "commits_made" not in summary["git"]


def test_find_task_by_project_change(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, project_root = _project_memory(tmp_settings, store)
    git = GitTaskState(
        ending_commit="abcdef123456",
        commits_made=[CommitInfo(hash="abcdef123456", message="Fix auth", timestamp="20240101-120001")],
        files_changed=["src/auth.py"],
    )
    store.create(_record("change-task", "alpha", project_root, status="completed", git=git))

    by_file = memory.find_tasks_by_change("alpha", file_path="src/auth.py")
    by_commit = memory.find_tasks_by_change("alpha", commit_hash="abcdef")
    by_absolute = memory.find_tasks_by_change("alpha", file_path=str(project_root / "src/auth.py"))

    assert [task["task_id"] for task in by_file] == ["change-task"]
    assert [task["task_id"] for task in by_commit] == ["change-task"]
    assert [task["task_id"] for task in by_absolute] == ["change-task"]


def test_no_circular_import_or_runtime_initialization(tmp_path):
    code = "\n".join([
        "from memory.project_memory import ProjectMemory",
        "from core.project_task_history import ProjectTaskHistory",
        "from pathlib import Path",
        "history = ProjectTaskHistory(None, Path('.'))",
        "memory = ProjectMemory(Path('memory'), Path('.'), history)",
        "assert memory.get_task_history('alpha') == []",
    ])

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_agent_project_context_uses_compact_task_history(tmp_settings):
    store = TaskStore(tmp_settings.sessions_dir)
    memory, project_root = _project_memory(tmp_settings, store, attach=False)
    store.create(_record(
        "agent-history",
        "alpha",
        project_root,
        status="completed",
        created_at="20240101-120000",
        finished_at="20240101-120100",
        outcome="Authentication fixed",
    ))
    provider = OfflineProvider(scratch_dir=tmp_settings.scratch_dir)
    agent = Agent(
        provider=provider,
        settings=tmp_settings,
        registry=build_registry(tmp_settings),
        permissions=PermissionManager(tmp_settings),
        events=EventBus(),
        project=ProjectContext(name="alpha", path=str(project_root)),
        memory_project=memory,
    )

    context = agent.task_history_context()
    references = agent._reference_sections()

    assert "Recent task history:" in context
    assert "Authentication fixed" in context
    assert "change_steps" not in context
    assert "#PROJECT TASK HISTORY" in references
    assert len(context) <= 900
