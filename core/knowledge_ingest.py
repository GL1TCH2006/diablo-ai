"""M2.14 Phase 2 — Project/File Knowledge Ingestion.

Controlled ingestion of project structure into the knowledge graph.
Reuses existing workspace-root/path authorization.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

from config.settings import Settings
from core.knowledge_code import ingest_symbols_for_files, reconcile_symbols
from core.knowledge_graph import (
    STALE_DELETED,
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_project_key,
    ingest_project,
    ingest_directory,
    ingest_file,
    is_stale,
)
from core.logging_setup import get_logger

log = get_logger("knowledge_ingest")

# Directories to ignore during indexing
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "out", "target", ".idea", ".vscode", ".pytest_cache",
    ".tox", ".nox", "data", "logs", "backups", ".next", "coverage",
    "env", ".env", "venvs", "virtualenv",
}

# Binary-like extensions to skip content hashing
BINARY_EXT = {
    ".exe", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif",
    ".pyd", ".class", ".jar", ".zip", ".7z", ".gz", ".bin", ".obj",
    ".lib", ".whl", ".pyc", ".dat", ".db", ".ico", ".woff", ".woff2",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

# Maximum file size to process (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

#: Files whose *content* is credential material. Excluded by exact name so a
#: private key or environment file never enters the graph. Not a heuristic on
#: contents: the rule is that these names are secrets wherever they appear.
SECRET_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "secrets.yaml", "secrets.yml", "secrets.json",
    ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    "master.key", "service-account.json",
}

#: Extensions that carry key or credential material.
SECRET_EXTENSIONS = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
    ".ppk", ".asc", ".gpg", ".kdbx",
}

#: Substrings that mark a file as credential material by name.
SECRET_NAME_MARKERS = ("secret", "credential", "private_key", "privatekey", "apikey")


def is_secret_file(name: str) -> bool:
    """Whether a filename is credential material that must never be indexed.

    The milestone forbids storing raw secrets. Matching is by name only, so the
    decision is deterministic and cannot depend on reading file contents.
    """
    lowered = str(name or "").lower()
    if not lowered:
        return False
    if lowered in SECRET_FILENAMES:
        return True
    if Path(lowered).suffix in SECRET_EXTENSIONS:
        return True
    stem = Path(lowered).stem
    if stem in SECRET_FILENAMES:
        return True
    return any(marker in lowered for marker in SECRET_NAME_MARKERS)


def _is_ignored_dir(name: str) -> bool:
    return name in IGNORED_DIRS


def _is_binary_ext(ext: str) -> bool:
    return ext.lower() in BINARY_EXT


def _compute_content_hash(path: Path) -> str | None:
    """Compute SHA256 hash of file content (for text files only)."""
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return None
        if _is_binary_ext(path.suffix):
            return None
        content = path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]
    except (OSError, UnicodeDecodeError):
        return None


def _is_within_allowed(path: Path, settings: Settings) -> bool:
    """Check if path is within authorized workspace roots."""
    try:
        return any(path.is_relative_to(root) for root in settings.allowed_roots)
    except (ValueError, OSError):
        return False


def ingest_project_structure(
    graph: KnowledgeGraph,
    project_root: Path,
    project_name: str | None = None,
    settings: Settings | None = None,
    max_depth: int = 12,
    max_files: int = 2000,
) -> dict[str, int]:
    """Ingest a project's directory structure and files into the knowledge graph.

    Returns statistics about what was ingested.
    """
    project_root = project_root.resolve()

    # Authorization check
    if settings and not _is_within_allowed(project_root, settings):
        raise ValueError(f"Project root {project_root} is outside allowed workspace roots")

    name = project_name or project_root.name
    stats = {"directories": 0, "files": 0, "skipped_files": 0, "errors": 0}

    # Create/update project node
    proj_node = ingest_project(graph, project_root, name)

    # Walk directory tree
    dir_nodes: dict[str, str] = {"": proj_node.node_id}  # rel_path -> node_id
    indexed_python: list[str] = []
    indexed_files: list[str] = []

    def walk(directory: Path, depth: int, parent_rel: str) -> None:
        if depth > max_depth or stats["files"] >= max_files:
            return

        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            log.warning("Cannot read directory %s: %s", directory, exc)
            stats["errors"] += 1
            return

        for entry in entries:
            if stats["files"] >= max_files:
                break

            if entry.is_dir():
                if _is_ignored_dir(entry.name):
                    continue
                rel = entry.resolve().relative_to(project_root).as_posix()
                dir_node = ingest_directory(graph, proj_node, rel)
                dir_nodes[rel] = dir_node.node_id
                stats["directories"] += 1
                walk(entry, depth + 1, rel)
            elif entry.is_file():
                if entry.name.startswith("."):
                    continue
                if is_secret_file(entry.name):
                    # Credential material never enters the graph.
                    stats["skipped_files"] += 1
                    continue
                if not _is_within_allowed(entry, settings) if settings else True:
                    stats["skipped_files"] += 1
                    continue

                try:
                    stat = entry.stat()
                except OSError:
                    stats["skipped_files"] += 1
                    continue

                if stat.st_size > MAX_FILE_SIZE:
                    stats["skipped_files"] += 1
                    continue

                rel = entry.resolve().relative_to(project_root).as_posix()
                parent_rel_path = "/".join(rel.split("/")[:-1]) or ""
                parent_node_id = dir_nodes.get(parent_rel_path, proj_node.node_id)

                content_hash = _compute_content_hash(entry)
                file_node = ingest_file(
                    graph,
                    graph.get_node_by_id(parent_node_id) if parent_node_id != proj_node.node_id else proj_node,
                    rel,
                    stat.st_size,
                    stat.st_mtime,
                    entry.suffix.lower(),
                    content_hash,
                )
                stats["files"] += 1
                indexed_files.append(rel)
                # Same traversal, same authorization and size decisions as the
                # file node above: symbols are only ever derived from a file the
                # walk already decided to index.
                if entry.suffix.lower() == ".py":
                    indexed_python.append(rel)

    walk(project_root, 0, "")

    # Symbol ingestion runs off the traversal's own file list, so there is one
    # indexing pipeline rather than a second walk with its own authorization,
    # ignore rules and caps.
    symbol_stats = ingest_symbols_for_files(graph, project_root, indexed_python)
    stats["python_files"] = symbol_stats["files_processed"]
    stats["symbols"] = symbol_stats["symbols_ingested"]

    # Files that vanished since the last index must stop looking authoritative.
    stale = reconcile_symbols(graph, indexed_python)
    stats["stale_symbols"] = stale["stale_symbols"]

    # The file nodes themselves, and anything recorded about files that are no
    # longer on disk. A deleted file that still looks current is exactly the
    # failure this phase exists to prevent.
    removed = reconcile_deleted_files(graph, indexed_files, project_root)
    stats["stale_files"] = removed["stale_files"]
    stats["stale_datasets"] = removed["stale_datasets"]
    stats["stale_charts"] = removed["stale_charts"]
    stats["stale_artifacts"] = removed["stale_artifacts"]

    log.info(
        "Ingested project %s: %d dirs, %d files (%d symbols), %d skipped, %d errors",
        name, stats["directories"], stats["files"], stats["symbols"],
        stats["skipped_files"], stats["errors"],
    )
    return stats


def reconcile_deleted_files(
    graph: KnowledgeGraph,
    indexed_paths: list[str],
    project_root: Path | None = None,
) -> dict[str, int]:
    """Demote knowledge about things that are no longer on disk.

    A node is marked rather than deleted. Deleting would throw away the record
    that the thing ever existed, which is the useful part; keeping it unmarked
    would let a query offer a deleted file as current. The mark is explicit, and
    the query layer withholds stale nodes unless asked.

    Two different tests are used, because absence means different things:

    * FILE nodes are judged against the walk, because the walk is what decides
      what belongs to the project.
    * DATASET, CHART and ARTIFACT nodes are judged by asking the filesystem
      whether the file is still there. They may legitimately live outside the
      walk, so "the walk did not see it" would be a false alarm.
    """
    live = {str(p).replace("\\", "/").strip("/") for p in indexed_paths}
    counts = {"stale_files": 0, "stale_datasets": 0,
              "stale_charts": 0, "stale_artifacts": 0}
    types = {
        NodeType.FILE: "stale_files",
        NodeType.DATASET: "stale_datasets",
        NodeType.CHART: "stale_charts",
        NodeType.ARTIFACT: "stale_artifacts",
    }
    for node_type, counter in types.items():
        for node in graph.find_nodes(node_type=node_type, limit=100000,
                                     include_stale=True):
            if is_stale(node):
                continue
            path = str(node.metadata.get("path", "") or "").replace("\\", "/").strip("/")
            if not path:
                continue
            if node_type == NodeType.FILE:
                if path in live:
                    continue
            elif _still_on_disk(path, project_root):
                continue
            graph.mark_stale(node, STALE_DELETED)
            counts[counter] += 1
            if node_type == NodeType.FILE:
                # A deleted file's symbols are no longer defined by anything.
                for symbol in graph.find_nodes_by_key_prefix(
                    NodeType.CODE_SYMBOL, f"symbol:{graph.project_id}:{path}:"
                ):
                    if not is_stale(symbol):
                        graph.mark_stale(symbol, STALE_DELETED)
    return counts


def _still_on_disk(path: str, project_root: Path | None) -> bool:
    """Whether a recorded path still resolves to a real file.

    Without a project root there is nothing to resolve a relative path against,
    so the answer is "cannot tell" and the node is left alone. Guessing here
    would mark live knowledge stale, which is the worse error.
    """
    if project_root is None:
        return True
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(project_root) / path
    try:
        return candidate.is_file()
    except OSError:
        return True


def ingest_changed_file(
    graph: KnowledgeGraph,
    project_root: Path,
    rel_path: str,
    settings: Settings | None = None,
) -> Optional[dict[str, Any]]:
    """Re-ingest a single file that has changed.

    Returns the updated file node's metadata or None if not found/authorized.
    """
    project_root = project_root.resolve()
    abs_path = (project_root / rel_path).resolve()

    if settings and not _is_within_allowed(abs_path, settings):
        return None

    if not abs_path.is_file():
        return None

    try:
        stat = abs_path.stat()
    except OSError:
        return None

    if stat.st_size > MAX_FILE_SIZE:
        return None

    content_hash = _compute_content_hash(abs_path)
    file_key = canonical_file_key(graph.project_id, rel_path)
    existing = graph.get_node(file_key)

    if existing:
        # Update metadata with new hash/size/mtime
        metadata = dict(existing.metadata)
        metadata.update({
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "content_hash": content_hash or metadata.get("content_hash"),
        })
        updated = graph.upsert_node(NodeType.FILE, file_key, existing.display_name, metadata)
        return updated.metadata
    else:
        # New file - need to ensure parent directory exists
        parent_rel = "/".join(rel_path.split("/")[:-1]) or ""
        if parent_rel:
            # Walk up to find/create parent directories
            proj_node = graph.get_node(canonical_project_key(str(project_root)))
            if proj_node:
                dir_nodes: dict[str, str] = {"": proj_node.node_id}
                parts = parent_rel.split("/")
                current = ""
                for part in parts:
                    current = f"{current}/{part}" if current else part
                    if current not in dir_nodes:
                        dir_node = ingest_directory(graph, proj_node, current)
                        dir_nodes[current] = dir_node.node_id
                parent_node_id = dir_nodes[parent_rel]
            else:
                return None
        else:
            proj_node = graph.get_node(canonical_project_key(str(project_root)))
            if not proj_node:
                return None
            parent_node_id = proj_node.node_id

        parent_node = graph.get_node_by_id(parent_node_id)
        if not parent_node:
            return None
        file_node = ingest_file(
            graph,
            parent_node,
            rel_path,
            stat.st_size,
            stat.st_mtime,
            abs_path.suffix.lower(),
            content_hash,
        )
        return file_node.metadata


def remove_deleted_file(graph: KnowledgeGraph, rel_path: str) -> bool:
    """Mark a file as deleted (remove from graph)."""
    file_key = canonical_file_key(graph.project_id, rel_path)
    return graph.delete_node(file_key)


def get_project_stats(graph: KnowledgeGraph) -> dict[str, int]:
    """Get statistics about the ingested project."""
    nodes = graph.find_nodes(limit=10000)
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node.node_type.value] = counts.get(node.node_type.value, 0) + 1
    return counts


# ──────────────────────────────────────────────────────────────
# Integration with existing project detection


def discover_and_ingest_workspace(
    knowledge_manager,  # KnowledgeGraphManager
    settings: Settings,
    project_name: str | None = None,
) -> KnowledgeGraph:
    """Discover projects in workspace and ingest the active one.

    Returns the KnowledgeGraph for the active project.
    """
    from memory.project_memory import ProjectMemory

    memory = ProjectMemory(settings.memory_dir, settings.workspace_root)
    snapshots = memory.discover()

    if not snapshots:
        raise ValueError("No projects discovered in workspace")

    # Use active project or first discovered
    active = memory.get_active()
    target = None
    if active and project_name is None:
        target = next((s for s in snapshots if s.name == active.name), None)
    elif project_name:
        target = next((s for s in snapshots if s.name == project_name), None)
    if target is None:
        target = snapshots[0]

    project_id = canonical_project_key(target.path)
    graph = knowledge_manager.get_graph(project_id)

    # Ingest if not already done (check for existing PROJECT node)
    existing = graph.get_node(canonical_project_key(target.path))
    if not existing:
        ingest_project_structure(graph, Path(target.path), target.name, settings)

    return graph