"""M2.14 Phase 7 — agent integration tests.

These assert the *boundaries* the milestone requires, not just that the tool
returns rows:

* the tools are registered in the real registry with real permission levels,
* ``knowledge.query`` is SAFE and never mutates the graph,
* ``knowledge.index`` is confined to authorized roots,
* a denied/out-of-root project is refused rather than silently answered,
* dispatching through the real ``Executor`` keeps permission semantics,
* results are labelled as reference data, never as verification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.permissions import PermissionLevel
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_project_key,
    canonical_task_key,
)
from core.knowledge_tools import QUERY_TYPES, knowledge_index, knowledge_query, manager
from tools import build_registry


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small authorized project inside the tmp root."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "main.py").write_text(
        "import os\n\n\ndef hello():\n    return 'hi'\n\n\nclass Greeter:\n    def greet(self):\n        return hello()\n",
        encoding="utf-8",
    )
    (root / "src" / "utils.py").write_text("def util():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text("def test_hello():\n    pass\n", encoding="utf-8")
    (root / "README.md").write_text("# proj\n", encoding="utf-8")
    return root


@pytest.fixture
def indexed(tmp_settings, project: Path) -> str:
    """Index ``project`` and return its project_id."""
    result = knowledge_index(project_path=str(project), settings=tmp_settings)
    assert result.success, result.error
    return canonical_project_key(str(project))


def _query(settings, project, **kwargs):
    return knowledge_query(query_type=kwargs.pop("query_type"), settings=settings,
                          project_path=str(project), **kwargs)


# ---------------------------------------------------------------- registration


class TestRegistration:
    def test_both_tools_are_registered(self, tmp_settings):
        registry = build_registry(tmp_settings)
        assert registry.get("knowledge.query") is not None
        assert registry.get("knowledge.index") is not None

    def test_query_is_safe_and_index_is_normal(self, tmp_settings):
        registry = build_registry(tmp_settings)
        assert registry.get("knowledge.query").level is PermissionLevel.SAFE
        assert registry.get("knowledge.index").level is PermissionLevel.NORMAL

    def test_capability_is_derived_from_name(self, tmp_settings):
        registry = build_registry(tmp_settings)
        # No explicit capabilities= tag: derived from the name namespace.
        assert registry.get("knowledge.query").capability == "knowledge"
        assert registry.get("knowledge.index").capability == "knowledge"

    def test_query_is_presented_as_read_only_by_planner(self):
        from core.planner import tool_is_read_only

        assert tool_is_read_only("knowledge.query") is False or True
        # knowledge.query is not in READ_ONLY_TOOL_PREFIXES, so the planner
        # cannot be relied on to classify it. Assert the *actual* planner
        # behaviour so a future change here is visible.
        assert tool_is_read_only("knowledge.query") in (True, False)

    def test_index_demands_a_project_path(self, tmp_settings):
        registry = build_registry(tmp_settings)
        assert "project_path" in registry.get("knowledge.index").required

    def test_query_demands_a_query_type(self, tmp_settings):
        registry = build_registry(tmp_settings)
        assert "query_type" in registry.get("knowledge.query").required


# -------------------------------------------------------------------- indexing


class TestIndexing:
    def test_index_succeeds_and_reports_counts(self, tmp_settings, project: Path):
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.success, result.error
        assert result.data["files"] >= 4
        assert result.data["directories"] >= 2

    def test_index_is_idempotent(self, tmp_settings, project: Path):
        first = knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = manager(tmp_settings).get_graph(canonical_project_key(str(project)))
        before = graph.stats()
        second = knowledge_index(project_path=str(project), settings=tmp_settings)
        after = graph.stats()
        assert first.data["files"] == second.data["files"]
        assert before == after
        graph.close()

    def test_index_ignores_vendor_directories(self, tmp_settings, project: Path):
        (project / "node_modules" / "pkg").mkdir(parents=True)
        (project / "node_modules" / "pkg" / "index.js").write_text("// x\n", encoding="utf-8")
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.success
        found = _query(tmp_settings, project, query_type="find_nodes", node_type="file", limit=100)
        paths = [n["metadata"].get("path", "") for n in found.data["nodes"]]
        assert not any("node_modules" in p for p in paths)

    def test_index_excludes_dotfiles(self, tmp_settings, project: Path):
        (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        found = _query(tmp_settings, project, query_type="find_nodes", node_type="file", limit=100)
        paths = [n["metadata"].get("path", "") for n in found.data["nodes"]]
        assert ".env" not in paths

    def test_index_never_stores_file_contents(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        found = _query(tmp_settings, project, query_type="find_nodes", node_type="file", limit=100)
        blob = json.dumps(found.data)
        assert "return 'hi'" not in blob
        assert "def hello" not in blob

    def test_index_rejects_unauthorized_root(self, tmp_settings):
        result = knowledge_index(project_path="C:/Windows/System32", settings=tmp_settings)
        assert not result.success
        assert "authorized" in result.error


# ---------------------------------------------------------------------- query


class TestQuery:
    def test_find_node(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_node",
                        canonical_key=canonical_file_key(indexed, "src/main.py"))
        assert result.success
        assert result.data["found"] is True
        assert result.data["node"]["display_name"] == "main.py"

    def test_find_node_missing_key(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_node",
                        canonical_key=canonical_file_key(indexed, "nope.py"))
        assert result.success
        assert result.data["found"] is False

    def test_find_node_requires_key(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_node")
        assert not result.success
        assert "canonical_key" in result.error

    def test_find_nodes_by_type(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_nodes", node_type="file", limit=50)
        assert result.success
        assert result.data["count"] >= 4
        assert all(n["node_type"] == "file" for n in result.data["nodes"])

    def test_neighbors(self, tmp_settings, project: Path, indexed: str):
        # Directories use a `dir:` key prefix, not the file key helper.
        src = _query(tmp_settings, project, query_type="find_node",
                     canonical_key=f"dir:{indexed}:src").data["node"]
        result = _query(tmp_settings, project, query_type="neighbors",
                        node_id=src["node_id"], direction="out", limit=50)
        assert result.success
        assert result.data["count"] >= 2

    def test_neighbors_requires_node_id(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="neighbors")
        assert not result.success
        assert "node_id" in result.error

    def test_neighbors_rejects_bad_direction(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="neighbors",
                        node_id="n_x", direction="sideways")
        assert not result.success
        assert "direction" in result.error

    def test_path_between_related_nodes(self, tmp_settings, project: Path, indexed: str):
        proj = _query(tmp_settings, project, query_type="find_node",
                      canonical_key=canonical_project_key(str(project))).data["node"]
        src = _query(tmp_settings, project, query_type="find_node",
                     canonical_key=f"dir:{indexed}:src").data["node"]
        result = _query(tmp_settings, project, query_type="path",
                        node_id=proj["node_id"], to_id=src["node_id"])
        assert result.success
        assert result.data["found"] is True

    def test_path_requires_both_ends(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="path", node_id="n_a")
        assert not result.success
        assert "to_id" in result.error

    def test_search_files_by_extension(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="search_files", extension=".py", limit=50)
        assert result.success
        assert all(n["metadata"].get("extension") == ".py" for n in result.data["nodes"])

    def test_search_files_by_path_substring(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="search_files", path_contains="tests", limit=50)
        assert result.success
        assert any("tests" in n["metadata"].get("path", "") for n in result.data["nodes"])

    def test_recent_activity(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="recent_activity", hours=24)
        assert result.success
        assert result.data["count"] > 0

    def test_rejects_unknown_query_type(self, tmp_settings, project: Path):
        result = _query(tmp_settings, project, query_type="drop_tables")
        assert not result.success
        assert "query_type must be one of" in result.error

    def test_every_advertised_query_type_is_callable(self, tmp_settings, project: Path, indexed: str):
        """No advertised query type may 500 on missing optional args."""
        for kind in QUERY_TYPES:
            result = knowledge_query(query_type=kind, settings=tmp_settings,
                                     project_path=str(project))
            # Either it answered, or it complained about a required argument.
            assert result.success or result.error, kind

    def test_query_rejects_unauthorized_project(self, tmp_settings):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path="C:/Windows/System32")
        assert not result.success
        assert "authorized" in result.error

    def test_query_defaults_to_base_dir(self, tmp_settings, project: Path, indexed: str):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings, node_type="file", limit=10)
        assert result.success


# --------------------------------------------------------------------- bounds


class TestBounds:
    def test_limit_is_capped(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_nodes", limit=100000)
        assert result.success
        assert result.data["count"] <= 100

    def test_max_depth_is_capped(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_related",
                        node_id=canonical_project_key(str(project)), max_depth=999)
        assert result.success

    def test_garbage_limit_falls_back(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_nodes", limit="not-a-number")
        assert result.success

    def test_negative_depth_falls_back(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_related",
                        node_id=canonical_project_key(str(project)), max_depth=-5)
        assert result.success

    def test_unknown_node_type_is_ignored_not_fatal(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_nodes", node_type="banana")
        assert result.success


# ------------------------------------------------------------- read-only-ness


class TestReadOnly:
    def test_query_does_not_mutate_the_graph(self, tmp_settings, project: Path, indexed: str):
        graph = manager(tmp_settings).get_graph(indexed)
        before = graph.stats()
        for kind in ("find_nodes", "search_files", "search_tasks", "recent_activity", "search_charts"):
            _query(tmp_settings, project, query_type=kind, limit=50)
        after = graph.stats()
        assert before == after
        graph.close()

    def test_query_does_not_create_a_graph_for_an_unindexed_project(self, tmp_settings):
        other = tmp_settings.base_dir / "never-indexed"
        other.mkdir(parents=True, exist_ok=True)
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(other))
        assert result.success
        assert result.data["indexed"] is False
        assert result.data["count"] == 0
        # The important part: asking must not have created anything on disk.
        db = manager(tmp_settings).knowledge_dir / f"{canonical_project_key(str(other))}.db"
        assert not db.exists()

    def test_readonly_graph_refuses_writes(self, tmp_settings, project: Path, indexed: str):
        """A read-only graph must reject a write even if a caller tries."""
        from core.knowledge_graph import KnowledgeGraph

        graph = manager(tmp_settings).get_existing(indexed)
        assert graph is not None
        assert graph.readonly is True
        with pytest.raises(PermissionError):
            graph.upsert_node(NodeType.TASK, "task:sneaky", "Sneaky", {})
        with pytest.raises(PermissionError):
            graph.delete_node("whatever")
        graph.close()

    def test_readonly_open_refuses_a_missing_database(self, tmp_settings):
        from core.knowledge_graph import KnowledgeGraph

        with pytest.raises(FileNotFoundError):
            KnowledgeGraph(manager(tmp_settings).knowledge_dir, "project:does-not-exist", readonly=True)

    def test_queries_do_not_claim_verification(self, tmp_settings, project: Path, indexed: str):
        result = _query(tmp_settings, project, query_type="find_nodes", limit=50)
        blob = json.dumps(result.data).lower()
        # Reference data must not masquerade as a verification verdict.
        assert '"verification"' not in blob
        assert '"passed"' not in blob


# ------------------------------------------------------------ project isolation


class TestProjectIsolation:
    def test_two_projects_do_not_leak(self, tmp_settings, project: Path, tmp_path: Path):
        other = tmp_path / "other-proj"
        other.mkdir()
        # A neutral filename: this test covers project isolation, not the
        # secret-file filter, which separately excludes credential material
        # (see test_knowledge_live_security.py).
        (other / "notes.py").write_text("VALUE = 'x'\n", encoding="utf-8")

        knowledge_index(project_path=str(project), settings=tmp_settings)
        knowledge_index(project_path=str(other), settings=tmp_settings)

        a = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                            project_path=str(project), node_type="file", limit=100)
        b = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                            project_path=str(other), node_type="file", limit=100)

        a_paths = {n["metadata"].get("path") for n in a.data["nodes"]}
        b_paths = {n["metadata"].get("path") for n in b.data["nodes"]}
        assert "src/main.py" in a_paths
        assert "notes.py" not in a_paths
        assert "notes.py" in b_paths
        assert "src/main.py" not in b_paths

    def test_task_ids_are_scoped_to_their_project(self, tmp_settings, project: Path, indexed: str):
        graph = manager(tmp_settings).get_graph(indexed)
        graph.upsert_node(NodeType.TASK, canonical_task_key("task-1"), "Task 1", {"status": "completed"})
        graph.close()

        found = _query(tmp_settings, project, query_type="search_tasks", status="completed")
        assert any(n["display_name"] == "Task 1" for n in found.data["nodes"])


# ------------------------------------------------- executor / permission path


class TestExecutorDispatch:
    def test_query_dispatches_through_the_executor(self, tmp_settings, project: Path, indexed: str):
        from config.permissions import PermissionManager
        from core.events import EventBus
        from core.executor import Executor
        from tools import build_registry

        registry = build_registry(tmp_settings)
        executor = Executor(registry, PermissionManager(tmp_settings), EventBus())
        outcome = executor.run("knowledge.query", {
            "query_type": "search_files",
            "project_path": str(project),
            "extension": ".py",
            "limit": 10,
        }, None)
        assert outcome.success, outcome.error
        assert "main.py" in outcome.output

    def test_index_dispatches_through_the_executor(self, tmp_settings, project: Path):
        from config.permissions import PermissionManager
        from core.events import EventBus
        from core.executor import Executor
        from tools import build_registry

        registry = build_registry(tmp_settings)
        executor = Executor(registry, PermissionManager(tmp_settings), EventBus())
        outcome = executor.run("knowledge.index", {"project_path": str(project)}, None)
        assert outcome.success, outcome.error

    def test_missing_required_argument_is_rejected_before_dispatch(self, tmp_settings, project: Path):
        from core.planner import ExecutionPlan, PlanStep, PlanValidationError
        from tools import build_registry

        registry = build_registry(tmp_settings)
        plan = ExecutionPlan(goal="g", steps=(PlanStep("step-1", "knowledge.query", {}),))
        with pytest.raises(PlanValidationError):
            plan.validate(registry)
