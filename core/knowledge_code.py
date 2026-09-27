"""M2.14 Phase 3 — Code Relationships Extraction.

Lightweight Python code intelligence using stdlib ast module.
Extracts symbols (functions, classes, methods) and import relationships.
"""

from __future__ import annotations

import ast
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.knowledge_graph import (
    STALE_CHANGED,
    STALE_DELETED,
    STALE_UNREADABLE,
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_symbol_key,
    is_stale,
    with_stale,
)
from core.logging_setup import get_logger

log = get_logger("knowledge_code")

# Maximum file size to parse (1MB)
MAX_PARSE_SIZE = 1 * 1024 * 1024


@dataclass
class Symbol:
    name: str
    kind: str  # "function", "class", "method"
    qualified_name: str
    line_start: int
    line_end: int
    parent_class: str | None = None


def _compute_file_hash(path: Path) -> str | None:
    """Compute content hash for change detection."""
    try:
        if path.stat().st_size > MAX_PARSE_SIZE:
            return None
        content = path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]
    except OSError:
        return None


def extract_python_symbols(file_path: Path) -> list[Symbol]:
    """Extract symbols from a Python file using AST."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    if len(content) > MAX_PARSE_SIZE:
        return []

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as exc:
        log.debug("Syntax error in %s: %s", file_path, exc)
        return []

    symbols: list[Symbol] = []

    class SymbolVisitor(ast.NodeVisitor):
        def __init__(self):
            self.current_class: str | None = None

        def visit_ClassDef(self, node: ast.ClassDef):
            qualified = f"{self.current_class}.{node.name}" if self.current_class else node.name
            symbols.append(Symbol(
                name=node.name,
                kind="class",
                qualified_name=qualified,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
            ))
            prev_class = self.current_class
            self.current_class = qualified
            self.generic_visit(node)
            self.current_class = prev_class

        def visit_FunctionDef(self, node: ast.FunctionDef):
            if self.current_class:
                kind = "method"
                qualified = f"{self.current_class}.{node.name}"
                parent = self.current_class
            else:
                kind = "function"
                qualified = node.name
                parent = None
            symbols.append(Symbol(
                name=node.name,
                kind=kind,
                qualified_name=qualified,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                parent_class=parent,
            ))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self.visit_FunctionDef(node)

    visitor = SymbolVisitor()
    visitor.visit(tree)
    return symbols


def extract_python_imports(file_path: Path) -> list[str]:
    """Extract import statements from a Python file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    if len(content) > MAX_PARSE_SIZE:
        return []

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return []

    imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level
            prefix = "." * level
            for alias in node.names:
                if module:
                    imports.append(f"{prefix}{module}.{alias.name}")
                else:
                    imports.append(f"{prefix}{alias.name}")

    return imports


def resolve_import_to_file(
    import_name: str,
    source_file: Path,
    project_root: Path,
    graph: KnowledgeGraph,
) -> Optional[str]:
    """Resolve an import name to a file's canonical key in the graph.

    Returns the canonical_key of the target file if found in graph.
    """
    # Base directory the dotted path is resolved against: the project root for
    # an absolute import, the source file's (level-adjusted) directory for a
    # relative one. Getting this wrong silently drops every relative import.
    if import_name.startswith("."):
        # Count leading dots for level
        level = 0
        for ch in import_name:
            if ch == ".":
                level += 1
            else:
                break
        rest = import_name[level:]
        parts = rest.split(".") if rest else []
        base = source_file.parent
        for _ in range(level - 1):
            base = base.parent
    else:
        parts = import_name.split(".")
        base = project_root

    if not parts:
        return None

    target_path = base.joinpath(*parts)

    # Check for .py file or package
    candidates = [
        target_path.with_suffix(".py"),
        target_path / "__init__.py",
    ]

    # `from X.Y import name` refers to a module, not to a module called `name`.
    # Try the module one level up against the same base, so this also works for
    # relative imports.
    if len(parts) > 1:
        parent_path = base.joinpath(*parts[:-1])
        candidates.extend([
            parent_path.with_suffix(".py"),
            parent_path / "__init__.py",
        ])

    for candidate in candidates:
        try:
            rel = candidate.resolve().relative_to(project_root.resolve()).as_posix()
        except (ValueError, OSError):
            continue

        file_key = canonical_file_key(graph.project_id, rel)
        if graph.get_node(file_key):
            return file_key

    return None


def _stale(metadata: dict[str, Any], reason: str = STALE_DELETED) -> dict[str, Any]:
    """Mark a node's metadata as no longer authoritative, with a reason."""
    return with_stale(metadata, reason, time.strftime("%Y-%m-%d %H:%M:%S"))


#: Staleness now lives in one place so the query layer and the ingestion code
#: cannot disagree about what "stale" means.
_is_stale = is_stale


def _symbol_key_prefix(graph: KnowledgeGraph, file_rel_path: str) -> str:
    return f"symbol:{graph.project_id}:{file_rel_path.replace(chr(92), '/').strip('/')}:"


def _symbols_for_file(graph: KnowledgeGraph, file_rel_path: str) -> list:
    """Every CODE_SYMBOL node the graph believes ``file_rel_path`` defines."""
    return graph.find_nodes_by_key_prefix(NodeType.CODE_SYMBOL, _symbol_key_prefix(graph, file_rel_path))


def ingest_python_file_symbols(
    graph: KnowledgeGraph,
    file_rel_path: str,
    project_root: Path,
    force: bool = False,
) -> int:
    """Ingest symbols and imports for a single Python file.

    Idempotent and change-aware. Re-running on an unchanged file is a no-op;
    re-running on a changed file rewrites that file's symbols and marks any
    symbol it no longer defines as stale, so a renamed or removed function can
    never keep looking authoritative.

    A file that no longer parses (syntax error) keeps its previously ingested
    symbols *marked stale* rather than being silently emptied, because "we
    could not read it" and "it defines nothing" are different facts.

    Returns the number of symbols now live for this file.
    """
    abs_path = project_root / file_rel_path
    if not abs_path.is_file():
        return 0

    file_key = canonical_file_key(graph.project_id, file_rel_path)
    file_node = graph.get_node(file_key)
    if file_node is None:
        # Symbols are only meaningful once the file itself is indexed, so the
        # file node is the anchor. Not an error: the caller may be indexing a
        # file it decided to skip.
        log.debug("File node not found for %s; skipping symbols", file_rel_path)
        return 0

    # Change detection: the file node records the content hash its symbols were
    # derived from, so an unchanged file costs one dict lookup instead of a
    # re-parse.
    current_hash = _compute_file_hash(abs_path) or ""
    if not force and file_node.metadata.get("symbol_hash") == current_hash:
        return len([n for n in _symbols_for_file(graph, file_rel_path) if not _is_stale(n)])

    symbols = extract_python_symbols(abs_path)
    parse_failed = _has_syntax_error(abs_path)

    live_keys: set[str] = set()
    for symbol in symbols:
        sym_key = canonical_symbol_key(
            graph.project_id,
            file_rel_path,
            symbol.qualified_name,
            symbol.kind,
        )
        metadata: dict[str, Any] = {
            "file": file_rel_path,
            "kind": symbol.kind,
            "line_start": symbol.line_start,
            "line_end": symbol.line_end,
            # Source identity only. Never the source text itself.
            "content_hash": current_hash,
        }
        if symbol.parent_class:
            metadata["parent_class"] = symbol.parent_class
        metadata["stale"] = False

        sym_node = graph.upsert_node(NodeType.CODE_SYMBOL, sym_key, symbol.name, metadata)
        graph.upsert_edge(file_node.node_id, sym_node.node_id, RelationType.DEFINES)
        live_keys.add(sym_key)

        if symbol.kind == "method" and symbol.parent_class:
            class_key = canonical_symbol_key(
                graph.project_id, file_rel_path, symbol.parent_class, "class",
            )
            class_node = graph.get_node(class_key)
            if class_node is not None:
                graph.upsert_edge(class_node.node_id, sym_node.node_id, RelationType.CONTAINS)

    # Anything previously attributed to this file but not re-observed is
    # demoted. A file that stopped parsing is not evidence that its symbols
    # were removed, so that case gets its own reason rather than claiming a
    # change that may not have happened.
    if parse_failed:
        for existing in _symbols_for_file(graph, file_rel_path):
            if not _is_stale(existing):
                graph.mark_stale(existing, STALE_UNREADABLE)
    else:
        for existing in _symbols_for_file(graph, file_rel_path):
            if existing.canonical_key not in live_keys and not _is_stale(existing):
                graph.mark_stale(existing, STALE_CHANGED)

    _handle_imports(graph, abs_path, file_rel_path, project_root, file_node)

    # Record which content the symbols came from so the next run can skip.
    file_meta = dict(file_node.metadata)
    file_meta["symbol_hash"] = current_hash
    file_meta["symbol_count"] = len(live_keys)
    if parse_failed:
        file_meta["symbol_parse_error"] = True
    else:
        file_meta.pop("symbol_parse_error", None)
    graph.upsert_node(NodeType.FILE, file_key, file_node.display_name, file_meta)

    return len(live_keys)


def _has_syntax_error(path: Path) -> bool:
    """Whether the file exists but does not parse."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    if len(content) > MAX_PARSE_SIZE:
        return True
    try:
        ast.parse(content, filename=str(path))
    except SyntaxError:
        return True
    except ValueError:
        # e.g. source containing null bytes
        return True
    return False


def _handle_imports(
    graph: KnowledgeGraph,
    abs_path: Path,
    file_rel_path: str,
    project_root: Path,
    file_node,
) -> None:
    """Create FILE -> IMPORTS -> FILE edges for resolvable project imports.

    Only resolvable imports become edges; an import that cannot be tied to an
    indexed file is omitted rather than guessed.
    """
    for imp in extract_python_imports(abs_path):
        target_key = resolve_import_to_file(imp, abs_path, project_root, graph)
        if not target_key:
            continue
        target_node = graph.get_node(target_key)
        if target_node is not None:
            graph.upsert_edge(file_node.node_id, target_node.node_id, RelationType.IMPORTS)


def mark_file_symbols_stale(graph: KnowledgeGraph, file_rel_path: str) -> int:
    """Mark every symbol of a file stale. Used when the file is gone.

    Returns the number of symbols marked.
    """
    marked = 0
    for existing in _symbols_for_file(graph, file_rel_path):
        if _is_stale(existing):
            continue
        graph.upsert_node(
            NodeType.CODE_SYMBOL,
            existing.canonical_key,
            existing.display_name,
            _stale(dict(existing.metadata)),
        )
        marked += 1
    return marked


def ingest_symbols_for_files(
    graph: KnowledgeGraph,
    project_root: Path,
    rel_paths,
) -> dict[str, int]:
    """Ingest symbols for the given project-relative Python file paths.

    This is the single symbol-ingestion implementation. Both the live indexing
    flow and the standalone entry point route through it, so there is one code
    path to reason about.
    """
    project_root = project_root.resolve()
    stats = {"files_processed": 0, "symbols_ingested": 0}
    for rel in rel_paths:
        try:
            count = ingest_python_file_symbols(graph, rel, project_root)
        except (OSError, TypeError, ValueError, RecursionError, SyntaxError) as exc:
            # One unparseable file must never abort a project index.
            log.warning("symbol ingestion failed for %s: %s", rel, exc)
            continue
        if count > 0:
            stats["files_processed"] += 1
            stats["symbols_ingested"] += count
    return stats


def reconcile_symbols(graph: KnowledgeGraph, live_py_files) -> dict[str, int]:
    """Mark symbols stale for Python files that are no longer present.

    ``live_py_files`` is the set of project-relative paths the current walk
    actually indexed. Anything the graph still attributes to a Python file
    outside that set is stale, which is how a deleted or renamed module stops
    looking authoritative without losing its history.
    """
    live = {str(p).replace("\\", "/").strip("/") for p in live_py_files}
    marked = 0
    stale_files: set[str] = set()
    for node in graph.find_nodes(node_type=NodeType.CODE_SYMBOL, limit=100000):
        owner = str(node.metadata.get("file", "") or "")
        if not owner or owner in live or _is_stale(node):
            continue
        graph.upsert_node(
            NodeType.CODE_SYMBOL,
            node.canonical_key,
            node.display_name,
            _stale(dict(node.metadata)),
        )
        stale_files.add(owner)
        marked += 1
    return {"stale_symbols": marked, "stale_files": len(stale_files)}


def ingest_project_symbols(
    graph: KnowledgeGraph,
    project_root: Path,
    max_files: int = 500,
) -> dict[str, int]:
    """Ingest symbols for all Python files under a project root.

    Standalone entry point. Delegates to
    :func:`ingest_symbols_for_files` so the live indexing flow and this share
    one implementation.
    """
    project_root = project_root.resolve()
    py_files = []
    for path in sorted(project_root.rglob("*.py")):
        parts = path.relative_to(project_root).parts
        if any(part in IGNORED_DIRS for part in parts):
            continue
        py_files.append(path.relative_to(project_root).as_posix())
        if len(py_files) >= max_files:
            break

    stats = ingest_symbols_for_files(graph, project_root, py_files)
    stats.update(reconcile_symbols(graph, py_files))
    log.info(
        "Ingested symbols for project %s: %d files, %d symbols",
        project_root.name, stats["files_processed"], stats["symbols_ingested"],
    )
    return stats



# Re-export IGNORED_DIRS from knowledge_ingest for consistency
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "out", "target", ".idea", ".vscode", ".pytest_cache",
    ".tox", ".nox", "data", "logs", "backups", ".next", "coverage",
    "env", ".env", "venvs", "virtualenv",
}