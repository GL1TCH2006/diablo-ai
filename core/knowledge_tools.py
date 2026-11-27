"""M2.14 Phase 7 — knowledge graph presentation/tools layer.

Two rules govern this module, and both come from the existing architecture
rather than from M2.14:

1. **The knowledge graph is a projection, never an authority.** Nothing here
   adjudicates a verification verdict, drives task lifecycle, or grants a
   permission. A knowledge record is *reference data*; observed evidence stays
   authoritative in :class:`~core.verification_ledger.VerificationLedger` and
   :class:`~core.task_store.TaskStore` regardless of what this module reports.

2. **Tools close over ``settings`` only.** ``build_registry`` is called from
   ``main.py`` with ``settings`` and no ``Agent``, so a tool here cannot capture
   the agent. Project identity is therefore resolved at *call* time from the
   path the caller names, exactly the way ``filesystem.read_file`` resolves its
   own target. This keeps the seam identical to existing tools instead of
   inventing a new injection mechanism.

Every query is bounded (``MAX_RESULTS``, ``MAX_TRAVERSAL_DEPTH``) and confined
to a project the caller is authorized to see.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from config.permissions import path_is_within
from config.settings import Settings
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_project_key,
    shared_manager,
    stale_state,
)
from core.knowledge_ingest import ingest_project_structure
from core.knowledge_query import KnowledgeQueryEngine
from core.logging_setup import get_logger
from tools.registry import ToolResult

log = get_logger("knowledge_tools")

#: Query types this tool accepts. An explicit allowlist keeps the model from
#: reaching arbitrary attribute access on the graph, and keeps the presented
#: schema honest about what actually exists.
QUERY_TYPES = (
    "find_node",
    "find_nodes",
    "neighbors",
    "find_related",
    "path",
    "search_files",
    "search_tasks",
    "search_artifacts",
    "search_datasets",
    "search_charts",
    "recent_activity",
    "find_stale",
)


def knowledge_dir(settings: Settings) -> Path:
    """Where per-project knowledge graphs live. Derived from settings only."""
    return Path(settings.data_dir) / "knowledge"


def manager(settings: Settings) -> KnowledgeGraphManager:
    """A manager bound to this installation's knowledge directory.

    Shared per directory, so a run reuses one connection cache. Opening a fresh
    manager per call would leave a SQLite handle open every time, and on Windows
    an open handle makes the database undeletable.
    """
    return shared_manager(knowledge_dir(settings))


def _authorized(settings: Settings, path: str) -> bool:
    """Whether ``path`` is inside an authorized workspace root.

    Reuses the same containment check the filesystem tools use, so knowledge
    access can never be broader than direct file access.
    """
    try:
        candidate = Path(path).resolve()
    except (OSError, TypeError, ValueError):
        return False
    return path_is_within(candidate, settings.allowed_roots)


def _resolve_project(settings: Settings, project_path: str) -> tuple[str, str]:
    """Resolve ``(project_id, error)``. Empty error means success.

    An empty/omitted path falls back to ``settings.base_dir`` so the tool is
    usable without the model having to name a path on every call.
    """
    raw = str(project_path or "").strip() or str(settings.base_dir)
    if not _authorized(settings, raw):
        return "", f"Path is outside the authorized workspace roots: {raw}"
    root = Path(raw).resolve()
    return canonical_project_key(str(root)), ""


def _limit(value, default: int = 25) -> int:
    """Clamp a caller-supplied limit into a sane, bounded range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return min(parsed, 100)


def _depth(value, default: int = 2) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return min(parsed, 4)


def _coerce_node_type(value) -> NodeType | None:
    if not value:
        return None
    try:
        return NodeType(str(value).strip().lower())
    except ValueError:
        return None


def _coerce_relation(value) -> RelationType | None:
    if not value:
        return None
    try:
        return RelationType(str(value).strip().lower())
    except ValueError:
        return None


def _node_view(node) -> dict:
    """Model-facing node shape. Metadata is passed through as stored.

    Metadata is already bounded at write time (``MAX_METADATA_*`` in
    :mod:`core.knowledge_graph`), so re-truncating here would be redundant.
    ``state`` is always present so a reader is told whether the node still
    describes anything, rather than having to infer it from metadata.
    """
    return {
        "node_id": node.node_id,
        "node_type": node.node_type.value,
        "canonical_key": node.canonical_key,
        "display_name": node.display_name,
        "metadata": node.metadata,
        "updated_at": node.updated_at,
        "state": stale_state(node),
    }


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def knowledge_index(
    project_path: str,
    settings: Settings,
    max_files: int = 2000,
    max_depth: int = 12,
) -> ToolResult:
    """Index (or re-index) a project's structure into the knowledge graph.

    Idempotent: re-running updates size/mtime/hash and adds anything new
    without duplicating nodes. Reads only files inside authorized roots and
    never stores file contents.
    """
    project_id, error = _resolve_project(settings, project_path)
    if error:
        return ToolResult(False, error=error)

    root = Path(str(project_path or settings.base_dir)).resolve()
    if not root.is_dir():
        return ToolResult(False, error=f"Not a directory: {root}")

    try:
        graph = manager(settings).get_graph(project_id)
        stats = ingest_project_structure(
            graph,
            root,
            root.name,
            settings,
            max_depth=_depth(max_depth, 12),
            max_files=_limit(max_files, 2000),
        )
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        return ToolResult(False, error=f"Could not index project: {exc}")

    log.info("knowledge.index %s: %s", root, stats)
    return ToolResult(
        True,
        output=_json({"project_path": str(root), "project_id": project_id, **stats}),
        data={"project_id": project_id, **stats},
    )


def knowledge_query(
    query_type: str,
    settings: Settings,
    project_path: str = "",
    node_type: str = "",
    relation_type: str = "",
    canonical_key: str = "",
    node_id: str = "",
    to_id: str = "",
    direction: str = "both",
    status: str = "",
    path_contains: str = "",
    extension: str = "",
    hours: int = 24,
    limit: int = 25,
    max_depth: int = 2,
    include_stale: bool = False,
) -> ToolResult:
    """Query the persistent project knowledge graph. Strictly read-only.

    Returns reference data only. Nothing returned here is verification
    evidence and nothing here can mark a task verified.

    Stale knowledge is withheld unless ``include_stale`` is set, so a default
    answer never offers something that no longer exists. ``find_stale`` reaches
    the history explicitly.
    """
    kind = str(query_type or "").strip().lower()
    if kind not in QUERY_TYPES:
        return ToolResult(False, error=f"query_type must be one of: {', '.join(QUERY_TYPES)}")

    project_id, error = _resolve_project(settings, project_path)
    if error:
        return ToolResult(False, error=error)

    lim = _limit(limit)
    depth = _depth(max_depth)

    try:
        graph = manager(settings).get_existing(project_id)
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        return ToolResult(False, error=f"Could not open knowledge graph: {exc}")

    if graph is None:
        # Never indexed. Answering "no knowledge" is the honest result, and it
        # must not create a database as a side effect of asking.
        return ToolResult(
            True,
            output=_json({
                "query_type": kind,
                "project_id": project_id,
                "count": 0,
                "truncated": False,
                "nodes": [],
                "note": "no knowledge indexed for this project yet; run knowledge.index first",
            }),
            data={"query_type": kind, "project_id": project_id, "count": 0,
                  "truncated": False, "nodes": [], "indexed": False},
        )

    engine = KnowledgeQueryEngine(graph, max_results=lim)

    try:
        if kind == "find_node":
            key = str(canonical_key or "").strip()
            if not key:
                return ToolResult(False, error="canonical_key is required for find_node")
            node = engine.find_node(key)
            if node is None:
                return ToolResult(True, output="(no node with that canonical key)", data={"found": False})
            return ToolResult(True, output=_json({"found": True, "node": _node_view(node)}),
                              data={"found": True, "node": _node_view(node)})

        if kind == "find_nodes":
            result = engine.find_nodes(
                node_type=_coerce_node_type(node_type),
                limit=lim,
                include_stale=include_stale,
            )
        elif kind == "find_stale":
            result = engine.find_stale(limit=lim)
        elif kind == "neighbors":
            if not node_id:
                return ToolResult(False, error="node_id is required for neighbors")
            if direction not in ("in", "out", "both"):
                return ToolResult(False, error="direction must be one of: in, out, both")
            result = engine.neighbors(node_id, _coerce_relation(relation_type), direction, lim)
        elif kind == "find_related":
            if not node_id:
                return ToolResult(False, error="node_id is required for find_related")
            result = engine.find_related(node_id, _coerce_relation(relation_type), depth, lim)
        elif kind == "path":
            if not node_id or not to_id:
                return ToolResult(False, error="node_id and to_id are both required for path")
            found = engine.path(node_id, to_id, depth)
            if not found.found:
                return ToolResult(True, output=_json({"found": False, "paths": []}), data={"found": False})
            paths = [
                {
                    "edges": [edge.relation_type.value for edge, _ in leg],
                    "nodes": [_node_view(node) for _, node in leg],
                }
                for leg in found.paths
            ]
            return ToolResult(True, output=_json({"found": True, "paths": paths}),
                              data={"found": True, "paths": paths})
        elif kind == "search_files":
            result = engine.search_files(extension=extension or None,
                                         path_contains=path_contains or None,
                                         limit=lim)
        elif kind == "search_tasks":
            result = engine.search_tasks(status=status or None, limit=lim)
        elif kind == "search_artifacts":
            result = engine.search_artifacts(_coerce_node_type(node_type), lim)
        elif kind == "search_datasets":
            result = engine.search_datasets(lim)
        elif kind == "search_charts":
            result = engine.search_charts(lim)
        else:  # recent_activity
            result = engine.recent_activity(hours=max(1, _limit(hours, 24)), limit=lim)
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        # A damaged database is reported, not raised and not papered over. The
        # graph is a projection, so failing to read it must never take down the
        # request that asked.
        return ToolResult(False, error=f"Query failed: {exc}")

    payload = {
        "query_type": kind,
        "project_id": project_id,
        "count": result.total_count,
        "truncated": result.truncated,
        "nodes": [_node_view(node) for node in result.nodes],
    }
    if result.stale_withheld:
        # Reported so "nothing matched" is never confused with "what matched no
        # longer exists".
        payload["stale_withheld"] = result.stale_withheld
    return ToolResult(True, output=_json(payload), data=payload)
