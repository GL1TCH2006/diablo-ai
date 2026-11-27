"""M2.14 Phase D — live data/chart projection tests.

Drives the real tools through the real ``Executor`` and then the real
projection boundary, so this covers the wiring rather than the helpers.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from config.permissions import PermissionManager
from core.events import EventBus
from core.executor import Executor
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_project_key,
)
from core.knowledge_projection import knowledge_dir, project_tool_result
from core.knowledge_tasks import upsert_task_node
from core.knowledge_tools import knowledge_index
from core.task_store import TaskRecord
from tools import build_registry


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "charts").mkdir(parents=True)
    (root / "sales.csv").write_text(
        "product,amount,qty\nA,10,1\nB,20,2\nC,30,3\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def settings(tmp_settings):
    """Alias so the projection calls read naturally."""
    return tmp_settings


@pytest.fixture
def executor(tmp_settings):
    return Executor(build_registry(tmp_settings), PermissionManager(tmp_settings), EventBus())


@pytest.fixture
def ctx(tmp_settings, project: Path):
    """Indexed project, graph, project_id, manager and a TASK anchor node."""
    assert knowledge_index(project_path=str(project), settings=tmp_settings).success
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    project_id = canonical_project_key(str(project))
    graph = mgr.get_graph(project_id)
    record = TaskRecord(
        task_id="t1", project_name=project.name, project_root=str(project),
        title="data task", goal="analyse", created_at=time.strftime("%Y%m%d-%H%M%S"),
    )
    task_node_id = upsert_task_node(graph, record)
    yield mgr, graph, project_id, task_node_id, record
    mgr.close_all()


def _project(ctx, tool, args, data, settings, task_id="t1", project: Path = None):
    mgr, graph, project_id, _, record = ctx
    return project_tool_result(
        tool=tool, args=args, result_data=data, task_id=task_id,
        project_root=record.project_root, settings=settings, manager=mgr,
    )


def _targets(graph, node_id, relation):
    return [graph.get_node_by_id(e.target_id) for e in
            graph.get_edges(source_id=node_id, relation_type=relation)]


# ------------------------------------------------------------- normalization


class TestResultNormalization:
    def test_load_csv_column_types_shape(self):
        from core.knowledge_data import normalize_dataset_result
        shape = normalize_dataset_result({
            "headers": ["a", "b"], "row_count": 4,
            "column_types": [{"name": "a", "type": "integer"},
                             {"name": "b", "type": "string"}],
        })
        assert shape["columns"] == ["a", "b"]
        assert shape["row_count"] == 4
        assert shape["column_types"] == {"a": "integer", "b": "string"}
        assert shape["format"] == ""

    def test_inspect_column_stats_shape(self):
        from core.knowledge_data import normalize_dataset_result
        shape = normalize_dataset_result({
            "headers": ["a", "b"], "row_count": 9,
            "column_stats": [{"name": "a", "type": "float", "count": 9},
                             {"name": "b", "type": "string", "count": 9}],
            "path": "/tmp/x.csv",
        })
        assert shape["columns"] == ["a", "b"]
        assert shape["row_count"] == 9
        assert shape["column_types"] == {"a": "float", "b": "string"}
        assert shape["format"] == "csv"

    def test_both_shapes_normalise_identically(self):
        from core.knowledge_data import normalize_dataset_result
        a = normalize_dataset_result({
            "headers": ["a"], "row_count": 1,
            "column_types": [{"name": "a", "type": "int"}]})
        b = normalize_dataset_result({
            "headers": ["a"], "row_count": 1,
            "column_stats": [{"name": "a", "type": "int"}]})
        assert a == b

    @pytest.mark.parametrize("payload", [None, {}, "nonsense", 42, [1, 2]])
    def test_malformed_results_degrade(self, payload):
        from core.knowledge_data import normalize_dataset_result
        shape = normalize_dataset_result(payload)
        assert shape["columns"] == []
        assert shape["row_count"] == 0
        assert shape["column_types"] == {}

    def test_garbage_row_count_is_coerced(self):
        from core.knowledge_data import normalize_dataset_result
        assert normalize_dataset_result({"row_count": "lots"})["row_count"] == 0


# ----------------------------------------------------------------- real tools


class TestRealDataToolProjection:
    def test_inspect_produces_dataset(self, ctx, executor, settings, project):
        mgr, graph, project_id, task_node_id, _ = ctx
        args = {"path": str(project / "sales.csv")}
        outcome = executor.run("data.inspect", args, None)
        assert outcome.success, outcome.error
        assert "column_stats" in outcome.data

        assert _project(ctx, "data.inspect", args, outcome.data, settings)

        datasets = graph.find_nodes(node_type=NodeType.DATASET, limit=10)
        assert len(datasets) == 1
        assert datasets[0].metadata["row_count"] == 3
        assert datasets[0].metadata["columns"] == ["product", "amount", "qty"]
        assert datasets[0].metadata["column_types"]["amount"] == "integer"

    def test_load_csv_produces_the_same_dataset(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        args = {"path": str(project / "sales.csv")}
        outcome = executor.run("data.load_csv", args, None)
        assert outcome.success
        assert _project(ctx, "data.load_csv", args, outcome.data, settings)
        datasets = graph.find_nodes(node_type=NodeType.DATASET, limit=10)
        assert len(datasets) == 1
        assert datasets[0].metadata["row_count"] == 3

    def test_both_shapes_converge_on_one_dataset_node(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        args = {"path": str(project / "sales.csv")}
        a = executor.run("data.inspect", args, None)
        b = executor.run("data.load_csv", args, None)
        _project(ctx, "data.inspect", args, a.data, settings)
        _project(ctx, "data.load_csv", args, b.data, settings)
        assert len(graph.find_nodes(node_type=NodeType.DATASET, limit=10)) == 1

    def test_dataset_derived_from_its_file(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        args = {"path": str(project / "sales.csv")}
        outcome = executor.run("data.inspect", args, None)
        _project(ctx, "data.inspect", args, outcome.data, settings)
        dataset = graph.find_nodes(node_type=NodeType.DATASET, limit=1)[0]
        derived = _targets(graph, dataset.node_id, RelationType.DERIVED_FROM)
        assert [n.node_type for n in derived] == [NodeType.FILE]

    def test_task_produced_the_dataset(self, ctx, executor, settings, project):
        mgr, graph, _, task_node_id, _ = ctx
        args = {"path": str(project / "sales.csv")}
        outcome = executor.run("data.inspect", args, None)
        _project(ctx, "data.inspect", args, outcome.data, settings)
        produced = _targets(graph, task_node_id, RelationType.PRODUCED)
        assert [n.node_type for n in produced] == [NodeType.DATASET]

    def test_dataset_contents_are_not_stored(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        args = {"path": str(project / "sales.csv")}
        outcome = executor.run("data.inspect", args, None)
        _project(ctx, "data.inspect", args, outcome.data, settings)
        dataset = graph.find_nodes(node_type=NodeType.DATASET, limit=1)[0]
        blob = str(dataset.metadata)
        # The preview rows must not be copied into the graph.
        assert "preview" not in dataset.metadata
        assert "'A'" not in blob and '"A"' not in blob


class TestRealChartToolProjection:
    def _chart(self, executor, project, settings):
        args = {
            "chart_type": "bar",
            "data_path": str(project / "sales.csv"),
            "x_column": "product",
            "y_column": "amount",
            "title": "Sales",
            "output_path": str(project / "charts" / "sales.png"),
        }
        outcome = executor.run("chart.generate", args, None)
        assert outcome.success, outcome.error
        return args, outcome

    def test_chart_artifact_and_dataset_edges(self, ctx, executor, settings, project):
        mgr, graph, _, task_node_id, _ = ctx
        # Dataset first, so the chart can point at it.
        dargs = {"path": str(project / "sales.csv")}
        d = executor.run("data.inspect", dargs, None)
        _project(ctx, "data.inspect", dargs, d.data, settings)

        cargs, cout = self._chart(executor, project, settings)
        _project(ctx, "chart.generate", cargs, cout.data, settings)

        charts = graph.find_nodes(node_type=NodeType.CHART, limit=5)
        assert len(charts) == 1
        assert charts[0].metadata["chart_type"] == "bar"
        assert charts[0].metadata["data_points"] == 3

        generated = _targets(graph, charts[0].node_id, RelationType.GENERATED_FROM)
        assert [n.node_type for n in generated] == [NodeType.DATASET]

        produced = _targets(graph, charts[0].node_id, RelationType.PRODUCED)
        assert [n.node_type for n in produced] == [NodeType.ARTIFACT]

    def test_dataset_path_comes_from_args(self, ctx, executor, settings, project):
        """The chart must link to the dataset named in the invocation."""
        mgr, graph, _, _, _ = ctx
        dargs = {"path": str(project / "sales.csv")}
        d = executor.run("data.inspect", dargs, None)
        _project(ctx, "data.inspect", dargs, d.data, settings)

        # Result payload carries no data_path, so only args can supply it.
        cargs, cout = self._chart(executor, project, settings)
        assert "data_path" not in cout.data
        _project(ctx, "chart.generate", cargs, cout.data, settings)
        chart = graph.find_nodes(node_type=NodeType.CHART, limit=1)[0]
        assert _targets(graph, chart.node_id, RelationType.GENERATED_FROM)

    def test_task_produced_the_chart(self, ctx, executor, settings, project):
        mgr, graph, _, task_node_id, _ = ctx
        cargs, cout = self._chart(executor, project, settings)
        _project(ctx, "chart.generate", cargs, cout.data, settings)
        produced = _targets(graph, task_node_id, RelationType.PRODUCED)
        assert NodeType.CHART in [n.node_type for n in produced]

    def test_image_bytes_are_not_stored(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        cargs, cout = self._chart(executor, project, settings)
        _project(ctx, "chart.generate", cargs, cout.data, settings)
        for node in graph.find_nodes(node_type=NodeType.ARTIFACT, limit=5):
            assert "data" not in node.metadata
            assert "image" not in node.metadata
            assert len(str(node.metadata)) < 2000

    def test_chart_verify_creates_a_verification_node(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        cargs, cout = self._chart(executor, project, settings)
        _project(ctx, "chart.generate", cargs, cout.data, settings)

        vargs = {"path": cout.data["path"]}
        vout = executor.run("chart.verify", vargs, None)
        assert vout.success
        _project(ctx, "chart.verify", vargs, vout.data, settings)

        verifs = graph.find_nodes(node_type=NodeType.VERIFICATION, limit=5)
        assert len(verifs) == 1
        assert verifs[0].metadata["state"] == "passed"


# ---------------------------------------------------------------- full workflow


class TestCompleteWorkflow:
    def test_inspect_analyze_chart_verify(self, ctx, executor, settings, project):
        mgr, graph, project_id, task_node_id, _ = ctx

        dargs = {"path": str(project / "sales.csv")}
        d = executor.run("data.inspect", dargs, None)
        _project(ctx, "data.inspect", dargs, d.data, settings)

        aargs = {"path": str(project / "sales.csv"), "operation": "stats",
                 "column": "amount"}
        a = executor.run("data.analyze", aargs, None)
        assert a.success

        cargs = {"chart_type": "bar", "data_path": str(project / "sales.csv"),
                 "x_column": "product", "y_column": "amount",
                 "output_path": str(project / "charts" / "w.png")}
        c = executor.run("chart.generate", cargs, None)
        _project(ctx, "chart.generate", cargs, c.data, settings)

        vargs = {"path": c.data["path"]}
        v = executor.run("chart.verify", vargs, None)
        _project(ctx, "chart.verify", vargs, v.data, settings)

        kinds = {n.node_type for n in graph.find_nodes(limit=100)}
        assert {NodeType.DATASET, NodeType.CHART, NodeType.ARTIFACT,
                NodeType.VERIFICATION, NodeType.FILE} <= kinds

    def test_workflow_is_idempotent(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        dargs = {"path": str(project / "sales.csv")}
        cargs = {"chart_type": "bar", "data_path": str(project / "sales.csv"),
                 "x_column": "product", "y_column": "amount",
                 "output_path": str(project / "charts" / "w.png")}

        d = executor.run("data.inspect", dargs, None)
        c = executor.run("chart.generate", cargs, None)
        for _ in range(3):
            _project(ctx, "data.inspect", dargs, d.data, settings)
            _project(ctx, "chart.generate", cargs, c.data, settings)
        counts = {n.value: len(graph.find_nodes(node_type=n, limit=200))
                  for n in (NodeType.DATASET, NodeType.CHART, NodeType.ARTIFACT)}
        assert counts == {"dataset": 1, "chart": 1, "artifact": 1}

    def test_changed_dataset_updates_row_count(self, ctx, executor, settings, project):
        mgr, graph, _, _, _ = ctx
        dargs = {"path": str(project / "sales.csv")}
        d = executor.run("data.inspect", dargs, None)
        _project(ctx, "data.inspect", dargs, d.data, settings)

        (project / "sales.csv").write_text(
            "product,amount,qty\nA,1,1\nB,2,2\n", encoding="utf-8")
        d2 = executor.run("data.inspect", dargs, None)
        assert d2.data["row_count"] == 2
        _project(ctx, "data.inspect", dargs, d2.data, settings)

        dataset = graph.find_nodes(node_type=NodeType.DATASET, limit=1)[0]
        assert dataset.metadata["row_count"] == 2


# ------------------------------------------------------------- refusals/filters


class TestProjectionFilters:
    def test_unrelated_tool_is_ignored(self, ctx, settings):
        mgr, graph, _, _, record = ctx
        assert _project(ctx, "system.info", {}, {"cpu": "x"}, settings) == ""

    def test_empty_result_is_ignored(self, ctx, settings):
        assert _project(ctx, "data.inspect", {"path": "x"}, {}, settings) == ""
        assert _project(ctx, "data.inspect", {"path": "x"}, None, settings) == ""

    def test_missing_path_argument_is_ignored(self, ctx, settings):
        assert _project(ctx, "data.inspect", {}, {"row_count": 1}, settings) == ""

    def test_path_outside_project_is_ignored(self, ctx, settings, tmp_path):
        mgr, graph, _, _, _ = ctx
        outside = tmp_path / "outside.csv"
        outside.write_text("a,b\n1,2\n", encoding="utf-8")
        assert _project(ctx, "data.inspect", {"path": str(outside)},
                        {"headers": ["a"], "row_count": 1}, settings) == ""
        assert graph.find_nodes(node_type=NodeType.DATASET, limit=5) == []

    def test_non_string_path_is_ignored(self, ctx, settings):
        assert _project(ctx, "data.inspect", {"path": 12345},
                        {"row_count": 1}, settings) == ""

    def test_oversized_argument_is_rejected(self, ctx, settings, project):
        """An over-long path is rejected, never truncated into a wrong path."""
        mgr, graph, _, _, _ = ctx
        from core.knowledge_projection import MAX_ARG_LEN
        long_path = str(project / "sales.csv") + "/" + ("x" * (MAX_ARG_LEN + 100))
        assert _project(ctx, "data.inspect", {"path": long_path},
                        {"row_count": 1}, settings) == ""
        assert graph.find_nodes(node_type=NodeType.DATASET, limit=5) == []

    def test_unauthorized_project_is_refused(self, ctx, settings):
        mgr, graph, _, _, record = ctx
        from core.knowledge_projection import project_tool_result
        assert project_tool_result(
            tool="data.inspect", args={"path": "C:/Windows/x.csv"},
            result_data={"row_count": 1}, task_id="t1",
            project_root="C:/Windows/System32", settings=settings,
            manager=mgr,
        ) == ""

    def test_missing_task_anchor_still_records_the_dataset(self, ctx, executor,
                                                           settings, project):
        """Dataset knowledge is recorded even before the TASK node exists."""
        mgr, graph, _, _, _ = ctx
        args = {"path": str(project / "sales.csv")}
        outcome = executor.run("data.inspect", args, None)
        assert _project(ctx, "data.inspect", args, outcome.data, settings, task_id="unseen")
        assert graph.find_nodes(node_type=NodeType.DATASET, limit=5)
