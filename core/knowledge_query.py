"""M2.14 Phase 6 — Knowledge Graph Query Engine.

Safe, deterministic, read-only query interface for the knowledge graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphManager,
    Node,
    Edge,
    NodeType,
    RelationType,
)
from core.logging_setup import get_logger

log = get_logger("knowledge_query")

# Query limits
MAX_RESULTS = 100
MAX_TRAVERSAL_DEPTH = 4
MAX_PATH_RESULTS = 10


@dataclass
class QueryResult:
    """Result of a graph query."""
    nodes: list[Node]
    edges: list[Edge]
    total_count: int
    truncated: bool


@dataclass
class PathResult:
    """Result of a path query."""
    paths: list[list[tuple[Edge, Node]]]
    found: bool


class KnowledgeQueryEngine:
    """Read-only query engine for knowledge graph.

    All queries are bounded, deterministic, and project-scoped.
    """

    def __init__(self, graph: KnowledgeGraph, max_results: int = MAX_RESULTS):
        self.graph = graph
        self.max_results = min(max_results, MAX_RESULTS)

    # ──────────────────────────────────────────────────────────────
    # Basic lookups

    def find_node(self, canonical_key: str) -> Optional[Node]:
        """Exact lookup by canonical key."""
        return self.graph.get_node(canonical_key)

    def find_nodes(
        self,
        node_type: Optional[NodeType] = None,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Find nodes by type with optional limit."""
        lim = min(limit or self.max_results, self.max_results)
        nodes = self.graph.find_nodes(node_type=node_type, limit=lim)
        return QueryResult(
            nodes=nodes,
            edges=[],
            total_count=len(nodes),
            truncated=len(nodes) >= lim,
        )

    def search_project(self, project_id: str) -> QueryResult:
        """Get all nodes for a project."""
        nodes = self.graph.find_nodes(project_id=project_id, limit=self.max_results)
        return QueryResult(
            nodes=nodes,
            edges=[],
            total_count=len(nodes),
            truncated=len(nodes) >= self.max_results,
        )

    # ──────────────────────────────────────────────────────────────
    # Relation traversal

    def neighbors(
        self,
        node_id: str,
        relation_type: Optional[RelationType] = None,
        direction: str = "both",
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Get neighbors of a node."""
        lim = min(limit or self.max_results, self.max_results)
        pairs = self.graph.neighbors(node_id, relation_type, direction, lim)
        nodes = [n for _, n in pairs]
        edges = [e for e, _ in pairs]
        return QueryResult(
            nodes=nodes,
            edges=edges,
            total_count=len(nodes),
            truncated=len(nodes) >= lim,
        )

    def find_related(
        self,
        node_id: str,
        relation_type: Optional[RelationType] = None,
        max_depth: int = 2,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Find all nodes reachable within max_depth (BFS)."""
        lim = min(limit or self.max_results, self.max_results)
        max_d = min(max_depth, MAX_TRAVERSAL_DEPTH)

        from collections import deque

        visited = {node_id}
        queue = deque([(node_id, 0)])
        found_nodes: list[Node] = []
        found_edges: list[Edge] = []

        while queue and len(found_nodes) < lim:
            current_id, depth = queue.popleft()
            if depth >= max_d:
                continue

            pairs = self.graph.neighbors(current_id, relation_type, "both", lim - len(found_nodes))
            for edge, node in pairs:
                if node.node_id not in visited:
                    visited.add(node.node_id)
                    found_nodes.append(node)
                    found_edges.append(edge)
                    if len(found_nodes) >= lim:
                        break
                    queue.append((node.node_id, depth + 1))

        return QueryResult(
            nodes=found_nodes,
            edges=found_edges,
            total_count=len(found_nodes),
            truncated=len(found_nodes) >= lim,
        )

    def path(
        self,
        from_id: str,
        to_id: str,
        max_depth: int = MAX_TRAVERSAL_DEPTH,
    ) -> PathResult:
        """Find shortest path between two nodes."""
        max_d = min(max_depth, MAX_TRAVERSAL_DEPTH)
        paths = self.graph.path(from_id, to_id, max_d)
        return PathResult(
            paths=paths or [],
            found=paths is not None and len(paths) > 0,
        )

    # ──────────────────────────────────────────────────────────────
    # Specialized searches

    def search_files(
        self,
        extension: Optional[str] = None,
        path_contains: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Search for FILE nodes by extension or path."""
        lim = min(limit or self.max_results, self.max_results)
        all_files = self.graph.find_nodes(node_type=NodeType.FILE, limit=lim * 2)

        results = []
        for node in all_files:
            path = node.metadata.get("path", "")
            ext = node.metadata.get("extension", "")
            if extension and ext != extension:
                continue
            if path_contains and path_contains not in path:
                continue
            results.append(node)
            if len(results) >= lim:
                break

        return QueryResult(
            nodes=results,
            edges=[],
            total_count=len(results),
            truncated=len(results) >= lim,
        )

    def search_tasks(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Search for TASK nodes by status."""
        lim = min(limit or self.max_results, self.max_results)
        all_tasks = self.graph.find_nodes(node_type=NodeType.TASK, limit=lim * 2)

        results = []
        for node in all_tasks:
            if status and node.metadata.get("status") != status:
                continue
            results.append(node)
            if len(results) >= lim:
                break

        return QueryResult(
            nodes=results,
            edges=[],
            total_count=len(results),
            truncated=len(results) >= lim,
        )

    def search_artifacts(
        self,
        artifact_type: Optional[NodeType | str] = None,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Search for ARTIFACT, CHART, or DATASET nodes.

        Args:
            artifact_type: NodeType enum (DATASET, CHART, ARTIFACT) or string format ("csv", "png", etc.)
            limit: Maximum results
        """
        lim = min(limit or self.max_results, self.max_results)

        # Determine which node types to search
        if artifact_type is None:
            types = [NodeType.ARTIFACT, NodeType.CHART, NodeType.DATASET]
        elif isinstance(artifact_type, NodeType):
            types = [artifact_type]
        else:
            # String format filter - search all artifact types and filter by metadata
            types = [NodeType.ARTIFACT, NodeType.CHART, NodeType.DATASET]

        results = []
        for ntype in types:
            nodes = self.graph.find_nodes(node_type=ntype, limit=lim)
            for node in nodes:
                # If string format filter provided, check metadata
                # Use type() check because NodeType is a str-Enum so isinstance(NodeType.X, str) == True
                if type(artifact_type) is str and node.metadata.get("format") != artifact_type:
                    continue
                results.append(node)
                if len(results) >= lim:
                    break
            if len(results) >= lim:
                break

        return QueryResult(
            nodes=results,
            edges=[],
            total_count=len(results),
            truncated=len(results) >= lim,
        )

    def search_datasets(self, limit: Optional[int] = None) -> QueryResult:
        """Search for DATASET nodes."""
        return self.search_artifacts(NodeType.DATASET, limit)

    def search_charts(self, limit: Optional[int] = None) -> QueryResult:
        """Search for CHART nodes."""
        return self.search_artifacts(NodeType.CHART, limit)

    def recent_activity(
        self,
        hours: int = 24,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Get nodes updated within the last N hours."""
        lim = min(limit or self.max_results, self.max_results)
        cutoff = datetime.now().timestamp() - (hours * 3600)

        all_nodes = self.graph.find_nodes(limit=lim * 3)
        results = []

        for node in all_nodes:
            try:
                updated = datetime.strptime(node.updated_at, "%Y-%m-%d %H:%M:%S").timestamp()
                if updated >= cutoff:
                    results.append(node)
                    if len(results) >= lim:
                        break
            except ValueError:
                pass

        return QueryResult(
            nodes=results,
            edges=[],
            total_count=len(results),
            truncated=len(results) >= lim,
        )

    # ──────────────────────────────────────────────────────────────
    # Formatted output for agent

    def format_node_summary(self, node: Node) -> str:
        """Format a node for agent context."""
        parts = [f"[{node.node_type.value}] {node.display_name}"]
        if node.metadata.get("path"):
            parts.append(f"  path: {node.metadata['path']}")
        if node.metadata.get("size"):
            parts.append(f"  size: {node.metadata['size']} bytes")
        return "\n".join(parts)

    def format_edge_summary(self, edge: Edge, neighbor: Node) -> str:
        """Format an edge for agent context."""
        return f"  {edge.relation_type.value} -> [{neighbor.node_type.value}] {neighbor.display_name}"

    def format_query_result(self, result: QueryResult) -> str:
        """Format query result for agent context."""
        if not result.nodes:
            return "(no results)"

        lines = [f"Found {result.total_count} node(s){' (truncated)' if result.truncated else ''}:"]
        for node in result.nodes:
            lines.append(self.format_node_summary(node))
            # Include first few edges
            edges = self.graph.get_edges(source_id=node.node_id)[:3]
            for edge in edges:
                neighbor = self.graph.get_node_by_id(edge.target_id)
                if neighbor:
                    lines.append(self.format_edge_summary(edge, neighbor))
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# High-level query tools for agent integration


def query_graph_tool(
    graph_manager: KnowledgeGraphManager,
    project_id: str,
    query_type: str,
    **params,
) -> dict:
    """Unified query tool for agent integration.

    Args:
        graph_manager: KnowledgeGraphManager instance
        project_id: Project to query
        query_type: One of:
            - "find_node": exact lookup by canonical_key
            - "find_nodes": by node_type
            - "neighbors": from node_id
            - "find_related": BFS from node_id
            - "path": between two node_ids
            - "search_files": by extension/path
            - "search_tasks": by status
            - "search_artifacts": datasets/charts/artifacts
            - "search_datasets": datasets
            - "search_charts": charts
            - "recent_activity": recent updates
        **params: Additional parameters for the query type

    Returns:
        Dict with 'success', 'output', 'data', 'error'
    """
    try:
        graph = graph_manager.get_graph(project_id)
        engine = KnowledgeQueryEngine(graph)

        if query_type == "find_node":
            canonical_key = params.get("canonical_key")
            if not canonical_key:
                return {"success": False, "error": "canonical_key required"}
            node = engine.find_node(canonical_key)
            if not node:
                return {"success": True, "output": "(node not found)", "data": {}}
            return {"success": True, "output": engine.format_node_summary(node), "data": node.to_dict()}

        elif query_type == "find_nodes":
            node_type = params.get("node_type")
            if isinstance(node_type, str):
                try:
                    node_type = NodeType(node_type)
                except ValueError:
                    return {"success": False, "error": f"invalid node_type: {node_type}"}
            result = engine.find_nodes(node_type=node_type, limit=params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        elif query_type == "neighbors":
            node_id = params.get("node_id")
            if not node_id:
                return {"success": False, "error": "node_id required"}
            relation_type = params.get("relation_type")
            if isinstance(relation_type, str):
                try:
                    relation_type = RelationType(relation_type)
                except ValueError:
                    return {"success": False, "error": f"invalid relation_type: {relation_type}"}
            result = engine.neighbors(node_id, relation_type, params.get("direction", "both"), params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes], "edges": [e.to_dict() for e in result.edges]}}

        elif query_type == "find_related":
            node_id = params.get("node_id")
            if not node_id:
                return {"success": False, "error": "node_id required"}
            relation_type = params.get("relation_type")
            if isinstance(relation_type, str):
                try:
                    relation_type = RelationType(relation_type)
                except ValueError:
                    return {"success": False, "error": f"invalid relation_type: {relation_type}"}
            result = engine.find_related(node_id, relation_type, params.get("max_depth", 2), params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes], "edges": [e.to_dict() for e in result.edges]}}

        elif query_type == "path":
            from_id = params.get("from_id")
            to_id = params.get("to_id")
            if not from_id or not to_id:
                return {"success": False, "error": "from_id and to_id required"}
            result = engine.path(from_id, to_id, params.get("max_depth", MAX_TRAVERSAL_DEPTH))
            if not result.found:
                return {"success": True, "output": "No path found", "data": {}}
            path_str = " -> ".join(f"[{n.node_type.value}] {n.display_name}" for _, n in result.paths[0])
            return {"success": True, "output": f"Path: {path_str}", "data": {"paths": [[e.to_dict() for e, n in p] for p in result.paths]}}

        elif query_type == "search_files":
            result = engine.search_files(
                extension=params.get("extension"),
                path_contains=params.get("path_contains"),
                limit=params.get("limit"),
            )
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        elif query_type == "search_tasks":
            result = engine.search_tasks(status=params.get("status"), limit=params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        elif query_type == "search_artifacts":
            result = engine.search_artifacts(artifact_type=params.get("artifact_type"), limit=params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        elif query_type == "search_datasets":
            result = engine.search_datasets(limit=params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        elif query_type == "search_charts":
            result = engine.search_charts(limit=params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        elif query_type == "recent_activity":
            result = engine.recent_activity(hours=params.get("hours", 24), limit=params.get("limit"))
            return {"success": True, "output": engine.format_query_result(result), "data": {"nodes": [n.to_dict() for n in result.nodes]}}

        else:
            return {"success": False, "error": f"unknown query_type: {query_type}"}

    except Exception as exc:
        log.error("Query failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────
# Project-scoped query helper


def get_project_query_engine(graph_manager: KnowledgeGraphManager, project_id: str) -> KnowledgeQueryEngine:
    """Get a query engine scoped to a project."""
    graph = graph_manager.get_graph(project_id)
    return KnowledgeQueryEngine(graph)