"""M2.14 Phase 1 — Knowledge Graph schema tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.knowledge_graph import (
    SCHEMA_VERSION,
    KnowledgeGraph,
    KnowledgeGraphManager,
    Node,
    NodeType,
    Edge,
    RelationType,
    canonical_file_key,
    canonical_symbol_key,
    canonical_project_key,
    canonical_task_key,
    canonical_plan_key,
    canonical_dataset_key,
    canonical_chart_key,
    canonical_artifact_key,
    ingest_project,
    ingest_directory,
    ingest_file,
    link_task_to_file,
    link_task_to_plan,
    link_plan_to_artifact,
    link_dataset_to_file,
    link_chart_to_dataset,
    link_task_to_chart,
    link_verification_to_evidence,
    link_checkpoint,
    _validate_metadata,
    MAX_METADATA_KEYS,
    MAX_METADATA_VALUE_LEN,
    MAX_METADATA_TOTAL_LEN,
)


@pytest.fixture
def temp_dir(tmp_path: Path):
    yield tmp_path


@pytest.fixture
def graph(temp_dir: Path):
    g = KnowledgeGraph(temp_dir, "test-project")
    yield g
    g.close()


class TestMetadataBounds:
    def test_empty_metadata(self):
        assert _validate_metadata({}) == {}
        assert _validate_metadata(None) == {}

    def test_non_dict_input(self):
        assert _validate_metadata("not a dict") == {}
        assert _validate_metadata([1, 2, 3]) == {}

    def test_key_limit(self):
        metadata = {f"key{i}": f"value{i}" for i in range(MAX_METADATA_KEYS + 10)}
        result = _validate_metadata(metadata)
        assert len(result) == MAX_METADATA_KEYS

    def test_value_length_limit(self):
        long_value = "x" * (MAX_METADATA_VALUE_LEN + 100)
        result = _validate_metadata({"key": long_value})
        assert len(result["key"]) <= MAX_METADATA_VALUE_LEN + 3  # +3 for "..."

    def test_total_length_limit(self):
        metadata = {f"key{i}": "x" * 2000 for i in range(10)}
        result = _validate_metadata(metadata)
        total = sum(len(json.dumps(v, ensure_ascii=False)) for v in result.values())
        assert total <= MAX_METADATA_TOTAL_LEN


class TestCanonicalKeys:
    def test_canonical_file_key(self):
        key = canonical_file_key("proj1", "src/main.py")
        assert key == "file:proj1:src/main.py"
        key2 = canonical_file_key("proj1", "src\\main.py")
        assert key2 == key  # normalized

    def test_canonical_symbol_key(self):
        key = canonical_symbol_key("proj1", "src/main.py", "MyClass.my_method", "method")
        assert key == "symbol:proj1:src/main.py:method:MyClass.my_method"

    def test_canonical_project_key(self):
        key = canonical_project_key("/home/user/myproject")
        assert key.startswith("project:")
        assert len(key) > 10

    def test_canonical_task_key(self):
        key = canonical_task_key("task-123")
        assert key == "task:task-123"

    def test_canonical_plan_key(self):
        key = canonical_plan_key("plan-456")
        assert key == "plan:plan-456"

    def test_canonical_dataset_key(self):
        key = canonical_dataset_key("/data/sales.csv")
        assert key.startswith("dataset:")

    def test_canonical_chart_key(self):
        key = canonical_chart_key("/charts/bar.png")
        assert key.startswith("chart:")

    def test_canonical_artifact_key(self):
        key = canonical_artifact_key("/artifacts/report.pdf")
        assert key.startswith("artifact:")


class TestKnowledgeGraph:
    def test_node_creation(self, graph: KnowledgeGraph):
        node = graph.upsert_node(NodeType.FILE, "file:test:test.py", "test.py", {"size": 100})
        assert node.node_id.startswith("n_")
        assert node.node_type == NodeType.FILE
        assert node.canonical_key == "file:test:test.py"
        assert node.display_name == "test.py"
        assert node.metadata["size"] == 100
        assert node.schema_version == SCHEMA_VERSION

    def test_node_upsert_updates(self, graph: KnowledgeGraph):
        node1 = graph.upsert_node(NodeType.FILE, "file:test:test.py", "test.py", {"size": 100})
        time.sleep(0.01)
        node2 = graph.upsert_node(NodeType.FILE, "file:test:test.py", "test.py", {"size": 200})
        assert node1.node_id == node2.node_id
        assert node2.metadata["size"] == 200
        assert node2.updated_at >= node1.updated_at

    def test_node_get_by_canonical(self, graph: KnowledgeGraph):
        graph.upsert_node(NodeType.FILE, "file:test:test.py", "test.py", {"size": 100})
        node = graph.get_node("file:test:test.py")
        assert node is not None
        assert node.metadata["size"] == 100

    def test_node_get_by_id(self, graph: KnowledgeGraph):
        node = graph.upsert_node(NodeType.FILE, "file:test:test.py", "test.py")
        fetched = graph.get_node_by_id(node.node_id)
        assert fetched is not None
        assert fetched.node_id == node.node_id

    def test_node_delete(self, graph: KnowledgeGraph):
        graph.upsert_node(NodeType.FILE, "file:test:test.py", "test.py")
        assert graph.get_node("file:test:test.py") is not None
        assert graph.delete_node("file:test:test.py") is True
        assert graph.get_node("file:test:test.py") is None
        assert graph.delete_node("file:test:test.py") is False

    def test_edge_creation(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        edge = graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        assert edge.edge_id.startswith("e_")
        assert edge.source_id == source.node_id
        assert edge.target_id == target.node_id
        assert edge.relation_type == RelationType.IMPORTS
        assert edge.schema_version == SCHEMA_VERSION

    def test_edge_upsert_updates(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        edge1 = graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS, {"count": 1})
        time.sleep(0.01)
        edge2 = graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS, {"count": 2})
        assert edge1.edge_id == edge2.edge_id
        assert edge2.metadata["count"] == 2

    def test_edge_get_by_source(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        edges = graph.get_edges(source_id=source.node_id)
        assert len(edges) == 1
        assert edges[0].target_id == target.node_id

    def test_edge_get_by_target(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        edges = graph.get_edges(target_id=target.node_id)
        assert len(edges) == 1
        assert edges[0].source_id == source.node_id

    def test_edge_get_by_relation(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        graph.upsert_edge(source.node_id, target.node_id, RelationType.DEPENDS_ON)
        edges = graph.get_edges(source_id=source.node_id, relation_type=RelationType.IMPORTS)
        assert len(edges) == 1
        assert edges[0].relation_type == RelationType.IMPORTS

    def test_edge_delete(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        edge = graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        assert graph.get_edges(source_id=source.node_id)
        assert graph.delete_edge(edge.edge_id) is True
        assert not graph.get_edges(source_id=source.node_id)
        assert graph.delete_edge(edge.edge_id) is False

    def test_find_nodes_by_type(self, graph: KnowledgeGraph):
        graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        graph.upsert_node(NodeType.DIRECTORY, "dir:test:src", "src")
        files = graph.find_nodes(node_type=NodeType.FILE)
        assert len(files) == 2
        dirs = graph.find_nodes(node_type=NodeType.DIRECTORY)
        assert len(dirs) == 1

    def test_find_nodes_limit_offset(self, graph: KnowledgeGraph):
        for i in range(5):
            graph.upsert_node(NodeType.FILE, f"file:test:f{i}.py", f"f{i}.py")
        page1 = graph.find_nodes(node_type=NodeType.FILE, limit=2, offset=0)
        page2 = graph.find_nodes(node_type=NodeType.FILE, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].node_id != page2[0].node_id

    def test_neighbors_outgoing(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        c = graph.upsert_node(NodeType.FILE, "file:test:c.py", "c.py")
        graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS)
        graph.upsert_edge(a.node_id, c.node_id, RelationType.DEPENDS_ON)
        neighbors = graph.neighbors(a.node_id, direction="out")
        assert len(neighbors) == 2
        target_ids = {n.node_id for _, n in neighbors}
        assert target_ids == {b.node_id, c.node_id}

    def test_neighbors_incoming(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS)
        neighbors = graph.neighbors(b.node_id, direction="in")
        assert len(neighbors) == 1
        assert neighbors[0][1].node_id == a.node_id

    def test_neighbors_filtered_by_relation(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        c = graph.upsert_node(NodeType.FILE, "file:test:c.py", "c.py")
        graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS)
        graph.upsert_edge(a.node_id, c.node_id, RelationType.DEPENDS_ON)
        neighbors = graph.neighbors(a.node_id, relation_type=RelationType.IMPORTS)
        assert len(neighbors) == 1
        assert neighbors[0][1].node_id == b.node_id

    def test_path_finding(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        c = graph.upsert_node(NodeType.FILE, "file:test:c.py", "c.py")
        graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS)
        graph.upsert_edge(b.node_id, c.node_id, RelationType.IMPORTS)
        paths = graph.path(a.node_id, c.node_id)
        assert paths is not None
        assert len(paths) >= 1
        assert len(paths[0]) == 2

    def test_path_not_found(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        paths = graph.path(a.node_id, b.node_id)
        assert paths is None

    def test_persistence_across_instances(self, temp_dir: Path):
        g1 = KnowledgeGraph(temp_dir, "persist-test")
        node = g1.upsert_node(NodeType.FILE, "file:persist:test.py", "test.py", {"size": 123})
        g1.close()

        g2 = KnowledgeGraph(temp_dir, "persist-test")
        fetched = g2.get_node("file:persist:test.py")
        assert fetched is not None
        assert fetched.metadata["size"] == 123
        g2.close()

    def test_schema_version_persisted(self, graph: KnowledgeGraph):
        node = graph.upsert_node(NodeType.FILE, "file:test:ver.py", "ver.py")
        assert node.schema_version == SCHEMA_VERSION

    def test_foreign_key_cascade_delete(self, graph: KnowledgeGraph):
        source = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        target = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        graph.delete_node("file:test:a.py")
        # Edge should be cascade deleted
        edges = graph.get_edges(source_id=source.node_id)
        assert len(edges) == 0

    def test_stats(self, graph: KnowledgeGraph):
        graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        source = graph.get_node("file:test:a.py")
        target = graph.get_node("file:test:b.py")
        graph.upsert_edge(source.node_id, target.node_id, RelationType.IMPORTS)
        stats = graph.stats()
        assert stats["nodes"] == 2
        assert stats["edges"] == 1


class TestKnowledgeGraphManager:
    def test_multi_project(self, temp_dir: Path):
        mgr = KnowledgeGraphManager(temp_dir)
        g1 = mgr.get_graph("proj1")
        g2 = mgr.get_graph("proj2")
        g1.upsert_node(NodeType.PROJECT, "project:proj1", "Project 1")
        g2.upsert_node(NodeType.PROJECT, "project:proj2", "Project 2")
        projects = mgr.list_projects()
        assert "proj1" in projects
        assert "proj2" in projects

    def test_delete_project(self, temp_dir: Path):
        mgr = KnowledgeGraphManager(temp_dir)
        g = mgr.get_graph("proj1")
        g.upsert_node(NodeType.PROJECT, "project:proj1", "Project 1")
        assert mgr.delete_project("proj1") is True
        assert "proj1" not in mgr.list_projects()


class TestIngestionHelpers:
    def test_ingest_project(self, graph: KnowledgeGraph):
        proj = ingest_project(graph, Path("/tmp/myproj"), "MyProject")
        assert proj.node_type == NodeType.PROJECT
        assert "root" in proj.metadata

    def test_ingest_directory(self, graph: KnowledgeGraph):
        proj = ingest_project(graph, Path("/tmp/myproj"), "MyProject")
        dir_node = ingest_directory(graph, proj, "src")
        assert dir_node.node_type == NodeType.DIRECTORY
        edges = graph.get_edges(source_id=proj.node_id, relation_type=RelationType.CONTAINS)
        assert len(edges) == 1
        assert edges[0].target_id == dir_node.node_id

    def test_ingest_file(self, graph: KnowledgeGraph):
        proj = ingest_project(graph, Path("/tmp/myproj"), "MyProject")
        dir_node = ingest_directory(graph, proj, "src")
        file_node = ingest_file(graph, dir_node, "src/main.py", 1024, time.time(), ".py", "abc123")
        assert file_node.node_type == NodeType.FILE
        assert file_node.metadata["size"] == 1024
        assert file_node.metadata["content_hash"] == "abc123"
        edges = graph.get_edges(source_id=dir_node.node_id, relation_type=RelationType.CONTAINS)
        assert len(edges) == 1
        assert edges[0].target_id == file_node.node_id

    def test_link_helpers(self, graph: KnowledgeGraph):
        task = graph.upsert_node(NodeType.TASK, canonical_task_key("task-1"), "Task 1")
        plan = graph.upsert_node(NodeType.PLAN, canonical_plan_key("plan-1"), "Plan 1")
        file_node = graph.upsert_node(NodeType.FILE, canonical_file_key("proj", "a.py"), "a.py")
        artifact = graph.upsert_node(NodeType.ARTIFACT, canonical_artifact_key("/tmp/out.txt"), "out.txt")
        dataset = graph.upsert_node(NodeType.DATASET, canonical_dataset_key("/tmp/data.csv"), "data.csv")
        chart = graph.upsert_node(NodeType.CHART, canonical_chart_key("/tmp/chart.png"), "chart.png")
        verification = graph.upsert_node(NodeType.VERIFICATION, "verif:1", "Verification 1")
        checkpoint = graph.upsert_node(NodeType.CHECKPOINT, "checkpoint:1", "Checkpoint 1")

        link_task_to_plan(graph, task, plan)
        link_plan_to_artifact(graph, plan, artifact)
        link_task_to_file(graph, task, file_node)
        link_dataset_to_file(graph, dataset, file_node)
        link_chart_to_dataset(graph, chart, dataset)
        link_task_to_chart(graph, task, chart)
        link_verification_to_evidence(graph, verification, file_node)
        link_checkpoint(graph, task, checkpoint)

        edges = graph.get_edges(source_id=task.node_id)
        assert len(edges) == 4  # task->plan, task->file, task->chart, task->checkpoint


class TestDuplicateHandling:
    def test_duplicate_file_ingestion_idempotent(self, graph: KnowledgeGraph):
        proj = ingest_project(graph, Path("/tmp/proj"), "Proj")
        dir_node = ingest_directory(graph, proj, "src")
        file1 = ingest_file(graph, dir_node, "src/main.py", 100, 1000.0, ".py", "hash1")
        file2 = ingest_file(graph, dir_node, "src/main.py", 200, 2000.0, ".py", "hash2")
        assert file1.node_id == file2.node_id
        assert file2.metadata["size"] == 200
        assert file2.metadata["content_hash"] == "hash2"

    def test_duplicate_edge_idempotent(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        e1 = graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS)
        e2 = graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS)
        assert e1.edge_id == e2.edge_id


class TestMalformedRecords:
    def test_malformed_metadata_in_node(self, graph: KnowledgeGraph):
        # Metadata with non-serializable value should be handled
        node = graph.upsert_node(NodeType.FILE, "file:test:bad.py", "bad.py", {"bad": object()})
        # Should not crash, object will be stringified
        assert node is not None

    def test_malformed_metadata_in_edge(self, graph: KnowledgeGraph):
        a = graph.upsert_node(NodeType.FILE, "file:test:a.py", "a.py")
        b = graph.upsert_node(NodeType.FILE, "file:test:b.py", "b.py")
        edge = graph.upsert_edge(a.node_id, b.node_id, RelationType.IMPORTS, {"bad": object()})
        assert edge is not None


class TestAuthorizedProjectIdentity:
    def test_project_isolation(self, temp_dir: Path):
        mgr = KnowledgeGraphManager(temp_dir)
        g1 = mgr.get_graph("proj1")
        g2 = mgr.get_graph("proj2")
        g1.upsert_node(NodeType.FILE, "file:proj1:a.py", "a.py")
        g2.upsert_node(NodeType.FILE, "file:proj2:a.py", "a.py")
        # Each graph only sees its own nodes
        assert len(g1.find_nodes()) == 1
        assert len(g2.find_nodes()) == 1
        # Node IDs are distinct
        n1 = g1.get_node("file:proj1:a.py")
        n2 = g2.get_node("file:proj2:a.py")
        assert n1.node_id != n2.node_id