"""M2.14 Phase 5 — Data/Chart Knowledge Integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_dataset_key,
    canonical_chart_key,
    canonical_artifact_key,
    canonical_file_key,
)
from core.knowledge_data import (
    DatasetInfo,
    ChartInfo,
    AnalysisInfo,
    upsert_dataset_node,
    upsert_chart_node,
    upsert_analysis_node,
    link_dataset_to_file,
    link_chart_to_dataset,
    link_chart_to_artifact,
    link_task_to_chart,
    link_task_to_dataset,
    link_analysis_to_dataset,
    link_task_to_analysis,
    sync_dataset_inspection,
    sync_data_analysis,
    sync_chart_generation,
    sync_chart_verification,
)


@pytest.fixture
def graph(tmp_path: Path):
    from core.knowledge_graph import KnowledgeGraph
    g = KnowledgeGraph(tmp_path, "test-proj")
    yield g
    g.close()


class TestDatasetNode:
    def test_upsert_dataset_node(self, graph: KnowledgeGraph):
        info = DatasetInfo(
            path="/data/sales.csv",
            format="csv",
            columns=["date", "product", "amount"],
            row_count=1000,
            column_types={"date": "string", "product": "string", "amount": "float"},
        )
        node_id = upsert_dataset_node(graph, info)
        assert node_id.startswith("n_")

        ds_key = canonical_dataset_key("/data/sales.csv")
        node = graph.get_node(ds_key)
        assert node is not None
        assert node.node_type == NodeType.DATASET
        assert node.metadata["path"] == "/data/sales.csv"
        assert node.metadata["format"] == "csv"
        assert node.metadata["row_count"] == 1000
        assert node.metadata["columns"] == ["date", "product", "amount"]


class TestChartNode:
    def test_upsert_chart_node(self, graph: KnowledgeGraph):
        info = ChartInfo(
            path="/charts/bar.png",
            chart_type="bar",
            title="Sales by Product",
            x_column="product",
            y_column="amount",
            data_points=10,
            dataset_path="/data/sales.csv",
        )
        node_id = upsert_chart_node(graph, info)
        assert node_id.startswith("n_")

        chart_key = canonical_chart_key("/charts/bar.png")
        node = graph.get_node(chart_key)
        assert node is not None
        assert node.node_type == NodeType.CHART
        assert node.metadata["chart_type"] == "bar"
        assert node.metadata["title"] == "Sales by Product"
        assert node.metadata["x_column"] == "product"
        assert node.metadata["y_column"] == "amount"
        assert node.metadata["data_points"] == 10
        assert node.metadata["dataset_path"] == "/data/sales.csv"


class TestAnalysisNode:
    def test_upsert_analysis_node(self, graph: KnowledgeGraph):
        info = AnalysisInfo(
            dataset_path="/data/sales.csv",
            operation="groupby",
            column="amount",
            extra_params={"group_by": "product"},
        )
        node_id = upsert_analysis_node(graph, info)
        assert node_id.startswith("n_")

        node = graph.get_node_by_id(node_id)
        assert node is not None
        # An analysis is its own node type, not a chart artefact.
        assert node.node_type == NodeType.ANALYSIS
        assert node.metadata["dataset_path"] == "/data/sales.csv"
        assert node.metadata["operation"] == "groupby"
        assert node.metadata["column"] == "amount"
        assert node.metadata["params"]["group_by"] == "product"

    def test_analysis_identity_is_the_question_not_a_clock(self, graph: KnowledgeGraph):
        """Asking the same question twice must not create a second node."""
        def ask():
            return upsert_analysis_node(
                graph,
                AnalysisInfo(dataset_path="/data/sales.csv", operation="groupby",
                             column="amount", extra_params={"group_by": "product"}),
            )

        assert ask() == ask()
        assert len(graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)) == 1

    def test_a_different_question_is_a_different_node(self, graph: KnowledgeGraph):
        first = upsert_analysis_node(
            graph,
            AnalysisInfo(dataset_path="/data/sales.csv", operation="groupby",
                         column="amount", extra_params={"group_by": "product"}))
        second = upsert_analysis_node(
            graph,
            AnalysisInfo(dataset_path="/data/sales.csv", operation="stats",
                         column="amount", extra_params={}))
        assert first != second
        assert len(graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)) == 2


class TestDataLinking:
    def test_link_dataset_to_file(self, graph: KnowledgeGraph):
        ds_info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        ds_id = upsert_dataset_node(graph, ds_info)

        file_key = canonical_file_key("test-proj", "data/sales.csv")
        file_id = graph.upsert_node(NodeType.FILE, file_key, "sales.csv", {"path": "data/sales.csv"}).node_id

        link_dataset_to_file(graph, ds_id, file_id)

        edges = graph.get_edges(source_id=ds_id, relation_type=RelationType.DERIVED_FROM)
        assert len(edges) == 1
        assert edges[0].target_id == file_id

    def test_link_chart_to_dataset(self, graph: KnowledgeGraph):
        ds_info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        ds_id = upsert_dataset_node(graph, ds_info)

        chart_info = ChartInfo(
            path="/charts/bar.png", chart_type="bar", title="T", x_column="x", y_column="y", data_points=5,
            dataset_path="/data/sales.csv"
        )
        chart_id = upsert_chart_node(graph, chart_info)

        link_chart_to_dataset(graph, chart_id, ds_id)

        edges = graph.get_edges(source_id=chart_id, relation_type=RelationType.GENERATED_FROM)
        assert len(edges) == 1
        assert edges[0].target_id == ds_id

    def test_link_chart_to_artifact(self, graph: KnowledgeGraph):
        chart_info = ChartInfo(
            path="/charts/bar.png", chart_type="bar", title="T", x_column="x", y_column="y", data_points=5
        )
        chart_id = upsert_chart_node(graph, chart_info)

        art_key = canonical_artifact_key("/charts/bar.png")
        art_id = graph.upsert_node(NodeType.ARTIFACT, art_key, "bar.png", {"path": "/charts/bar.png"}).node_id

        link_chart_to_artifact(graph, chart_id, art_id)

        edges = graph.get_edges(source_id=chart_id, relation_type=RelationType.PRODUCED)
        assert len(edges) == 1
        assert edges[0].target_id == art_id

    def test_link_task_to_chart(self, graph: KnowledgeGraph):
        task_key = canonical_file_key("test-proj", "tasks/task-1")
        task_id = graph.upsert_node(NodeType.TASK, task_key, "Task 1", {}).node_id

        chart_info = ChartInfo(
            path="/charts/bar.png", chart_type="bar", title="T", x_column="x", y_column="y", data_points=5
        )
        chart_id = upsert_chart_node(graph, chart_info)

        link_task_to_chart(graph, task_id, chart_id)

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.PRODUCED)
        chart_edges = [e for e in edges if graph.get_node_by_id(e.target_id).node_type == NodeType.CHART]
        assert len(chart_edges) == 1

    def test_link_task_to_dataset(self, graph: KnowledgeGraph):
        task_key = canonical_file_key("test-proj", "tasks/task-1")
        task_id = graph.upsert_node(NodeType.TASK, task_key, "Task 1", {}).node_id

        ds_info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        ds_id = upsert_dataset_node(graph, ds_info)

        link_task_to_dataset(graph, task_id, ds_id)

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.PRODUCED)
        ds_edges = [e for e in edges if graph.get_node_by_id(e.target_id).node_type == NodeType.DATASET]
        assert len(ds_edges) == 1

    def test_link_analysis_to_dataset(self, graph: KnowledgeGraph):
        ds_info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        ds_id = upsert_dataset_node(graph, ds_info)

        info = AnalysisInfo(dataset_path="/data/sales.csv", operation="stats", column="amount", extra_params={})
        analysis_id = upsert_analysis_node(graph, info)

        link_analysis_to_dataset(graph, analysis_id, ds_id)

        edges = graph.get_edges(source_id=analysis_id, relation_type=RelationType.DERIVED_FROM)
        assert len(edges) == 1
        assert edges[0].target_id == ds_id

    def test_link_task_to_analysis(self, graph: KnowledgeGraph):
        task_key = canonical_file_key("test-proj", "tasks/task-1")
        task_id = graph.upsert_node(NodeType.TASK, task_key, "Task 1", {}).node_id

        info = AnalysisInfo(dataset_path="/data/sales.csv", operation="stats", column="amount", extra_params={})
        analysis_id = upsert_analysis_node(graph, info)

        link_task_to_analysis(graph, task_id, analysis_id)

        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.PRODUCED)
        analysis_edges = [
            e for e in edges
            if graph.get_node_by_id(e.target_id).node_type == NodeType.ANALYSIS
        ]
        assert len(analysis_edges) >= 1

    def test_link_task_to_analysis_without_a_task_is_a_noop(self, graph: KnowledgeGraph):
        """No task anchor means no edge, not a foreign key violation."""
        info = AnalysisInfo(dataset_path="/data/sales.csv", operation="stats",
                           column="amount", extra_params={})
        analysis_id = upsert_analysis_node(graph, info)
        link_task_to_analysis(graph, "", analysis_id)
        assert graph.get_edges(source_id="", relation_type=RelationType.PRODUCED) == []
        assert graph.get_node_by_id(analysis_id) is not None


class TestSyncFunctions:
    def test_sync_dataset_inspection(self, graph: KnowledgeGraph):
        # Pre-create file node
        file_key = canonical_file_key("test-proj", "data/sales.csv")
        graph.upsert_node(NodeType.FILE, file_key, "sales.csv", {"path": "data/sales.csv"})

        task_key = canonical_file_key("test-proj", "tasks/task-1")
        task_id = graph.upsert_node(NodeType.TASK, task_key, "Task 1", {}).node_id

        result_data = {
            "format": "csv",
            "headers": ["date", "product", "amount"],
            "row_count": 1000,
            "column_types": [
                {"name": "date", "type": "string"},
                {"name": "product", "type": "string"},
                {"name": "amount", "type": "float"},
            ],
        }

        ds_id = sync_dataset_inspection(graph, task_id, "data/sales.csv", result_data)
        assert ds_id is not None

        # Verify links
        ds_node = graph.get_node(canonical_dataset_key("data/sales.csv"))
        assert ds_node is not None
        assert ds_node.metadata["row_count"] == 1000

        # Check task -> PRODUCED -> dataset
        edges = graph.get_edges(source_id=task_id, relation_type=RelationType.PRODUCED)
        ds_edges = [e for e in edges if graph.get_node_by_id(e.target_id).node_type == NodeType.DATASET]
        assert len(ds_edges) == 1

    def test_sync_data_analysis(self, graph: KnowledgeGraph):
        # Pre-create dataset node
        ds_info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        ds_id = upsert_dataset_node(graph, ds_info)

        task_key = canonical_file_key("test-proj", "tasks/task-1")
        task_id = graph.upsert_node(NodeType.TASK, task_key, "Task 1", {}).node_id

        result_data = {"groups": [{"product": "A", "mean": 100}]}

        analysis_id = sync_data_analysis(
            graph, task_id, "/data/sales.csv", "groupby", "amount",
            result_data, {"group_by": "product"}
        )
        assert analysis_id is not None

        # Verify analysis node
        analysis_node = graph.get_node_by_id(analysis_id)
        assert analysis_node.node_type == NodeType.ANALYSIS
        assert analysis_node.metadata["operation"] == "groupby"
        assert analysis_node.metadata["column"] == "amount"
        assert analysis_node.metadata["params"]["group_by"] == "product"
        # The result payload itself is never recorded.
        assert "groups" not in analysis_node.metadata

        # Check links
        edges = graph.get_edges(source_id=analysis_id, relation_type=RelationType.DERIVED_FROM)
        assert len(edges) == 1
        assert edges[0].target_id == ds_id

    def test_sync_chart_generation(self, graph: KnowledgeGraph):
        task_key = canonical_file_key("test-proj", "tasks/task-1")
        task_id = graph.upsert_node(NodeType.TASK, task_key, "Task 1", {}).node_id

        # Pre-create dataset
        ds_info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        ds_id = upsert_dataset_node(graph, ds_info)

        result_data = {
            "title": "Sales Chart",
            "x_column": "product",
            "y_column": "amount",
            "data_points": 10,
            "evidence": {"valid": True, "width": 800, "height": 600},
        }

        chart_id = sync_chart_generation(
            graph, task_id, "/charts/bar.png", "bar", "/data/sales.csv", result_data
        )
        assert chart_id is not None

        # Verify chart node
        chart_node = graph.get_node(canonical_chart_key("/charts/bar.png"))
        assert chart_node is not None
        assert chart_node.metadata["chart_type"] == "bar"
        assert chart_node.metadata["title"] == "Sales Chart"

        # Check chart -> artifact link
        edges = graph.get_edges(source_id=chart_id, relation_type=RelationType.PRODUCED)
        assert len(edges) == 1
        art_node = graph.get_node_by_id(edges[0].target_id)
        assert art_node.node_type == NodeType.ARTIFACT

        # Check chart -> dataset link
        edges = graph.get_edges(source_id=chart_id, relation_type=RelationType.GENERATED_FROM)
        assert len(edges) == 1
        assert edges[0].target_id == ds_id

    def test_sync_chart_verification(self, graph: KnowledgeGraph):
        # Pre-create chart
        chart_info = ChartInfo(
            path="/charts/bar.png", chart_type="bar", title="T", x_column="x", y_column="y", data_points=5
        )
        chart_id = upsert_chart_node(graph, chart_info)

        evidence = {"valid": True, "width": 800, "height": 600, "format": "PNG"}

        verif_id = sync_chart_verification(graph, "/charts/bar.png", True, evidence)
        assert verif_id is not None

        # Verify verification node
        verif_node = graph.get_node_by_id(verif_id)
        assert verif_node.node_type == NodeType.VERIFICATION
        assert "passed" in verif_node.metadata["detail"].lower() or verif_node.metadata["state"] == "passed"

        # Check verification -> evidence link
        edges = graph.get_edges(source_id=verif_id, relation_type=RelationType.VERIFIED_BY)
        assert len(edges) == 1
        assert edges[0].target_id == chart_id


class TestIdempotency:
    def test_dataset_upsert_idempotent(self, graph: KnowledgeGraph):
        info = DatasetInfo(path="/data/sales.csv", format="csv", columns=["a"], row_count=10)
        id1 = upsert_dataset_node(graph, info)
        id2 = upsert_dataset_node(graph, info)
        assert id1 == id2

    def test_chart_upsert_idempotent(self, graph: KnowledgeGraph):
        info = ChartInfo(path="/charts/bar.png", chart_type="bar", title="T", x_column="x", y_column="y", data_points=5)
        id1 = upsert_chart_node(graph, info)
        id2 = upsert_chart_node(graph, info)
        assert id1 == id2