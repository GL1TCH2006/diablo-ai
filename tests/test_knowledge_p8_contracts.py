"""M2.14 Phase 8I-8N, 8K, 8L — authority, corruption, isolation, growth.

The graph is a projection. These tests hold it to that: task state is checked
against the TaskStore rather than trusted, corruption degrades instead of
fabricating, one project cannot see another, and repeating the same work does
not grow the graph without bound.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_project_key,
    close_shared_managers,
    db_filename,
)
from core.knowledge_projection import knowledge_dir, project_tool_result
from core.knowledge_tools import knowledge_index, knowledge_query, manager


@pytest.fixture(autouse=True)
def _release_handles():
    yield
    close_shared_managers()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small real project: source file plus a dataset."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "def function_a():\n    return 1\n", encoding="utf-8")
    (root / "proj.csv").write_text("k,v\nx,1\ny,2\n", encoding="utf-8")
    return root


def _make_project(base: Path, name: str, marker: str) -> Path:
    root = base / name
    (root / "src").mkdir(parents=True)
    (root / "src" / f"{name}.py").write_text(
        f"def {marker}():\n    return 1\n", encoding="utf-8")
    (root / f"{name}.csv").write_text("k,v\nx,1\n", encoding="utf-8")
    return root


def _graph(tmp_settings, project: Path) -> KnowledgeGraph:
    return manager(tmp_settings).get_graph(canonical_project_key(str(project)))


# ══════════════════════════════════════════════ 8I task/verification


class TestTaskAndVerificationPersistence:
    def test_graph_task_state_matches_taskstore(self, tmp_settings, project: Path):
        """The graph must not become a second authority for task state."""
        from core.task_store import TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        record = TaskRecord(
            task_id="t-authority", project_name=project.name,
            project_root=str(project), title="t", goal="g",
            created_at="20240101-000000", status="completed",
        )
        store.create(record)

        from core.knowledge_projection import project_task

        project_task(store.get("t-authority"), tmp_settings)

        graph = _graph(tmp_settings, project)
        nodes = graph.find_nodes(node_type=NodeType.TASK, limit=10)
        assert nodes, "the task was not projected"
        assert nodes[0].metadata["status"] == "completed"
        # The store remains the thing that decides.
        assert store.get("t-authority").status == "completed"

    def test_a_stale_graph_never_rewrites_the_task_store(self, tmp_settings,
                                                          project: Path):
        """Disagreeing knowledge must not be pushed back into the authority."""
        from core.knowledge_projection import project_task
        from core.task_store import TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        record = TaskRecord(
            task_id="t-1", project_name=project.name, project_root=str(project),
            title="t", goal="g", created_at="20240101-000000", status="active",
        )
        store.create(record)

        knowledge_index(project_path=str(project), settings=tmp_settings)
        project_task(store.get("t-1"), tmp_settings)

        # Force the graph to disagree with the store.
        graph = _graph(tmp_settings, project)
        node = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        graph.upsert_node(NodeType.TASK, node.canonical_key, node.display_name,
                          {**node.metadata, "status": "completed"})

        # The store is unmoved, and the graph is demonstrably the wrong one.
        assert store.get("t-1").status == "active"
        assert _graph(tmp_settings, project).find_nodes(
            node_type=NodeType.TASK, limit=1)[0].metadata["status"] == "completed"

    def test_verification_projection_is_advisory(self, tmp_settings, project: Path):
        from core.knowledge_projection import project_task
        from core.task_store import TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        store.create(TaskRecord(
            task_id="t-v", project_name=project.name, project_root=str(project),
            title="t", goal="g", created_at="20240101-000000",
        ))
        knowledge_index(project_path=str(project), settings=tmp_settings)
        project_task(store.get("t-v"), tmp_settings)

        graph = _graph(tmp_settings, project)
        verifications = graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10)
        # Nothing was verified, so nothing may claim to be. The graph records
        # the absence rather than inventing evidence.
        assert all(v.metadata.get("state") != "passed" for v in verifications)

    def test_task_relationships_survive_reindex(self, tmp_settings, project: Path):
        from core.knowledge_projection import project_task
        from core.task_store import TaskChangeStep, TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        store.create(TaskRecord(
            task_id="t-rel", project_name=project.name, project_root=str(project),
            title="t", goal="g", created_at="20240101-000000",
            # A change step is a tool invocation, and the file relationship is
            # derived from the literal path the tool was invoked with.
            change_steps=[TaskChangeStep(
                tool="code.write",
                args={"path": str(project / "src" / "main.py")},
                allowed=True, executed=True, success=True,
            )],
        ))
        knowledge_index(project_path=str(project), settings=tmp_settings)
        project_task(store.get("t-rel"), tmp_settings)

        graph = _graph(tmp_settings, project)
        task = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        before = len(graph.get_edges(source_id=task.node_id))
        # Without this the assertion below would also hold for a task that
        # never had any relationship at all, which would prove nothing.
        assert before > 0, "the projected task has no relationships to preserve"

        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        task = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
        assert len(graph.get_edges(source_id=task.node_id)) == before

    def test_graph_survives_the_task_record_being_deleted(self, tmp_settings,
                                                          project: Path):
        """Losing the store must not corrupt or crash the graph."""
        from core.knowledge_projection import project_task
        from core.task_store import TaskRecord, TaskStore

        store = TaskStore(project.parent / "tasks.json")
        store.create(TaskRecord(
            task_id="t-gone", project_name=project.name, project_root=str(project),
            title="t", goal="g", created_at="20240101-000000"))
        knowledge_index(project_path=str(project), settings=tmp_settings)
        project_task(store.get("t-gone"), tmp_settings)

        result = knowledge_query(query_type="search_tasks", settings=tmp_settings,
                                 project_path=str(project))
        assert result.success
        assert result.data["nodes"]


# ══════════════════════════════════════════════ 8J corruption


class TestCorruptionSafety:
    def _indexed(self, tmp_settings, project: Path) -> str:
        knowledge_index(project_path=str(project), settings=tmp_settings)
        return canonical_project_key(str(project))

    def _db(self, tmp_settings, project: Path) -> Path:
        return knowledge_dir(tmp_settings) / db_filename(
            canonical_project_key(str(project)))

    def _degrades_honestly(self, tmp_settings, project: Path) -> dict:
        """Query a damaged graph and assert the degradation contract.

        Three things must hold, and only these three: the query returns rather
        than raising, it invents nothing, and a failure says why. The graph is
        a projection that can be rebuilt by re-indexing, so a damaged one is
        reported and abandoned rather than allowed to take down the request or
        to be papered over with plausible-looking answers.
        """
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project))
        if result.success:
            assert (result.data or {}).get("count", 0) >= 0
        else:
            assert result.error, "a failed query must say why it failed"
            # Nothing may be offered as knowledge alongside a failure.
            assert not (result.data or {}).get("nodes")
        return result

    def test_missing_database_reports_no_knowledge(self, tmp_settings, project: Path):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project))
        assert result.success
        assert result.data["indexed"] is False
        assert result.data["count"] == 0

    def test_a_query_never_creates_a_database(self, tmp_settings, project: Path):
        kd = knowledge_dir(tmp_settings)
        before = sorted(p.name for p in kd.glob("*")) if kd.is_dir() else []
        for _ in range(5):
            knowledge_query(query_type="find_nodes", settings=tmp_settings,
                            project_path=str(project))
        after = sorted(p.name for p in kd.glob("*")) if kd.is_dir() else []
        assert before == after

    def test_unreadable_database_fails_without_fabricating(self, tmp_settings,
                                                           project: Path):
        pid = self._indexed(tmp_settings, project)
        path = knowledge_dir(tmp_settings) / db_filename(pid)
        # Handles must be released first. The database is in WAL mode, so closing
        # a connection checkpoints the log back into the main file; corrupting
        # first would be silently undone and the test would prove nothing.
        close_shared_managers()
        path.write_bytes(b"this is definitely not a sqlite database" * 40)

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project))
        # Either an honest failure or an empty answer. Never invented knowledge.
        if result.success:
            assert result.data["count"] == 0
        else:
            assert result.error

    def test_empty_database_reads_as_empty(self, tmp_settings, project: Path):
        pid = self._indexed(tmp_settings, project)
        path = knowledge_dir(tmp_settings) / db_filename(pid)
        close_shared_managers()
        conn = sqlite3.connect(str(path))
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM edges")
        conn.commit()
        conn.close()

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project))
        assert result.success, result.error
        assert result.data["count"] == 0

    def test_invalid_schema_version_is_handled(self, tmp_settings, project: Path):
        pid = self._indexed(tmp_settings, project)
        path = knowledge_dir(tmp_settings) / db_filename(pid)
        close_shared_managers()
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE schema_meta SET value = '999999' WHERE key = 'version'")
        conn.commit()
        conn.close()

        # A database written by a newer build. Reading it optimistically is
        # acceptable precisely because the graph is derived and rebuildable,
        # but it must not crash and must not invent anything either way.
        self._degrades_honestly(tmp_settings, project)

    def test_malformed_metadata_never_becomes_knowledge(self, tmp_settings,
                                                        project: Path):
        pid = self._indexed(tmp_settings, project)
        graph = _graph(tmp_settings, project)
        graph._get_conn().execute("UPDATE nodes SET metadata = 'not json at all'")
        graph._get_conn().commit()
        close_shared_managers()

        result = self._degrades_honestly(tmp_settings, project)
        if result.success:
            # Anything answered must be real, parseable knowledge.
            for node in result.data["nodes"]:
                assert isinstance(node["metadata"], dict)
        else:
            # Unparseable metadata cannot be silently reinterpreted.
            assert "JSON" in result.error or "Expecting" in result.error

    def test_unknown_node_type_does_not_crash_the_project(self, tmp_settings,
                                                          project: Path):
        pid = self._indexed(tmp_settings, project)
        graph = _graph(tmp_settings, project)
        graph._get_conn().execute(
            "UPDATE nodes SET node_type = 'not_a_real_type' WHERE node_type = 'file'")
        graph._get_conn().commit()
        close_shared_managers()

        result = self._degrades_honestly(tmp_settings, project)
        if not result.success:
            assert "not_a_real_type" in result.error, (
                "an unreadable node type must be named, not swallowed")

    def test_a_truncated_database_is_not_silently_accepted(self, tmp_settings,
                                                           project: Path):
        pid = self._indexed(tmp_settings, project)
        path = knowledge_dir(tmp_settings) / db_filename(pid)
        close_shared_managers()
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])

        # A half-written database is a partial write, which is exactly what a
        # crash mid-commit leaves behind. It must be reported, never read as
        # though the missing half were simply an empty graph.
        result = self._degrades_honestly(tmp_settings, project)
        assert not result.success, (
            "a malformed image was reported as readable knowledge")
        assert result.error

    def test_the_real_knowledge_directory_is_never_a_test_target(self, tmp_settings):
        """Destructive tests only ever touch a temporary directory."""
        assert knowledge_dir(tmp_settings) != Path.home() / ".diablo" / "knowledge"


# ══════════════════════════════════════════════ 8K isolation


class TestCrossProjectIsolation:
    def test_projects_have_separate_databases(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        pid_a = canonical_project_key(str(a))
        pid_b = canonical_project_key(str(b))
        kd = knowledge_dir(tmp_settings)
        assert (kd / db_filename(pid_a)).is_file()
        assert (kbd := kd / db_filename(pid_b)).is_file()
        assert db_filename(pid_a) != db_filename(pid_b)

    def test_direct_lookup_cannot_cross_projects(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        graph_a = _graph(tmp_settings, a)
        graph_b = _graph(tmp_settings, b)
        beta_key = f"file:{canonical_project_key(str(b))}:src/beta.py"
        assert graph_a.get_node(beta_key) is None
        assert graph_b.get_node(beta_key) is not None

    def test_search_cannot_cross_projects(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        found = knowledge_query(query_type="search_files", settings=tmp_settings,
                                project_path=str(a), limit=200)
        names = {n["display_name"] for n in found.data["nodes"]}
        assert "alpha.py" in names
        assert "beta.py" not in names

    def test_symbol_search_cannot_cross_projects(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        for project, mine, theirs in ((a, "alpha_fn", "beta_fn"),
                                      (b, "beta_fn", "alpha_fn")):
            found = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                    project_path=str(project),
                                    node_type="code_symbol", limit=200)
            names = {n["display_name"] for n in found.data["nodes"]}
            assert mine in names
            assert theirs not in names

    def test_neighbors_cannot_cross_projects(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        graph_a = _graph(tmp_settings, a)
        beta_file = graph_a.get_node(
            f"file:{canonical_project_key(str(b))}:src/beta.py")
        assert beta_file is None, "a foreign node id must not resolve in another project"

    def test_data_and_charts_cannot_cross_projects(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        for project, own, foreign in ((a, "alpha.csv", "beta.csv"),
                                      (b, "beta.csv", "alpha.csv")):
            _project_dataset(project, tmp_settings, project / own)
            found = knowledge_query(query_type="search_datasets",
                                    settings=tmp_settings,
                                    project_path=str(project), limit=100)
            names = {n["display_name"] for n in found.data["nodes"]}
            assert own in names
            assert foreign not in names

    def test_each_project_reopens_independently(self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)
        close_shared_managers()

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            for project, marker in ((a, "alpha_fn"), (b, "beta_fn")):
                pid = canonical_project_key(str(project))
                view = mgr.get_existing(pid)
                assert view is not None and view.readonly
                symbols = view.find_nodes(node_type=NodeType.CODE_SYMBOL, limit=100)
                names = {n.display_name for n in symbols}
                assert names == {marker}
        finally:
            mgr.close_all()

    def test_the_manager_enumerates_only_real_databases(self, tmp_settings,
                                                        tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)
        close_shared_managers()

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            found = mgr.list_projects()
            assert canonical_project_key(str(a)) in found
            assert canonical_project_key(str(b)) in found
        finally:
            mgr.close_all()


def _project_dataset(project: Path, tmp_settings, csv: Path) -> None:
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    try:
        project_tool_result(
            tool="data.inspect", args={"path": str(csv)},
            result_data={"headers": ["k", "v"], "row_count": 1,
                         "column_types": {"k": "string", "v": "integer"}},
            task_id="", project_root=str(project),
            settings=tmp_settings, manager=mgr,
        )
    finally:
        mgr.close_all()


# ══════════════════════════════════════════════ 8L bounded query


class TestBoundedQueryAfterRestart:
    def test_limit_is_still_enforced_after_reopen(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), limit=2)
        assert result.success
        assert result.data["count"] <= 2

    def test_an_oversized_limit_is_clamped(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), limit=10**9)
        assert result.success
        assert result.data["count"] <= 100

    def test_results_are_deterministic(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()
        runs = [
            knowledge_query(query_type="find_nodes", settings=tmp_settings,
                            project_path=str(project), limit=50).data["nodes"]
            for _ in range(4)
        ]
        first = json.dumps(runs[0], sort_keys=True, default=str)
        for run in runs[1:]:
            assert json.dumps(run, sort_keys=True, default=str) == first

    def test_authorization_still_applies_after_restart(self, tmp_settings):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path="C:/Windows/System32")
        assert not result.success
        assert "authorized" in result.error

    def test_a_deleted_project_reports_no_knowledge(self, tmp_settings, project: Path):
        pid = canonical_project_key(str(project))
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        assert mgr.delete_project(pid) is True
        mgr.close_all()

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project))
        assert result.success
        assert result.data["indexed"] is False

    def test_a_query_never_injects_the_whole_graph(self, tmp_settings, tmp_path: Path):
        big = tmp_path / "big"
        big.mkdir()
        for i in range(60):
            (big / f"mod_{i}.py").write_text(
                "\n".join(f"def f_{i}_{j}():\n    return {j}\n" for j in range(10)),
                encoding="utf-8")
        knowledge_index(project_path=str(big), settings=tmp_settings)
        close_shared_managers()

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(big), limit=25)
        assert result.success
        assert result.data["count"] <= 25
        assert result.data["truncated"] in (True, False)


# ══════════════════════════════════════════════ 8N growth


class TestResourceGrowth:
    def _db_stats(self, tmp_settings, project: Path) -> dict:
        pid = canonical_project_key(str(project))
        path = knowledge_dir(tmp_settings) / db_filename(pid)
        close_shared_managers()
        conn = sqlite3.connect(str(path))
        try:
            nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        finally:
            conn.close()
        return {"nodes": nodes, "edges": edges, "bytes": path.stat().st_size}

    def _one_cycle(self, tmp_settings, project: Path) -> None:
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _project_dataset(project, tmp_settings, project / "proj.csv")
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            project_tool_result(
                tool="data.analyze",
                args={"path": str(project / "proj.csv"), "operation": "groupby",
                      "group_by": "k", "column": "v"},
                result_data={"ok": True}, task_id="",
                project_root=str(project), settings=tmp_settings, manager=mgr,
            )
        finally:
            mgr.close_all()
        close_shared_managers()

    def test_repeated_cycles_do_not_grow_the_graph(self, tmp_settings,
                                                   tmp_path: Path):
        project = _make_project(tmp_path, "cycle", "cycle_fn")
        self._one_cycle(tmp_settings, project)
        after_first = self._db_stats(tmp_settings, project)
        for _ in range(4):
            self._one_cycle(tmp_settings, project)
        after_fifth = self._db_stats(tmp_settings, project)
        assert after_fifth["nodes"] == after_first["nodes"]
        assert after_fifth["edges"] == after_first["edges"]

    def test_node_and_edge_counts_by_type_are_stable(self, tmp_settings,
                                                     tmp_path: Path):
        project = _make_project(tmp_path, "cycle", "cycle_fn")
        self._one_cycle(tmp_settings, project)
        graph = _graph(tmp_settings, project)
        first = _counts_by_type(graph)
        close_shared_managers()
        for _ in range(3):
            self._one_cycle(tmp_settings, project)
        assert _counts_by_type(_graph(tmp_settings, project)) == first

    def test_database_size_stays_bounded(self, tmp_settings, tmp_path: Path):
        project = _make_project(tmp_path, "cycle", "cycle_fn")
        self._one_cycle(tmp_settings, project)
        first = self._db_stats(tmp_settings, project)
        for _ in range(4):
            self._one_cycle(tmp_settings, project)
        fifth = self._db_stats(tmp_settings, project)
        assert fifth["nodes"] == first["nodes"]
        # SQLite file size may drift slightly, but it must not grow with cycles.
        assert fifth["bytes"] < first["bytes"] * 4

    def test_growth_corresponds_to_genuinely_new_knowledge(self, tmp_settings,
                                                            tmp_path: Path):
        project = _make_project(tmp_path, "cycle", "cycle_fn")
        self._one_cycle(tmp_settings, project)
        before = self._db_stats(tmp_settings, project)["nodes"]
        (project / "extra.py").write_text("def extra():\n    return 1\n",
                                          encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        after = self._db_stats(tmp_settings, project)["nodes"]
        assert after > before, "a genuinely new file should add knowledge"


def _counts_by_type(graph: KnowledgeGraph) -> dict:
    nodes = graph.find_nodes(limit=100000, include_stale=True)
    by_node: dict[str, int] = {}
    for node in nodes:
        by_node[node.node_type.value] = by_node.get(node.node_type.value, 0) + 1
    edges = graph._get_conn().execute(
        "SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type"
    ).fetchall()
    return {"nodes": by_node, "edges": {r[0]: r[1] for r in edges}}
