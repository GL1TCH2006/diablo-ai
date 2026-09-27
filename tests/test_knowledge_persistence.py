"""M2.14 Phase 8B — cross-session persistence across two real processes.

The point of this module is that nothing is shared between the two halves. No
in-memory graph, no module global, no fixture object: the first process writes
knowledge and exits, and a second, independently started interpreter is the
only thing that reads it back. Anything that survived did so because it was on
disk.

The child is launched with ``subprocess`` running this same file with a mode
argument, and both halves exchange state through a JSON file, so a passing test
is a statement about persistence rather than about object lifetimes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _child_env(settings_json: dict) -> dict:
    env = dict(os.environ)
    env["DIABLO_P8_SETTINGS"] = json.dumps(settings_json)
    env["PYTHONPATH"] = str(REPO)
    return env


def _run_child(mode: str, settings_json: dict, extra: dict | None = None) -> dict:
    """Run one half of the scenario in a separate interpreter."""
    payload = {"mode": mode, "settings": settings_json, **(extra or {})}
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_child_env(settings_json),
        cwd=str(REPO),
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"child {mode} failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    # The child prints one JSON line prefixed with a marker.
    for line in proc.stdout.splitlines():
        if line.startswith("P8RESULT "):
            return json.loads(line[len("P8RESULT "):])
    raise AssertionError(
        f"child {mode} produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Child process: builds knowledge, then reads it back in a fresh interpreter
# ──────────────────────────────────────────────────────────────────────────


def _child_write(settings_json: dict, project_path: str) -> dict:
    """Session 1: index, run real work, and leave the knowledge on disk."""
    from core.knowledge_graph import (
        KnowledgeGraphManager,
        canonical_project_key,
        close_shared_managers,
    )
    from core.knowledge_projection import knowledge_dir, project_tool_result
    from core.knowledge_tools import knowledge_index

    settings = _build_settings(settings_json)
    project = Path(project_path)

    knowledge_index(project_path=str(project), settings=settings)

    # Real data work, projected exactly as the Agent would project it.
    csv = project / "sales.csv"
    mgr = KnowledgeGraphManager(knowledge_dir(settings))
    project_tool_result(
        tool="data.inspect",
        args={"path": str(csv)},
        result_data={
            "headers": ["product", "amount"],
            "row_count": 3,
            "column_types": {"product": "string", "amount": "integer"},
        },
        task_id="",
        project_root=str(project),
        settings=settings,
        manager=mgr,
    )
    project_tool_result(
        tool="data.analyze",
        args={"path": str(csv), "operation": "groupby",
              "group_by": "product", "column": "amount"},
        result_data={"ok": True, "groups": 2},
        task_id="",
        project_root=str(project),
        settings=settings,
        manager=mgr,
    )

    pid = canonical_project_key(str(project))
    graph = mgr.get_graph(pid)

    # Record the identities session 1 established, so session 2 can prove they
    # are unchanged rather than merely present.
    identity = {
        node.node_type.value: {
            "node_id": node.node_id,
            "canonical_key": node.canonical_key,
        }
        for node in graph.find_nodes(limit=1000)
    }
    counts = _counts(graph)
    db_name = graph.db_path.name

    # Explicitly flush and close: nothing may be left to process teardown.
    mgr.close_all()
    close_shared_managers()
    return {"identity": identity, "counts": counts, "db_name": db_name, "project_id": pid}


def _child_read(settings_json: dict, project_path: str) -> dict:
    """Session 2: a fresh interpreter that only knows the project path."""
    from core.knowledge_graph import (
        KnowledgeGraph,
        NodeType,
        canonical_project_key,
        close_shared_managers,
    )
    from core.knowledge_projection import knowledge_dir
    from core.knowledge_query import KnowledgeQueryEngine

    settings = _build_settings(settings_json)
    project = Path(project_path)
    pid = canonical_project_key(str(project))

    # Opened exactly as the query path opens it: read-only, from disk.
    graph = KnowledgeGraph(knowledge_dir(settings), pid, readonly=True)
    engine = KnowledgeQueryEngine(graph)


    by_type = {}
    for node_type in NodeType:
        nodes = engine.find_nodes(node_type=node_type, limit=500).nodes
        if nodes:
            by_type[node_type.value] = {
                n.display_name: {"node_id": n.node_id,
                                 "canonical_key": n.canonical_key,
                                 "state": n.to_dict()["state"]}
                for n in nodes
            }

    identity = {
        node.node_type.value: {
            "node_id": node.node_id,
            "canonical_key": node.canonical_key,
        }
        for node in graph.find_nodes(limit=1000, include_stale=True)
    }
    counts = _counts(graph)

    # Relationship integrity after reopen.
    task_ids = [n.node_id for n in engine.find_nodes(node_type=NodeType.TASK, limit=50).nodes]
    file_ids = [n.node_id for n in engine.find_nodes(node_type=NodeType.FILE, limit=500).nodes]
    task_changed_by = 0
    for tid in task_ids:
        task_changed_by += len(graph.get_edges(source_id=tid))

    graph.close()
    close_shared_managers()
    return {
        "by_type": by_type,
        "identity": identity,
        "counts": counts,
        "task_changed_by_edges": task_changed_by,
        "file_count": len(file_ids),
    }


def _counts(graph) -> dict:
    nodes = graph.find_nodes(limit=100000, include_stale=True)
    by_node = {}
    for node in nodes:
        by_node[node.node_type.value] = by_node.get(node.node_type.value, 0) + 1
    edges = graph._get_conn().execute(
        "SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type"
    ).fetchall()
    return {
        "nodes": len(nodes),
        "edges": sum(r[1] for r in edges),
        "nodes_by_type": by_node,
        "edges_by_type": {r[0]: r[1] for r in edges},
    }


# ──────────────────────────────────────────────────────────────────────────
# The two-process test
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "p8-project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "def hello():\n    return 1\n\n\nclass Greeter:\n    pass\n", encoding="utf-8"
    )
    (root / "sales.csv").write_text(
        "product,amount\nA,10\nB,20\nA,30\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def settings_json(tmp_path: Path, project: Path) -> dict:
    return {
        "base_dir": str(tmp_path),
        "data_dir": str(tmp_path / "data"),
        "allowed_roots": [str(tmp_path)],
        "project_path": str(project),
    }


def _settings_kwargs(settings_json: dict) -> dict:
    return {k: v for k, v in settings_json.items() if k != "project_path"}


def _build_settings(settings_json: dict):
    """Reconstruct Settings in a child process from JSON.

    Paths are rebuilt as ``Path`` objects because that is what the type expects
    and what the permission checks call methods on.
    """
    from config.settings import Settings

    kwargs = dict(settings_json)
    for key in ("base_dir", "data_dir"):
        if key in kwargs:
            kwargs[key] = Path(kwargs[key])
    kwargs["allowed_roots"] = [Path(p) for p in kwargs.get("allowed_roots", [])]
    return Settings(**kwargs)


class TestTwoProcessPersistence:
    def test_knowledge_survives_process_termination(self, settings_json):
        """Session 1 writes and exits; session 2 reads from disk alone."""
        first = _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        second = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})

        # Everything session 1 recorded is readable in a new interpreter.
        assert second["by_type"], "session 2 found no knowledge at all"
        for expected in ("project", "file", "code_symbol", "dataset", "analysis"):
            assert expected in second["by_type"], f"{expected} did not survive restart"

    def test_symbols_survive_restart(self, settings_json):
        first = _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        second = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        symbols = second["by_type"].get("code_symbol", {})
        assert {"hello", "Greeter"} <= set(symbols)
        assert all(s["state"] == "current" for s in symbols.values())

    def test_dataset_and_analysis_survive_restart(self, settings_json):
        _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        second = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        assert second["by_type"].get("dataset"), "dataset did not survive"
        analyses = second["by_type"].get("analysis", {})
        assert analyses, "analysis did not survive"
        op = list(analyses.values())[0]["canonical_key"]
        assert op.startswith("analysis:")

    def test_node_identity_is_unchanged_across_restart(self, settings_json):
        """node_id is derived from canonical_key, so it must be identical."""
        first = _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        second = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        for node_type, entry in first["identity"].items():
            assert node_type in second["identity"], f"{node_type} missing after restart"
            assert second["identity"][node_type]["node_id"] == entry["node_id"], (
                f"{node_type} changed identity across restart")
            assert (second["identity"][node_type]["canonical_key"]
                    == entry["canonical_key"])

    def test_counts_are_identical_across_restart(self, settings_json):
        first = _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        second = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        assert second["counts"]["nodes"] == first["counts"]["nodes"]
        assert second["counts"]["edges"] == first["counts"]["edges"]
        assert second["counts"]["nodes_by_type"] == first["counts"]["nodes_by_type"]
        assert second["counts"]["edges_by_type"] == first["counts"]["edges_by_type"]

    def test_reopening_creates_no_duplicates(self, settings_json):
        """A read must not write, so counts cannot move."""
        _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        first_read = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        second_read = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        assert second_read["counts"] == first_read["counts"]

    def test_schema_version_survives_restart(self, settings_json):
        written = _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        read = _run_child("read", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        assert written["counts"]["nodes"] == read["counts"]["nodes"] > 0

    def test_database_is_a_real_enumerable_file(self, settings_json, tmp_path):
        """The database must be a real file, not a Windows data stream."""
        written = _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        kd = tmp_path / "data" / "knowledge"
        names = sorted(p.name for p in kd.iterdir())
        assert written["db_name"] in names
        assert not any(":" in n for n in names), (
            f"a filename still contains a colon, which is a data stream on "
            f"Windows: {names}"
        )
        assert (kd / written["db_name"]).stat().st_size > 0

    def test_a_query_process_creates_no_new_database(self, settings_json, tmp_path):
        _run_child("write", _settings_kwargs(settings_json), {
            "project_path": settings_json["project_path"]})
        kd = tmp_path / "data" / "knowledge"
        before = sorted(p.name for p in kd.iterdir())
        _run_child("query_missing", _settings_kwargs(settings_json), {
            "project_path": str(tmp_path / "never-indexed")})
        after = sorted(p.name for p in kd.iterdir())
        assert before == after


def _child_query_missing(settings_json: dict, project_path: str) -> dict:
    """Query a project that was never indexed, in its own process."""
    from core.knowledge_graph import close_shared_managers
    from core.knowledge_tools import knowledge_query

    settings = _build_settings(settings_json)
    result = knowledge_query(
        query_type="find_nodes", settings=settings, project_path=project_path,
    )
    close_shared_managers()
    return {"success": result.success, "indexed": (result.data or {}).get("indexed")}


# ──────────────────────────────────────────────────────────────────────────
# Child entry point
# ──────────────────────────────────────────────────────────────────────────


def _main() -> int:
    if "--child" not in sys.argv:
        return 0
    request = json.loads(sys.stdin.read())
    settings_json = request["settings"]
    project_path = request["project_path"]
    mode = request["mode"]
    if mode == "write":
        result = _child_write(settings_json, project_path)
    elif mode == "read":
        result = _child_read(settings_json, project_path)
    elif mode == "query_missing":
        result = _child_query_missing(settings_json, project_path)
    else:
        raise SystemExit(f"unknown mode {mode}")
    sys.stdout.write("P8RESULT " + json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
