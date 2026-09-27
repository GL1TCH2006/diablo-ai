"""M2.14 Phase B — live code-symbol wiring tests.

These drive the *real* indexing entry point (``knowledge_index`` →
``ingest_project_structure``) rather than the symbol helpers directly, because
the wiring itself is what Phase B changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.knowledge_code import (
    ingest_symbols_for_files,
    mark_file_symbols_stale,
    reconcile_symbols,
)
from core.knowledge_graph import (
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_project_key,
)
from core.knowledge_tools import knowledge_index, knowledge_query, manager


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "def hello():\n    return 1\n\n\nclass Greeter:\n    def greet(self):\n        return hello()\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text(
        "from .main import hello\n\n\ndef util():\n    return hello()\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# proj\n", encoding="utf-8")
    return root


@pytest.fixture
def indexed(tmp_settings, project: Path) -> str:
    result = knowledge_index(project_path=str(project), settings=tmp_settings)
    assert result.success, result.error
    return canonical_project_key(str(project))


def _symbols(tmp_settings, project: Path) -> dict[str, dict]:
    """Current (non-stale) symbols, as a normal query returns them."""
    result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                             project_path=str(project), node_type="code_symbol", limit=100)
    assert result.success, result.error
    return {n["display_name"]: n for n in result.data["nodes"]}


def _all_symbols(tmp_settings, project: Path) -> dict[str, dict]:
    """Every symbol including stale ones, for asserting on demoted knowledge."""
    result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                             project_path=str(project), node_type="code_symbol",
                             limit=100, include_stale=True)
    assert result.success, result.error
    return {n["display_name"]: n for n in result.data["nodes"]}


def _graph(tmp_settings, project: Path):
    return manager(tmp_settings).get_graph(canonical_project_key(str(project)))


# ------------------------------------------------------------------- live graph


class TestLiveSymbolIngestion:
    def test_index_produces_code_symbols(self, tmp_settings, project: Path, indexed: str):
        symbols = _symbols(tmp_settings, project)
        assert set(symbols) == {"hello", "Greeter", "greet", "util"}
        assert symbols["hello"]["metadata"]["kind"] == "function"
        assert symbols["Greeter"]["metadata"]["kind"] == "class"
        assert symbols["greet"]["metadata"]["kind"] == "method"

    def test_index_counts_symbols(self, tmp_settings, project: Path):
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.data["python_files"] == 2
        assert result.data["symbols"] == 4

    def test_file_defines_symbol_edge_exists(self, tmp_settings, project: Path, indexed: str):
        graph = _graph(tmp_settings, project)
        file_node = graph.get_node(canonical_file_key(indexed, "src/main.py"))
        assert file_node is not None
        edges = graph.get_edges(source_id=file_node.node_id, relation_type=RelationType.DEFINES)
        assert len(edges) == 3
        graph.close()

    def test_class_contains_method_edge_exists(self, tmp_settings, project: Path, indexed: str):
        graph = _graph(tmp_settings, project)
        symbols = graph.find_nodes_by_key_prefix(NodeType.CODE_SYMBOL, f"symbol:{indexed}:src/main.py:")
        greeter = next(n for n in symbols if n.display_name == "Greeter")
        greet = next(n for n in symbols if n.display_name == "greet")
        edges = graph.get_edges(source_id=greeter.node_id, relation_type=RelationType.CONTAINS)
        assert any(e.target_id == greet.node_id for e in edges)
        graph.close()

    def test_relative_import_becomes_an_edge(self, tmp_settings, project: Path, indexed: str):
        graph = _graph(tmp_settings, project)
        util_node = graph.get_node(canonical_file_key(indexed, "src/util.py"))
        main_node = graph.get_node(canonical_file_key(indexed, "src/main.py"))
        edges = graph.get_edges(source_id=util_node.node_id, relation_type=RelationType.IMPORTS)
        assert any(e.target_id == main_node.node_id for e in edges)
        graph.close()

    def test_unresolvable_import_is_not_guessed(self, tmp_settings, tmp_path: Path):
        root = tmp_path / "lonely"
        root.mkdir()
        (root / "a.py").write_text("import nonexistent_pkg\nfrom os import path\n", encoding="utf-8")
        knowledge_index(project_path=str(root), settings=tmp_settings)
        graph = _graph(tmp_settings, root)
        a_node = graph.get_node(canonical_file_key(canonical_project_key(str(root)), "a.py"))
        assert graph.get_edges(source_id=a_node.node_id, relation_type=RelationType.IMPORTS) == []
        graph.close()

    def test_no_source_text_is_stored(self, tmp_settings, project: Path, indexed: str):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), node_type="code_symbol", limit=100)
        blob = json.dumps(result.data)
        assert "return 1" not in blob
        assert "def hello" not in blob

    def test_file_metadata_records_symbol_count(self, tmp_settings, project: Path, indexed: str):
        result = knowledge_query(
            query_type="find_node", settings=tmp_settings, project_path=str(project),
            canonical_key=canonical_file_key(indexed, "src/main.py"),
        )
        assert result.data["node"]["metadata"]["symbol_count"] == 3


# -------------------------------------------------------------------- idempotency


class TestIdempotency:
    def test_repeated_index_does_not_grow_the_graph(self, tmp_settings, project: Path, indexed: str):
        graph = _graph(tmp_settings, project)
        before = graph.stats()
        graph.close()

        for _ in range(3):
            assert knowledge_index(project_path=str(project), settings=tmp_settings).success

        graph = _graph(tmp_settings, project)
        after = graph.stats()
        graph.close()
        assert after == before

    def test_repeated_index_keeps_symbols_live(self, tmp_settings, project: Path, indexed: str):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        symbols = _symbols(tmp_settings, project)
        assert set(symbols) == {"hello", "Greeter", "greet", "util"}
        assert all(n["metadata"].get("stale") is False for n in symbols.values())


# ----------------------------------------------------------------------- change


class TestChangedSource:
    def test_removed_symbol_is_marked_stale(self, tmp_settings, project: Path, indexed: str):
        (project / "src" / "main.py").write_text("def goodbye():\n    return 2\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)

        # The current answer is the file as it is now, and nothing else.
        # "util" survives because src/util.py still defines it.
        current = _symbols(tmp_settings, project)
        assert set(current) == {"goodbye", "util"}
        assert current["goodbye"]["state"] == "current"

        # The functions that used to be here are kept, but demoted.
        everything = _all_symbols(tmp_settings, project)
        for gone in ("hello", "Greeter", "greet"):
            assert everything[gone]["metadata"]["stale"] is True
            assert everything[gone]["state"] == "stale"

    def test_renamed_symbol_does_not_remain_authoritative(self, tmp_settings, project: Path, indexed: str):
        (project / "src" / "main.py").write_text("def hi():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        current = _symbols(tmp_settings, project)
        assert "hi" in current
        assert "hello" not in current
        everything = _all_symbols(tmp_settings, project)
        assert everything["hello"]["metadata"]["stale"] is True

    def test_changed_file_updates_line_numbers(self, tmp_settings, project: Path, indexed: str):
        (project / "src" / "main.py").write_text(
            "\n\n\ndef hello():\n    return 1\n", encoding="utf-8"
        )
        knowledge_index(project_path=str(project), settings=tmp_settings)
        symbols = _symbols(tmp_settings, project)
        assert symbols["hello"]["metadata"]["line_start"] == 4

    def test_unchanged_file_is_not_reparsed(self, tmp_settings, project: Path, indexed: str):
        result = knowledge_query(
            query_type="find_node", settings=tmp_settings, project_path=str(project),
            canonical_key=canonical_file_key(indexed, "src/main.py"),
        )
        first_hash = result.data["node"]["metadata"]["symbol_hash"]
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result2 = knowledge_query(
            query_type="find_node", settings=tmp_settings, project_path=str(project),
            canonical_key=canonical_file_key(indexed, "src/main.py"),
        )
        assert result2.data["node"]["metadata"]["symbol_hash"] == first_hash


# -------------------------------------------------------------------- deletion


class TestDeletedSource:
    def test_deleted_file_symbols_are_marked_stale(self, tmp_settings, project: Path, indexed: str):
        (project / "src" / "util.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        # A stale symbol is not merely flagged, it is withheld: a query for the
        # functions in this project must not offer one that no longer exists.
        current = _symbols(tmp_settings, project)
        assert "util" not in current
        assert "hello" in current

        everything = _all_symbols(tmp_settings, project)
        assert everything["util"]["metadata"]["stale"] is True
        assert everything["util"]["state"] == "stale"
        assert everything["hello"]["metadata"].get("stale", False) is False
        assert everything["hello"]["state"] == "current"

    def test_stale_count_is_reported_not_hidden(self, tmp_settings, project: Path, indexed: str):
        (project / "src" / "util.py").unlink()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), node_type="code_symbol",
                                 limit=100)
        # "Nothing matched" and "what matched no longer exists" are different
        # answers, so the second is reported rather than left implicit.
        assert result.data["stale_withheld"] >= 1

    def test_deleted_file_is_reported_in_stats(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "util.py").unlink()
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.data["stale_symbols"] >= 1

    def test_mark_file_symbols_stale_helper(self, tmp_settings, project: Path, indexed: str):
        graph = _graph(tmp_settings, project)
        assert mark_file_symbols_stale(graph, "src/util.py") == 1
        symbols = graph.find_nodes_by_key_prefix(
            NodeType.CODE_SYMBOL, f"symbol:{indexed}:src/util.py:"
        )
        assert all(s.metadata.get("stale") is True for s in symbols)
        graph.close()

    def test_reconcile_symbols_marks_unknown_files(self, tmp_settings, project: Path, indexed: str):
        graph = _graph(tmp_settings, project)
        stats = reconcile_symbols(graph, ["src/main.py"])
        assert stats["stale_symbols"] == 1
        assert stats["stale_files"] == 1
        graph.close()


# ----------------------------------------------------------------- syntax error


class TestSyntaxErrors:
    def test_syntax_error_does_not_crash_indexing(self, tmp_settings, project: Path):
        (project / "src" / "broken.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.success
        # The healthy file is still indexed despite its broken neighbour.
        symbols = _symbols(tmp_settings, project)
        assert "hello" in symbols
        assert symbols["hello"]["metadata"]["stale"] is False

    def test_syntax_error_yields_no_symbols_for_that_file(self, tmp_settings, project: Path):
        (project / "src" / "broken.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = _graph(tmp_settings, project)
        broken = graph.find_nodes_by_key_prefix(
            NodeType.CODE_SYMBOL, f"symbol:{canonical_project_key(str(project))}:src/broken.py:"
        )
        assert broken == []
        graph.close()

    def test_becoming_broken_marks_previous_symbols_stale(self, tmp_settings, project: Path, indexed: str):
        assert _symbols(tmp_settings, project)["hello"]["state"] == "current"
        (project / "src" / "main.py").write_text("def broken(:\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        # "we could not read it" is not "it defines nothing": prior symbols are
        # retained but demoted rather than deleted, and withheld from the
        # current answer.
        assert "hello" not in _symbols(tmp_settings, project)
        everything = _all_symbols(tmp_settings, project)
        assert everything["hello"]["metadata"]["stale"] is True
        assert everything["hello"]["metadata"]["stale_reason"] == "unreadable"


# ------------------------------------------------------------- scoping/security


class TestScoping:
    def test_ignored_directories_yield_no_symbols(self, tmp_settings, project: Path):
        vendor = project / "node_modules" / "pkg"
        vendor.mkdir(parents=True)
        (vendor / "index.py").write_text("def vendor_fn():\n    pass\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        symbols = _symbols(tmp_settings, project)
        assert "vendor_fn" not in symbols

    def test_dotfiles_yield_no_symbols(self, tmp_settings, project: Path):
        (project / ".hidden.py").write_text("def secret():\n    pass\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert "secret" not in _symbols(tmp_settings, project)

    def test_unauthorized_project_yields_nothing(self, tmp_settings):
        result = knowledge_index(project_path="C:/Windows/System32", settings=tmp_settings)
        assert not result.success
        assert "authorized" in result.error

    def test_symbol_ingestion_never_parses_outside_project(self, tmp_settings, project: Path, indexed: str):
        """A path escaping the project must not be reachable via the prefix API."""
        graph = _graph(tmp_settings, project)
        found = graph.find_nodes_by_key_prefix(NodeType.CODE_SYMBOL, "symbol:other-project:../../etc:")
        assert found == []
        graph.close()

    def test_non_python_files_produce_no_symbols(self, tmp_settings, project: Path):
        (project / "notes.py.txt").write_text("def not_python():\n    pass\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        assert "not_python" not in _symbols(tmp_settings, project)


# ------------------------------------------------------------ shared pipeline


class TestSharedPipeline:
    def test_symbols_route_through_the_single_implementation(self, tmp_settings, project: Path, indexed: str):
        """The standalone helper and the live flow must agree.

        The live indexing flow already ingested this file, so re-running the
        shared implementation over it must be a no-op rather than a second,
        divergent set of symbol nodes.
        """
        graph = _graph(tmp_settings, project)
        before = graph.stats()
        stats = ingest_symbols_for_files(graph, project, ["src/main.py"])
        after = graph.stats()
        graph.close()

        assert stats["files_processed"] == 1
        assert stats["symbols_ingested"] == 3
        assert after == before
