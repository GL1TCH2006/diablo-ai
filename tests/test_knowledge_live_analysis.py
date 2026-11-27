"""M2.14 Phase D follow-up — live projection of ``data.analyze``.

The milestone requires that a groupby/statistics run be persisted as useful
knowledge, not just the file it read. These tests drive the real tool through
the real registry and assert the recorded analysis is bounded, correctly
linked, and never carries the data itself.
"""

from __future__ import annotations

from pathlib import Path

import json
import pytest

from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_project_key,
)
from core.knowledge_projection import knowledge_dir, project_tool_result
from core.knowledge_data import normalize_dataset_result


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "analysis-proj"
    root.mkdir()
    (root / "sales.csv").write_text(
        "product,region,amount\n"
        "A,north,10\n"
        "B,south,20\n"
        "A,south,30\n"
        "C,north,40\n",
        encoding="utf-8",
    )
    return root


def _project(project_tool_result_kwargs, settings, mgr) -> str:
    return project_tool_result(**project_tool_result_kwargs)


def _analyze_call(project: Path, settings, mgr, **overrides):
    args = {
        "path": str(project / "sales.csv"),
        "operation": "groupby",
        "group_by": "region",
        "column": "amount",
    }
    args.update(overrides)
    return project_tool_result(
        tool="data.analyze",
        args=args,
        result_data={"ok": True, "operation": "groupby", "groups": 2},
        task_id="",
        project_root=str(project),
        settings=settings,
        manager=mgr,
    )


class TestAnalyzeProjection:
    def test_groupby_creates_an_analysis_node(self, tmp_settings, project: Path):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            assert _analyze_call(project, tmp_settings, mgr)
            graph = mgr.get_graph(canonical_project_key(str(project)))
            analyses = graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)
            assert len(analyses) == 1
            assert analyses[0].metadata["operation"] == "groupby"
        finally:
            mgr.close_all()

    def test_analysis_records_the_question_not_the_answer(self, tmp_settings, project: Path):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            _analyze_call(project, tmp_settings, mgr)
            graph = mgr.get_graph(canonical_project_key(str(project)))
            node = graph.find_nodes(node_type=NodeType.ANALYSIS, limit=1)[0]
            blob = json.dumps(node.metadata)
            # The group_by/column question is kept; no rows or aggregates are.
            assert "region" in blob
            assert "north" not in blob
            assert "south" not in blob
        finally:
            mgr.close_all()

    def test_analysis_links_to_the_dataset_when_known(self, tmp_settings, project: Path):
        from core.knowledge_tools import knowledge_index

        knowledge_index(project_path=str(project), settings=tmp_settings)
        # Record the dataset first, as a real run would.
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            project_tool_result(
                tool="data.inspect", args={"path": str(project / "sales.csv")},
                result_data={"headers": ["product", "region", "amount"],
                             "row_count": 4,
                             "column_types": {"product": "string",
                                              "region": "string",
                                              "amount": "integer"}},
                task_id="", project_root=str(project),
                settings=tmp_settings, manager=mgr,
            )
            _analyze_call(project, tmp_settings, mgr)
            graph = mgr.get_graph(canonical_project_key(str(project)))
            analysis = graph.find_nodes(node_type=NodeType.ANALYSIS, limit=1)[0]
            dataset = graph.find_nodes(node_type=NodeType.DATASET, limit=1)[0]
            edges = graph.get_edges(source_id=analysis.node_id,
                                    relation_type=RelationType.DERIVED_FROM)
            assert any(e.target_id == dataset.node_id for e in edges)
        finally:
            mgr.close_all()

    def test_analysis_without_a_dataset_still_records_the_question(
            self, tmp_settings, project: Path):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            assert _analyze_call(project, tmp_settings, mgr)
            graph = mgr.get_graph(canonical_project_key(str(project)))
            assert len(graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)) == 1
        finally:
            mgr.close_all()


class TestAnalyzeBounds:
    def test_unrecognised_operation_is_not_recorded(self, tmp_settings, project: Path):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            assert not _analyze_call(project, tmp_settings, mgr, operation="drop_table")
            graph = mgr.get_graph(canonical_project_key(str(project)))
            assert graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10) == []
        finally:
            mgr.close_all()

    def test_overlong_expression_is_dropped_not_stored(self, tmp_settings, project: Path):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            _analyze_call(project, tmp_settings, mgr,
                          filter_expr="x == '" + "A" * 5000 + "'")
            graph = mgr.get_graph(canonical_project_key(str(project)))
            node = graph.find_nodes(node_type=NodeType.ANALYSIS, limit=1)[0]
            assert "filter_expr" not in node.metadata
        finally:
            mgr.close_all()

    def test_analyze_does_not_become_a_dataset_view(self, tmp_settings, project: Path):
        """An analysis must not masquerade as a new dataset observation."""
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            _analyze_call(project, tmp_settings, mgr)
            graph = mgr.get_graph(canonical_project_key(str(project)))
            assert graph.find_nodes(node_type=NodeType.DATASET, limit=10) == []
        finally:
            mgr.close_all()

    def test_unauthorized_analysis_path_is_refused(self, tmp_settings):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            result = project_tool_result(
                tool="data.analyze",
                args={"path": "C:/Windows/data.csv", "operation": "stats"},
                result_data={"ok": True}, task_id="",
                project_root="C:/Windows/System32",
                settings=tmp_settings, manager=mgr,
            )
            assert result == ""
        finally:
            mgr.close_all()

    def test_analyze_is_idempotent(self, tmp_settings, project: Path):
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            for _ in range(3):
                _analyze_call(project, tmp_settings, mgr)
            graph = mgr.get_graph(canonical_project_key(str(project)))
            assert len(graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)) == 1
        finally:
            mgr.close_all()


class TestRealToolDrivesProjection:
    def test_real_analyze_tool_result_is_projected(self, tmp_settings, project: Path):
        """Drive the real registered tool, then project what it actually returned."""
        from tools import build_registry

        registry = build_registry(tmp_settings)
        tool = registry.get("data.analyze")
        assert tool is not None
        observed = tool.fn(
            path=str(project / "sales.csv"),
            operation="groupby",
            group_by="region",
            column="amount",
            limit=10,
        )
        payload = observed if isinstance(observed, dict) else getattr(observed, "data", None)
        assert isinstance(payload, dict)

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            project_tool_result(
                tool="data.analyze",
                args={"path": str(project / "sales.csv"), "operation": "groupby",
                      "group_by": "region", "column": "amount"},
                result_data=payload, task_id="",
                project_root=str(project), settings=tmp_settings, manager=mgr,
            )
            graph = mgr.get_graph(canonical_project_key(str(project)))
            analyses = graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)
            assert len(analyses) == 1
            blob = json.dumps(analyses[0].metadata)
            assert "region" in blob
        finally:
            mgr.close_all()

    def test_normalize_ignores_analysis_shape_for_datasets(self):
        """An analyze payload must not be mistaken for a dataset shape."""
        shape = normalize_dataset_result({"ok": True, "operation": "stats",
                                          "groups": 2})
        assert shape["row_count"] == 0
        assert shape["columns"] == []
