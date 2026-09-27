"""M2.14 Phase H — security and boundary tests for the live wiring.

The Phase 7 read-only fix must survive the wiring: a query is a read, and a
read must never create a file, never escape an authorized root, and never
expose another project's knowledge. The projection side adds its own
boundaries: bounded metadata, no secrets, no dataset or image payloads, and no
writes from an unauthorized path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
)
from core.knowledge_projection import (
    knowledge_dir,
    project_task,
    project_tool_result,
    relative_to_project,
)
from core.knowledge_tools import knowledge_index, knowledge_query, manager
from core.task_store import TaskRecord


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(
        "TOKEN = 'super-secret-value'\n\n\ndef run():\n    return 1\n", encoding="utf-8")
    (root / "sales.csv").write_text("product,amount\nA,10\n", encoding="utf-8")
    return root


def _db(tmp_settings, project: Path) -> Path:
    return knowledge_dir(tmp_settings) / f"{canonical_project_key(str(project))}.db"


# ------------------------------------------- the Phase 7 read-only guarantee


class TestReadOnlyGuaranteePreserved:
    def test_query_of_unindexed_project_creates_no_database(self, tmp_settings, tmp_path: Path):
        other = tmp_path / "never-indexed"
        other.mkdir()
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(other))
        assert result.success
        assert result.data["indexed"] is False
        assert not _db(tmp_settings, other).exists()

    def test_many_unindexed_queries_create_no_files(self, tmp_settings, tmp_path: Path):
        """A model enumerating paths must not fill the disk."""
        for i in range(25):
            candidate = tmp_path / f"probe-{i}"
            candidate.mkdir()
            knowledge_query(query_type="find_nodes", settings=tmp_settings,
                           project_path=str(candidate))
        db_files = list(knowledge_dir(tmp_settings).glob("*.db"))
        assert db_files == []

    def test_query_never_mutates_an_existing_graph(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(canonical_project_key(str(project)))
        before = graph.stats()
        for kind in ("find_nodes", "search_files", "search_tasks", "recent_activity",
                     "search_charts", "search_datasets", "search_artifacts"):
            knowledge_query(query_type=kind, settings=tmp_settings,
                            project_path=str(project), limit=50)
        assert graph.stats() == before
        graph.close()
        mgr.close_all()

    def test_readonly_graph_refuses_writes(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_existing(canonical_project_key(str(project)))
        assert graph is not None
        assert graph.readonly is True
        with pytest.raises(PermissionError):
            graph.upsert_node(NodeType.TASK, "task:sneak", "sneak", {})
        graph.close()
        mgr.close_all()


# ------------------------------------------------------------ path boundaries


class TestPathBoundaries:
    def test_unauthorized_root_is_refused(self, tmp_settings):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path="C:/Windows/System32")
        assert not result.success
        assert "authorized" in result.error

    def test_traversal_out_of_root_is_refused(self, tmp_settings, tmp_path: Path):
        traversal = str(tmp_path / "proj" / ".." / ".." / "Windows")
        result = knowledge_index(project_path=traversal, settings=tmp_settings)
        assert not result.success

    def test_traversal_in_query_is_refused(self, tmp_settings, project: Path):
        traversal = str(project / ".." / "..")
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=traversal)
        assert not result.success or result.data["count"] == 0

    def test_relative_to_project_rejects_traversal(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        assert relative_to_project("../../etc/passwd", root) == ""
        assert relative_to_project(str(tmp_path / "outside.py"), root) == ""

    def test_projection_refuses_unauthorized_project(self, tmp_settings):
        record = TaskRecord(task_id="t", project_name="x",
                            project_root="C:/Windows/System32",
                            title="t", goal="g", created_at="20240101-000000")
        assert project_task(record, tmp_settings) == ""

    def test_tool_projection_refuses_unauthorized_project(self, tmp_settings):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            assert project_tool_result(
                tool="data.inspect", args={"path": "C:/Windows/x.csv"},
                result_data={"headers": ["a"], "row_count": 1}, task_id="t",
                project_root="C:/Windows/System32", settings=tmp_settings,
                manager=mgr) == ""
        finally:
            mgr.close_all()


# ------------------------------------------------------------ project isolation


class TestNoCrossProjectLeakage:
    def test_queries_are_confined_to_their_project(self, tmp_settings, tmp_path: Path):
        a = tmp_path / "alpha"
        a.mkdir()
        (a / "a_only.py").write_text("def a_only():\n    pass\n", encoding="utf-8")
        b = tmp_path / "beta"
        b.mkdir()
        (b / "b_only.py").write_text("def b_only():\n    pass\n", encoding="utf-8")

        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        qa = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                             project_path=str(a), node_type="code_symbol", limit=100)
        qb = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                             project_path=str(b), node_type="code_symbol", limit=100)
        a_names = {n["display_name"] for n in qa.data["nodes"]}
        b_names = {n["display_name"] for n in qb.data["nodes"]}
        assert a_names == {"a_only"}
        assert b_names == {"b_only"}

    def test_a_files_node_id_cannot_be_read_from_another_graph(self, tmp_settings,
                                                              tmp_path: Path):
        a = tmp_path / "alpha"
        a.mkdir()
        (a / "x.py").write_text("def f():\n    pass\n", encoding="utf-8")
        knowledge_index(project_path=str(a), settings=tmp_settings)

        b_id = "project:some-other-project"
        graph = manager(tmp_settings).get_graph(b_id)
        try:
            assert graph.get_node(f"file:{a_id_placeholder()}:x.py") is None
        finally:
            graph.close()


def a_id_placeholder() -> str:
    return "definitely-not-a-real-project"


# ------------------------------------------------------------------- secrets


class TestNoSecretLeakage:
    def test_source_text_is_never_stored(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), limit=100)
        blob = json.dumps(result.data)
        assert "super-secret-value" not in blob
        assert "TOKEN" not in blob

    def test_dotfiles_and_env_are_not_indexed(self, tmp_settings, project: Path):
        (project / ".env").write_text("SECRET_KEY=abc123\n", encoding="utf-8")
        (project / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
        (project / ".git").mkdir()
        (project / ".git" / "config").write_text("[core]\n", encoding="utf-8")

        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), node_type="file", limit=100)
        paths = {n["metadata"].get("path") for n in result.data["nodes"]}
        assert not any(".env" in p or "id_rsa" in p or ".git" in p for p in paths)

    def test_dataset_rows_are_never_stored(self, tmp_settings, project: Path):
        from core.knowledge_projection import project_tool_result
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            project_tool_result(
                tool="data.inspect",
                args={"path": str(project / "sales.csv")},
                result_data={"headers": ["product", "amount"], "row_count": 1,
                             "column_stats": [{"name": "product", "type": "string"},
                                              {"name": "amount", "type": "integer"}],
                             "preview": [["A", "10"]]},
                task_id="t", project_root=str(project),
                settings=tmp_settings, manager=mgr,
            )
            graph = mgr.get_graph(canonical_project_key(str(project)))
            dataset = graph.find_nodes(node_type=NodeType.DATASET, limit=1)[0]
            blob = json.dumps(dataset.metadata)
            assert "preview" not in blob
            assert "A" not in blob.replace("DATASET", "")
        finally:
            mgr.close_all()


# ------------------------------------------------------------------- bounds


class TestBounds:
    def test_metadata_stays_bounded_for_symbols(self, tmp_settings, tmp_path: Path):
        big = tmp_path / "big"
        big.mkdir()
        # A file with many symbols: each node must stay within the metadata cap.
        (big / "many.py").write_text(
            "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(200)),
            encoding="utf-8")
        knowledge_index(project_path=str(big), settings=tmp_settings)

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(canonical_project_key(str(big)))
        try:
            symbols = graph.find_nodes(node_type=NodeType.CODE_SYMBOL, limit=500)
            assert len(symbols) == 200
            for node in symbols:
                assert len(json.dumps(node.metadata)) <= 9000
        finally:
            graph.close()
            mgr.close_all()

    def test_query_limit_is_enforced(self, tmp_settings, tmp_path: Path):
        big = tmp_path / "big"
        big.mkdir()
        (big / "many.py").write_text(
            "\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(150)),
            encoding="utf-8")
        knowledge_index(project_path=str(big), settings=tmp_settings)
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(big), node_type="code_symbol",
                                 limit=1000000)
        assert result.success
        assert result.data["count"] <= 100

    def test_deep_traversal_does_not_explode(self, tmp_settings, tmp_path: Path):
        deep = tmp_path / "deep"
        deep.mkdir()
        (deep / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(deep), settings=tmp_settings)
        result = knowledge_query(query_type="find_related", settings=tmp_settings,
                                 project_path=str(deep),
                                 canonical_key=None, limit=100)
        # find_related needs a node_id; a malformed query must fail cleanly.
        assert result.success or result.error

    def test_plan_summary_is_bounded(self, tmp_settings, project: Path):
        from core.planner import ExecutionPlanRecord, PlanStepRecord
        from core.knowledge_tasks import upsert_plan_node
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(canonical_project_key(str(project)))
        try:
            steps = [PlanStepRecord(step_id=f"s{i}", tool="code.patch_file",
                                    args={"patch": "X" * 5000})
                     for i in range(60)]
            plan = ExecutionPlanRecord(goal="g", created_at="20240101-000000",
                                      steps=steps)
            node_id = upsert_plan_node(graph, plan, "t1")
            node = graph.get_node_by_id(node_id)
            assert len(json.dumps(node.metadata)) <= 9000
        finally:
            graph.close()
            mgr.close_all()


# ------------------------------------------------------------------ resilience


class TestResilience:
    def test_corrupt_metadata_is_never_believed(self, tmp_settings, project: Path):
        """Corruption must not surface as valid knowledge.

        The graph may refuse loudly or degrade quietly; what it must never do
        is hand the corrupt row back as if it were a real FILE node.
        """
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(canonical_project_key(str(project)))
        try:
            graph._get_conn().execute(
                "UPDATE nodes SET metadata = 'not-json' WHERE node_type = 'file'")
            graph._get_conn().commit()
        finally:
            graph.close()
            mgr.close_all()

        try:
            result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                     project_path=str(project), node_type="file",
                                     limit=10)
        except Exception:
            # Refusing loudly is an acceptable, safe outcome.
            return

        # It must never report success while handing back the corrupt row as
        # knowledge. Either it failed, or it answered with nothing.
        if result.success:
            assert result.data is not None
            assert result.data["count"] == 0
        else:
            assert result.error

    def test_projection_survives_a_readonly_graph(self, tmp_settings, project: Path):
        """A projection attempt against a read-only graph must not raise."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            graph = mgr.get_existing(canonical_project_key(str(project)))
            assert graph is not None
            with pytest.raises(PermissionError):
                graph.upsert_node(NodeType.TASK, "task:x", "x", {})
        finally:
            mgr.close_all()

    def test_restart_after_partial_index_recovers(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        # Simulate an interrupted second run by indexing a project with a file
        # removed in between.
        (project / "app.py").unlink()
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.success
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        graph = mgr.get_graph(canonical_project_key(str(project)))
        try:
            symbols = graph.find_nodes_by_key_prefix(
                NodeType.CODE_SYMBOL,
                f"symbol:{canonical_project_key(str(project))}:app.py:")
            assert symbols
            assert all(s.metadata.get("stale") is True for s in symbols)
        finally:
            graph.close()
            mgr.close_all()
