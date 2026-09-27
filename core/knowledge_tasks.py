"""M2.14 Phase 4 — Task/Plan/Verification Graph Integration.

Connects existing durable execution information to the knowledge graph.
Does NOT duplicate TaskStore, TaskManager, or VerificationLedger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pathlib import Path

from core.knowledge_graph import (
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_task_key,
    canonical_plan_key,
    canonical_file_key,
    canonical_artifact_key,
)
from core.task_store import TaskRecord, ExecutionPlanRecord
from core.planner import PlanStepRecord
from core.verification_ledger import VerificationEntry
from core.logging_setup import get_logger

log = get_logger("knowledge_tasks")


@dataclass
class TaskGraphLinks:
    """Result of linking a task to the knowledge graph."""
    task_node_id: str
    plan_node_id: str | None = None
    artifact_node_ids: list[str] = None
    verification_node_id: str | None = None
    checkpoint_node_id: str | None = None

    def __post_init__(self):
        if self.artifact_node_ids is None:
            self.artifact_node_ids = []


def upsert_task_node(graph: KnowledgeGraph, task: TaskRecord) -> str:
    """Create or update a TASK node from a TaskRecord.

    Returns the task node_id.
    """
    task_key = canonical_task_key(task.task_id)
    metadata = {
        "project_name": task.project_name,
        "project_root": task.project_root,
        "title": task.title,
        "goal": task.goal,
        "status": task.status,
        "outcome": task.outcome or "",
        "created_at": task.created_at,
        "started_at": task.started_at or "",
        "finished_at": task.finished_at or "",
        "run_id": getattr(task, "run_id", ""),
    }
    # Include git info if present
    if task.git:
        metadata["git_starting_commit"] = task.git.starting_commit or ""
        metadata["git_ending_commit"] = task.git.ending_commit or ""
        metadata["git_commits_count"] = len(task.git.commits_made or [])

    node = graph.upsert_node(NodeType.TASK, task_key, task.title, metadata)
    return node.node_id


#: Upper bound on how many plan steps the graph summarises. The plan itself
#: stays authoritative and complete in ``TaskRecord.plan``; the graph keeps
#: only enough shape for a reader to answer "how big was this plan, and did it
#: finish" without copying the durable record.
PLAN_STEP_SUMMARY_LIMIT = 40


def plan_step_summary(plan: ExecutionPlanRecord, limit: int = PLAN_STEP_SUMMARY_LIMIT) -> dict:
    """A bounded, non-authoritative digest of an ``ExecutionPlanRecord``.

    M2.14 decision: a plan step is **not** a graph node. ``ExecutionPlanRecord``
    already owns step identity, state, attempts, dependencies, recovery and
    transitions, and duplicating that into the graph would create a second,
    weaker source of truth. Step information therefore appears only as this
    summary on the PLAN node, and never as a ``STEP`` node or a ``TEST`` node.

    A ``TEST`` node is reserved for an actual test relationship, so labelling a
    plan step as one would make "this project has no tests" indistinguishable
    from "tests are not modelled here".
    """
    steps = list(getattr(plan, "steps", []) or [])
    shown = steps[:limit]
    counts: dict[str, int] = {}
    for step in steps:
        key = step.status.value if hasattr(step.status, "value") else str(step.status)
        counts[key] = counts.get(key, 0) + 1
    return {
        "step_count": len(steps),
        "truncated": len(steps) > limit,
        "status_counts": counts,
        "complete": bool(steps) and all(step.is_terminal for step in steps),
        # tool + status + step_id only: the minimum needed to describe shape.
        # No args, no results, no checkpoints: ExecutionPlanRecord owns those.
        "steps": [
            {
                "step_id": step.step_id,
                "tool": step.tool,
                "status": step.status.value if hasattr(step.status, "value") else str(step.status),
            }
            for step in shown
        ],
    }


def upsert_plan_node(graph: KnowledgeGraph, plan: ExecutionPlanRecord, task_id: str) -> str:
    """Create or update a PLAN node from an ExecutionPlanRecord.

    Returns the plan node_id.
    """
    plan_key = canonical_plan_key(f"{task_id}:{plan.created_at}")
    summary = plan_step_summary(plan)
    metadata = {
        "task_id": task_id,
        "goal": plan.goal,
        "plan_version": plan.plan_version,
        "created_at": plan.created_at,
        "step_count": summary["step_count"],
        "completed_count": sum(1 for s in plan.steps if s.is_terminal),
        "superseded_count": len(plan.superseded or []),
        "complete": summary["complete"],
        # Bounded digest, not a copy of the plan.
        "step_summary": summary,
    }
    node = graph.upsert_node(NodeType.PLAN, plan_key, f"Plan for {task_id}", metadata)
    return node.node_id


def upsert_verification_node(graph: KnowledgeGraph, task_id: str, entry: VerificationEntry) -> str:
    """Create or update a VERIFICATION node from a VerificationEntry.

    Returns the verification node_id.
    """
    verif_key = f"verification:{task_id}:{entry.seq}"
    metadata = {
        "task_id": task_id,
        "state": entry.state.value,
        "subject_kind": entry.subject_kind.value,
        "method_kind": entry.method_kind.value,
        "trust": entry.trust.value,
        "detail": entry.detail,
        "evidence": entry.evidence,
        "legacy_kind": entry.legacy_kind,
        "requested_state": entry.requested_state.value if entry.requested_state else "",
        "reason": entry.reason,
        "run_id": entry.run_id,
        "request_id": entry.request_id,
        "step_id": entry.step_id,
        "recorded_at": entry.recorded_at,
    }
    node = graph.upsert_node(NodeType.VERIFICATION, verif_key, f"Verification {entry.seq}", metadata)
    return node.node_id


def link_task_to_plan(graph: KnowledgeGraph, task_node_id: str, plan_node_id: str) -> None:
    """Link TASK -> EXECUTED_BY -> PLAN."""
    graph.upsert_edge(task_node_id, plan_node_id, RelationType.EXECUTED_BY)


def link_plan_to_artifacts(graph: KnowledgeGraph, plan_node_id: str, artifact_paths: list[str]) -> list[str]:
    """Link PLAN -> PRODUCED -> ARTIFACT for each artifact path.

    Returns list of artifact node_ids.
    """
    artifact_ids = []
    for path in artifact_paths:
        art_key = canonical_artifact_key(path)
        art_node = graph.upsert_node(NodeType.ARTIFACT, art_key, Path(path).name, {"path": path})
        graph.upsert_edge(plan_node_id, art_node.node_id, RelationType.PRODUCED)
        artifact_ids.append(art_node.node_id)
    return artifact_ids


def link_task_to_verification(graph: KnowledgeGraph, task_node_id: str, verification_node_id: str) -> None:
    """Link TASK -> VERIFIED_BY -> VERIFICATION."""
    graph.upsert_edge(task_node_id, verification_node_id, RelationType.VERIFIED_BY)


def link_verification_to_evidence(graph: KnowledgeGraph, verification_node_id: str, evidence_node_id: str) -> None:
    """Link VERIFICATION -> VERIFIED_BY -> evidence node (FILE, ARTIFACT, etc.)."""
    graph.upsert_edge(verification_node_id, evidence_node_id, RelationType.VERIFIED_BY)


def link_task_to_checkpoint(graph: KnowledgeGraph, task_node_id: str, checkpoint_id: str, label: str = "") -> str:
    """Link TASK -> CHECKPOINTED_AS -> CHECKPOINT.

    Returns the checkpoint node_id.
    """
    cp_key = f"checkpoint:{task_node_id}:{checkpoint_id}"
    metadata = {"checkpoint_id": checkpoint_id, "label": label}
    cp_node = graph.upsert_node(NodeType.CHECKPOINT, cp_key, label or checkpoint_id, metadata)
    graph.upsert_edge(task_node_id, cp_node.node_id, RelationType.CHECKPOINTED_AS)
    return cp_node.node_id


def link_task_to_changed_files(graph: KnowledgeGraph, task_node_id: str, file_paths: list[str], project_id: str) -> int:
    """Link TASK -> CHANGED_BY -> FILE for each file path.

    Returns number of links created.
    """
    count = 0
    for rel_path in file_paths:
        file_key = canonical_file_key(project_id, rel_path)
        file_node = graph.get_node(file_key)
        if file_node:
            graph.upsert_edge(task_node_id, file_node.node_id, RelationType.CHANGED_BY)
            count += 1
    return count


def link_task_to_git_commits(graph: KnowledgeGraph, task_node_id: str, task: TaskRecord) -> int:
    """Link TASK -> CHANGED_BY -> GIT_COMMIT for each commit.

    Returns number of links created.
    """
    if not task.git or not task.git.commits_made:
        return 0

    # Use project_name as project identifier for commit keys
    proj_id = task.project_name or "unknown"
    count = 0
    for commit in task.git.commits_made:
        commit_key = f"git_commit:{proj_id}:{commit.hash}"
        metadata = {
            "hash": commit.hash,
            "message": commit.message,
            "timestamp": commit.timestamp,
            "task_id": task.task_id,
        }
        commit_node = graph.upsert_node(NodeType.GIT_COMMIT, commit_key, commit.hash[:8], metadata)
        graph.upsert_edge(task_node_id, commit_node.node_id, RelationType.CHANGED_BY)
        count += 1
    return count


def sync_task_to_graph(graph: KnowledgeGraph, task: TaskRecord) -> TaskGraphLinks:
    """Full sync of a TaskRecord to the knowledge graph.

    Creates/updates TASK, PLAN, VERIFICATION nodes and links them.
    """
    # 1. Task node
    task_node_id = upsert_task_node(graph, task)

    links = TaskGraphLinks(task_node_id=task_node_id)

    # 2. Plan node (if exists)
    if task.plan and task.plan.steps:
        plan_node_id = upsert_plan_node(graph, task.plan, task.task_id)
        links.plan_node_id = plan_node_id
        link_task_to_plan(graph, task_node_id, plan_node_id)

        # No step nodes. See plan_step_summary: ExecutionPlanRecord remains the
        # only durable representation of step state.

    # 3. Verification nodes (from ledger projection in metadata)
    if task.metadata and "lifecycle" in task.metadata:
        lifecycle = task.metadata["lifecycle"]
        if "verification_ledger" in lifecycle:
            ledger_data = lifecycle["verification_ledger"]
            if "entries" in ledger_data:
                for entry_data in ledger_data["entries"]:
                    entry = VerificationEntry.from_dict(entry_data)
                    verif_node_id = upsert_verification_node(graph, task.task_id, entry)
                    link_task_to_verification(graph, task_node_id, verif_node_id)
                    links.verification_node_id = verif_node_id

    # 4. Changed files from change_steps
    if task.change_steps:
        file_paths = [step.args.get("path") for step in task.change_steps if step.args.get("path")]
        file_paths = [p for p in file_paths if p]
        link_task_to_changed_files(graph, task_node_id, file_paths, graph.project_id)

    # 5. Git commits
    link_task_to_git_commits(graph, task_node_id, task)

    log.info("Synced task %s to graph: %d links", task.task_id, len(links.artifact_node_ids))
    return links


def sync_all_tasks(graph: KnowledgeGraph, task_store) -> int:
    """Sync all tasks for this project to the knowledge graph.

    Returns number of tasks synced.
    """
    tasks = task_store.list_by_project(graph.project_id)
    count = 0
    for task in tasks:
        try:
            sync_task_to_graph(graph, task)
            count += 1
        except Exception as exc:
            log.warning("Failed to sync task %s: %s", task.task_id, exc)
    return count