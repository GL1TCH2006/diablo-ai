"""M2.14 — Persistent Project Knowledge Graph.

Minimal graph storage using SQLite. Designed as a projection/context layer,
not a replacement for TaskStore, VerificationLedger, or Git.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
# Staleness
# ──────────────────────────────────────────────────────────────
# A node records what was true when it was written. The filesystem, the task
# store and Git keep moving afterwards, so a node can stop being an accurate
# description of anything. Rather than deleting that history or pretending it is
# still current, the node is *marked*: the fact survives, and its status is
# explicit. The query layer never returns a stale node as current.

#: Metadata key holding the boolean staleness flag.
STALE_KEY = "stale"
#: Metadata key holding why the node went stale.
STALE_REASON_KEY = "stale_reason"
#: Metadata key holding when it went stale.
STALE_SINCE_KEY = "stale_since"

#: Reasons a node can be marked stale. Kept explicit so a reader can tell a
#: deleted file from a changed one, and so no caller invents an ad hoc reason.
STALE_DELETED = "deleted"
STALE_CHANGED = "changed"
STALE_MISSING_ARTIFACT = "missing_artifact"
STALE_SOURCE_CHANGED = "source_changed"
STALE_UNREADABLE = "unreadable"

#: Node types whose subject can stop existing on disk without the graph knowing,
#: so reindexing is what establishes whether they are still current.
PHYSICAL_NODE_TYPES = (
    NodeType.FILE,
    NodeType.DATASET,
    NodeType.CHART,
    NodeType.ARTIFACT,
)

#: Edges that assert a relationship holds *now*. These are removed when their
#: endpoint goes stale, because leaving them would have the graph claiming
#: something it has just recorded as untrue.
_EDGES_CLAIMING_CURRENCY = frozenset({
    RelationType.DEFINES,
    RelationType.GENERATED_FROM,
    RelationType.DERIVED_FROM,
    RelationType.PRODUCED,
    RelationType.CONTAINS,
})


def is_stale(node: Any) -> bool:
    """Whether a node's metadata marks it as no longer current."""
    metadata = getattr(node, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return metadata.get(STALE_KEY) is True


def stale_state(node: Any) -> str:
    """``"stale"`` or ``"current"``, for presenting a node's status."""
    return "stale" if is_stale(node) else "current"


def with_stale(metadata: dict[str, Any], reason: str, when: str) -> dict[str, Any]:
    """A copy of ``metadata`` marked stale for a stated reason."""
    out = dict(metadata or {})
    out[STALE_KEY] = True
    out[STALE_REASON_KEY] = str(reason or STALE_CHANGED)
    out[STALE_SINCE_KEY] = when
    return out


def with_current(metadata: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``metadata`` marked current again, dropping the stale reason.

    Seeing a thing again is what makes it current; nothing else may clear the
    flag, so knowledge cannot quietly resume looking authoritative.
    """
    out = dict(metadata or {})
    if out.get(STALE_KEY) is not True:
        return out
    out.pop(STALE_KEY, None)
    out.pop(STALE_REASON_KEY, None)
    out.pop(STALE_SINCE_KEY, None)
    return out


#: SQL predicate selecting rows whose metadata does not mark them stale.
#: Metadata is a JSON blob and SQLite has no JSON path here, so the flag is
#: matched as text. Both spacings are covered because the blob is produced by
#: ``json.dumps`` with and without separators elsewhere. Only a literal
#: ``true`` counts, so a string value containing the word cannot be mistaken
#: for the flag.
STALE_CURRENT_PREDICATE = (
    "(metadata NOT LIKE '%\"stale\": true%' AND metadata NOT LIKE '%\"stale\":true%')"
)

#: SQL predicate selecting rows whose metadata *does* mark them stale.
STALE_PREDICATE = (
    "(metadata LIKE '%\"stale\": true%' OR metadata LIKE '%\"stale\":true%')"
)


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
            # Always stated, so a consumer can never read a node without also
            # reading whether it still describes anything.
            "state": stale_state(self),
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


# ──────────────────────────────────────────────────────────────
# SQLite storage
# ──────────────────────────────────────────────────────────────

#: Prefix for knowledge database files. Lets the manager recognise a knowledge
#: database without opening it, and keeps them apart from any other file in the
#: knowledge directory.
DB_FILENAME_PREFIX = "kg_"
DB_SUFFIX = ".db"

#: Characters that cannot appear in a filename on a supported platform. The
#: colon matters most: on Windows it is the NTFS alternate-data-stream
#: separator, so a database named ``project:abc.db`` is not a file at all but a
#: stream attached to a 0-byte host file called ``project``. Such a database is
#: invisible to a directory listing, so it cannot be enumerated or deleted, and
#: every project shares one host file. Encoding the separator keeps one real
#: file per project on every platform.
#:
#: Path separators are in the set for a different, sharper reason. Prefixing a
#: name does not make it safe: only the *first* component is protected, so
#: ``kg_a/../../../x.db`` still resolves outside the directory it was joined to.
#: A project id is not attacker-controlled today, but this function builds a
#: path out of a string, and a path built from a string has to be confined by
#: construction rather than by the accident of who calls it.
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"|?*/\\\x00-\x1f]')

#: A name made only of dots is never usable: it is a relative directory
#: reference, and on Windows it is an illegal filename besides.
_DOTS_ONLY = re.compile(r"^\.+$")


def db_filename(project_id: str) -> str:
    """The on-disk filename for a project's knowledge database.

    Always exactly one path component inside the knowledge directory, for any
    input at all. The real ``project_id`` is stored inside the database, so
    nothing downstream has to reverse this encoding to recover identity.
    """
    raw = str(project_id)
    encoded = _UNSAFE_FILENAME_CHARS.sub("_", raw)
    if not encoded or _DOTS_ONLY.match(encoded):
        # Nothing usable survived encoding. Fall back to a digest rather than
        # emitting an empty or dot-only name, which would resolve to the
        # directory itself or to its parent.
        encoded = "x" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{DB_FILENAME_PREFIX}{encoded}{DB_SUFFIX}"


def legacy_db_path(knowledge_dir: Path, project_id: str) -> Path:
    """The pre-Phase-8 path, kept so existing databases stay readable.

    On Windows that path is an alternate data stream rather than a real file, so
    this usually resolves to nothing and costs one ``is_file()`` check. A
    project id that is not a plain name yields no legacy path at all, so this
    can never become a way out of the knowledge directory.
    """
    name = str(project_id)
    if _UNSAFE_FILENAME_CHARS.search(name) or _DOTS_ONLY.match(name):
        return knowledge_dir / f"{DB_FILENAME_PREFIX}__no_legacy_path__"
    return knowledge_dir / f"{name}.db"


def _legacy_streams(host: Path) -> list[Path]:
    """Alternate data streams that may live on a legacy host file.

    Pre-Phase-8 databases named ``project:abc.db`` were created as NTFS
    streams on a host file called ``project``. Windows exposes them through
    ``host:stream`` paths, so enumeration has to look for them explicitly.
    On a platform without alternate data streams this finds nothing.
    """
    if os.name != "nt" or not host.is_file():
        return []
    try:
        # Opening a nonexistent stream for append would create it, so read the
        # directory link count instead: streams are not listed as entries.
        found: list[Path] = []
        for stream in (DB_SUFFIX,):
            candidate = Path(f"{host}:{stream}")
            if candidate.is_file():
                found.append(candidate)
        # Names carry a project id before the extension, e.g. "project:abc.db".
        for child in sorted(host.parent.glob(f"{host.name}:*{DB_SUFFIX}")):
            if child not in found and child.is_file():
                found.append(child)
        return found
    except OSError:
        return []


class KnowledgeGraph:
    """Persistent knowledge graph per project.

    One database file per project_id at data/knowledge/{filename}.db.

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
        primary = knowledge_dir / db_filename(project_id)
        # A database written before Phase 8 used the raw project id as its
        # filename. Prefer the safe name, but still open an existing legacy
        # database so upgrading does not orphan real knowledge.
        legacy = legacy_db_path(knowledge_dir, project_id)
        if not primary.is_file() and legacy.is_file():
            self.db_path = legacy
        else:
            self.db_path = primary
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
            try:
                self._conn.close()
            except sqlite3.Error:
                # Closing is best effort; a failure here must not mask the
                # caller's own result.
                pass
            self._conn = None

    def __del__(self) -> None:
        # A graph can be part of a reference cycle, so it may only become
        # unreachable at collection time. Without this the SQLite handle would
        # survive until the next collection and, on Windows, would keep the
        # database file locked against deletion or replacement.
        try:
            self.close()
        except Exception:
            pass

    def readonly_view(self) -> "KnowledgeGraph":
        """A read-only handle onto the same database file.

        The query path may run in the same process as an index. Returning the
        writable graph there would let a read-path bug write, so the view
        refuses writes before SQLite sees them while still observing the writes
        the writable handle has already committed.
        """
        if self.readonly:
            return self
        view = KnowledgeGraph.__new__(KnowledgeGraph)
        view.project_id = self.project_id
        view.readonly = True
        view.db_path = self.db_path
        view._conn = None
        return view

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
        # Record the owning project so a database can be identified from its
        # contents. The filename is an encoding of this id, not a reliable
        # source of truth for it.
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('project_id', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (self.project_id,),
        )
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
    # Staleness
    # ──────────────────────────────────────────────────────────

    def mark_stale(self, node: Node, reason: str = STALE_CHANGED) -> Node:
        """Mark a node as no longer describing anything current.

        Identity is preserved: the same ``node_id`` and ``canonical_key`` keep
        pointing at the same history, so a later sighting can clear the flag
        instead of creating a second identity for the same thing.

        Edges that assert a current relationship are dropped. Keeping
        ``FILE --defines--> symbol`` after the symbol stopped being defined would
        leave the graph asserting something it now knows is untrue; the symbol
        node itself stays, so the history survives without the false claim.
        """
        if is_stale(node):
            return node
        if node.node_type in PHYSICAL_NODE_TYPES or node.node_type == NodeType.CODE_SYMBOL:
            for edge in self.get_edges(target_id=node.node_id):
                if edge.relation_type in _EDGES_CLAIMING_CURRENCY:
                    self.delete_edge(edge.edge_id)
        return self.upsert_node(
            node.node_type,
            node.canonical_key,
            node.display_name,
            with_stale(node.metadata, reason, self._now()),
        )

    def mark_current(self, node: Node) -> Node:
        """Clear the stale flag because the subject was observed again."""
        if not is_stale(node):
            return node
        return self.upsert_node(
            node.node_type,
            node.canonical_key,
            node.display_name,
            with_current(node.metadata),
        )

    def mark_stale_many(self, nodes: list[Node], reason: str = STALE_DELETED) -> int:
        """Mark many nodes stale, returning how many changed."""
        changed = 0
        for node in nodes:
            if is_stale(node):
                continue
            self.mark_stale(node, reason)
            changed += 1
        return changed

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
        include_stale: bool = False,
        only_stale: bool = False,
    ) -> list[Node]:
        """Nodes of a type, most recently updated first, bounded.

        Stale nodes are excluded unless ``include_stale`` is set, so the default
        answer to "what is in this project" is never padded with knowledge that
        no longer describes anything. ``only_stale`` is the mirror image, used to
        reach the history without dragging current knowledge along with it.
        """
        conn = self._get_conn()
        where = []
        params: list[Any] = []
        if node_type:
            where.append("node_type = ?")
            params.append(node_type.value)
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if only_stale:
            where.append(STALE_PREDICATE)
        elif not include_stale:
            where.append(STALE_CURRENT_PREDICATE)
        sql = "SELECT * FROM nodes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = conn.execute(sql, params)
        return [self._row_to_node(row) for row in cur.fetchall()]

    def find_stale_nodes(
        self,
        node_type: NodeType | None = None,
        limit: int = 100,
    ) -> list[Node]:
        """Every node currently marked stale, and nothing else."""
        return self.find_nodes(
            node_type=node_type,
            limit=limit,
            only_stale=True,
            project_id=self.project_id,
        )

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
    """Manages per-project knowledge graphs.

    Writable and read-only handles for the same project are cached separately.
    Sharing one cache would hand the query path a writable handle whenever an
    index had already run in the same process, which would quietly defeat the
    guarantee that reading cannot modify knowledge.
    """

    def __init__(self, knowledge_dir: Path):
        self.knowledge_dir = knowledge_dir
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self._graphs: dict[str, KnowledgeGraph] = {}
        self._readonly: dict[str, KnowledgeGraph] = {}

    def get_graph(self, project_id: str) -> KnowledgeGraph:
        if project_id not in self._graphs:
            # A writable handle must see the other writable handle's writes.
            existing = self._readonly.pop(project_id, None)
            if existing is not None:
                existing.close()
            self._graphs[project_id] = KnowledgeGraph(self.knowledge_dir, project_id)
        return self._graphs[project_id]

    def get_existing(self, project_id: str) -> KnowledgeGraph | None:
        """Open an already-indexed project for reading, or return ``None``.

        This is the entry point the query path uses. It never creates a
        database: a project that was never indexed reports "no knowledge"
        instead of leaving an empty file behind for every path ever queried.
        The returned graph is opened read-only, so a bug on the query path
        cannot write even when an index has already happened in this process.
        """
        writable = self._graphs.get(project_id)
        if writable is not None:
            # Cache the view so repeated queries reuse one handle. An uncached
            # view would be collected only at an unpredictable later point, and
            # each live handle keeps the database file locked on Windows.
            view = self._readonly.get(project_id)
            if view is None:
                view = writable.readonly_view()
                self._readonly[project_id] = view
            return view
        if project_id in self._readonly:
            return self._readonly[project_id]
        try:
            graph = KnowledgeGraph(self.knowledge_dir, project_id, readonly=True)
        except (FileNotFoundError, OSError):
            return None
        self._readonly[project_id] = graph
        return graph

    def close_all(self) -> None:
        for g in self._graphs.values():
            g.close()
        for g in self._readonly.values():
            g.close()
        self._graphs.clear()
        self._readonly.clear()

    def list_projects(self) -> list[str]:
        """Every project id this directory holds knowledge for.

        Read from the databases themselves rather than from filenames, because
        the filename is only an encoding of the id.
        """
        found: list[str] = []
        seen: set[str] = set()
        candidates = sorted(self.knowledge_dir.glob(f"{DB_FILENAME_PREFIX}*{DB_SUFFIX}"))
        # Also consider legacy pre-Phase-8 files, including the alternate data
        # streams Windows created for them.
        for path in sorted(self.knowledge_dir.iterdir()) if self.knowledge_dir.is_dir() else []:
            if path in candidates:
                continue
            if path.is_file() and not path.name.startswith(DB_FILENAME_PREFIX):
                for stream in _legacy_streams(path):
                    candidates.append(stream)
        for path in candidates:
            if not path.is_file():
                continue
            project_id = self._read_project_id(path)
            if project_id and project_id not in seen:
                seen.add(project_id)
                found.append(project_id)
        return found

    @staticmethod
    def _read_project_id(path: Path) -> str:
        """The project id recorded inside a knowledge database, or ""."""
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'project_id'"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            # Not a knowledge database, or unreadable: not an error here, this
            # is only an enumeration.
            return ""
        return str(row[0]) if row else ""

    def delete_project(self, project_id: str) -> bool:
        for cache in (self._graphs, self._readonly):
            graph = cache.pop(project_id, None)
            if graph is not None:
                graph.close()
        # Another manager over the same directory may still hold the file open,
        # and on Windows that makes it undeletable. Release those handles rather
        # than reporting a deletion that cannot happen.
        _release_shared(self.knowledge_dir, project_id)
        removed = False
        for path in (self.knowledge_dir / db_filename(project_id),
                     legacy_db_path(self.knowledge_dir, project_id)):
            try:
                if path.is_file():
                    path.unlink()
                    removed = True
                    for sidecar in (path.with_name(path.name + "-wal"),
                                    path.with_name(path.name + "-shm")):
                        if sidecar.is_file():
                            sidecar.unlink()
            except OSError:
                # A stream still held open elsewhere cannot be removed; report
                # honestly rather than claiming a deletion that did not happen.
                log.warning("could not delete knowledge database %s", path)
        return removed


# One manager per knowledge directory, held for the life of the process. Tool
# calls reuse it, so a session opens one connection per project it touches
# rather than a fresh set on every call. Handles are released explicitly, by
# ``close_shared_managers()`` at shutdown or per project on deletion, because
# an open SQLite connection makes its file undeletable on Windows.
_SHARED_MANAGERS: dict[Path, KnowledgeGraphManager] = {}


def shared_manager(knowledge_dir: Path) -> KnowledgeGraphManager:
    """The manager for a knowledge directory, one per directory."""
    key = Path(knowledge_dir).resolve()
    existing = _SHARED_MANAGERS.get(key)
    if existing is not None:
        return existing
    created = KnowledgeGraphManager(key)
    _SHARED_MANAGERS[key] = created
    return created


def close_shared_managers() -> None:
    """Close and forget every shared manager. Used by tests and shutdown."""
    for mgr in list(_SHARED_MANAGERS.values()):
        mgr.close_all()
    _SHARED_MANAGERS.clear()


def _release_shared(knowledge_dir: Path, project_id: str) -> None:
    """Drop a shared manager's handle on one project's database.

    Called before the file is removed, because an open SQLite connection makes
    the file undeletable on Windows.
    """
    try:
        key = Path(knowledge_dir).resolve()
    except OSError:
        return
    mgr = _SHARED_MANAGERS.get(key)
    if mgr is None:
        return
    for cache in (mgr._graphs, mgr._readonly):
        graph = cache.pop(project_id, None)
        if graph is not None:
            graph.close()


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