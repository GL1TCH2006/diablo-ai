"""M2.14 Phases C/E — live task, verification and Git projection.

The projection boundary
-----------------------
Everything in this module reads an **authoritative** record that an existing
subsystem already wrote, and writes derived nodes. It never decides anything:

* ``TaskStore`` owns task lifecycle and persistence. This module reads a
  ``TaskRecord``; it never creates, mutates or finishes a task.
* ``VerificationLedger`` owns verification adjudication. The states projected
  here are the ledger's own ``VerificationEntry.state`` values, read back out
  of the record's ``lifecycle.verification_ledger`` projection that
  ``Agent._sync_task_metadata`` already writes. There is no second verification
  store, and no path by which a graph node can turn a verdict into ``PASSED``.
* Git owns history. Commits come from ``TaskRecord.git``, which Git populated.

A verification node is therefore incapable of asserting success: it can only
copy what the ledger adjudicated, and an absent ledger means no node at all.
"""

from __future__ import annotations

from pathlib import Path

from config.permissions import path_is_within
from config.settings import Settings
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_project_key,
    canonical_task_key,
)
from core.knowledge_tasks import (
    link_task_to_changed_files,
    link_task_to_checkpoint,
    link_task_to_git_commits,
    link_task_to_plan,
    link_task_to_verification,
    plan_step_summary,
    upsert_plan_node,
    upsert_task_node,
    upsert_verification_node,
)
from core.logging_setup import get_logger
from core.task_store import TaskRecord
from core.verification_ledger import VerificationEntry, VerificationState

log = get_logger("knowledge_projection")

#: Basename patterns that make a module a test module. Deliberately narrow: a
#: TEST node must correspond to a real test, so anything unrecognised stays an
#: ordinary FILE rather than being promoted on a guess.
_TEST_BASENAME_PREFIXES = ("test_",)
_TEST_BASENAME_SUFFIXES = ("_test.py",)
_TEST_DIR_SEGMENTS = ("tests", "test")


def knowledge_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "knowledge"


def is_test_module(rel_path: str) -> bool:
    """Whether a project-relative path is recognisably a test module."""
    parts = [p for p in str(rel_path or "").replace("\\", "/").split("/") if p]
    if not parts:
        return False
    name = parts[-1].lower()
    if not name.endswith(".py"):
        return False
    if any(part.lower() in _TEST_DIR_SEGMENTS for part in parts[:-1]):
        return True
    if name.startswith(_TEST_BASENAME_PREFIXES):
        return True
    return name.endswith(_TEST_BASENAME_SUFFIXES)


def relative_to_project(raw_path: str, project_root: Path) -> str:
    """Normalise a possibly-absolute path to a project-relative POSIX path.

    Returns "" whenever the path does not denote a location inside the
    project. That covers three cases, and all three must refuse:

    * an absolute path outside the project,
    * a *relative* path containing ``..`` that climbs out,
    * a relative path that normalises to nothing.

    A relative path is resolved against the project root before the check, so
    ``../../etc/passwd`` cannot be laundered into a canonical key that merely
    looks project-scoped.
    """
    if not raw_path:
        return ""
    try:
        root = Path(project_root).resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            # Reject traversal lexically first: resolving first would silently
            # absorb "..", which is exactly what we must not do.
            if ".." in candidate.parts:
                return ""
            candidate = root / candidate
        resolved = candidate.resolve()
        rel = resolved.relative_to(root).as_posix()
        return "" if rel in (".", "") else rel
    except (ValueError, OSError):
        return ""


def authorized_project(settings: Settings, project_root: str) -> bool:
    """Whether a project root is inside an authorized workspace root."""
    if not project_root:
        return False
    try:
        return path_is_within(Path(project_root).resolve(), settings.allowed_roots)
    except (OSError, ValueError, TypeError):
        return False


def test_key(project_id: str, rel_path: str) -> str:
    return f"test:{project_id}:{rel_path}"


def _changed_paths(task: TaskRecord, project_root: Path) -> list[str]:
    """Every project-relative file path this task is known to have changed.

    Two authoritative sources, no inference:

    * ``TaskRecord.git.files_changed`` - Git porcelain status, already
      project-relative, owned by Git.
    * ``TaskRecord.change_steps[*].args['path']`` - the literal path the tool
      was invoked with, normalised to project-relative.

    ``code.patch_file`` carries a unified diff rather than a path, so it
    contributes nothing here unless Git reported the resulting file.
    """
    found: list[str] = []
    for raw in list(getattr(task.git, "files_changed", None) or []):
        rel = relative_to_project(str(raw), project_root)
        if rel:
            found.append(rel)
    for step in list(getattr(task, "change_steps", None) or []):
        raw = (getattr(step, "args", None) or {}).get("path")
        if not raw:
            continue
        rel = relative_to_project(str(raw), project_root)
        if rel:
            found.append(rel)
    # Stable order, no duplicates.
    return sorted(dict.fromkeys(found))


def _checkpoint_ids(task: TaskRecord) -> list[str]:
    """Checkpoint ids this task owns, from TaskRecord only."""
    ids: list[str] = []
    ids.extend(str(s) for s in (getattr(task, "snapshots", None) or []) if s)
    for step in list(getattr(task, "change_steps", None) or []):
        value = str(getattr(step, "checkpoint_id", "") or "")
        if value:
            ids.append(value)
    return sorted(dict.fromkeys(ids))


def _ledger_entries(task: TaskRecord) -> list[VerificationEntry]:
    """Authoritative ledger entries as persisted on the task record.

    Read from the ``lifecycle.verification_ledger`` projection that
    ``Agent._sync_task_metadata`` already writes. If it is absent, the ledger
    has nothing recorded and no verification node is created.
    """
    metadata = getattr(task, "metadata", None) or {}
    lifecycle = metadata.get("lifecycle") if isinstance(metadata, dict) else None
    if not isinstance(lifecycle, dict):
        return []
    ledger = lifecycle.get("verification_ledger")
    if not isinstance(ledger, dict):
        return []
    entries = ledger.get("entries")
    if not isinstance(entries, (list, tuple)):
        return []
    out: list[VerificationEntry] = []
    for raw in entries:
        if isinstance(raw, dict):
            out.append(VerificationEntry.from_dict(raw))
    return out


def upsert_test_node(graph, rel_path: str, project_id: str) -> str:
    """Create or update a TEST node for a recognised test module.

    A TEST node is only ever created for a file that is actually a test, and
    the node records what it covers rather than asserting a result. A test that
    has not been run has no verdict; that lives in the ledger, not here.
    """
    key = test_key(project_id, rel_path)
    name = rel_path.replace("\\", "/").split("/")[-1]
    metadata = {
        "path": rel_path,
        "kind": "test_module",
        # Explicitly not a verdict: this node says what a test is, not that it
        # passed. Verification remains the ledger's exclusive authority.
        "result_state": "",
    }
    return graph.upsert_node(NodeType.TEST, key, name, metadata).node_id


def project_task(
    task: TaskRecord,
    settings: Settings,
    manager: KnowledgeGraphManager | None = None,
) -> str:
    """Project one authoritative ``TaskRecord`` into the knowledge graph.

    Returns the project_id written, or "" when the projection was refused
    (unauthorized project, or no resolvable project root). Never raises: a
    projection failure must not disturb the task it is describing.
    """
    if task is None:
        return ""
    project_root = str(getattr(task, "project_root", "") or "")
    if not authorized_project(settings, project_root):
        log.info("knowledge projection refused for %s: project not authorized", getattr(task, "task_id", "?"))
        return ""

    project_id = canonical_project_key(project_root)
    mgr = manager or KnowledgeGraphManager(knowledge_dir(settings))
    try:
        graph = mgr.get_graph(project_id)

        task_node_id = upsert_task_node(graph, task)

        # Plan: bounded summary only. ExecutionPlanRecord stays authoritative.
        plan = getattr(task, "plan", None)
        if plan is not None and getattr(plan, "steps", None):
            plan_node_id = upsert_plan_node(graph, plan, task.task_id)
            link_task_to_plan(graph, task_node_id, plan_node_id)

        # Verification: copied from the ledger's own projection.
        for entry in _ledger_entries(task):
            verif_node_id = upsert_verification_node(graph, task.task_id, entry)
            link_task_to_verification(graph, task_node_id, verif_node_id)

        # Files: TEST nodes for test modules, otherwise plain FILE links.
        for rel in _changed_paths(task, Path(project_root)):
            file_node = graph.get_node(canonical_file_key(project_id, rel))
            if file_node is None:
                # The file was never indexed; do not invent a node for it.
                continue
            if is_test_module(rel):
                test_node_id = upsert_test_node(graph, rel, project_id)
                graph.upsert_edge(test_node_id, file_node.node_id, RelationType.PART_OF)
                graph.upsert_edge(task_node_id, test_node_id, RelationType.PRODUCED)
            link_task_to_changed_files(graph, task_node_id, [rel], project_id)

        # Checkpoints and Git commits: straight from the authoritative record.
        for checkpoint_id in _checkpoint_ids(task):
            link_task_to_checkpoint(graph, task_node_id, checkpoint_id)
        link_task_to_git_commits(graph, task_node_id, task)

        log.info(
            "projected task %s into %s (plan=%s, verifications=%d)",
            task.task_id, project_id, bool(plan and getattr(plan, "steps", None)),
            len(_ledger_entries(task)),
        )
        return project_id
    except Exception as exc:  # projection must never break the task lifecycle
        log.warning("knowledge projection failed for %s: %s", getattr(task, "task_id", "?"), exc)
        return ""


def project_task_verification_state(
    graph,
    task_node_id: str,
    task_id: str,
    state,
    seq: int,
) -> str | None:
    """Project a single adjudicated verification state.

    The state passed in must already have been adjudicated by the ledger; this
    function does not adjudicate and cannot upgrade anything.
    """
    try:
        from core.verification_ledger import (
            EvidenceTrust,
            VerificationMethod,
            VerificationSubject,
        )

        entry = VerificationEntry.create(
            state=state,
            subject_kind=VerificationSubject.TASK,
            method_kind=VerificationMethod.EXECUTION_OUTCOME,
            trust=EvidenceTrust.OBSERVED,
            detail="Projected from the authoritative verification ledger.",
            evidence={"task_id": task_id, "seq": int(seq)},
            legacy_kind="execution",
            task_id=task_id,
        )
        node_id = upsert_verification_node(graph, task_id, entry)
        link_task_to_verification(graph, task_node_id, node_id)
        return node_id
    except Exception as exc:
        log.warning("could not project verification state for %s: %s", task_id, exc)
        return None


# --------------------------------------------------------------------- Phase D
#
# data.* and chart.* are not ``_is_coding_tool`` tools, so ``_note_step`` never
# sees them. The live seam is ``Agent._record_execution``, which already has
# the authoritative tool name, the authoritative invocation args, the observed
# result data and the request's task identity. Nothing here re-runs a tool or
# re-implements analysis: this only reads what already happened.

#: Tools whose observed result is projected as dataset knowledge.
_DATASET_TOOLS = ("data.inspect", "data.load_csv", "data.load", "data.load_json")

#: Tools whose observed result is projected as analysis knowledge.
_ANALYZE_TOOLS = ("data.analyze",)

#: Tools whose observed result is projected as chart/artifact knowledge.
_CHART_TOOLS = ("chart.generate",)
_CHART_VERIFY_TOOLS = ("chart.verify",)

#: The analysis operations the ``data.analyze`` tool advertises. A projection
#: only records an operation from this list, so a model cannot invent an
#: unbounded parameter set by naming something else.
_ANALYZE_OPERATIONS = frozenset(
    {"stats", "groupby", "filter", "sort", "correlation", "unique", "head"}
)

#: How much of a model-supplied filter/sort expression is kept. The expression
#: is a description of a question, not the answer, so a short prefix is enough
#: to make the analysis findable later.
MAX_EXPR_LEN = 120


#: Longest argument value the projection will read. Generous enough for a deep
#: but legitimate project path.
MAX_ARG_LEN = 1024


def _bounded_arg(args, name: str, max_len: int = MAX_ARG_LEN) -> str:
    """Read one named argument, bounded.

    Only the specific argument the projection needs is ever read; raw args are
    never stored wholesale, because a model can put anything in an argument.

    An over-long value is **rejected, not truncated**. Truncating a path yields
    a different path that may name a real but unrelated location, which is
    worse than recording nothing.
    """
    value = (args or {}).get(name)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > max_len:
        return ""
    return value


def project_tool_result(
    tool: str,
    args: dict,
    result_data: dict | None,
    task_id: str,
    project_root: str,
    settings: Settings,
    manager: KnowledgeGraphManager | None = None,
) -> str:
    """Project an observed data/chart tool result into the knowledge graph.

    Returns the project_id written, or "" when nothing was projected. Never
    raises: a projection problem must not affect the execution that produced
    the result.
    """
    if not result_data or not isinstance(result_data, dict):
        return ""
    name = str(tool or "")
    if name not in _DATASET_TOOLS + _ANALYZE_TOOLS + _CHART_TOOLS + _CHART_VERIFY_TOOLS:
        return ""
    if not authorized_project(settings, project_root):
        return ""

    project_id = canonical_project_key(project_root)
    mgr = manager or KnowledgeGraphManager(knowledge_dir(settings))
    try:
        graph = mgr.get_graph(project_id)
        from core.knowledge_data import (
            sync_chart_generation,
            sync_chart_verification,
            sync_data_analysis,
            sync_dataset_inspection,
        )

        # The TASK node is the anchor for TASK -> PRODUCED -> ...
        task_node = graph.get_node(canonical_task_key(task_id))
        if task_node is None:
            # Task not projected yet. Project the bare dataset/chart without a
            # task edge rather than minting a task node here: identity is the
            # Agent's to supply, and it always does via _note_step/_finish_tracking.
            task_node = None

        if name in _DATASET_TOOLS:
            # Authoritative path comes from the invocation args, never from
            # model text or a reconstructed filename.
            dataset_path = _bounded_arg(args, "path")
            if not dataset_path:
                return ""
            rel = relative_to_project(dataset_path, Path(project_root))
            if not rel:
                return ""
            if task_node is not None:
                sync_dataset_inspection(graph, task_node.node_id, rel, result_data)
            else:
                upsert_dataset_only(graph, rel, result_data)
            return project_id

        if name in _ANALYZE_TOOLS:
            # An analysis is a question asked of a dataset, so it is recorded as
            # ANALYSIS, not as another view of the DATASET itself.
            dataset_path = _bounded_arg(args, "path")
            if not dataset_path:
                return ""
            rel = relative_to_project(dataset_path, Path(project_root))
            if not rel:
                return ""
            operation = _bounded_arg(args, "operation", max_len=32)
            if operation not in _ANALYZE_OPERATIONS:
                # Not a recognised operation: record nothing rather than an
                # analysis the system cannot interpret.
                return ""
            extra: dict = {}
            for arg_name in ("group_by", "column", "filter_expr", "sort_by"):
                value = _bounded_arg(args, arg_name, max_len=MAX_EXPR_LEN)
                if value:
                    extra[arg_name] = value
            sync_data_analysis(
                graph,
                task_node.node_id if task_node is not None else "",
                rel,
                operation,
                _bounded_arg(args, "column", max_len=MAX_EXPR_LEN),
                result_data,
                extra,
            )
            return project_id

        if name in _CHART_TOOLS:
            chart_path = str(result_data.get("path", "") or "")
            if not chart_path:
                return ""
            # The source dataset is an invocation argument, not part of the
            # result payload.
            dataset_arg = _bounded_arg(args, "data_path")
            dataset_rel = relative_to_project(dataset_arg, Path(project_root)) if dataset_arg else ""
            sync_chart_generation(
                graph,
                task_node.node_id if task_node is not None else "",
                chart_path,
                str(result_data.get("chart_type", "") or ""),
                dataset_rel or None,
                result_data,
            )
            return project_id

        # chart.verify
        chart_path = str(result_data.get("path", "") or _bounded_arg(args, "path"))
        if not chart_path:
            return ""
        evidence = result_data.get("evidence")
        verified = bool(result_data.get("verified")) or bool(
            isinstance(evidence, dict) and evidence.get("valid") is True
        )
        sync_chart_verification(
            graph, chart_path, verified, evidence if isinstance(evidence, dict) else {}
        )
        return project_id
    except Exception as exc:
        log.warning("tool result projection failed for %s: %s", name, exc)
        return ""


def upsert_dataset_only(graph, rel_path: str, result_data: dict) -> str:
    """Record a dataset observation with no task anchor."""
    from core.knowledge_data import DatasetInfo, normalize_dataset_result, upsert_dataset_node

    shape = normalize_dataset_result(result_data or {})
    info = DatasetInfo(
        path=rel_path,
        format=shape["format"],
        columns=shape["columns"],
        row_count=shape["row_count"],
        column_types=shape["column_types"],
    )
    return upsert_dataset_node(graph, info)
