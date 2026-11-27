"""M2.14 Phases C/E — live task, verification and Git projection tests.

These assert the *ownership boundaries* as much as the produced nodes:

* TaskStore still owns the task record; the graph is a copy.
* Verification state is copied from the ledger, never invented.
* A failed or cancelled task cannot yield a passing verification node.
* Projection is additive: a broken projector cannot fail a task.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_project_key,
)
from core.knowledge_projection import (
    authorized_project,
    is_test_module,
    knowledge_dir,
    project_task,
    relative_to_project,
)
from core.knowledge_projection import test_key as knowledge_test_key
from core.knowledge_tools import knowledge_index
from core.planner import ExecutionPlanRecord, PlanStepRecord, StepStatus
from core.task_manager import TaskManager
from core.task_store import (
    CommitInfo,
    GitTaskState,
    TaskChangeStep,
    TaskRecord,
    TaskStore,
)
from core.verification import VerificationResult, VerificationState


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import run\n\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def graph_for(tmp_settings, project: Path):
    """Index the project and return (manager, graph, project_id)."""
    assert knowledge_index(project_path=str(project), settings=tmp_settings).success
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    project_id = canonical_project_key(str(project))
    yield mgr, mgr.get_graph(project_id), project_id
    mgr.close_all()


def _record(tmp_path, project: Path, *, task_id="task-1", status="completed",
            plan_steps=None, verification=None, change_paths=(), commits=(),
            snapshots=(), outcome="done") -> TaskRecord:
    record = TaskRecord(
        task_id=task_id,
        project_name=project.name,
        project_root=str(project),
        title="projection task",
        goal="do the thing",
        created_at=time.strftime("%Y%m%d-%H%M%S"),
        status=status,
        outcome=outcome,
        change_steps=[
            TaskChangeStep(tool="filesystem.write_file", args={"path": str(p)},
                           allowed=True, executed=True, success=True)
            for p in change_paths
        ],
        git=GitTaskState(
            project_root=str(project),
            starting_commit="aaa111",
            files_changed=[str(Path(p).name) for p in change_paths],
            commits_made=[CommitInfo(hash=h, message=m, timestamp="20240101-000000")
                          for h, m in commits],
        ),
        snapshots=list(snapshots),
    )
    if plan_steps:
        record.plan = ExecutionPlanRecord(goal="g", created_at="20240101-000000",
                                          steps=plan_steps)
    if verification is not None:
        record.metadata = {
            "lifecycle": {"verification_ledger": verification}
        }
    return record


def _ledger_for(project: Path, task_id: str, state: VerificationState, evidence: dict):
    tasks = TaskManager()
    tasks.begin_task("t")
    tasks.bind_task(task_id)
    entry = tasks.record_verification(
        VerificationResult(state, "detail", "file_and_output", evidence)
    )
    return entry, tasks.ledger.to_persisted_dict()


def _targets(graph, node_id, relation):
    return [graph.get_node_by_id(e.target_id) for e in
            graph.get_edges(source_id=node_id, relation_type=relation)]


# ------------------------------------------------------------------- helpers


class TestHelpers:
    @pytest.mark.parametrize("path,expected", [
        ("src/app.py", False),
        ("tests/test_app.py", True),
        ("test_root.py", True),
        ("widget_test.py", True),
        ("app.py", False),
        ("src/helper.py", False),
        ("README.md", False),
        ("", False),
        ("contest.py", False),
    ])
    def test_is_test_module(self, path, expected):
        assert is_test_module(path) is expected

    def test_relative_to_project_handles_absolute_and_relative(self, tmp_path):
        root = tmp_path / "p"
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        assert relative_to_project(str(target), root) == "sub/a.py"
        assert relative_to_project("sub/a.py", root) == "sub/a.py"

    def test_relative_to_project_rejects_escape(self, tmp_path):
        root = tmp_path / "p"
        root.mkdir()
        outside = tmp_path / "evil.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        assert relative_to_project(str(outside), root) == ""

    def test_authorized_project(self, tmp_settings, tmp_path):
        assert authorized_project(tmp_settings, str(tmp_path)) is True
        assert authorized_project(tmp_settings, "C:/Windows/System32") is False
        assert authorized_project(tmp_settings, "") is False


def test_test_key_shape():
    assert knowledge_test_key("p1", "tests/t.py") == "test:p1:tests/t.py"


# -------------------------------------------------------------- live projection


class TestTaskProjection:
    def test_task_node_is_created(self, tmp_settings, project: Path, graph_for):
        _, graph, project_id = graph_for
        project_task(_record(tmp_settings, project), tmp_settings)
        tasks = graph.find_nodes(node_type=NodeType.TASK, limit=10)
        assert len(tasks) == 1
        assert tasks[0].display_name == "projection task"
        assert tasks[0].metadata["status"] == "completed"

    def test_task_references_authoritative_task_id(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project, task_id="authoritative-id-42"), tmp_settings)
        from core.knowledge_graph import canonical_task_key
        assert graph.get_node(canonical_task_key("authoritative-id-42")) is not None

    def test_task_changed_by_file(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project, change_paths=[project / "app.py"]),
                     tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        files = _targets(graph, task_node.node_id, RelationType.CHANGED_BY)
        assert [f.display_name for f in files] == ["app.py"]
        assert all(f.node_type is NodeType.FILE for f in files)

    def test_test_module_produces_a_test_node(self, tmp_settings, project: Path, graph_for):
        _, graph, project_id = graph_for
        project_task(_record(tmp_settings, project,
                             change_paths=[project / "tests" / "test_app.py"]),
                     tmp_settings)
        tests = graph.find_nodes(node_type=NodeType.TEST, limit=10)
        assert len(tests) == 1
        assert tests[0].canonical_key == knowledge_test_key(project_id, "tests/test_app.py")
        # A TEST node describes a test; it never asserts a result.
        assert tests[0].metadata["result_state"] == ""

    def test_non_test_file_produces_no_test_node(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project, change_paths=[project / "app.py"]),
                     tmp_settings)
        assert graph.find_nodes(node_type=NodeType.TEST, limit=10) == []

    def test_test_node_is_part_of_its_file(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project,
                             change_paths=[project / "tests" / "test_app.py"]),
                     tmp_settings)
        test_node = graph.find_nodes(node_type=NodeType.TEST, limit=1)[0]
        linked = _targets(graph, test_node.node_id, RelationType.PART_OF)
        assert [n.display_name for n in linked] == ["test_app.py"]

    def test_task_produced_the_test_node(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project,
                             change_paths=[project / "tests" / "test_app.py"]),
                     tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        produced = _targets(graph, task_node.node_id, RelationType.PRODUCED)
        assert [n.node_type for n in produced] == [NodeType.TEST]

    def test_unindexed_file_is_not_invented(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        ghost = project / "never_indexed.py"
        ghost.write_text("x = 1\n", encoding="utf-8")
        project_task(_record(tmp_settings, project, change_paths=[ghost]), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        assert _targets(graph, task_node.node_id, RelationType.CHANGED_BY) == []

    def test_outside_project_path_is_dropped(self, tmp_settings, project: Path,
                                             graph_for, tmp_path):
        _, graph, _ = graph_for
        outside = tmp_path / "elsewhere.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        project_task(_record(tmp_settings, project, change_paths=[outside]), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        assert _targets(graph, task_node.node_id, RelationType.CHANGED_BY) == []


# ------------------------------------------------------------------- plan


class TestPlanProjection:
    def test_plan_is_projected_as_a_summary(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        steps = [
            PlanStepRecord(step_id="step-1", tool="filesystem.write_file", args={},
                           status=StepStatus.DONE),
            PlanStepRecord(step_id="step-2", tool="python.run_script", args={},
                           status=StepStatus.PENDING),
        ]
        project_task(_record(tmp_settings, project, plan_steps=steps), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        plans = _targets(graph, task_node.node_id, RelationType.EXECUTED_BY)
        assert len(plans) == 1
        assert plans[0].metadata["step_count"] == 2
        assert plans[0].metadata["step_summary"]["status_counts"] == {"done": 1, "pending": 1}

    def test_plan_steps_are_not_nodes(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        steps = [PlanStepRecord(step_id=f"step-{i}", tool="code.patch_file", args={})
                 for i in range(3)]
        project_task(_record(tmp_settings, project, plan_steps=steps), tmp_settings)
        # Neither STEP (which does not exist) nor TEST (which would be a lie).
        assert graph.find_nodes(node_type=NodeType.TEST, limit=10) == []

    def test_execution_plan_record_stays_authoritative(self, tmp_settings, project: Path):
        """The record keeps full step detail the graph does not copy."""
        steps = [PlanStepRecord(step_id="step-1", tool="code.patch_file",
                                args={"patch": "SECRET-PATCH-TEXT"},
                                status=StepStatus.DONE)]
        record = _record(tmp_settings, project, plan_steps=steps)
        project_task(record, tmp_settings)
        assert record.plan.steps[0].args["patch"] == "SECRET-PATCH-TEXT"
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(canonical_project_key(str(project)))
        plans = graph.find_nodes(node_type=NodeType.PLAN, limit=5)
        assert "SECRET-PATCH-TEXT" not in str(plans[0].metadata)
        graph.close()
        mgr.close_all()


# -------------------------------------------------------------- verification


class TestVerificationProjection:
    def test_passed_verification_is_projected(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        entry, ledger = _ledger_for(project, "task-1", VerificationState.PASSED,
                                    {"path": "app.py", "exists": True, "exit_code": 0})
        assert entry.state is VerificationState.PASSED
        project_task(_record(tmp_settings, project, verification=ledger), tmp_settings)

        verifs = graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10)
        assert len(verifs) == 1
        assert verifs[0].metadata["state"] == "passed"
        assert verifs[0].metadata["trust"] == "observed"

    def test_failed_verification_stays_failed(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        _, ledger = _ledger_for(project, "task-1", VerificationState.FAILED,
                                {"path": "app.py", "exists": False})
        project_task(_record(tmp_settings, project, status="failed",
                             verification=ledger, outcome="broke"), tmp_settings)
        verifs = graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10)
        assert [v.metadata["state"] for v in verifs] == ["failed"]

    def test_task_refers_to_its_verification(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        _, ledger = _ledger_for(project, "task-1", VerificationState.PASSED,
                                {"path": "app.py", "exists": True})
        project_task(_record(tmp_settings, project, verification=ledger), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        verifs = _targets(graph, task_node.node_id, RelationType.VERIFIED_BY)
        assert len(verifs) == 1
        assert verifs[0].node_type is NodeType.VERIFICATION

    def test_no_ledger_means_no_verification_node(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project), tmp_settings)
        assert graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10) == []

    def test_graph_cannot_invent_a_passed_verdict(self, tmp_settings, project: Path, graph_for):
        """A downgraded ledger entry must project as downgraded, not as passed."""
        _, graph, _ = graph_for
        tasks = TaskManager()
        tasks.begin_task("t")
        tasks.bind_task("task-1")
        # CLAIMED evidence with no observation: the ledger must refuse PASSED.
        entry = tasks.record_verification(
            VerificationResult(VerificationState.PASSED, "model says it worked",
                               "some_unmapped_kind", {"model_claim": "done"}),
            subject_kind="unknown",
            method_kind="none",
            trust="claimed",
        )
        assert entry.state is VerificationState.UNVERIFIED
        ledger = tasks.ledger.to_persisted_dict()
        project_task(_record(tmp_settings, project, verification=ledger), tmp_settings)
        verifs = graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10)
        assert [v.metadata["state"] for v in verifs] == ["unverified"]
        assert not any(v.metadata["state"] == "passed" for v in verifs)

    def test_cancelled_task_projects_no_passed_verification(self, tmp_settings,
                                                            project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project, status="cancelled",
                             outcome="Task cancelled."), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        assert task_node.metadata["status"] == "cancelled"
        assert graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10) == []


# --------------------------------------------------------------------- git


class TestGitProjection:
    def test_commits_are_projected(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project,
                             commits=[("deadbee", "add app")]), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        commits = _targets(graph, task_node.node_id, RelationType.CHANGED_BY)
        git_nodes = [c for c in commits if c.node_type is NodeType.GIT_COMMIT]
        assert len(git_nodes) == 1
        assert git_nodes[0].metadata["hash"] == "deadbee"

    def test_checkpoints_are_projected(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        project_task(_record(tmp_settings, project, snapshots=["snap-1"]), tmp_settings)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        cps = _targets(graph, task_node.node_id, RelationType.CHECKPOINTED_AS)
        assert [c.metadata["checkpoint_id"] for c in cps] == ["snap-1"]


# ---------------------------------------------------------------- idempotency


class TestIdempotency:
    def test_repeated_projection_does_not_grow(self, tmp_settings, project: Path, graph_for):
        _, graph, _ = graph_for
        _, ledger = _ledger_for(project, "task-1", VerificationState.PASSED,
                                {"path": "app.py", "exists": True})
        steps = [PlanStepRecord(step_id="step-1", tool="filesystem.write_file", args={},
                               status=StepStatus.DONE)]
        record = _record(tmp_settings, project, plan_steps=steps, verification=ledger,
                         change_paths=[project / "app.py"],
                         commits=[("deadbee", "m")], snapshots=["snap-1"])
        project_task(record, tmp_settings)
        before = graph.stats()
        for _ in range(3):
            project_task(record, tmp_settings)
        assert graph.stats() == before


# ------------------------------------------------------------------ refusals


class TestRefusals:
    def test_unauthorized_project_is_refused(self, tmp_settings):
        record = TaskRecord(
            task_id="t", project_name="x", project_root="C:/Windows/System32",
            title="t", goal="g", created_at="20240101-000000",
        )
        assert project_task(record, tmp_settings) == ""

    def test_none_task_is_refused(self, tmp_settings):
        assert project_task(None, tmp_settings) == ""

    def test_missing_project_root_is_refused(self, tmp_settings):
        record = TaskRecord(task_id="t", project_name="x", project_root="",
                            title="t", goal="g", created_at="20240101-000000")
        assert project_task(record, tmp_settings) == ""

    def test_projection_never_raises_on_hostile_input(self, tmp_settings, project: Path):
        """A malformed record must degrade, not raise, and not corrupt."""
        from core.task_store import GitTaskState

        record = TaskRecord(
            task_id="t", project_name="p", project_root=str(project),
            title="t", goal="g", created_at="20240101-000000",
            status="completed",
            # Deliberately wrong types in every optional field.
            change_steps=[TaskChangeStep(tool="x", args={"path": 12345})],
            git=GitTaskState(project_root=str(project), files_changed=None,
                             commits_made=[CommitInfo(hash="h", message="m", timestamp="t")]),
        )
        record.metadata = {"lifecycle": {"verification_ledger": {"entries": "not-a-list"}}}

        project_id = project_task(record, tmp_settings)
        assert project_id != ""

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(project_id)
        task_node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        # The non-string path produced no FILE link, and no bogus verification.
        # (CHANGED_BY legitimately also carries the GIT_COMMIT node.)
        changed = _targets(graph, task_node.node_id, RelationType.CHANGED_BY)
        assert [n for n in changed if n.node_type is NodeType.FILE] == []
        assert graph.find_nodes(node_type=NodeType.VERIFICATION, limit=5) == []
        graph.close()
        mgr.close_all()


# ------------------------------------------------------------------ authority


class TestAuthorityPreserved:
    def test_taskstore_record_is_untouched_by_projection(self, tmp_settings, project: Path):
        settings = tmp_settings
        store = TaskStore(settings.sessions_dir)
        record = _record(tmp_settings, project, change_paths=[project / "app.py"])
        store.create(record)
        before = store.get(record.task_id).to_dict()

        project_task(store.get(record.task_id), settings)

        after = store.get(record.task_id).to_dict()
        assert after == before

    def test_agent_lifecycle_still_completes(self, tmp_settings):
        """The projection hook must not disturb a real Agent request."""
        from ai.offline_provider import OfflineProvider
        from tests.test_phase4_execution import _agent

        agent = _agent(OfflineProvider(), tmp_settings)
        reply = agent.respond("hello")
        assert reply
        # A terminal task record still exists and is authoritative.
        task_id = agent.request_context.tasks.task_id
        assert task_id
        assert agent._task_store.get(task_id) is not None
