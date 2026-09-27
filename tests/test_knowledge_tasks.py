"""M2.14 Phase 4 — Task/Plan/Verification Graph Integration tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_task_key,
    canonical_plan_key,
    canonical_file_key,
    canonical_artifact_key,
)
from core.knowledge_tasks import (
    upsert_task_node,
    upsert_plan_node,
    plan_step_summary,
    PLAN_STEP_SUMMARY_LIMIT,
    upsert_verification_node,
    link_task_to_plan,
    link_plan_to_artifacts,
    link_task_to_verification,
    link_verification_to_evidence,
    link_task_to_checkpoint,
    link_task_to_changed_files,
    link_task_to_git_commits,
    sync_task_to_graph,
    TaskGraphLinks,
)
from core.task_store import TaskRecord, GitTaskState, CommitInfo, TaskChangeStep
from core.planner import ExecutionPlanRecord, PlanStepRecord, StepStatus
from core.verification_ledger import VerificationEntry, VerificationState, VerificationSubject, VerificationMethod, EvidenceTrust


@pytest.fixture
def graph(tmp_path: Path):
    from core.knowledge_graph import KnowledgeGraph
    g = KnowledgeGraph(tmp_path, "test-proj")
    yield g
    g.close()


@pytest.fixture
def sample_task(tmp_path: Path):
    return TaskRecord(
        task_id="task-123",
        project_name="test-proj",
        project_root=str(tmp_path),
        title="Test Task",
        goal="Test goal",
        created_at=time.strftime("%Y%m%d-%H%M%S"),
        status="active",
        git=GitTaskState(
            project_root=str(tmp_path),
            starting_commit="abc123",
            starting_branch="main",
            commits_made=[
                CommitInfo(hash="abc123", message="Initial", timestamp="20240101-120000"),
                CommitInfo(hash="def456", message="Add feature", timestamp="20240101-120001"),
            ],
            files_changed=["src/main.py", "src/utils.py"],
        ),
        change_steps=[
            TaskChangeStep(
                tool="filesystem.write_file",
                args={"path": "src/main.py", "content": "print('hi')"},
                allowed=True,
                executed=True,
                success=True,
            ),
        ],
    )


@pytest.fixture
def sample_plan():
    steps = [
        PlanStepRecord(
            step_id="step-1",
            tool="filesystem.write_file",
            args={"path": "src/main.py"},
            status=StepStatus.DONE,
            started_at="20240101-120000",
            finished_at="20240101-120001",
        ),
        PlanStepRecord(
            step_id="step-2",
            tool="python.run_script",
            args={"script": "src/main.py"},
            status=StepStatus.PENDING,
        ),
    ]
    return ExecutionPlanRecord(
        goal="Create and run main.py",
        created_at="20240101-120000",
        steps=steps,
    )


@pytest.fixture
def sample_verification():
    return VerificationEntry.create(
        state=VerificationState.PASSED,
        subject_kind=VerificationSubject.FILE,
        method_kind=VerificationMethod.FILE_AND_OUTPUT,
        trust=EvidenceTrust.OBSERVED,
        detail="File exists and runs",
        evidence={"path": "src/main.py", "exists": True, "exit_code": 0},
        legacy_kind="file_and_output",
        task_id="task-123",
        run_id="run-123",
    )


class TestTaskNode:
    def test_upsert_task_node(self, graph: KnowledgeGraph, sample_task: TaskRecord):
        node_id = upsert_task_node(graph, sample_task)
        assert node_id.startswith("n_")

        task_key = canonical_task_key("task-123")
        node = graph.get_node(task_key)
        assert node is not None
        assert node.node_type == NodeType.TASK
        assert node.display_name == "Test Task"
        assert node.metadata["project_name"] == "test-proj"
        assert node.metadata["status"] == "active"
        assert node.metadata["git_commits_count"] == 2


class TestPlanNode:
    def test_upsert_plan_node(self, graph: KnowledgeGraph, sample_plan: ExecutionPlanRecord):
        node_id = upsert_plan_node(graph, sample_plan, "task-123")
        assert node_id.startswith("n_")

        plan_key = canonical_plan_key("task-123:20240101-120000")
        node = graph.get_node(plan_key)
        assert node is not None
        assert node.node_type == NodeType.PLAN
        assert node.metadata["task_id"] == "task-123"
        assert node.metadata["step_count"] == 2
        assert node.metadata["completed_count"] == 1

    def test_plan_steps_are_not_graph_nodes(self, graph: KnowledgeGraph, sample_plan: ExecutionPlanRecord):
        """M2.14 decision: a plan step is not a node, and not a TEST.

        ``ExecutionPlanRecord`` owns step state durably. Materialising steps in
        the graph would duplicate that authority, and typing them as TEST would
        make "no tests exist" indistinguishable from "tests not modelled".
        """
        plan_id = upsert_plan_node(graph, sample_plan, "task-123")

        # No node of any kind was created for the steps.
        assert graph.get_edges(source_id=plan_id, relation_type=RelationType.CONTAINS) == []
        all_nodes = graph.find_nodes(limit=100)
        assert [n for n in all_nodes if n.node_type is NodeType.TEST] == []
        assert [n for n in all_nodes if n.node_type is NodeType.PLAN] != []

    def test_plan_step_summary_is_bounded_and_carries_no_step_payload(
        self, graph: KnowledgeGraph, sample_plan: ExecutionPlanRecord
    ):
        plan_id = upsert_plan_node(graph, sample_plan, "task-123")
        node = graph.get_node_by_id(plan_id)
        summary = node.metadata["step_summary"]

        assert summary["step_count"] == 2
        assert summary["complete"] is False
        assert summary["status_counts"]["done"] == 1
        assert summary["status_counts"]["pending"] == 1
        # step_id / tool / status only: no args, no results, no checkpoints.
        assert set(summary["steps"][0]) == {"step_id", "tool", "status"}
        assert "args" not in summary["steps"][0]
        assert "result_summary" not in summary["steps"][0]

    def test_plan_step_summary_is_truncated_for_a_very_large_plan(
        self, graph: KnowledgeGraph
    ):
        from core.planner import ExecutionPlanRecord, PlanStepRecord

        steps = [
            PlanStepRecord(step_id=f"step-{i}", tool="filesystem.read_file", args={})
            for i in range(PLAN_STEP_SUMMARY_LIMIT + 25)
        ]
        plan = ExecutionPlanRecord(goal="big", created_at="20240101-000000", steps=steps)
        summary = plan_step_summary(plan)
        assert summary["step_count"] == PLAN_STEP_SUMMARY_LIMIT + 25
        assert summary["truncated"] is True
        assert len(summary["steps"]) == PLAN_STEP_SUMMARY_LIMIT
        # The true count survives even though the step list is clipped.
        assert summary["status_counts"]["pending"] == PLAN_STEP_SUMMARY_LIMIT + 25


class TestVerificationNode:
    def test_upsert_verification_node(self, graph: KnowledgeGraph, sample_verification: VerificationEntry):
        node_id = upsert_verification_node(graph, "task-123", sample_verification)
        assert node_id.startswith("n_")

        verif_key = f"verification:task-123:{sample_verification.seq}"
        node = graph.get_node(verif_key)
        assert node is not None
        assert node.node_type == NodeType.VERIFICATION
        assert node.metadata["state"] == "passed"
        assert node.metadata["subject_kind"] == "file"
        assert node.metadata["method_kind"] == "file_and_output"
        assert node.metadata["trust"] == "observed"


class TestLinking:
    def test_link_task_to_plan(self, graph: KnowledgeGraph, sample_task: TaskRecord, sample_plan: ExecutionPlanRecord):
        task_id = upsert_task_node(graph, sample_task)
        plan_id = upsert_plan_node(graph, sample_plan, "task-123")

        link_task_to_plan(graph, task_id, plan_id)

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.EXECUTED_BY)
        assert len(edges) == 1
        assert edges[0].target_id == plan_id

    def test_link_plan_to_artifacts(self, graph: KnowledgeGraph, sample_plan: ExecutionPlanRecord):
        plan_id = upsert_plan_node(graph, sample_plan, "task-123")
        artifact_ids = link_plan_to_artifacts(graph, plan_id, ["/tmp/out1.txt", "/tmp/out2.png"])

        assert len(artifact_ids) == 2

        edges = graph.get_edges(source_id=plan_id, relation_type=RelationType.PRODUCED)
        assert len(edges) == 2

    def test_link_task_to_verification(self, graph: KnowledgeGraph, sample_task: TaskRecord, sample_verification: VerificationEntry):
        task_id = upsert_task_node(graph, sample_task)
        verif_id = upsert_verification_node(graph, "task-123", sample_verification)

        link_task_to_verification(graph, task_id, verif_id)

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.VERIFIED_BY)
        assert len(edges) == 1
        assert edges[0].target_id == verif_id

    def test_link_verification_to_evidence(self, graph: KnowledgeGraph, sample_verification: VerificationEntry):
        verif_id = upsert_verification_node(graph, "task-123", sample_verification)

        # Create evidence file node
        file_key = canonical_file_key("test-proj", "src/main.py")
        file_id = graph.upsert_node(NodeType.FILE, file_key, "main.py", {"path": "src/main.py"}).node_id

        link_verification_to_evidence(graph, verif_id, file_id)

        edges = graph.get_edges(source_id=verif_id, relation_type=RelationType.VERIFIED_BY)
        assert len(edges) == 1
        assert edges[0].target_id == file_id

    def test_link_task_to_checkpoint(self, graph: KnowledgeGraph, sample_task: TaskRecord):
        task_id = upsert_task_node(graph, sample_task)

        cp_id = link_task_to_checkpoint(graph, task_id, "snap-123", "pre-write")

        assert cp_id.startswith("n_")

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.CHECKPOINTED_AS)
        assert len(edges) == 1
        cp_node = graph.get_node_by_id(edges[0].target_id)
        assert cp_node.metadata["checkpoint_id"] == "snap-123"
        assert cp_node.metadata["label"] == "pre-write"

    def test_link_task_to_changed_files(self, graph: KnowledgeGraph, sample_task: TaskRecord):
        task_id = upsert_task_node(graph, sample_task)

        # Pre-create file nodes
        for rel in ["src/main.py", "src/utils.py"]:
            fk = canonical_file_key("test-proj", rel)
            graph.upsert_node(NodeType.FILE, fk, rel.split("/")[-1], {"path": rel})

        count = link_task_to_changed_files(graph, task_id, ["src/main.py", "src/utils.py", "nonexistent.py"], "test-proj")
        assert count == 2

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.CHANGED_BY)
        # Should have 2 from change_steps + 2 from this = 4
        file_edges = [e for e in edges if graph.get_node_by_id(e.target_id).node_type == NodeType.FILE]
        assert len(file_edges) >= 2

    def test_link_task_to_git_commits(self, graph: KnowledgeGraph, sample_task: TaskRecord):
        task_id = upsert_task_node(graph, sample_task)

        count = link_task_to_git_commits(graph, task_id, sample_task)
        assert count == 2

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.CHANGED_BY)
        commit_edges = [e for e in edges if graph.get_node_by_id(e.target_id).node_type == NodeType.GIT_COMMIT]
        assert len(commit_edges) == 2


class TestFullSync:
    def test_sync_task_to_graph(self, graph: KnowledgeGraph, sample_task: TaskRecord, sample_plan: ExecutionPlanRecord, sample_verification: VerificationEntry):
        # Add plan and verification to task
        sample_task.plan = sample_plan
        sample_task.metadata = {
            "lifecycle": {
                "verification_ledger": {
                    "entries": [sample_verification.to_dict()]
                }
            }
        }

        links = sync_task_to_graph(graph, sample_task)

        assert links.task_node_id is not None
        assert links.plan_node_id is not None
        assert links.verification_node_id is not None

        # Verify all nodes exist
        assert graph.get_node(canonical_task_key("task-123")) is not None
        assert graph.get_node(canonical_plan_key("task-123:20240101-120000")) is not None
        assert graph.get_node(f"verification:task-123:{sample_verification.seq}") is not None


class TestIdempotency:
    def test_upsert_idempotent(self, graph: KnowledgeGraph, sample_task: TaskRecord):
        id1 = upsert_task_node(graph, sample_task)
        id2 = upsert_task_node(graph, sample_task)
        assert id1 == id2

        # Verify no duplicate edges
        edges = graph.get_edges(source_id=id1, relation_type=RelationType.EXECUTED_BY)
        # First sync doesn't add EXECUTED_BY without plan
        assert len(edges) == 0