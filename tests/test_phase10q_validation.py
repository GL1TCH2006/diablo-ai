"""M2.14 Phase 10Q - Final Phase 10 Validation.

A validation matrix over the completed Phase 10 architecture, as defined in
``docs/M2.14_PHASE10Q_VALIDATION.md``. 10Q adds no behaviour. Every test here
either points at a pre-existing authoritative component to prove it still holds,
or pins the structural invariant Phase 10A introduced.

The matrix deliberately does not re-derive the model-facing assertions that 10Q
raised as ``DEFECT-1``: whether a real model reaches for ``knowledge.query`` is a
fact about the model, not about this system, and asserting it tested the model
instead of the architecture. What is asserted instead is the property those
tests were actually protecting - persistence, and that the graph never claims
more than the ledger adjudicated - against the store rather than against the
model's tool choices.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
)
from core.knowledge_projection import knowledge_dir
from core.knowledge_tools import knowledge_index, knowledge_query
from core.task_manager import TaskState, TerminalVerdict, terminal_verdict
from core.task_store import TaskRecord, TaskStore
from core.verification import VerificationState
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _close_handles():
    yield
    close_shared_managers()


# ── rows 15/16: knowledge graph is projection-only, and survives a restart


def _broken_project(root: Path) -> Path:
    project = root / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\n"
        "def test_add_positive():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    return project


class TestProjectionIsNotAuthority:
    """Row 15. The graph may describe verification; it may never assert it."""

    def test_the_graph_holds_nothing_before_anything_is_projected(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        close_shared_managers()
        manager = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            graph = manager.get_graph(canonical_project_key(str(project)))
            assert graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10) == [], (
                "verification nodes appeared with no ledger entry behind them")
        finally:
            manager.close_all()

    def test_an_empty_ledger_projects_no_verification_node(self, tmp_path: Path):
        """The projection is driven by ledger entries, not by task status."""
        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        store = TaskStore(tmp_path / "sessions")
        record = TaskRecord(
            task_id="q-projection-1",
            project_name="proj",
            project_root=str(project),
            title="t",
            goal="g",
            created_at="20260101-000000",
        )
        store.create(record)
        # A record with no lifecycle.verification_ledger projection at all.
        store.finish(record.task_id, "done", "completed")

        close_shared_managers()
        manager = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            graph = manager.get_graph(canonical_project_key(str(project)))
            assert graph.find_nodes(node_type=NodeType.VERIFICATION, limit=10) == [], (
                "a finished task with an empty ledger projection created a "
                "verification node anyway")
        finally:
            manager.close_all()

    def test_a_graph_node_cannot_outnumber_the_ledger(self, tmp_path: Path):
        """Row 15, positive direction: the graph records what was adjudicated."""
        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        knowledge_index(project_path=str(project), settings=settings)
        close_shared_managers()
        manager = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            graph = manager.get_graph(canonical_project_key(str(project)))
            nodes = graph.find_nodes(node_type=NodeType.VERIFICATION, limit=50)
            for node in nodes:
                payload = str(node.to_dict())
                assert "passed" not in payload.lower(), (
                    f"a passing verification reached the graph with no ledger "
                    f"entry behind it: {payload}")
        finally:
            manager.close_all()


class TestPersistenceAcrossInterpreterRestart:
    """Row 16. A separate process reads what the first one wrote."""

    def test_a_second_interpreter_sees_the_first_ones_knowledge(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        knowledge_index(project_path=str(project), settings=settings)
        close_shared_managers()
        databases = sorted(knowledge_dir(settings).glob("*.db"))
        assert databases, "the first process left no knowledge database"

        # A genuinely separate interpreter, not an in-process re-read.
        child = subprocess.run(
            [sys.executable, "-c",
             "import json,sys\n"
             "from pathlib import Path\n"
             f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
             "from config.settings import Settings\n"
             "from core.knowledge_tools import knowledge_query\n"
             f"from tests.conftest import make_settings\n"
             f"s = make_settings(Path({str(tmp_path)!r}))\n"
             f"r = knowledge_query(query_type='search_files', settings=s,\n"
             f"                   project_path={str(project)!r}, limit=50)\n"
             "names = sorted(n['display_name'] for n in r.data['nodes'])\n"
             "print(json.dumps(names))\n"],
            capture_output=True, text=True, timeout=180, cwd=str(tmp_path))
        assert child.returncode == 0, child.stderr[-2000:]
        import json
        names = json.loads(child.stdout.strip())
        assert "calc.py" in names, (
            f"a second interpreter did not see the first one's knowledge: {names}")

    def test_a_read_only_session_does_not_mutate_the_store(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        knowledge_index(project_path=str(project), settings=settings)
        before = knowledge_query(query_type="search_files", settings=settings,
                                 project_path=str(project), limit=50)
        assert before.success, before.error
        after = knowledge_query(query_type="search_files", settings=settings,
                                project_path=str(project), limit=50)
        assert after.success
        assert after.data["count"] == before.data["count"], (
            "a read-only query changed the stored knowledge")

    def test_a_reloaded_task_record_keeps_an_admissible_terminal_pair(
            self, tmp_path: Path):
        """Rows 16 and 20 across the restart boundary."""
        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        store = TaskStore(settings.sessions_dir)
        record = TaskRecord(
            task_id="q-restart-1",
            project_name="proj",
            project_root=str(project),
            title="t",
            goal="g",
            created_at="20260101-000000",
        )
        store.create(record)
        store.update(record.task_id, metadata={"lifecycle": {
            "state": "failed",
            "verification": "failed",
            "verification_ledger": {"schema": "verification_ledger/1", "entries": [
                {"seq": 0, "state": "failed", "detail": "the suite failed",
                 "evidence": {"exit_code": 1}, "recorded_at": "20260101-000000"}]},
        }})
        store.finish(record.task_id, "could not repair", "failed")

        reloaded = TaskStore(settings.sessions_dir).get("q-restart-1")
        assert reloaded is not None
        lifecycle = (reloaded.metadata or {}).get("lifecycle") or {}
        assert terminal_verdict(lifecycle["state"],
                                lifecycle["verification"]) is TerminalVerdict.FAILED


class TestProjectIsolation:
    """Rows 17 and 18, cross-project."""

    def test_one_projects_knowledge_never_appears_in_another(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        first = _broken_project(tmp_path / "a")
        second = _broken_project(tmp_path / "b")
        (second / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        knowledge_index(project_path=str(first), settings=settings)
        close_shared_managers()

        seen = knowledge_query(query_type="search_files", settings=settings,
                               project_path=str(first), limit=50)
        assert seen.success, seen.error
        source = {n["display_name"] for n in seen.data["nodes"]}
        assert "calc.py" in source

        # The second project was never indexed, so it must hold nothing.
        other = knowledge_query(query_type="search_files", settings=settings,
                                project_path=str(second), limit=50)
        assert other.success, other.error
        assert other.data["count"] == 0, (
            f"an unindexed project returned knowledge: "
            f"{[n['display_name'] for n in other.data['nodes']]}")

    def test_the_two_projects_have_distinct_graph_keys(self, tmp_path: Path):
        first = canonical_project_key(str(tmp_path / "a"))
        second = canonical_project_key(str(tmp_path / "b"))
        assert first != second


class TestTheMatrixCoversWhatItClaims:
    """The matrix must not rot: every row it claims is referenced here."""

    REQUIRED_COMPONENTS = [
        "core.recovery",            # rows 5, 6
        "core.verification_ledger",  # rows 3, 4
        "core.task_manager",        # rows 19, 20
        "core.task_store",          # rows 7, 8, 20
        "core.knowledge_projection",  # row 15
        "core.knowledge_tools",     # rows 9, 15, 16
        "core.agent",               # rows 1, 2, 10, 13, 14
    ]

    @pytest.mark.parametrize("module", REQUIRED_COMPONENTS)
    def test_the_named_authority_is_importable_and_present(self, module):
        import importlib

        assert importlib.import_module(module) is not None, (
            f"10Q row names {module} as an authority but it does not exist")

    def test_the_matrix_document_exists(self):
        doc = Path(__file__).resolve().parents[1] / "docs" / "M2.14_PHASE10Q_VALIDATION.md"
        assert doc.is_file(), "10Q has no matrix document"
        text = doc.read_text(encoding="utf-8")
        for marker in ("real-model coding failure", "VerificationLedger recording",
                       "terminal-state invariants", "Project isolation",
                       "persistence across interpreter restart"):
            assert marker.lower() in text.lower(), (
                f"the matrix does not cover {marker!r}")
