"""M2.14 Phase 6 — Knowledge Graph Query Engine tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_task_key,
)
from core.knowledge_query import (
    KnowledgeQueryEngine,
    MAX_RESULTS,
    MAX_TRAVERSAL_DEPTH,
    query_graph_tool,
    get_project_query_engine,
)


@pytest.fixture
def graph(tmp_path: Path):
    g = KnowledgeGraph(tmp_path, "test-proj")
    yield g
    g.close()


@pytest.fixture
def graph_manager(tmp_path: Path):
    mgr = KnowledgeGraphManager(tmp_path)
    yield mgr
    mgr.close_all()


@pytest.fixture
def populated_graph(graph: KnowledgeGraph):
    """Create a graph with test data."""
    # Project
    proj_key = canonical_file_key("test-proj", "")
    proj_id = graph.upsert_node(NodeType.PROJECT, proj_key, "Test Project", {"root": "/tmp"}).node_id

    # Directories
    src_dir = graph.upsert_node(NodeType.DIRECTORY, "dir:test-proj:src", "src", {"path": "src"}).node_id
    tests_dir = graph.upsert_node(NodeType.DIRECTORY, "dir:test-proj:tests", "tests", {"path": "tests"}).node_id

    graph.upsert_edge(proj_id, src_dir, RelationType.CONTAINS)
    graph.upsert_edge(proj_id, tests_dir, RelationType.CONTAINS)

    # Files
    main_file = graph.upsert_node(NodeType.FILE, canonical_file_key("test-proj", "src/main.py"), "main.py",
                                  {"path": "src/main.py", "extension": ".py", "size": 100}).node_id
    utils_file = graph.upsert_node(NodeType.FILE, canonical_file_key("test-proj", "src/utils.py"), "utils.py",
                                   {"path": "src/utils.py", "extension": ".py", "size": 200}).node_id
    test_file = graph.upsert_node(NodeType.FILE, canonical_file_key("test-proj", "tests/test_main.py"), "test_main.py",
                                  {"path": "tests/test_main.py", "extension": ".py", "size": 300}).node_id

    graph.upsert_edge(src_dir, main_file, RelationType.CONTAINS)
    graph.upsert_edge(src_dir, utils_file, RelationType.CONTAINS)
    graph.upsert_edge(tests_dir, test_file, RelationType.CONTAINS)

    # Imports
    graph.upsert_edge(main_file, utils_file, RelationType.IMPORTS)

    # Task
    task_id = graph.upsert_node(NodeType.TASK, canonical_task_key("task-1"), "Task 1",
                                {"status": "completed", "project_name": "test-proj"}).node_id

    # Chart
    chart_id = graph.upsert_node(NodeType.CHART, "chart:test-proj:charts/bar.png", "bar.png",
                                 {"chart_type": "bar", "path": "/charts/bar.png"}).node_id

    # Dataset
    ds_id = graph.upsert_node(NodeType.DATASET, "dataset:test-proj:data/sales.csv", "sales.csv",
                              {"format": "csv", "path": "/data/sales.csv"}).node_id

    graph.upsert_edge(task_id, chart_id, RelationType.PRODUCED)
    graph.upsert_edge(task_id, ds_id, RelationType.PRODUCED)

    return graph


class TestBasicQueries:
    def test_find_node_exact(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        node = engine.find_node("file:test-proj:src/main.py")
        assert node is not None
        assert node.display_name == "main.py"

    def test_find_node_not_found(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        node = engine.find_node("file:test-proj:nonexistent.py")
        assert node is None

    def test_find_nodes_by_type(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.find_nodes(node_type=NodeType.FILE, limit=10)
        assert result.total_count >= 3
        assert all(n.node_type == NodeType.FILE for n in result.nodes)

    def test_find_nodes_limit(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.find_nodes(node_type=NodeType.FILE, limit=2)
        assert result.total_count <= 2
        assert result.truncated is True

    def test_search_project(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_project("test-proj")
        assert result.total_count > 0


class TestRelationTraversal:
    def test_neighbors_outgoing(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = engine.neighbors(main_file.node_id, direction="out")
        # main.py -> imports -> utils.py
        assert len(result.nodes) >= 1
        neighbor_names = [n.display_name for n in result.nodes]
        assert "utils.py" in neighbor_names

    def test_neighbors_incoming(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        utils_file = populated_graph.get_node(canonical_file_key("test-proj", "src/utils.py"))
        result = engine.neighbors(utils_file.node_id, direction="in", relation_type=RelationType.IMPORTS)
        assert len(result.nodes) >= 1
        assert result.nodes[0].display_name == "main.py"

    def test_neighbors_filtered_by_relation(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = engine.neighbors(main_file.node_id, relation_type=RelationType.IMPORTS)
        assert all(e.relation_type == RelationType.IMPORTS for e in result.edges)

    def test_find_related_bfs(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        proj = populated_graph.get_node("file:test-proj:")
        result = engine.find_related(proj.node_id, max_depth=2, limit=10)
        # Should find project -> directories -> files
        assert result.total_count >= 3
        types = {n.node_type for n in result.nodes}
        assert NodeType.DIRECTORY in types
        assert NodeType.FILE in types

    def test_find_related_depth_limit(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        proj = populated_graph.get_node("file:test-proj:")
        result = engine.find_related(proj.node_id, max_depth=1, limit=10)
        # Depth 1 should only get directories
        types = {n.node_type for n in result.nodes}
        assert NodeType.DIRECTORY in types
        assert NodeType.FILE not in types  # files are depth 2

    def test_path_finding(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        proj = populated_graph.get_node("file:test-proj:")
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = engine.path(proj.node_id, main_file.node_id)
        assert result.found
        assert len(result.paths) > 0
        # Path: project -> src -> main.py
        assert len(result.paths[0]) >= 2

    def test_path_not_found(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        task = populated_graph.get_node(canonical_task_key("task-1"))
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = engine.path(task.node_id, main_file.node_id)
        assert not result.found


class TestSpecializedSearches:
    def test_search_files_by_extension(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_files(extension=".py")
        assert result.total_count >= 3
        assert all(n.metadata.get("extension") == ".py" for n in result.nodes)

    def test_search_files_by_path(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_files(path_contains="tests")
        assert result.total_count >= 1
        assert any("tests" in n.metadata.get("path", "") for n in result.nodes)

    def test_search_tasks_by_status(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_tasks(status="completed")
        assert result.total_count >= 1
        assert all(n.metadata.get("status") == "completed" for n in result.nodes)

    def test_search_tasks_no_filter(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_tasks()
        assert result.total_count >= 1

    def test_search_artifacts(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_artifacts()
        assert result.total_count >= 2  # chart + dataset

    def test_search_datasets(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_datasets()
        assert result.total_count >= 1
        assert all(n.node_type == NodeType.DATASET for n in result.nodes)

    def test_search_charts(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.search_charts()
        assert result.total_count >= 1
        assert all(n.node_type == NodeType.CHART for n in result.nodes)


class TestResultFormatting:
    def test_format_node_summary(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        node = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        formatted = engine.format_node_summary(node)
        assert "[file] main.py" in formatted
        assert "path: src/main.py" in formatted

    def test_format_query_result(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        result = engine.find_nodes(node_type=NodeType.FILE, limit=2)
        formatted = engine.format_query_result(result)
        assert "Found" in formatted
        assert "main.py" in formatted


class TestQueryTool:
    def test_query_tool_find_node(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "find_node",
                                  canonical_key="file:test-proj:src/main.py")
        assert result["success"] is True
        assert "main.py" in result["output"]

    def test_query_tool_find_nodes(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "find_nodes",
                                  node_type="file", limit=5)
        assert result["success"] is True
        assert "main.py" in result["output"]

    def test_query_tool_neighbors(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = query_graph_tool(graph_manager, "test-proj", "neighbors",
                                  node_id=main_file.node_id, direction="out")
        assert result["success"] is True
        assert "utils.py" in result["output"]

    def test_query_tool_find_related(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        proj = populated_graph.get_node("file:test-proj:")
        result = query_graph_tool(graph_manager, "test-proj", "find_related",
                                  node_id=proj.node_id, max_depth=2)
        assert result["success"] is True
        assert "src/main.py" in result["output"] or "utils.py" in result["output"]

    def test_query_tool_path(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        proj = populated_graph.get_node("file:test-proj:")
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = query_graph_tool(graph_manager, "test-proj", "path",
                                  from_id=proj.node_id, to_id=main_file.node_id)
        assert result["success"] is True
        assert "Path:" in result["output"]

    def test_query_tool_path_not_found(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        task = populated_graph.get_node(canonical_task_key("task-1"))
        main_file = populated_graph.get_node(canonical_file_key("test-proj", "src/main.py"))
        result = query_graph_tool(graph_manager, "test-proj", "path",
                                  from_id=task.node_id, to_id=main_file.node_id)
        assert result["success"] is True
        assert "No path found" in result["output"]

    def test_query_tool_search_files(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "search_files",
                                  extension=".py", path_contains="src")
        assert result["success"] is True
        assert "main.py" in result["output"]

    def test_query_tool_search_tasks(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "search_tasks",
                                  status="completed")
        assert result["success"] is True
        assert "Task 1" in result["output"]

    def test_query_tool_search_artifacts(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "search_artifacts")
        assert result["success"] is True

    def test_query_tool_search_datasets(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "search_datasets")
        assert result["success"] is True
        assert "sales.csv" in result["output"]

    def test_query_tool_search_charts(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "search_charts")
        assert result["success"] is True
        assert "bar.png" in result["output"]

    def test_query_tool_recent_activity(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        result = query_graph_tool(graph_manager, "test-proj", "recent_activity",
                                  hours=24)
        assert result["success"] is True

    def test_query_tool_invalid_type(self, graph_manager: KnowledgeGraphManager):
        result = query_graph_tool(graph_manager, "test-proj", "invalid_type")
        assert result["success"] is False
        assert "unknown query_type" in result["error"]

    def test_query_tool_missing_params(self, graph_manager: KnowledgeGraphManager):
        result = query_graph_tool(graph_manager, "test-proj", "find_node")
        assert result["success"] is False
        assert "canonical_key required" in result["error"]


class TestBounds:
    def test_max_results_respected(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        # Create many files
        for i in range(200):
            fk = canonical_file_key("test-proj", f"src/file{i}.py")
            populated_graph.upsert_node(NodeType.FILE, fk, f"file{i}.py", {"path": f"src/file{i}.py", "extension": ".py"})

        engine = KnowledgeQueryEngine(populated_graph, max_results=50)
        result = engine.find_nodes(node_type=NodeType.FILE)
        assert result.total_count <= MAX_RESULTS
        assert result.truncated

    def test_max_traversal_depth(self, populated_graph: KnowledgeGraph):
        engine = KnowledgeQueryEngine(populated_graph)
        proj = populated_graph.get_node("file:test-proj:")
        result = engine.find_related(proj.node_id, max_depth=100, limit=1000)
        # Should be bounded by MAX_TRAVERSAL_DEPTH
        assert len(result.nodes) < 1000


class TestProjectQueryEngine:
    def test_get_project_query_engine(self, graph_manager: KnowledgeGraphManager, populated_graph: KnowledgeGraph):
        engine = get_project_query_engine(graph_manager, "test-proj")
        assert engine.graph.project_id == "test-proj"
        result = engine.find_nodes(node_type=NodeType.FILE)
        assert result.total_count >= 3


class TestUnauthorizedProject:
    def test_cross_project_isolation(self, graph_manager: KnowledgeGraphManager, tmp_path: Path):
        # Create two projects
        g1 = graph_manager.get_graph("proj1")
        g2 = graph_manager.get_graph("proj2")

        fk1 = canonical_file_key("proj1", "a.py")
        g1.upsert_node(NodeType.FILE, fk1, "a.py", {"path": "a.py"})

        fk2 = canonical_file_key("proj2", "b.py")
        g2.upsert_node(NodeType.FILE, fk2, "b.py", {"path": "b.py"})

        # Query proj1
        engine1 = get_project_query_engine(graph_manager, "proj1")
        result1 = engine1.find_nodes(node_type=NodeType.FILE)
        assert result1.total_count == 1
        assert result1.nodes[0].display_name == "a.py"

        # Query proj2
        engine2 = get_project_query_engine(graph_manager, "proj2")
        result2 = engine2.find_nodes(node_type=NodeType.FILE)
        assert result2.total_count == 1
        assert result2.nodes[0].display_name == "b.py"