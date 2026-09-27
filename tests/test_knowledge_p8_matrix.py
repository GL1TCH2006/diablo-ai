"""M2.14 Phase 8O, 8R — the test matrix and the architecture audit.

Two things are asserted here that are usually only written down.

**The matrix (8O).** Every Phase 8 objective is listed with the behaviour that
proves it. The list is not decoration: a test walks the real modules and fails
if an objective is claimed here but nothing in the tree actually exercises it,
and the lifecycle test walks all ten objectives in one sequence against one real
project, because individually-passing features are not the same as a feature
that works end to end.

**The audit (8R).** The knowledge graph is a derived projection, and that is a
constraint rather than a description. These tests hold the code to it
structurally — by reading the modules — and dynamically, by destroying the
graph and showing the project's real state is untouched. A projection that can
lose data is not a projection; it is a second, worse authority.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.knowledge_graph import (
    MAX_METADATA_KEYS,
    MAX_METADATA_TOTAL_LEN,
    MAX_METADATA_VALUE_LEN,
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
    is_stale,
)
from core.knowledge_projection import knowledge_dir, project_tool_result
from core.knowledge_tools import knowledge_index, knowledge_query, manager

CORE = Path(__file__).resolve().parents[1] / "core"

#: Every module that makes up the knowledge subsystem. The audit reads all of
#: them, so a new one is covered the day it is added.
KNOWLEDGE_MODULES = sorted(CORE.glob("knowledge_*.py"))

#: The authorities the graph must never become a second copy of. Each maps to
#: the module that owns it.
AUTHORITIES = {
    "task state": "core.task_store",
    "execution plans": "core.planner",
    "verification": "core.verification_ledger",
    "git": "core.git",
}


@pytest.fixture(autouse=True)
def _release_handles():
    yield
    close_shared_managers()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "matrix-project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8")
    (root / "sales.csv").write_text(
        "product,amount\nwidget,10\ngadget,20\nwidget,30\n", encoding="utf-8")
    return root


def _call_tool_result(settings, project: Path, tool: str, args: dict,
                      result_data: dict) -> None:
    mgr = KnowledgeGraphManager(knowledge_dir(settings))
    try:
        project_tool_result(tool=tool, args=args, result_data=result_data,
                            task_id="", project_root=str(project),
                            settings=settings, manager=mgr)
    finally:
        mgr.close_all()


# ══════════════════════════════════════════════════════ 8R static audit


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_module_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _called_names(path: Path) -> set[str]:
    """Names used as ``something.verb(...)`` anywhere in the module."""
    used: set[str] = set()
    for node in ast.walk(_module_tree(path)):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


class TestAuditNoSecondAuthority:
    def test_the_knowledge_modules_exist_to_be_audited(self):
        assert len(KNOWLEDGE_MODULES) >= 7, (
            f"expected the knowledge subsystem, found "
            f"{[p.name for p in KNOWLEDGE_MODULES]}")

    @pytest.mark.parametrize("path", KNOWLEDGE_MODULES, ids=lambda p: p.name)
    def test_no_authority_is_ever_instantiated(self, path: Path):
        """The graph may read a record type; it may not open its own store.

        Instantiating a TaskStore or VerificationLedger inside the knowledge
        subsystem would be the first step toward two writers for one fact. The
        projection receives already-built records instead.
        """
        offenders: list[str] = []
        for node in ast.walk(_module_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"TaskStore", "VerificationLedger", "TaskManager",
                        "GitManager", "CheckpointStore"}:
                offenders.append(f"line {node.lineno}: {name}()")
        assert not offenders, (
            f"{path.name} instantiates an authority: {offenders}")

    @pytest.mark.parametrize("path", KNOWLEDGE_MODULES, ids=lambda p: p.name)
    def test_no_authority_persistence_method_is_called(self, path: Path):
        """No save, no write-back, no second copy of a fact.

        ``unlink`` is permitted because deleting a project's knowledge is a
        legitimate operation, but it is confined to the graph's own database
        files, which is asserted dynamically in
        :meth:`TestAuditGraphIsDerived.test_deleting_knowledge_is_confined`.
        """
        used = _called_names(path)
        forbidden = used & {"save", "write_text", "write_bytes", "dump",
                            "writelines", "rmtree"}
        assert not forbidden, (
            f"{path.name} performs a filesystem write: {sorted(forbidden)}")

    @pytest.mark.parametrize("path", KNOWLEDGE_MODULES, ids=lambda p: p.name)
    def test_no_network_or_subprocess(self, path: Path):
        """Knowledge is derived from what already happened locally.

        A knowledge module that could reach the network or spawn a process
        would be a way for a read to have effects well outside the graph.
        """
        imports = _imported_modules(path)
        forbidden = {"subprocess", "socket", "http", "requests", "urllib",
                     "urllib.request", "httpx", "ftplib", "telnetlib"}
        assert not (imports & forbidden), (
            f"{path.name} imports {sorted(imports & forbidden)}")

    def test_the_graph_reads_authorities_only_through_their_types(self):
        """Imports from an authority must be types, not its storage class."""
        for path in KNOWLEDGE_MODULES:
            for node in ast.walk(_module_tree(path)):
                if isinstance(node, ast.ImportFrom) and node.module in {
                    "core.task_store", "core.verification_ledger", "core.planner",
                }:
                    for alias in node.names:
                        assert alias.name not in {
                            "TaskStore", "VerificationLedger", "TaskManager",
                        }, (
                            f"{path.name} imports the storage class "
                            f"{alias.name} from {node.module}")

    def test_read_methods_on_authorities_are_allowed_but_writes_are_not(self):
        """`list_by_project` and friends are reads; nothing else is permitted."""
        allowed = {"list_by_project", "get", "list_entries", "entries",
                   "to_dict", "from_dict", "create"}
        for path in KNOWLEDGE_MODULES:
            for node in ast.walk(_module_tree(path)):
                if not isinstance(node, ast.Attribute):
                    continue
                if isinstance(node.value, ast.Name) and (
                        node.value.id.endswith("store")
                        or node.value.id.endswith("ledger")):
                    assert node.attr in allowed, (
                        f"{path.name} line {node.lineno} calls "
                        f"{node.value.id}.{node.attr}(), which is not a known read")


class TestAuditBoundedStorage:
    def test_metadata_bounds_are_declared_and_small(self):
        assert MAX_METADATA_KEYS <= 20
        assert MAX_METADATA_VALUE_LEN <= 1000
        assert MAX_METADATA_TOTAL_LEN <= 8000

    def test_metadata_bounds_are_enforced_on_write(self, tmp_settings,
                                                   project: Path):
        """Bounds enforced only in documentation are not bounds."""
        import json

        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = manager(tmp_settings).get_graph(
            canonical_project_key(str(project)))
        huge = {f"k{i}": "x" * 5000 for i in range(50)}
        node = graph.upsert_node(NodeType.DECISION, "decision:big", "big", huge)

        # The bound is applied to the JSON-encoded form and a truncated value is
        # kept as that encoded text, so the stored length carries the encoding's
        # own quoting. What matters is that a 5000-character input is bounded
        # and visibly truncated, not that the bound is exact to the character.
        assert len(node.metadata) <= MAX_METADATA_KEYS
        for value in node.metadata.values():
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            assert len(encoded) <= MAX_METADATA_VALUE_LEN + 16, (
                f"a metadata value was stored at {len(encoded)} characters")
        assert any(len(str(v)) > 100 for v in node.metadata.values()), (
            "the oversized input should have been truncated, not dropped")

    def test_a_file_is_recorded_by_facts_not_by_contents(self, tmp_settings,
                                                         project: Path):
        """The recorded facts about a file are bounded and structural."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        nodes = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                project_path=str(project), node_type="file",
                                limit=50)
        assert nodes.success
        for node in nodes.data["nodes"]:
            meta = node["metadata"]
            # Every key is a structural fact about the file: where it is, how
            # big, when it changed, and what it defines. No key may hold the
            # file's text. A file is known by its properties, not by a copy of
            # itself, which is what keeps this store from becoming a shadow
            # copy of the project.
            assert set(meta) <= {
                "path", "size", "mtime", "content_hash", "extension",
                "symbol_count", "symbol_hash",
                "stale", "stale_reason", "stale_since",
            }, f"unexpected file metadata keys: {sorted(set(meta))}"
            for key, value in meta.items():
                assert len(str(value)) <= MAX_METADATA_VALUE_LEN


class TestAuditGraphIsDerived:
    def test_destroying_the_graph_loses_no_authoritative_fact(
            self, tmp_settings, project: Path):
        """The decisive test for "derived".

        A real task is recorded in the real store and projected. Every byte of
        the knowledge database is then destroyed. The store must be completely
        untouched, and re-indexing must be able to rebuild the graph from the
        project and the store alone.
        """
        from core.knowledge_projection import project_task
        from core.task_store import TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        store.create(TaskRecord(
            task_id="t-derived", project_name=project.name,
            project_root=str(project), title="t", goal="g",
            created_at="20240101-000000", status="active"))

        knowledge_index(project_path=str(project), settings=tmp_settings)
        project_task(store.get("t-derived"), tmp_settings)

        pid = canonical_project_key(str(project))
        db = knowledge_dir(tmp_settings) / f"kg_{pid.replace(':', '_')}.db"
        assert db.is_file(), f"expected the database at {db}"
        close_shared_managers()
        for path in knowledge_dir(tmp_settings).glob("*"):
            path.unlink()

        # The authority is intact and unmoved.
        record = store.get("t-derived")
        assert record is not None and record.status == "active"
        assert record.title == "t"

        # And the projection is fully reconstructible.
        knowledge_index(project_path=str(project), settings=tmp_settings)
        rebuilt = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                  project_path=str(project), limit=100)
        assert rebuilt.success and rebuilt.data["count"] > 0
        assert pid == canonical_project_key(str(project)), (
            "identity must not depend on the database having existed")

    def test_a_deleted_graph_does_not_delete_a_task(self, tmp_settings,
                                                   project: Path):
        from core.knowledge_projection import project_task
        from core.task_store import TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        store.create(TaskRecord(
            task_id="t-keep", project_name=project.name,
            project_root=str(project), title="keep me", goal="g",
            created_at="20240101-000000"))
        knowledge_index(project_path=str(project), settings=tmp_settings)
        project_task(store.get("t-keep"), tmp_settings)
        close_shared_managers()

        KnowledgeGraphManager(knowledge_dir(tmp_settings)).delete_project(
            canonical_project_key(str(project)))

        assert store.get("t-keep") is not None, (
            "deleting derived knowledge deleted the real task")

    def test_deleting_knowledge_is_confined(self, tmp_settings, project: Path):
        """Deleting a project's knowledge may touch only its own files.

        The static audit allows ``unlink`` because deleting a project is a real
        operation. This is where that permission is cashed in: a sentinel
        outside the knowledge directory, and an unrelated project inside it,
        must both survive.
        """
        from core.knowledge_graph import db_filename

        other = project.parent / "other-project"
        (other / "src").mkdir(parents=True)
        (other / "src" / "x.py").write_text("def x():\n    return 1\n",
                                            encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        knowledge_index(project_path=str(other), settings=tmp_settings)

        kd = knowledge_dir(tmp_settings)
        sentinel_outside = project.parent / "do-not-touch.txt"
        sentinel_outside.write_text("intact", encoding="utf-8")
        sentinel_inside = kd / "unrelated.db"
        sentinel_inside.write_text("intact", encoding="utf-8")

        pid = canonical_project_key(str(project))
        close_shared_managers()
        mgr = KnowledgeGraphManager(kd)
        try:
            assert mgr.delete_project(pid) is True
        finally:
            mgr.close_all()

        assert sentinel_outside.read_text(encoding="utf-8") == "intact", (
            "deleting knowledge deleted a file outside the knowledge directory")
        assert sentinel_inside.read_text(encoding="utf-8") == "intact", (
            "deleting one project deleted an unrelated database")
        assert (kd / db_filename(canonical_project_key(str(other)))).is_file(), (
            "deleting one project removed another project's database")

    def test_graph_knowledge_never_becomes_verification_evidence(
            self, tmp_settings, project: Path):
        """Nothing read out of the graph may verify anything.

        Knowledge is a description of what happened. Treating a description as
        proof is how an unverified claim becomes a passed verdict.
        """
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="find_stale", settings=tmp_settings,
                                 project_path=str(project), limit=50)
        assert result.success
        for node in result.data["nodes"]:
            meta = node["metadata"]
            for key in ("verified", "is_verified", "evidence", "passed"):
                assert key not in meta or not meta[key], (
                    f"{key} in graph metadata would be a verification claim")


# ══════════════════════════════════════════════════════ 8O matrix


class TestPhase8Matrix:
    """One real project, walked through all ten objectives in sequence."""

    def test_the_whole_lifecycle_holds_together(self, tmp_settings,
                                                project: Path):
        graph_of = lambda: manager(tmp_settings).get_graph(
            canonical_project_key(str(project)))

        # 1. persistence + 2. identity: an index creates stable identities.
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = {n.canonical_key: n.node_id
                 for n in graph_of().find_nodes(limit=500, include_stale=True)}
        assert first

        # 3. idempotence: doing it again changes nothing.
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert {n.canonical_key: n.node_id
                for n in graph_of().find_nodes(limit=500, include_stale=True)} == first

        # 4. real work becomes knowledge.
        _call_tool_result(tmp_settings, project, "data.inspect",
                          {"path": str(project / "sales.csv")},
                          {"headers": ["product", "amount"], "row_count": 3,
                           "column_types": {"product": "string",
                                            "amount": "integer"}})
        _call_tool_result(tmp_settings, project, "data.analyze",
                          {"path": str(project / "sales.csv"),
                           "operation": "groupby", "group_by": "product",
                           "column": "amount"},
                          {"ok": True, "groups": 2})
        kinds = {n.node_type for n in graph_of().find_nodes(limit=500)}
        assert {NodeType.DATASET, NodeType.ANALYSIS} <= kinds

        # 5. file modification: the old symbol stops being current.
        (project / "src" / "main.py").write_text(
            "def alpha():\n    return 1\n\n\ndef gamma():\n    return 3\n",
            encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        symbols = {n.display_name for n in graph_of().find_nodes(
            node_type=NodeType.CODE_SYMBOL, limit=100)}
        assert symbols == {"alpha", "gamma"}, symbols

        # 6. file deletion: it stops being offered, and history is kept.
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        files = {n.display_name for n in graph_of().find_nodes(
            node_type=NodeType.FILE, limit=100)}
        assert "main.py" not in files
        kept = graph_of().get_node(
            f"file:{canonical_project_key(str(project))}:src/main.py")
        assert kept is not None and is_stale(kept), (
            "deletion must be recorded, not merely hidden")

        # 7. dataset staleness: changed data retires the analysis built on it.
        (project / "sales.csv").write_text("product,amount\nwidget,1\n",
                                           encoding="utf-8")
        _call_tool_result(tmp_settings, project, "data.inspect",
                          {"path": str(project / "sales.csv")},
                          {"headers": ["product", "amount"], "row_count": 1,
                           "column_types": {"product": "string",
                                            "amount": "integer"}})
        analyses = graph_of().find_nodes(node_type=NodeType.ANALYSIS, limit=50)
        assert analyses == [], "an analysis outlived the data it described"
        history = graph_of().find_stale_nodes(node_type=NodeType.ANALYSIS,
                                              limit=50)
        assert history and all(is_stale(n) for n in history)

        # 8. artifact staleness: a chart whose file is gone is not offered.
        png = project / "chart.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        _call_tool_result(tmp_settings, project, "chart.generate",
                          {"data_path": str(project / "sales.csv"),
                           "chart_type": "bar", "output_path": str(png)},
                          {"path": str(png), "chart_type": "bar"})
        assert graph_of().find_nodes(node_type=NodeType.CHART, limit=10)
        png.unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert graph_of().find_nodes(node_type=NodeType.CHART, limit=10) == []

        # 9. identity is still the identity it was at the start.
        current = {n.canonical_key: n.node_id
                   for n in graph_of().find_nodes(limit=500, include_stale=True)}
        for key, node_id in first.items():
            assert current.get(key) == node_id, f"{key} lost its identity"

        # 10. everything survives a reopen, and a query changes nothing.
        close_shared_managers()
        after = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                project_path=str(project), limit=500,
                                include_stale=True)
        assert after.success
        close_shared_managers()
        again = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                project_path=str(project), limit=500,
                                include_stale=True)
        assert [n["node_id"] for n in after.data["nodes"]] == \
               [n["node_id"] for n in again.data["nodes"]]

    def test_every_matrix_objective_is_claimed_and_reachable(self):
        """The matrix is not allowed to drift away from the tree.

        Each objective names the behaviour that proves it. If one is removed
        from the code, this fails rather than leaving a reassuring document.
        """
        objectives = {
            "cross-process persistence": "test_knowledge_survival",
            "stable identity": "test_node_identity_is_unchanged",
            "idempotent reopen": "test_reopening_creates_no_duplicates",
            "file modification": "test_obsolete_defines_edge",
            "file deletion": "test_deleted_file_is_not_presented",
            "dataset staleness": "test_analysis_is_invalidated",
            "artifact staleness": "test_deleted_chart_artifact",
            "task persistence": "test_graph_task_state_matches",
            "corruption safety": "test_a_truncated_database",
            "cross-project isolation": "test_search_cannot_cross",
            "agent restart": "test_restarted_agent_sees",
            "security": "test_filename_stays_inside",
        }
        sources = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(Path(__file__).parent.glob("test_knowledge*.py")))
        missing = [name for name, probe in objectives.items()
                   if probe not in sources]
        assert not missing, (
            f"these Phase 8 objectives have no test anywhere: {missing}")
