"""M2.14 Phase 8C-8H, 8O — identity, idempotence and staleness.

The contract these tests pin down:

* identity is a pure function of *what* a node describes, so it survives a
  restart and a re-index, and re-running the same work adds nothing;
* knowledge that stops being true is marked, not deleted and not left looking
  current;
* a query never presents a stale node as current unless asked to.

Every project here is a temporary directory. Nothing touches a real workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.knowledge_graph import (
    STALE_CURRENT_PREDICATE,
    KnowledgeGraph,
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
    is_stale,
)
from core.knowledge_projection import knowledge_dir, project_tool_result
from core.knowledge_tools import knowledge_index, knowledge_query, manager


@pytest.fixture(autouse=True)
def _release_handles():
    yield
    close_shared_managers()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "def function_a():\n    return 1\n\n\ndef function_b():\n    return 2\n",
        encoding="utf-8",
    )
    (root / "data").mkdir()
    (root / "data" / "sales.csv").write_text(
        "product,amount\nwidget,10\ngadget,20\nwidget,30\n", encoding="utf-8"
    )
    return root


def _graph(tmp_settings, project: Path) -> KnowledgeGraph:
    return manager(tmp_settings).get_graph(canonical_project_key(str(project)))


def _nodes(tmp_settings, project: Path, node_type: str, stale: bool = False):
    result = knowledge_query(
        query_type="find_nodes", settings=tmp_settings, project_path=str(project),
        node_type=node_type, limit=200, include_stale=stale,
    )
    assert result.success, result.error
    return {n["display_name"]: n for n in result.data["nodes"]}


def _counts(graph: KnowledgeGraph) -> dict:
    nodes = graph.find_nodes(limit=100000, include_stale=True)
    by_type: dict[str, int] = {}
    for node in nodes:
        by_type[node.node_type.value] = by_type.get(node.node_type.value, 0) + 1
    edges = graph._get_conn().execute(
        "SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type"
    ).fetchall()
    return {
        "nodes": len(nodes),
        "edges": sum(r[1] for r in edges),
        "nodes_by_type": by_type,
        "edges_by_type": {r[0]: r[1] for r in edges},
    }


def _observe_dataset(project: Path, tmp_settings, path: Path | None = None,
                     task_id: str = "") -> None:
    """Project a dataset observation the way a real run would.

    The row count is read from the real file rather than hard-coded, so a test
    that changes the data sees the graph notice.
    """
    target = path or (project / "data" / "sales.csv")
    lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    headers = lines[0].split(",")
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    try:
        project_tool_result(
            tool="data.inspect", args={"path": str(target)},
            result_data={"headers": headers, "row_count": max(0, len(lines) - 1),
                         "column_types": {h: "string" for h in headers}},
            task_id=task_id, project_root=str(project),
            settings=tmp_settings, manager=mgr,
        )
    finally:
        mgr.close_all()


def _observe_analysis(project: Path, tmp_settings, operation: str = "groupby") -> None:
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    try:
        project_tool_result(
            tool="data.analyze",
            args={"path": str(project / "data" / "sales.csv"),
                  "operation": operation, "group_by": "product",
                  "column": "amount"},
            result_data={"ok": True}, task_id="",
            project_root=str(project), settings=tmp_settings, manager=mgr,
        )
    finally:
        mgr.close_all()


# ══════════════════════════════════════════════════════ 8C identity


class TestIdentityPersistence:
    def test_project_identity_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = _graph(tmp_settings, project).find_nodes(
            node_type=NodeType.PROJECT, limit=1)[0]
        knowledge_index(project_path=str(project), settings=tmp_settings)
        second = _graph(tmp_settings, project).find_nodes(
            node_type=NodeType.PROJECT, limit=1)[0]
        assert first.node_id == second.node_id
        assert first.canonical_key == second.canonical_key

    def test_file_identity_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = _nodes(tmp_settings, project, "file")
        for _ in range(3):
            knowledge_index(project_path=str(project), settings=tmp_settings)
        second = _nodes(tmp_settings, project, "file")
        assert set(first) == set(second)
        for name, node in first.items():
            assert second[name]["node_id"] == node["node_id"]

    def test_symbol_identity_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = _nodes(tmp_settings, project, "code_symbol")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        second = _nodes(tmp_settings, project, "code_symbol")
        for name, node in first.items():
            assert second[name]["node_id"] == node["node_id"]

    def test_dataset_identity_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        first = _nodes(tmp_settings, project, "dataset")
        _observe_dataset(project, tmp_settings)
        second = _nodes(tmp_settings, project, "dataset")
        assert len(first) == 1
        assert first["sales.csv"]["node_id"] == second["sales.csv"]["node_id"]

    def test_analysis_identity_is_the_question(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        _observe_analysis(project, tmp_settings)
        first = _nodes(tmp_settings, project, "analysis")
        _observe_analysis(project, tmp_settings)
        second = _nodes(tmp_settings, project, "analysis")
        assert len(first) == len(second) == 1
        node = list(first.values())[0]
        assert second["groupby on data/sales.csv"]["node_id"] == node["node_id"]

    def test_chart_identity_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        png = project / "data" / "chart.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        _observe_chart(project, tmp_settings, png)
        first = _nodes(tmp_settings, project, "chart")
        _observe_chart(project, tmp_settings, png)
        second = _nodes(tmp_settings, project, "chart")
        assert len(first) == len(second) == 1
        assert list(first.values())[0]["node_id"] == list(second.values())[0]["node_id"]

    def test_no_duplicate_project_or_file_nodes(self, tmp_settings, project: Path):
        for _ in range(4):
            knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        assert len(graph.find_nodes(node_type=NodeType.PROJECT, limit=50,
                                     include_stale=True)) == 1
        files = graph.find_nodes(node_type=NodeType.FILE, limit=200, include_stale=True)
        keys = [f.canonical_key for f in files]
        assert len(keys) == len(set(keys))


def _observe_chart(project: Path, tmp_settings, png: Path) -> None:
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    try:
        project_tool_result(
            tool="chart.generate",
            args={"data_path": str(project / "data" / "sales.csv"),
                  "chart_type": "bar", "output_path": str(png)},
            result_data={"path": str(png), "chart_type": "bar"},
            task_id="", project_root=str(project),
            settings=tmp_settings, manager=mgr,
        )
    finally:
        mgr.close_all()


# ══════════════════════════════════════════════════════ 8D idempotence


class TestIdempotentReindex:
    def test_index_twice_in_one_process_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = _counts(_graph(tmp_settings, project))
        knowledge_index(project_path=str(project), settings=tmp_settings)
        second = _counts(_graph(tmp_settings, project))
        assert first == second

    def test_index_five_times_is_stable(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = _counts(_graph(tmp_settings, project))
        for _ in range(4):
            knowledge_index(project_path=str(project), settings=tmp_settings)
        assert _counts(_graph(tmp_settings, project)) == first

    def test_repeated_index_does_not_duplicate_edges(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        first = _counts(_graph(tmp_settings, project))
        for _ in range(3):
            knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        rows = graph._get_conn().execute(
            "SELECT source_id, target_id, relation_type, COUNT(*) c "
            "FROM edges GROUP BY source_id, target_id, relation_type HAVING c > 1"
        ).fetchall()
        assert rows == [], "an edge was recorded more than once"
        assert _counts(graph) == first

    def test_changed_metadata_updates_rather_than_duplicates(self, tmp_settings,
                                                             project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        before = _nodes(tmp_settings, project, "file")["main.py"]
        (project / "src" / "main.py").write_text(
            "def function_a():\n    return 1\n" + "\n# padding\n" * 50,
            encoding="utf-8",
        )
        knowledge_index(project_path=str(project), settings=tmp_settings)
        after = _nodes(tmp_settings, project, "file")["main.py"]
        # Same identity, different facts.
        assert after["node_id"] == before["node_id"]
        assert after["metadata"]["size"] != before["metadata"]["size"]


# ══════════════════════════════════════════════════════ 8E modification


class TestFileModification:
    def test_new_symbol_appears_and_old_one_stops_being_current(
            self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert set(_nodes(tmp_settings, project, "code_symbol")) == {
            "function_a", "function_b"}

        (project / "src" / "main.py").write_text(
            "def function_a():\n    return 1\n\n\ndef function_c():\n    return 3\n",
            encoding="utf-8",
        )
        knowledge_index(project_path=str(project), settings=tmp_settings)

        current = _nodes(tmp_settings, project, "code_symbol")
        assert set(current) == {"function_a", "function_c"}
        assert all(n["state"] == "current" for n in current.values())

    def test_removed_symbol_is_not_silently_current(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").write_text(
            "def function_a():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)

        assert "function_b" not in _nodes(tmp_settings, project, "code_symbol")
        everything = _nodes(tmp_settings, project, "code_symbol", stale=True)
        assert everything["function_b"]["metadata"]["stale"] is True
        assert everything["function_b"]["metadata"]["stale_reason"] == "changed"

    def test_obsolete_defines_edge_does_not_look_current(self, tmp_settings,
                                                          project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        main = _nodes(tmp_settings, project, "file")["main.py"]
        before = len(graph.get_edges(
            source_id=_node_id(graph, main["canonical_key"])))
        assert before > 0

        (project / "src" / "main.py").write_text(
            "def function_a():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)

        graph = _graph(tmp_settings, project)
        live_symbol_ids = {
            n.node_id for n in graph.find_nodes(node_type=NodeType.CODE_SYMBOL, limit=200)
        }
        edges = graph.get_edges(source_id=_node_id(graph, main["canonical_key"]))
        for edge in edges:
            if edge.relation_type.value == "defines":
                assert edge.target_id in live_symbol_ids, (
                    "a defines edge still points at a symbol that is no longer "
                    "presented as current"
                )

    def test_content_hash_change_is_recorded(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        before = _nodes(tmp_settings, project, "file")["main.py"]["metadata"]
        (project / "src" / "main.py").write_text("def z():\n    return 9\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        after = _nodes(tmp_settings, project, "file")["main.py"]["metadata"]
        assert after["content_hash"] != before["content_hash"]


def _node_id(graph: KnowledgeGraph, canonical_key: str) -> str:
    node = graph.get_node(canonical_key)
    assert node is not None, canonical_key
    return node.node_id


# ══════════════════════════════════════════════════════ 8F deletion


class TestFileDeletion:
    def test_deleted_file_is_not_presented_as_current(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert "main.py" in _nodes(tmp_settings, project, "file")

        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)

        assert "main.py" not in _nodes(tmp_settings, project, "file")
        stale = _nodes(tmp_settings, project, "file", stale=True)
        assert stale["main.py"]["metadata"]["stale"] is True
        assert stale["main.py"]["metadata"]["stale_reason"] == "deleted"

    def test_deleted_file_symbols_are_demoted(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert _nodes(tmp_settings, project, "code_symbol") == {}
        everything = _nodes(tmp_settings, project, "code_symbol", stale=True)
        assert set(everything) == {"function_a", "function_b"}
        assert all(n["metadata"]["stale"] is True for n in everything.values())

    def test_deletion_is_reported_in_index_stats(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.data["stale_files"] >= 1
        assert result.data["stale_symbols"] >= 1

    def test_history_is_kept_not_deleted(self, tmp_settings, project: Path):
        """Marking, not deleting: the record that it existed survives."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        before = len(graph.find_nodes(node_type=NodeType.FILE, limit=200,
                                      include_stale=True))
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        after = len(_graph(tmp_settings, project).find_nodes(
            node_type=NodeType.FILE, limit=200, include_stale=True))
        assert after == before

    def test_restored_file_becomes_current_again(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert "main.py" not in _nodes(tmp_settings, project, "file")

        (project / "src" / "main.py").write_text(
            "def function_a():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        current = _nodes(tmp_settings, project, "file")
        assert "main.py" in current
        assert current["main.py"]["state"] == "current"

    def test_stale_knowledge_is_reachable_on_request(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="find_stale", settings=tmp_settings,
                                 project_path=str(project), limit=100)
        assert result.success, result.error
        names = {n["display_name"] for n in result.data["nodes"]}
        assert "main.py" in names
        assert all(n["state"] == "stale" for n in result.data["nodes"])


# ══════════════════════════════════════════════════════ 8G dataset


class TestDatasetStaleness:
    def test_changed_dataset_updates_in_place(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        before = _nodes(tmp_settings, project, "dataset")["sales.csv"]
        assert before["metadata"]["row_count"] == 3

        (project / "data" / "sales.csv").write_text(
            "product,amount\nwidget,10\ngadget,20\nwidget,30\nsprocket,40\n",
            encoding="utf-8",
        )
        _observe_dataset(project, tmp_settings)
        after = _nodes(tmp_settings, project, "dataset")["sales.csv"]
        assert after["node_id"] == before["node_id"]
        assert after["metadata"]["row_count"] == 4

    def test_analysis_is_invalidated_when_its_dataset_changes(self, tmp_settings,
                                                             project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        _observe_analysis(project, tmp_settings)
        assert len(_nodes(tmp_settings, project, "analysis")) == 1

        # The data moves underneath the analysis.
        (project / "data" / "sales.csv").write_text(
            "product,amount\nwidget,99\n", encoding="utf-8")
        _observe_dataset(project, tmp_settings)

        # The old analysis is not offered as a current answer any more.
        assert _nodes(tmp_settings, project, "analysis") == {}
        stale = _nodes(tmp_settings, project, "analysis", stale=True)
        node = list(stale.values())[0]
        assert node["metadata"]["stale_reason"] == "source_changed"

    def test_reanalysis_after_a_change_becomes_current(self, tmp_settings,
                                                       project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        _observe_analysis(project, tmp_settings)
        (project / "data" / "sales.csv").write_text(
            "product,amount\nwidget,99\n", encoding="utf-8")
        _observe_dataset(project, tmp_settings)
        assert _nodes(tmp_settings, project, "analysis") == {}

        _observe_analysis(project, tmp_settings)
        current = _nodes(tmp_settings, project, "analysis")
        assert len(current) == 1
        assert list(current.values())[0]["state"] == "current"

    def test_deleted_dataset_is_not_current(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        assert _nodes(tmp_settings, project, "dataset")

        (project / "data" / "sales.csv").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)

        assert _nodes(tmp_settings, project, "dataset") == {}
        stale = _nodes(tmp_settings, project, "dataset", stale=True)
        assert stale["sales.csv"]["metadata"]["stale_reason"] == "deleted"

    def test_a_dataset_the_walk_cannot_see_is_not_falsely_stale(self, tmp_settings,
                                                                project: Path):
        """Without a project root there is nothing to resolve against.

        "Cannot tell" must mean "leave it alone", never "mark it deleted".
        """
        from core.knowledge_ingest import reconcile_deleted_files

        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        before = _nodes(tmp_settings, project, "dataset")
        assert before

        graph = _graph(tmp_settings, project)
        counts = reconcile_deleted_files(graph, [], project_root=None)
        assert counts["stale_datasets"] == 0
        assert _nodes(tmp_settings, project, "dataset") == before

    def test_a_dataset_inside_the_project_survives_reindex(self, tmp_settings,
                                                            project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_dataset(project, tmp_settings)
        for _ in range(3):
            knowledge_index(project_path=str(project), settings=tmp_settings)
        assert _nodes(tmp_settings, project, "dataset"), (
            "a dataset whose file still exists was wrongly marked deleted"
        )


# ══════════════════════════════════════════════════════ 8H chart/artifact


class TestChartStaleness:
    def test_chart_survives_reindex_while_the_file_exists(self, tmp_settings,
                                                          project: Path):
        png = project / "data" / "chart.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_chart(project, tmp_settings, png)
        assert _nodes(tmp_settings, project, "chart")

        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert _nodes(tmp_settings, project, "chart"), (
            "a chart whose file still exists was wrongly marked deleted"
        )

    def test_deleted_chart_artifact_is_not_presented_as_present(self, tmp_settings,
                                                                 project: Path):
        png = project / "data" / "chart.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_chart(project, tmp_settings, png)
        assert _nodes(tmp_settings, project, "chart")

        png.unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)

        assert _nodes(tmp_settings, project, "chart") == {}
        stale = _nodes(tmp_settings, project, "chart", stale=True)
        assert list(stale.values())[0]["metadata"]["stale_reason"] == "deleted"

    def test_regenerating_a_chart_restores_it(self, tmp_settings, project: Path):
        png = project / "data" / "chart.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        knowledge_index(project_path=str(project), settings=tmp_settings)
        _observe_chart(project, tmp_settings, png)
        png.unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert _nodes(tmp_settings, project, "chart") == {}

        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 32)
        _observe_chart(project, tmp_settings, png)
        current = _nodes(tmp_settings, project, "chart")
        assert len(current) == 1
        assert list(current.values())[0]["state"] == "current"

    def test_regenerating_does_not_duplicate_chart_nodes(self, tmp_settings,
                                                          project: Path):
        png = project / "data" / "chart.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        knowledge_index(project_path=str(project), settings=tmp_settings)
        for _ in range(4):
            _observe_chart(project, tmp_settings, png)
        graph = _graph(tmp_settings, project)
        charts = graph.find_nodes(node_type=NodeType.CHART, limit=50,
                                  include_stale=True)
        assert len(charts) == 1


# ══════════════════════════════════════════════════════ staleness plumbing


class TestStalenessPlumbing:
    def test_node_dict_always_states_its_state(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        everything = _nodes(tmp_settings, project, "file", stale=True)
        assert everything, "include_stale should still return the current files"
        for node in everything.values():
            # Every node states its status, and it agrees with its metadata.
            expected = "stale" if node["metadata"].get("stale") is True else "current"
            assert node["state"] == expected
        assert "main.py" in {
            n["display_name"] for n in everything.values() if n["state"] == "stale"
        }

    def test_stale_predicate_does_not_match_a_string_value(self):
        """A metadata string that merely mentions stale is not the flag."""
        graph_predicate = STALE_CURRENT_PREDICATE
        assert '"stale": true' in graph_predicate
        assert graph_predicate.count("NOT LIKE") == 2

    def test_marking_twice_is_idempotent(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        node = graph.find_nodes(node_type=NodeType.FILE, limit=1)[0]
        first = graph.mark_stale(node, "deleted")
        again = graph.mark_stale(first, "deleted")
        assert first.metadata == again.metadata

    def test_stale_metadata_carries_a_reason_and_a_time(self, tmp_settings,
                                                        project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        node = _nodes(tmp_settings, project, "file", stale=True)["main.py"]
        assert node["metadata"]["stale_reason"] == "deleted"
        assert node["metadata"]["stale_since"]

    def test_stale_nodes_are_excluded_from_search_files(self, tmp_settings,
                                                        project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="search_files", settings=tmp_settings,
                                 project_path=str(project), limit=100)
        assert result.success, result.error
        assert all(n["display_name"] != "main.py" for n in result.data["nodes"])

    def test_readonly_graph_cannot_be_marked_stale(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            view = mgr.get_existing(canonical_project_key(str(project)))
            assert view is not None and view.readonly
            node = view.find_nodes(node_type=NodeType.FILE, limit=1)[0]
            with pytest.raises(PermissionError):
                view.mark_stale(node, "deleted")
        finally:
            mgr.close_all()
