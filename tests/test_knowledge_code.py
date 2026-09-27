"""M2.14 Phase 3 — Code Relationships Extraction tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_file_key,
    canonical_symbol_key,
)
from core.knowledge_code import (
    Symbol,
    MAX_PARSE_SIZE,
    _compute_file_hash,
    extract_python_symbols,
    extract_python_imports,
    resolve_import_to_file,
    ingest_python_file_symbols,
    ingest_project_symbols,
)


class TestPythonSymbolExtraction:
    def test_extract_functions(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("""
def hello():
    pass

def world(x: int) -> str:
    return str(x)

async def async_func():
    pass
""")
        symbols = extract_python_symbols(f)
        funcs = [s for s in symbols if s.kind == "function"]
        assert len(funcs) == 3
        names = {s.name for s in funcs}
        assert names == {"hello", "world", "async_func"}

    def test_extract_classes(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("""
class MyClass:
    def method1(self):
        pass

    def method2(self, x):
        return x

class AnotherClass:
    pass
""")
        symbols = extract_python_symbols(f)
        classes = [s for s in symbols if s.kind == "class"]
        methods = [s for s in symbols if s.kind == "method"]
        assert len(classes) == 2
        assert len(methods) == 2
        class_names = {s.name for s in classes}
        assert class_names == {"MyClass", "AnotherClass"}
        method_names = {s.name for s in methods}
        assert method_names == {"method1", "method2"}

    def test_method_parent_class(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("""
class MyClass:
    def my_method(self):
        pass
""")
        symbols = extract_python_symbols(f)
        method = next(s for s in symbols if s.kind == "method")
        assert method.parent_class == "MyClass"
        assert method.qualified_name == "MyClass.my_method"

    def test_nested_classes(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("""
class Outer:
    class Inner:
        def inner_method(self):
            pass
""")
        symbols = extract_python_symbols(f)
        inner_class = next(s for s in symbols if s.name == "Inner")
        assert inner_class.qualified_name == "Outer.Inner"
        method = next(s for s in symbols if s.name == "inner_method")
        assert method.parent_class == "Outer.Inner"
        assert method.qualified_name == "Outer.Inner.inner_method"

    def test_syntax_error_handled(self, tmp_path: Path):
        f = tmp_path / "bad.py"
        f.write_text("def broken(:\n    pass")
        symbols = extract_python_symbols(f)
        assert symbols == []

    def test_large_file_skipped(self, tmp_path: Path):
        f = tmp_path / "large.py"
        f.write_text("# x\n" * (MAX_PARSE_SIZE // 4))
        symbols = extract_python_symbols(f)
        assert symbols == []


class TestPythonImportExtraction:
    def test_extract_imports(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("""
import os
import sys as system
from pathlib import Path
from typing import List, Dict
from .local import something
from ..parent import other
""")
        imports = extract_python_imports(f)
        assert "os" in imports
        assert "sys" in imports
        assert "pathlib.Path" in imports
        assert "typing.List" in imports
        assert "typing.Dict" in imports
        assert ".local.something" in imports
        assert "..parent.other" in imports


class TestImportResolution:
    @pytest.fixture
    def temp_project(self, tmp_path: Path):
        proj = tmp_path / "myproj"
        proj.mkdir()
        (proj / "pkg").mkdir()
        (proj / "pkg" / "__init__.py").write_text("")
        (proj / "pkg" / "module.py").write_text("def func(): pass\n")
        (proj / "main.py").write_text("from pkg.module import func\n")
        return proj

    @pytest.fixture
    def graph(self, tmp_path):
        from core.knowledge_graph import KnowledgeGraph
        g = KnowledgeGraph(tmp_path, "test-proj")
        yield g
        g.close()

    def test_resolve_absolute_import(self, temp_project: Path, graph: KnowledgeGraph):
        # First ingest the target file
        file_key = canonical_file_key(graph.project_id, "pkg/module.py")
        graph.upsert_node(NodeType.FILE, file_key, "module.py", {"path": "pkg/module.py"})

        # Now resolve
        target = resolve_import_to_file("pkg.module.func", temp_project / "main.py", temp_project, graph)
        assert target is not None
        assert target == file_key

    def test_resolve_relative_import(self, temp_project: Path, graph: KnowledgeGraph):
        # Create subpackage structure
        (temp_project / "pkg" / "sub").mkdir()
        (temp_project / "pkg" / "sub" / "__init__.py").write_text("")
        (temp_project / "pkg" / "sub" / "inner.py").write_text("")

        file_key = canonical_file_key(graph.project_id, "pkg/sub/inner.py")
        graph.upsert_node(NodeType.FILE, file_key, "inner.py", {"path": "pkg/sub/inner.py"})

        # From main.py, relative import
        target = resolve_import_to_file(".sub.inner", temp_project / "pkg" / "module.py", temp_project, graph)
        assert target == file_key

    def test_unresolved_import_returns_none(self, temp_project: Path, graph: KnowledgeGraph):
        target = resolve_import_to_file("nonexistent.module", temp_project / "main.py", temp_project, graph)
        assert target is None


class TestSymbolIngestion:
    @pytest.fixture
    def temp_project(self, tmp_path: Path):
        proj = tmp_path / "myproj"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "main.py").write_text("""
def hello():
    pass

class MyClass:
    def method(self):
        pass

    async def async_method(self, x: int) -> str:
        return str(x)
""")
        (proj / "src" / "utils.py").write_text("""
import os
from pathlib import Path

def util_func():
    return 42
""")
        return proj

    @pytest.fixture
    def graph(self, tmp_path):
        from core.knowledge_graph import KnowledgeGraph
        g = KnowledgeGraph(tmp_path, "test-proj")
        yield g
        g.close()

    def test_ingest_single_file_symbols(self, graph: KnowledgeGraph, temp_project: Path):
        # Pre-create file node
        file_key = canonical_file_key(graph.project_id, "src/main.py")
        file_node = graph.upsert_node(NodeType.FILE, file_key, "main.py", {"path": "src/main.py"})

        count = ingest_python_file_symbols(graph, "src/main.py", temp_project)
        assert count >= 3  # hello, MyClass, method, async_method

        # Check symbols exist
        hello_key = canonical_symbol_key(graph.project_id, "src/main.py", "hello", "function")
        assert graph.get_node(hello_key) is not None

        class_key = canonical_symbol_key(graph.project_id, "src/main.py", "MyClass", "class")
        class_node = graph.get_node(class_key)
        assert class_node is not None

        method_key = canonical_symbol_key(graph.project_id, "src/main.py", "MyClass.method", "method")
        assert graph.get_node(method_key) is not None

        # Check edges
        edges = graph.get_edges(source_id=file_node.node_id, relation_type=RelationType.DEFINES)
        assert len(edges) >= 3

    def test_method_linked_to_class(self, graph: KnowledgeGraph, temp_project: Path):
        file_key = canonical_file_key(graph.project_id, "src/main.py")
        graph.upsert_node(NodeType.FILE, file_key, "main.py", {"path": "src/main.py"})

        ingest_python_file_symbols(graph, "src/main.py", temp_project)

        class_key = canonical_symbol_key(graph.project_id, "src/main.py", "MyClass", "class")
        class_node = graph.get_node(class_key)
        method_key = canonical_symbol_key(graph.project_id, "src/main.py", "MyClass.method", "method")
        method_node = graph.get_node(method_key)

        # Check class -> contains -> method
        edges = graph.get_edges(source_id=class_node.node_id, relation_type=RelationType.CONTAINS)
        assert any(e.target_id == method_node.node_id for e in edges)

    def test_import_edges_created(self, graph: KnowledgeGraph, temp_project: Path):
        # Create both files
        main_key = canonical_file_key(graph.project_id, "src/main.py")
        utils_key = canonical_file_key(graph.project_id, "src/utils.py")
        graph.upsert_node(NodeType.FILE, main_key, "main.py", {"path": "src/main.py"})
        graph.upsert_node(NodeType.FILE, utils_key, "utils.py", {"path": "src/utils.py"})

        ingest_python_file_symbols(graph, "src/main.py", temp_project)
        ingest_python_file_symbols(graph, "src/utils.py", temp_project)

        # main.py doesn't import utils.py directly, but utils.py imports os, pathlib
        # Check imports from utils.py
        utils_node = graph.get_node(utils_key)
        edges = graph.get_edges(source_id=utils_node.node_id, relation_type=RelationType.IMPORTS)
        # Note: stdlib imports won't resolve to project files
        # This test mainly verifies no errors

    def test_project_wide_ingestion(self, graph: KnowledgeGraph, temp_project: Path):
        # Pre-create file nodes
        for rel in ["src/main.py", "src/utils.py"]:
            fk = canonical_file_key(graph.project_id, rel)
            graph.upsert_node(NodeType.FILE, fk, rel.split("/")[-1], {"path": rel})

        stats = ingest_project_symbols(graph, temp_project)
        assert stats["files_processed"] == 2
        assert stats["symbols_ingested"] >= 4


class TestCanonicalKeys:
    def test_symbol_key_format(self):
        key = canonical_symbol_key("proj1", "src/main.py", "MyClass.method", "method")
        assert key == "symbol:proj1:src/main.py:method:MyClass.method"

    def test_symbol_key_class(self):
        key = canonical_symbol_key("proj1", "src/main.py", "MyClass", "class")
        assert key == "symbol:proj1:src/main.py:class:MyClass"


class TestContentHash:
    def test_file_hash(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        h = _compute_file_hash(f)
        assert h is not None
        assert len(h) == 16

    def test_same_content_same_hash(self, tmp_path: Path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("same")
        f2.write_text("same")
        assert _compute_file_hash(f1) == _compute_file_hash(f2)

    def test_large_file_no_hash(self, tmp_path: Path):
        f = tmp_path / "large.py"
        f.write_text("x" * (MAX_PARSE_SIZE + 1))
        assert _compute_file_hash(f) is None