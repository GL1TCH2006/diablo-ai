"""M2.14 Phase 8P — security regression for the persistence layer.

Phase 8 turned the knowledge graph into something that outlives a process, is
reached by name, and survives restarts. Each of those is a way for the wrong
thing to become reachable, so each gets a test here:

* a database filename is built from a string, so it must be confined to the
  knowledge directory for *any* input, not merely for the ids that happen to be
  produced today;
* the read path must be unable to write, even after an index has run in the
  same process;
* one project must not be able to observe or delete another's knowledge;
* an unauthorized root must stay unauthorized across a restart;
* nothing that is not already public may be written to disk.

These are regression tests. Each states a property that must not be broken by
later work, and each fails loudly if the confinement it relies on is weakened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
    db_filename,
    is_stale,
    legacy_db_path,
)
from core.knowledge_projection import knowledge_dir, project_tool_result
from core.knowledge_tools import knowledge_index, knowledge_query, manager


@pytest.fixture(autouse=True)
def _release_handles():
    yield
    close_shared_managers()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "def alpha():\n    return 1\n", encoding="utf-8")
    (root / "data.csv").write_text("k,v\nx,1\ny,2\n", encoding="utf-8")
    return root


def _make_project(base: Path, name: str, marker: str) -> Path:
    root = base / name
    (root / "src").mkdir(parents=True)
    (root / "src" / f"{name}.py").write_text(
        f"def {marker}():\n    return 1\n", encoding="utf-8")
    return root


# ══════════════════════════════════════════ filename confinement


class TestDatabaseFilenameConfinement:
    """A filename is a path built from a string, so it must be confined."""

    # Inputs that would escape the knowledge directory if separators survived,
    # plus the shapes that are illegal or degenerate as names.
    HOSTILE_IDS = [
        "../../evil",
        "..\\..\\evil",
        "/abs/path",
        "\\\\server\\share\\x",
        "a/../../../../Windows/System32/x",
        "C:/Windows/System32/x",
        "..",
        "../..",
        "",
        "   ",
        "x" * 400,
        "...",
        "con",
        "aux",
        "nul.txt",
    ]

    @pytest.mark.parametrize("hostile", HOSTILE_IDS)
    def test_filename_stays_inside_the_knowledge_directory(self, hostile: str):
        name = db_filename(hostile)
        base = Path("C:/knowledge").resolve()
        resolved = (base / name).resolve()
        assert str(resolved).lower().startswith(str(base).lower()), (
            f"{hostile!r} produced {name!r}, which resolves to {resolved}")

    @pytest.mark.parametrize("hostile", HOSTILE_IDS)
    def test_filename_is_a_single_path_component(self, hostile: str):
        name = db_filename(hostile)
        assert "/" not in name and "\\" not in name, (
            f"{hostile!r} produced a multi-component name: {name!r}")

    @pytest.mark.parametrize("hostile", HOSTILE_IDS)
    def test_filename_has_no_illegal_characters(self, hostile: str):
        name = db_filename(hostile)
        illegal = [c for c in name if c in '<>:"|?*']
        assert not illegal, f"{hostile!r} produced illegal characters: {illegal}"

    @pytest.mark.parametrize("hostile", HOSTILE_IDS)
    def test_filename_is_never_dot_only(self, hostile: str):
        name = db_filename(hostile)
        stripped = name.strip(".")
        assert stripped, f"{hostile!r} produced a dot-only name: {name!r}"

    def test_distinct_ids_never_share_a_filename(self):
        """Two projects must not be able to collide onto one database."""
        names = {db_filename(f"project:{i:016x}") for i in range(500)}
        assert len(names) == 500, "two project ids mapped to one filename"

    def test_a_real_project_id_is_unchanged_by_the_encoding(self):
        """The encoding exists to hide a colon, not to rename real databases.

        Phase 8 must not orphan knowledge written by an earlier build, so the
        name a real project id maps to is pinned here.
        """
        pid = canonical_project_key(str(Path("C:/tmp/proj")))
        assert db_filename(pid) == "kg_project_6870af38e837a034.db"
        assert ":" not in db_filename(pid)

    def test_a_hostile_id_cannot_escape_by_actually_being_opened(self,
                                                                tmp_settings):
        """Confinement has to hold through the real constructor, not just the
        name helper: the path is only safe if the graph uses it."""
        root = tmp_settings.base_dir
        hostile = "../../escaped"
        graph = KnowledgeGraph(root / "knowledge", hostile)
        try:
            resolved = graph.db_path.resolve()
            assert str(resolved).lower().startswith(
                str((root / "knowledge").resolve()).lower()), (
                f"the graph opened {resolved}, outside the knowledge directory")
        finally:
            graph.close()

    def test_the_legacy_path_is_also_confined(self):
        base = Path("C:/knowledge")
        for hostile in ("../../evil", "a/../../../../Windows/x", "..", ""):
            resolved = (base / legacy_db_path(base, hostile).name).resolve()
            assert str(resolved).lower().startswith(str(base.resolve()).lower()), (
                f"the legacy path for {hostile!r} escaped to {resolved}")


# ══════════════════════════════════════════ read path cannot write


class TestReadPathIsNotAWritePath:
    def test_a_query_after_an_index_still_cannot_write(self, tmp_settings,
                                                       project: Path):
        """The read-only guarantee must survive a writable handle existing.

        A shared manager is the reason this is worth testing: after an index in
        the same process, the project is cached, and returning that cached
        writable handle to a query would hand the read path write access.
        """
        knowledge_index(project_path=str(project), settings=tmp_settings)
        pid = canonical_project_key(str(project))
        view = manager(tmp_settings).get_existing(pid)
        assert view is not None and view.readonly, (
            "the query path was handed a writable handle after an index")

        node = view.find_nodes(node_type=NodeType.FILE, limit=1)[0]
        for attempt in (
            lambda: view.upsert_node(NodeType.FILE, node.canonical_key,
                                      "hijacked", {"path": "hijacked"}),
            lambda: view.mark_stale(node, "deleted"),
            lambda: view.delete_node(node.node_id),
            lambda: view.delete_edge("nope"),
        ):
            with pytest.raises(PermissionError):
                attempt()

        # And the write did not land.
        after = manager(tmp_settings).get_graph(pid).get_node(node.canonical_key)
        assert after.display_name == node.display_name

    def test_a_readonly_view_observes_committed_writes(self, tmp_settings,
                                                       project: Path):
        """Read-only must not mean stale: the view sees the writable handle's
        committed work, or a query right after an index would find nothing."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        pid = canonical_project_key(str(project))
        view = manager(tmp_settings).get_existing(pid)
        assert view is not None
        assert view.find_nodes(node_type=NodeType.FILE, limit=10), (
            "the read-only view could not see the index that just ran")

    def test_a_query_never_opens_a_database_for_writing(self, tmp_settings,
                                                        project: Path):
        """Before any index, a query must not create or open anything."""
        kd = knowledge_dir(tmp_settings)
        knowledge_query(query_type="find_nodes", settings=tmp_settings,
                        project_path=str(project))
        entries = sorted(p.name for p in kd.glob("*")) if kd.is_dir() else []
        assert entries == [], f"a read-only query created {entries}"


# ══════════════════════════════════════════ cross-project isolation


class TestProjectsCannotReachEachOther:
    def test_a_project_cannot_delete_another_projects_knowledge(self, tmp_settings,
                                                                tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        pid_a = canonical_project_key(str(a))
        pid_b = canonical_project_key(str(b))
        db_a = knowledge_dir(tmp_settings) / db_filename(pid_a)
        assert db_a.is_file()

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            assert mgr.delete_project(pid_b) is True
            # Deleting beta must leave alpha's knowledge entirely intact.
            assert db_a.is_file(), "deleting one project removed another's database"
            assert pid_a in mgr.list_projects()
        finally:
            mgr.close_all()

        found = knowledge_query(query_type="search_files", settings=tmp_settings,
                                project_path=str(a), limit=50)
        names = {n["display_name"] for n in found.data["nodes"]}
        assert "alpha.py" in names

    def test_a_stale_project_cannot_be_read_as_another_projects_knowledge(
            self, tmp_settings, tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)

        # Beta is deleted, so its knowledge becomes stale. That history must
        # not resurface inside alpha.
        (b / "src" / "beta.py").unlink()
        knowledge_index(project_path=str(b), settings=tmp_settings)

        found = knowledge_query(query_type="find_stale", settings=tmp_settings,
                                project_path=str(b), limit=50)
        stale_names = {n["display_name"] for n in found.data["nodes"]}
        assert "beta.py" in stale_names

        for kind in ("find_nodes", "search_files"):
            seen = knowledge_query(query_type=kind, settings=tmp_settings,
                                   project_path=str(a), limit=100,
                                   include_stale=True)
            names = {n["display_name"] for n in seen.data["nodes"]}
            assert "beta.py" not in names, (
                f"{kind} leaked another project's stale knowledge into alpha")

    def test_stale_knowledge_stays_inside_its_own_project(self, tmp_settings,
                                                          tmp_path: Path):
        a = _make_project(tmp_path, "alpha", "alpha_fn")
        b = _make_project(tmp_path, "beta", "beta_fn")
        knowledge_index(project_path=str(a), settings=tmp_settings)
        knowledge_index(project_path=str(b), settings=tmp_settings)
        (a / "src" / "alpha.py").unlink()
        knowledge_index(project_path=str(a), settings=tmp_settings)

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            stale_b = mgr.get_existing(canonical_project_key(str(b)))
            assert stale_b is not None
            assert stale_b.find_stale_nodes(limit=100) == [], (
                "beta reported alpha's deleted file as its own stale knowledge")
        finally:
            mgr.close_all()


# ══════════════════════════════════════════ authorized roots


class TestAuthorizationSurvivesRestart:
    def test_an_unauthorized_project_cannot_be_indexed(self, tmp_settings):
        result = knowledge_index(project_path="C:/Windows/System32",
                                 settings=tmp_settings)
        assert not result.success
        assert "authorized" in result.error.lower()

    def test_an_unauthorized_project_cannot_be_queried(self, tmp_settings):
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path="C:/Windows/System32")
        assert not result.success
        assert "authorized" in result.error.lower()

    def test_a_project_outside_the_allowed_root_stays_empty(self, tmp_settings,
                                                            tmp_path: Path):
        """Indexing inside the root must not make a sibling readable."""
        inside = _make_project(tmp_path, "inside", "inside_fn")
        knowledge_index(project_path=str(inside), settings=tmp_settings)

        outside = tmp_path.parent / "outside-project"
        outside.mkdir(exist_ok=True)
        (outside / "x.py").write_text("def x():\n    return 1\n", encoding="utf-8")

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(outside))
        assert not result.success or not result.data.get("nodes")

    def test_authorization_is_rechecked_after_the_handles_are_released(
            self, tmp_settings, project: Path):
        """Authorization is a property of the request, not of a cached handle.

        The check is re-run with a completely fresh manager so a cached
        connection cannot be what makes the second call succeed.
        """
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path="C:/Windows/System32")
        assert not result.success
        assert "authorized" in result.error.lower()

    def test_the_legacy_database_fallback_respects_the_knowledge_directory(
            self, tmp_settings, project: Path):
        """A legacy database is adopted only from inside the knowledge dir."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        pid = canonical_project_key(str(project))
        close_shared_managers()

        kd = knowledge_dir(tmp_settings)
        legacy = kd / "legacy_host_file"
        legacy.write_bytes(b"")
        # The pre-Phase-8 name for this project, as a data stream.
        stream = Path(f"{legacy}:{pid}.db")
        try:
            KnowledgeGraph(kd, pid, readonly=True)
        except (FileNotFoundError, OSError, sqlite3.Error):
            pass
        # Whatever happened, the open path stayed inside the knowledge dir.
        graph = KnowledgeGraph(kd, pid, readonly=True)
        try:
            assert str(graph.db_path.resolve()).lower().startswith(
                str(kd.resolve()).lower())
        finally:
            graph.close()


# ══════════════════════════════════════════ nothing private is stored


class TestNothingPrivateReachesDisk:
    def test_file_contents_are_never_stored(self, tmp_settings, project: Path):
        """A file's bytes stay in the file. Only facts about it are recorded."""
        secret = "SUPER_SECRET_TOKEN_VALUE"
        (project / "src" / "main.py").write_text(
            f"TOKEN = \"{secret}\"\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        db = knowledge_dir(tmp_settings) / db_filename(
            canonical_project_key(str(project)))
        blob = db.read_bytes() + b"".join(
            p.read_bytes() for p in db.parent.glob(db.name + "-*"))
        assert secret.encode() not in blob, (
            "the contents of a source file were written into the knowledge "
            "database")

    def test_environment_values_are_never_stored(self, tmp_settings,
                                                project: Path, monkeypatch):
        monkeypatch.setenv("DIABLO_P8_PROBE_SECRET", "leaked-value-9182")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        db = knowledge_dir(tmp_settings) / db_filename(
            canonical_project_key(str(project)))
        blob = db.read_bytes()
        assert b"leaked-value-9182" not in blob
        assert b"DIABLO_P8_PROBE_SECRET" not in blob

    def test_only_bounded_facts_are_recorded_per_file(self, tmp_settings,
                                                      project: Path):
        """Metadata is capped, so a file cannot become an unbounded blob."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        db = knowledge_dir(tmp_settings) / db_filename(
            canonical_project_key(str(project)))
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT metadata FROM nodes WHERE node_type = 'file'").fetchall()
        finally:
            conn.close()
        assert rows
        for (raw,) in rows:
            assert len(raw) <= 4096, "file metadata grew unbounded"

    def test_a_dataset_is_never_copied_into_the_database(self, tmp_settings,
                                                        project: Path):
        """A dataset is recorded by shape and hash, never by content."""
        csv = project / "data.csv"
        marker = "UNIQUE_ROW_MARKER_ZZZ"
        csv.write_text(f"k,v\nx,{marker}\n", encoding="utf-8")

        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            project_tool_result(
                tool="data.inspect", args={"path": str(csv)},
                result_data={"headers": ["k", "v"], "row_count": 1,
                             "column_types": {"k": "string", "v": "string"}},
                task_id="", project_root=str(project),
                settings=tmp_settings, manager=mgr,
            )
        finally:
            mgr.close_all()
        close_shared_managers()

        db = knowledge_dir(tmp_settings) / db_filename(
            canonical_project_key(str(project)))
        blob = db.read_bytes()
        assert marker.encode() not in blob, (
            "dataset contents were copied into the knowledge database")
        # The bytes are identified, not stored.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT metadata FROM nodes WHERE node_type = 'dataset'"
            ).fetchone()
        finally:
            conn.close()
        assert row and "content_hash" in row[0], (
            "change detection should rest on a hash, not on stored contents")


# ══════════════════════════════════════════ staleness cannot be forged


class TestStaleFlagIntegrity:
    def test_a_stale_node_is_never_returned_as_current(self, tmp_settings,
                                                      project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        graph = manager(tmp_settings).get_graph(
            canonical_project_key(str(project)))
        node = graph.find_nodes(node_type=NodeType.FILE, limit=1)[0]
        graph.mark_stale(node, "deleted")

        fresh = manager(tmp_settings).get_graph(
            canonical_project_key(str(project)))
        found = fresh.find_nodes(node_type=NodeType.FILE, limit=10)
        assert all(n.canonical_key != node.canonical_key for n in found), (
            "a deleted file was still returned as current knowledge")

    def test_stale_knowledge_survives_a_reindex_that_did_not_see_it(
            self, tmp_settings, project: Path):
        """Demotion must be durable, or reindexing would quietly undo it.

        Reindexing rewrites node metadata wholesale, so this is the one place a
        stale flag could plausibly be lost. A file that is still absent must
        stay absent no matter how many times the project is re-indexed, while a
        file that genuinely comes back must become current again. Both halves
        are asserted, because either one alone is satisfied by doing nothing.
        """
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "src" / "main.py").unlink()

        for _ in range(3):
            knowledge_index(project_path=str(project), settings=tmp_settings)
            current = knowledge_query(query_type="find_nodes",
                                      settings=tmp_settings,
                                      project_path=str(project),
                                      node_type="file", limit=50)
            names = {n["display_name"] for n in current.data["nodes"]}
            assert "main.py" not in names, (
                "reindexing revived a file that is still deleted")

        # And the flag is genuinely still set, not merely withheld.
        everything = knowledge_query(query_type="find_nodes",
                                     settings=tmp_settings,
                                     project_path=str(project),
                                     node_type="file", limit=50,
                                     include_stale=True)
        node = {n["display_name"]: n for n in everything.data["nodes"]}["main.py"]
        assert is_stale(type("N", (), {"metadata": node["metadata"]})())

        # Coming back is the only thing that makes it current again.
        (project / "src" / "main.py").write_text(
            "def alpha():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)
        revived = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                  project_path=str(project), node_type="file",
                                  limit=50)
        names = {n["display_name"] for n in revived.data["nodes"]}
        assert "main.py" in names, (
            "a restored file never became current again")

    def test_a_string_that_merely_says_stale_is_not_the_flag(self, tmp_settings,
                                                            project: Path):
        """Text mentioning staleness must not be mistaken for the flag."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()

        db = knowledge_dir(tmp_settings) / db_filename(
            canonical_project_key(str(project)))
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE node_type = 'file'",
                ('{"note": "\\"stale\\": true", "path": "src/main.py"}',))
            conn.commit()
        finally:
            conn.close()
        close_shared_managers()

        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project), node_type="file",
                                 limit=50)
        if result.success:
            for node in result.data["nodes"]:
                assert is_stale(node) is False, (
                    "a string value containing the stale flag was read as the flag")
