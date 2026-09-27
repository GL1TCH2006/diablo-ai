"""M2.14 — Persistent Project Knowledge Graph.

Minimal graph storage using SQLite. Designed as a projection/context layer,
not a replacement for TaskStore, VerificationLedger, or Git.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from core.logging_setup import get_logger

log = get_logger("knowledge_graph")

# ──────────────────────────────────────────────────────────────
# Schema version — bump on incompatible changes
SCHEMA_VERSION = 1

# ──────────────────────────────────────────────────────────────
# Node / Edge type enums (kept minimal, extensible)


class NodeType(str, Enum):
    PROJECT = "project"
    FILE = "file"
    DIRECTORY = "directory"
    CODE_SYMBOL = "code_symbol"
    TASK = "task"
    PLAN = "plan"
    TEST = "test"
    DATASET = "dataset"
    ANALYSIS = "analysis"
    CHART = "chart"
    ARTIFACT = "artifact"
    GIT_COMMIT = "git_commit"
    CHECKPOINT = "checkpoint"
    VERIFICATION = "verification"
    DECISION = "decision"


class RelationType(str, Enum):
    CONTAINS = "contains"
    DEPENDS_ON = "depends_on"
    DEFINES = "defines"
    IMPORTS = "imports"
    TESTS = "tests"
    GENERATED_FROM = "generated_from"
    PRODUCED = "produced"
    DERIVED_FROM = "derived_from"
    EXECUTED_BY = "executed_by"
    VERIFIED_BY = "verified_by"
    PART_OF = "part_of"
    CHANGED_BY = "changed_by"
    CHECKPOINTED_AS = "checkpointed_as"
    RELATED_TO = "related_to"


# ──────────────────────────────────────────────────────────────
# Bounded metadata constraints
MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE_LEN = 1000
MAX_METADATA_TOTAL_LEN = 8000


def _validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Enforce bounds on metadata. Returns sanitized copy."""
    if not isinstance(metadata, dict):
        return {}
    out: dict[str, Any] = {}
    total = 0
    for k, v in list(metadata.items())[:MAX_METADATA_KEYS]:
        if not isinstance(k, str):
            continue
        k = k[:100]
        sv = json.dumps(v, ensure_ascii=False, default=str)
        if len(sv) > MAX_METADATA_VALUE_LEN:
            sv = sv[:MAX_METADATA_VALUE_LEN] + "..."
        if total + len(sv) > MAX_METADATA_TOTAL_LEN:
            break
        try:
            out[k] = json.loads(sv)
        except json.JSONDecodeError:
            out[k] = sv
        total += len(sv)
    return out


# ──────────────────────────────────────────────────────────────
# Canonical key helpers


def canonical_file_key(project_id: str, rel_path: str) -> str:
    """Stable file identity: project_id + normalized relative path."""
    norm = rel_path.replace("\\", "/").strip("/")
    return f"file:{project_id}:{norm}"


def canonical_symbol_key(project_id: str, file_rel: str, symbol: str, kind: str) -> str:
    """Stable symbol identity: project + file + symbol qualified name."""
    norm = file_rel.replace("\\", "/").strip("/")
    return f"symbol:{project_id}:{norm}:{kind}:{symbol}"


def canonical_project_key(project_root: str) -> str:
    """Stable project identity from workspace root."""
    resolved = str(Path(project_root).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"project:{digest}"


def canonical_task_key(task_id: str) -> str:
    return f"task:{task_id}"


def canonical_plan_key(plan_id: str) -> str:
    return f"plan:{plan_id}"


def canonical_dataset_key(path: str) -> str:
    resolved = str(Path(path).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"dataset:{digest}"


def canonical_chart_key(chart_path: str) -> str:
    resolved = str(Path(chart_path).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"chart:{digest}"


def canonical_artifact_key(artifact_path: str) -> str:
    resolved = str(Path(artifact_path).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"artifact:{digest}"


def canonical_analysis_key(
    dataset_path: str, operation: str, column: str, params: dict | None = None
) -> str:
    """Identity for one analysis: the same question asked twice is one node.

    The key is derived only from the question (dataset, operation, column and
    the remaining parameters), never from a clock. A timestamp in the key would
    make every rerun a brand new node, so the graph would grow without bound
    while holding nothing new.
    """
    payload = {
        "dataset": str(dataset_path),
        "operation": str(operation),
        "column": str(column or ""),
        "params": {str(k): str(v) for k, v in sorted((params or {}).items())},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"analysis:{digest}"


# ──────────────────────────────────────────────────────────────
# Dataclasses


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: NodeType
    project_id: str
    canonical_key: str
    display_name: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "project_id": self.project_id,
            "canonical_key": self.canonical_key,
            "display_name": self.display_name,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source_id: str
    target_id: str
    relation_type: RelationType
    metadata: dict[str, Any]
    created_at: str
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation_type": self.relation_type.value,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


# ──────────────────────────────────────────────────────────────
# SQLite storage


class KnowledgeGraph:
    """Persistent knowledge graph per project.

    One database file per project_id at data/knowledge/{project_id}.db.

    ``readonly=True`` opens an existing graph through SQLite's own
    ``mode=ro`` and skips schema creation entirely. That matters because the
    query path must not have *any* filesystem side effect: a read that lazily
    created an empty database would let a caller materialise one file per
    path it named, which is unbounded disk growth dressed up as a read.
    A readonly graph therefore refuses to open a database that does not exist.
    """

    def __init__(self, knowledge_dir: Path, project_id: str, readonly: bool = False):
        self.project_id = project_id
        self.readonly = bool(readonly)
        self.db_path = knowledge_dir / f"{project_id}.db"
        if self.readonly:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"no knowledge graph for project {project_id}")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        if not self.readonly:
            self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.readonly:
                uri = f"file:{self.db_path.as_posix()}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def _writable(self) -> None:
        """Refuse a write on a readonly graph, before SQLite sees it."""
        if self.readonly:
            raise PermissionError("knowledge graph is open read-only")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                project_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edges (
                edge_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                FOREIGN KEY (source_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES nodes(node_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
            CREATE INDEX IF NOT EXISTS idx_nodes_canonical ON nodes(canonical_key);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation_type);
            CREATE INDEX IF NOT EXISTS idx_edges_source_rel ON edges(source_id, relation_type);
            CREATE INDEX IF NOT EXISTS idx_edges_target_rel ON edges(target_id, relation_type);
            """
        )
        # Ensure schema version
        cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'")
        row = cur.fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))
        else:
            stored = int(row[0])
            if stored < SCHEMA_VERSION:
                self._migrate(stored)
        conn.commit()

    def _migrate(self, from_version: int) -> None:
        """Migrate schema from from_version to SCHEMA_VERSION."""
        log.info("Migrating knowledge graph schema from %d to %d", from_version, SCHEMA_VERSION)
        conn = self._get_conn()
        # Future migrations go here. For v1, nothing to do.
        conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'version'", (str(SCHEMA_VERSION),))
        conn.commit()

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    # ──────────────────────────────────────────────────────────
    # Node operations

    def upsert_node(
        self,
        node_type: NodeType,
        canonical_key: str,
        display_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> Node:
        """Insert or update a node by canonical_key. Returns the node."""
        self._writable()
        conn = self._get_conn()
        metadata = _validate_metadata(metadata or {})
        now = self._now()

        # Try to get existing
        cur = conn.execute("SELECT node_id, created_at FROM nodes WHERE canonical_key = ?", (canonical_key,))
        row = cur.fetchone()
        if row:
            node_id, created_at = row
            conn.execute(
                "UPDATE nodes SET node_type=?, display_name=?, metadata=?, updated_at=?, schema_version=? WHERE node_id=?",
                (node_type.value, display_name, json.dumps(metadata), now, SCHEMA_VERSION, node_id),
            )
            conn.commit()
            return Node(
                node_id=node_id,
                node_type=node_type,
                project_id=self.project_id,
                canonical_key=canonical_key,
                display_name=display_name,
                metadata=metadata,
                created_at=created_at,
                updated_at=now,
            )
        else:
            node_id = f"n_{hashlib.sha256(canonical_key.encode()).hexdigest()[:16]}"
            conn.execute(
                "INSERT INTO nodes (node_id, node_type, project_id, canonical_key, display_name, metadata, created_at, updated_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (node_id, node_type.value, self.project_id, canonical_key, display_name, json.dumps(metadata), now, now, SCHEMA_VERSION),
            )
            conn.commit()
            return Node(
                node_id=node_id,
                node_type=node_type,
                project_id=self.project_id,
                canonical_key=canonical_key,
                display_name=display_name,
                metadata=metadata,
                created_at=now,
                updated_at=now,
            )

    def get_node(self, canonical_key: str) -> Node | None:
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM nodes WHERE canonical_key = ?", (canonical_key,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def get_node_by_id(self, node_id: str) -> Node | None:
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def _row_to_node(self, row: sqlite3.Row) -> Node:
        return Node(
            node_id=row["node_id"],
            node_type=NodeType(row["node_type"]),
            project_id=row["project_id"],
            canonical_key=row["canonical_key"],
            display_name=row["display_name"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=row["schema_version"],
        )

    def delete_node(self, canonical_key: str) -> bool:
        self._writable()
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM nodes WHERE canonical_key = ?", (canonical_key,))
        conn.commit()
        return cur.rowcount > 0

    # ──────────────────────────────────────────────────────────
    # Edge operations

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        relation_type: RelationType,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        """Insert or update an edge. Uses deterministic edge_id from source+target+type."""
        self._writable()
        conn = self._get_conn()
        metadata = _validate_metadata(metadata or {})
        now = self._now()

        edge_key = f"{source_id}|{relation_type.value}|{target_id}"
        edge_id = f"e_{hashlib.sha256(edge_key.encode()).hexdigest()[:16]}"

        cur = conn.execute("SELECT created_at FROM edges WHERE edge_id = ?", (edge_id,))
        row = cur.fetchone()
        if row:
            created_at = row[0]
            conn.execute(
                "UPDATE edges SET metadata=?, schema_version=? WHERE edge_id=?",
                (json.dumps(metadata), SCHEMA_VERSION, edge_id),
            )
            conn.commit()
            return Edge(
                edge_id=edge_id,
                source_id=source_id,
                target_id=target_id,
                relation_type=relation_type,
                metadata=metadata,
                created_at=created_at,
            )
        else:
            conn.execute(
                "INSERT INTO edges (edge_id, source_id, target_id, relation_type, metadata, created_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (edge_id, source_id, target_id, relation_type.value, json.dumps(metadata), now, SCHEMA_VERSION),
            )
            conn.commit()
            return Edge(
                edge_id=edge_id,
                source_id=source_id,
                target_id=target_id,
                relation_type=relation_type,
                metadata=metadata,
                created_at=now,
            )

    def get_edges(self, source_id: str = "", target_id: str = "", relation_type: RelationType | None = None) -> list[Edge]:
        conn = self._get_conn()
        where = []
        params: list[Any] = []
        if source_id:
            where.append("source_id = ?")
            params.append(source_id)
        if target_id:
            where.append("target_id = ?")
            params.append(target_id)
        if relation_type is not None:
            where.append("relation_type = ?")
            params.append(relation_type.value)
        sql = "SELECT * FROM edges"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at"
        cur = conn.execute(sql, params)
        return [self._row_to_edge(row) for row in cur.fetchall()]

    def _row_to_edge(self, row: sqlite3.Row) -> Edge:
        return Edge(
            edge_id=row["edge_id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relation_type=RelationType(row["relation_type"]),
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
            schema_version=row["schema_version"],
        )

    def delete_edge(self, edge_id: str) -> bool:
        self._writable()
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
        conn.commit()
        return cur.rowcount > 0

    # ──────────────────────────────────────────────────────────
    # Query helpers

    def find_nodes_by_key_prefix(self, node_type: NodeType, prefix: str) -> list[Node]:
        """Every node of ``node_type`` whose canonical key starts with ``prefix``.

        Canonical keys are structured (``symbol:<project>:<path>:...``), so a
        prefix is the natural way to ask "what does the graph think this one
        file defines?" without a caller reaching into the connection itself.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM nodes WHERE node_type = ? AND canonical_key LIKE ? ORDER BY canonical_key",
            (node_type.value, prefix.replace("\\", "/") + "%"),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def find_nodes(
        self,
        node_type: NodeType | None = None,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Node]:
        conn = self._get_conn()
        where = []
        params: list[Any] = []
        if node_type:
            where.append("node_type = ?")
            params.append(node_type.value)
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        sql = "SELECT * FROM nodes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = conn.execute(sql, params)
        return [self._row_to_node(row) for row in cur.fetchall()]

    def neighbors(
        self,
        node_id: str,
        relation_type: RelationType | None = None,
        direction: str = "both",  # "out", "in", "both"
        limit: int = 50,
    ) -> list[tuple[Edge, Node]]:
        """Return (edge, neighbor_node) pairs."""
        conn = self._get_conn()

        if direction == "out":
            base_sql = "SELECT * FROM edges WHERE source_id = ?"
            params: list[Any] = [node_id]
        elif direction == "in":
            base_sql = "SELECT * FROM edges WHERE target_id = ?"
            params = [node_id]
        elif direction == "both":
            base_sql = "SELECT * FROM edges WHERE (source_id = ? OR target_id = ?)"
            params = [node_id, node_id]
        else:
            return []

        if relation_type:
            base_sql += " AND relation_type = ?"
            params.append(relation_type.value)

        base_sql += " ORDER BY created_at LIMIT ?"
        params.append(limit)

        cur = conn.execute(base_sql, params)

        results: list[tuple[Edge, Node]] = []
        for row in cur.fetchall():
            edge = self._row_to_edge(row)
            neighbor_id = edge.target_id if edge.source_id == node_id else edge.source_id
            neighbor = self.get_node_by_id(neighbor_id)
            if neighbor:
                results.append((edge, neighbor))
        return results

    def path(
        self,
        from_id: str,
        to_id: str,
        max_depth: int = 4,
    ) -> list[list[tuple[Edge, Node]]] | None:
        """Find shortest path(s) between two nodes (BFS, bounded depth)."""
        if from_id == to_id:
            node = self.get_node_by_id(from_id)
            if node is None:
                return None
            edge = Edge(edge_id="", source_id=from_id, target_id=from_id, relation_type=RelationType.RELATED_TO, metadata={}, created_at=self._now())
            return [[(edge, node)]]

        from collections import deque

        conn = self._get_conn()
        # Build adjacency in memory for BFS (bounded)
        adj: dict[str, list[tuple[str, RelationType]]] = {}
        cur = conn.execute("SELECT source_id, target_id, relation_type FROM edges")
        for row in cur.fetchall():
            adj.setdefault(row["source_id"], []).append((row["target_id"], RelationType(row["relation_type"])))

        queue = deque([(from_id, [])])
        visited = {from_id}
        paths: list[list[tuple[Edge, Node]]] = []

        while queue and len(paths) < 3:  # limit to 3 paths
            current, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            for neighbor_id, rel_type in adj.get(current, []):
                if neighbor_id in visited:
                    continue
                edge = self.get_edges(source_id=current, target_id=neighbor_id, relation_type=rel_type)
                if not edge:
                    continue
                e = edge[0]
                node = self.get_node_by_id(neighbor_id)
                if not node:
                    continue
                new_path = path + [(e, node)]
                if neighbor_id == to_id:
                    paths.append(new_path)
                else:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, new_path))
        return paths if paths else None

    # ──────────────────────────────────────────────────────────
    # Bulk / transaction helpers

    def transaction(self):
        """Context manager for explicit transactions."""
        return self._get_conn()

    def stats(self) -> dict[str, int]:
        conn = self._get_conn()
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {"nodes": nodes, "edges": edges}


# ──────────────────────────────────────────────────────────────
# Graph manager (multi-project)


class KnowledgeGraphManager:
    """Manages per-project knowledge graphs."""

    def __init__(self, knowledge_dir: Path):
        self.knowledge_dir = knowledge_dir
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self._graphs: dict[str, KnowledgeGraph] = {}

    def get_graph(self, project_id: str) -> KnowledgeGraph:
        if project_id not in self._graphs:
            self._graphs[project_id] = KnowledgeGraph(self.knowledge_dir, project_id)
        return self._graphs[project_id]

    def get_existing(self, project_id: str) -> KnowledgeGraph | None:
        """Open an already-indexed project for reading, or return ``None``.

        This is the entry point the query path uses. It never creates a
        database: a project that was never indexed reports "no knowledge"
        instead of leaving an empty file behind for every path ever queried.
        The returned graph is cached only for its lifetime, and is opened
        read-only so a bug on the query path cannot write.
        """
        if project_id in self._graphs:
            return self._graphs[project_id]
        try:
            graph = KnowledgeGraph(self.knowledge_dir, project_id, readonly=True)
        except (FileNotFoundError, OSError):
            return None
        self._graphs[project_id] = graph
        return graph

    def close_all(self) -> None:
        for g in self._graphs.values():
            g.close()
        self._graphs.clear()

    def list_projects(self) -> list[str]:
        return [p.stem for p in self.knowledge_dir.glob("*.db")]

    def delete_project(self, project_id: str) -> bool:
        if project_id in self._graphs:
            self._graphs[project_id].close()
            del self._graphs[project_id]
        db_file = self.knowledge_dir / f"{project_id}.db"
        if db_file.exists():
            db_file.unlink()
            return True
        return False


# ──────────────────────────────────────────────────────────────
# High-level ingestion API (Phases 2-5 will use these)


def ingest_project(graph: KnowledgeGraph, project_root: Path, project_name: str) -> Node:
    """Create or update the PROJECT node."""
    proj_key = canonical_project_key(str(project_root))
    return graph.upsert_node(
        NodeType.PROJECT,
        proj_key,
        project_name,
        {"root": str(project_root.resolve())},
    )


def ingest_directory(graph: KnowledgeGraph, project_node: Node, rel_path: str) -> Node:
    """Create or update a DIRECTORY node and link to parent."""
    dir_key = f"dir:{graph.project_id}:{rel_path.replace('\\', '/').strip('/')}"
    dir_node = graph.upsert_node(NodeType.DIRECTORY, dir_key, rel_path.split("/")[-1] or "/", {"path": rel_path})
    graph.upsert_edge(project_node.node_id, dir_node.node_id, RelationType.CONTAINS)
    return dir_node


def ingest_file(
    graph: KnowledgeGraph,
    parent_dir_node: Node,
    rel_path: str,
    size: int,
    mtime: float,
    ext: str,
    content_hash: str | None = None,
) -> Node:
    """Create or update a FILE node and link to parent directory."""
    file_key = canonical_file_key(graph.project_id, rel_path)
    metadata: dict[str, Any] = {"path": rel_path, "size": size, "mtime": mtime, "extension": ext}
    if content_hash:
        metadata["content_hash"] = content_hash
    file_node = graph.upsert_node(NodeType.FILE, file_key, rel_path.split("/")[-1], metadata)
    graph.upsert_edge(parent_dir_node.node_id, file_node.node_id, RelationType.CONTAINS)
    return file_node


def link_task_to_file(graph: KnowledgeGraph, task_node: Node, file_node: Node, relation: RelationType = RelationType.CHANGED_BY) -> None:
    graph.upsert_edge(task_node.node_id, file_node.node_id, relation)


def link_task_to_plan(graph: KnowledgeGraph, task_node: Node, plan_node: Node) -> None:
    graph.upsert_edge(task_node.node_id, plan_node.node_id, RelationType.EXECUTED_BY)


def link_plan_to_artifact(graph: KnowledgeGraph, plan_node: Node, artifact_node: Node) -> None:
    graph.upsert_edge(plan_node.node_id, artifact_node.node_id, RelationType.PRODUCED)


def link_dataset_to_file(graph: KnowledgeGraph, dataset_node: Node, file_node: Node) -> None:
    graph.upsert_edge(dataset_node.node_id, file_node.node_id, RelationType.DERIVED_FROM)


def link_chart_to_dataset(graph: KnowledgeGraph, chart_node: Node, dataset_node: Node) -> None:
    graph.upsert_edge(chart_node.node_id, dataset_node.node_id, RelationType.GENERATED_FROM)


def link_task_to_chart(graph: KnowledgeGraph, task_node: Node, chart_node: Node) -> None:
    graph.upsert_edge(task_node.node_id, chart_node.node_id, RelationType.PRODUCED)


def link_verification_to_evidence(graph: KnowledgeGraph, verification_node: Node, evidence_node: Node) -> None:
    graph.upsert_edge(verification_node.node_id, evidence_node.node_id, RelationType.VERIFIED_BY)


def link_checkpoint(graph: KnowledgeGraph, task_node: Node, checkpoint_node: Node) -> None:
    graph.upsert_edge(task_node.node_id, checkpoint_node.node_id, RelationType.CHECKPOINTED_AS)