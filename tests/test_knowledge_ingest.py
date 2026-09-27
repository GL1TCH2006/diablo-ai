"""M2.14 Phase 2 — Project/File Knowledge Ingestion tests."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_project_key,
)
from core.knowledge_ingest import (
    IGNORED_DIRS,
    MAX_FILE_SIZE,
    _is_ignored_dir,
    _is_binary_ext,
    _compute_content_hash,
    _is_within_allowed,
    ingest_project_structure,
    ingest_changed_file,
    remove_deleted_file,
    get_project_stats,
)
from config.settings import Settings


@pytest.fixture
def temp_dir(tmp_path: Path):
    yield tmp_path


@pytest.fixture
def graph(temp_dir: Path):
    g = KnowledgeGraph(temp_dir, "test-project")
    yield g
    g.close()


@pytest.fixture
def settings(temp_dir: Path):
    class MockSettings:
        allowed_roots = [temp_dir]
    return MockSettings()


@pytest.fixture
def temp_project(tmp_path: Path):
    proj = tmp_path / "test-project"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "src" / "main.py").write_text("print('hello')\n")
    (proj / "src" / "utils.py").write_text("def foo(): pass\n")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_main.py").write_text("def test_foo(): pass\n")
    (proj / "README.md").write_text("# Test Project\n")
    (proj / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[core]\n")
    return proj


class TestIngestFilters:
    def test_ignored_dirs(self):
        assert _is_ignored_dir(".git") is True
        assert _is_ignored_dir("node_modules") is True
        assert _is_ignored_dir("__pycache__") is True
        assert _is_ignored_dir("src") is False

    def test_binary_ext(self):
        assert _is_binary_ext(".png") is True
        assert _is_binary_ext(".exe") is True
        assert _is_binary_ext(".py") is False
        assert _is_binary_ext(".txt") is False


class TestContentHash:
    def test_text_file_hash(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        hash_val = _compute_content_hash(f)
        assert hash_val is not None
        assert len(hash_val) == 16

    def test_same_content_same_hash(self, tmp_path: Path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("same")
        f2.write_text("same")
        assert _compute_content_hash(f1) == _compute_content_hash(f2)

    def test_binary_file_no_hash(self, tmp_path: Path):
        f = tmp_path / "test.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert _compute_content_hash(f) is None

    def test_large_file_no_hash(self, tmp_path: Path):
        f = tmp_path / "large.txt"
        f.write_text("x" * (MAX_FILE_SIZE + 1))
        assert _compute_content_hash(f) is None


class TestProjectStructureIngestion:
    def test_ingest_basic_structure(self, graph: KnowledgeGraph, temp_project: Path, settings):
        stats = ingest_project_structure(graph, temp_project, "test-project", settings)
        assert stats["directories"] >= 2  # src, tests
        assert stats["files"] >= 4  # main.py, utils.py, test_main.py, README.md, pyproject.toml

        # Check project node exists
        proj_node = graph.get_node(canonical_project_key(str(temp_project)))
        assert proj_node is not None
        assert proj_node.node_type == NodeType.PROJECT

        # Check directories exist
        src_node = graph.get_node(f"dir:{graph.project_id}:src")
        assert src_node is not None
        assert src_node.node_type == NodeType.DIRECTORY

        tests_node = graph.get_node(f"dir:{graph.project_id}:tests")
        assert tests_node is not None

        # Check files exist
        main_node = graph.get_node(canonical_file_key(graph.project_id, "src/main.py"))
        assert main_node is not None
        assert main_node.node_type == NodeType.FILE
        assert main_node.metadata["size"] > 0
        assert main_node.metadata["extension"] == ".py"
        assert "content_hash" in main_node.metadata

        # Check edges
        edges = graph.get_edges(source_id=proj_node.node_id, relation_type=RelationType.CONTAINS)
        assert len(edges) >= 2

    def test_ignored_dirs_not_ingested(self, graph: KnowledgeGraph, temp_project: Path, settings):
        # Create a node_modules directory that should be ignored
        (temp_project / "node_modules").mkdir()
        (temp_project / "node_modules" / "pkg").mkdir()
        (temp_project / "node_modules" / "pkg" / "index.js").write_text("// ignored\n")

        stats = ingest_project_structure(graph, temp_project, "test-project", settings)

        # node_modules should not be in graph
        nm_node = graph.get_node(f"dir:{graph.project_id}:node_modules")
        assert nm_node is None

        # Files inside should not be in graph
        js_node = graph.get_node(canonical_file_key(graph.project_id, "node_modules/pkg/index.js"))
        assert js_node is None

    def test_dotfiles_skipped(self, graph: KnowledgeGraph, temp_project: Path, settings):
        (temp_project / ".env").write_text("SECRET=123\n")
        (temp_project / ".hidden").write_text("hidden\n")

        stats = ingest_project_structure(graph, temp_project, "test-project", settings)

        env_node = graph.get_node(canonical_file_key(graph.project_id, ".env"))
        assert env_node is None

    def test_unauthorized_path_rejected(self, graph: KnowledgeGraph, temp_project: Path):
        class MockSettings:
            allowed_roots = [Path("/completely/different/path")]

        with pytest.raises(ValueError, match="outside allowed workspace"):
            ingest_project_structure(graph, temp_project, "test-project", MockSettings())

    def test_max_depth_respected(self, graph: KnowledgeGraph, temp_project: Path, settings):
        # Create deep nesting
        deep = temp_project
        for i in range(15):
            deep = deep / f"level{i}"
            deep.mkdir()
        (deep / "deep.py").write_text("# deep\n")

        stats = ingest_project_structure(graph, temp_project, "test-project", settings, max_depth=5)
        # Should not reach level 15
        deep_node = graph.get_node(canonical_file_key(graph.project_id, "level0/level1/level2/level3/level4/level5/deep.py"))
        assert deep_node is None

    def test_max_files_respected(self, graph: KnowledgeGraph, temp_dir: Path, settings):
        proj = temp_dir / "many-files"
        proj.mkdir()
        for i in range(100):
            (proj / f"file{i}.py").write_text(f"# file {i}\n")

        stats = ingest_project_structure(graph, proj, "many-files", settings, max_files=10)
        assert stats["files"] <= 10

    def test_idempotent_reingest(self, graph: KnowledgeGraph, temp_project: Path, settings):
        stats1 = ingest_project_structure(graph, temp_project, "test-project", settings)
        stats2 = ingest_project_structure(graph, temp_project, "test-project", settings)

        # Should not create duplicates
        assert stats1["files"] == stats2["files"]
        assert stats1["directories"] == stats2["directories"]


class TestChangedFileIngestion:
    def test_ingest_changed_file_updates_metadata(self, graph: KnowledgeGraph, temp_project: Path, settings):
        # Initial ingest
        ingest_project_structure(graph, temp_project, "test-project", settings)

        main_key = canonical_file_key(graph.project_id, "src/main.py")
        original = graph.get_node(main_key)
        original_size = original.metadata["size"]
        original_hash = original.metadata["content_hash"]

        # Modify file
        time.sleep(0.01)
        (temp_project / "src" / "main.py").write_text("print('hello world')\n")

        # Re-ingest
        updated = ingest_changed_file(graph, temp_project, "src/main.py", settings)
        assert updated is not None
        assert updated["size"] > original_size
        assert updated["content_hash"] != original_hash

    def test_ingest_new_file(self, graph: KnowledgeGraph, temp_project: Path, settings):
        ingest_project_structure(graph, temp_project, "test-project", settings)

        # Add new file
        (temp_project / "src" / "new_file.py").write_text("def new(): pass\n")

        updated = ingest_changed_file(graph, temp_project, "src/new_file.py", settings)
        assert updated is not None
        assert updated["size"] > 0
        assert "content_hash" in updated

        # Verify it's in graph
        node = graph.get_node(canonical_file_key(graph.project_id, "src/new_file.py"))
        assert node is not None

    def test_ingest_unauthorized_file_returns_none(self, graph: KnowledgeGraph, temp_project: Path):
        class MockSettings:
            allowed_roots = [Path("/completely/different/path")]

        result = ingest_changed_file(graph, temp_project, "src/main.py", MockSettings())
        assert result is None


class TestFileDeletion:
    def test_remove_deleted_file(self, graph: KnowledgeGraph, temp_dir: Path, settings):
        proj = temp_dir / "test-project"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "main.py").write_text("print('hello')\n")

        ingest_project_structure(graph, proj, "test-project", settings)

        main_key = canonical_file_key(graph.project_id, "src/main.py")
        assert graph.get_node(main_key) is not None

        # Remove from graph
        result = remove_deleted_file(graph, "src/main.py")
        assert result is True

        # Node should be gone
        assert graph.get_node(main_key) is None

        # Removing again should return False
        result = remove_deleted_file(graph, "src/main.py")
        assert result is False


class TestProjectStats:
    def test_get_project_stats(self, graph: KnowledgeGraph, temp_dir: Path, settings):
        proj = temp_dir / "test-project"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "main.py").write_text("print('hello')\n")
        (proj / "README.md").write_text("# Test\n")

        ingest_project_structure(graph, proj, "test-project", settings)

        stats = get_project_stats(graph)
        assert stats.get("project", 0) == 1
        assert stats.get("directory", 0) >= 1
        assert stats.get("file", 0) >= 2


class TestMultipleProjects:
    def test_project_isolation(self, temp_dir: Path):
        mgr = KnowledgeGraphManager(temp_dir)

        class MockSettings:
            allowed_roots = [temp_dir]

        settings = MockSettings()

        # Create two projects
        proj1 = temp_dir / "proj1"
        proj1.mkdir()
        (proj1 / "src").mkdir()
        (proj1 / "src" / "a.py").write_text("# a\n")

        proj2 = temp_dir / "proj2"
        proj2.mkdir()
        (proj2 / "src").mkdir()
        (proj2 / "src" / "b.py").write_text("# b\n")

        g1 = mgr.get_graph(canonical_project_key(str(proj1)))
        g2 = mgr.get_graph(canonical_project_key(str(proj2)))

        ingest_project_structure(g1, proj1, "proj1", settings)
        ingest_project_structure(g2, proj2, "proj2", settings)

        # Each graph should only have its own nodes
        assert len(g1.find_nodes()) > 0
        assert len(g2.find_nodes()) > 0

        # Project 1 nodes should not be in project 2
        assert g2.get_node(canonical_file_key(g2.project_id, "src/a.py")) is None
        assert g1.get_node(canonical_file_key(g1.project_id, "src/b.py")) is None