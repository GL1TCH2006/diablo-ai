"""M2.14 Phase 9 — project identity is the project, not a file's parent.

Found by running a real local model through the real Agent (Phase 9C), and it
could not have been found by a test that supplies the project root itself.

``Agent._project_root_from_args`` answers "which directory is this operation
about". For an argument that names a file it answers with the file's parent
directory. That is the correct answer for change tracking and for Git, and the
wrong answer for project *identity*, because a dataset at
``<project>/data/sales.csv`` describes ``<project>`` and not ``<project>/data``.

The consequence was that one directory tree became two projects. The chart, the
artifact and the task were recorded under the real project; the dataset was
recorded under a second project keyed on its own directory. Querying the real
project could not find the dataset, the chart's ``generated_from`` edge to its
source dataset could not exist at all, and cross-project isolation quietly
stopped meaning anything because a single tree was two projects.

The existing tests missed it because they call ``Executor.run`` and then
``project_tool_result`` with an explicit project root, which never exercises
the Agent's own root derivation. These tests drive the real Agent instead, with
the root derived the way it is in production.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai.provider import AIProvider, AIResponse, ToolCall
from config.permissions import PermissionManager
from core.agent import Agent
from core.context import ProjectContext
from core.events import EventBus
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
    db_filename,
)
from core.knowledge_projection import knowledge_dir
from core.knowledge_tools import knowledge_index, knowledge_query
from memory.preferences import PreferencesStore
from tools import build_registry


class ScriptedProvider(AIProvider):
    """Stands in for the model only. Everything downstream is production code."""

    name = "scripted"
    model_label = "scripted/local"
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def is_available(self):
        return True

    def list_models(self):
        return []

    def chat(self, messages, tools=None, model=None):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return AIResponse(text="done", tool_calls=[])


def _response(*calls, text=""):
    return AIResponse(text=text, tool_calls=list(calls))


def _call(name, **args):
    return ToolCall(name=name, arguments=args)


@pytest.fixture(autouse=True)
def _release_handles():
    yield
    close_shared_managers()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "data" / "sales.csv").write_text(
        "product,amount\nwidget,10\ngadget,20\nwidget,30\n", encoding="utf-8")
    return root


def _agent(settings, project: Path, responses):
    prefs = PreferencesStore(settings.memory_dir)
    return Agent(
        provider=ScriptedProvider(responses),
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, prefs),
        events=EventBus(),
        confirmer=lambda tool, description: "allow",
        project=ProjectContext(name=project.name, path=str(project)),
        memory_preferences=prefs,
        model_name="",
    )


def _datasets(project: Path, settings) -> dict:
    result = knowledge_query(query_type="search_datasets", settings=settings,
                             project_path=str(project), limit=50)
    assert result.success, result.error
    return {n["display_name"]: n for n in result.data["nodes"]}


def _database_files(settings) -> list[str]:
    kd = knowledge_dir(settings)
    return sorted(p.name for p in kd.glob("*.db")) if kd.is_dir() else []


class TestOneTreeIsOneProject:
    def test_a_dataset_does_not_mint_a_second_project(self, tmp_settings,
                                                      project: Path):
        """A data file inside the project belongs to the project."""
        csv = project / "data" / "sales.csv"
        knowledge_index(project_path=str(project), settings=tmp_settings)

        agent = _agent(tmp_settings, project, [
            _response(_call("data.inspect", path=str(csv))),
            _response(text="inspected"),
        ])
        agent.respond(f"inspect {csv}")

        # One directory tree, one knowledge database.
        assert _database_files(tmp_settings) == [
            db_filename(canonical_project_key(str(project)))], (
            f"one tree produced several projects: {_database_files(tmp_settings)}")

        # And the dataset is reachable by asking about the project.
        datasets = _datasets(project, tmp_settings)
        assert "sales.csv" in datasets, (
            f"the dataset was recorded somewhere other than the project: "
            f"{sorted(datasets)}")

    def test_the_chart_can_point_at_the_dataset_it_came_from(self, tmp_settings,
                                                            project: Path):
        """The edge that was impossible while the two lived in different projects."""
        csv = project / "data" / "sales.csv"
        png = project / "data" / "amounts.png"
        knowledge_index(project_path=str(project), settings=tmp_settings)

        agent = _agent(tmp_settings, project, [
            _response(_call("data.inspect", path=str(csv))),
            _response(_call("chart.generate", chart_type="bar", data_path=str(csv),
                            x_column="product", y_column="amount",
                            output_path=str(png))),
            _response(text="done"),
        ])
        agent.respond(f"chart {csv}")

        close_shared_managers()
        graph = KnowledgeGraphManager(
            knowledge_dir(tmp_settings)).get_graph(
                canonical_project_key(str(project)))
        charts = graph.find_nodes(node_type=NodeType.CHART, limit=10)
        datasets = graph.find_nodes(node_type=NodeType.DATASET, limit=10)
        assert charts, "no chart was recorded"
        assert datasets, "no dataset was recorded"

        sources = graph.get_edges(source_id=charts[0].node_id)
        generated = [e for e in sources
                     if e.relation_type.value == "generated_from"]
        assert generated, (
            "the chart has no generated_from edge, so it cannot be traced to "
            "the dataset it was built from")
        assert generated[0].target_id == datasets[0].node_id

    def test_a_file_in_a_nested_directory_joins_the_project(self, tmp_settings,
                                                            tmp_path: Path):
        """Depth is not identity. Three levels down is still the same project."""
        deep = tmp_path / "proj"
        nested = deep / "a" / "b" / "c"
        nested.mkdir(parents=True)
        csv = nested / "deep.csv"
        csv.write_text("k,v\nx,1\n", encoding="utf-8")
        knowledge_index(project_path=str(deep), settings=tmp_settings)

        agent = _agent(tmp_settings, deep, [
            _response(_call("data.inspect", path=str(csv))),
            _response(text="inspected"),
        ])
        agent.respond(f"inspect {csv}")

        assert _database_files(tmp_settings) == [
            db_filename(canonical_project_key(str(deep)))]
        assert "deep.csv" in _datasets(deep, tmp_settings)

    def test_the_recorded_path_is_relative_to_the_project(self, tmp_settings,
                                                         project: Path):
        """A dataset is stored as a project-relative path, not an absolute one.

        An absolute path would be a second copy of the project's location and
        would not survive the project moving.
        """
        csv = project / "data" / "sales.csv"
        knowledge_index(project_path=str(project), settings=tmp_settings)
        agent = _agent(tmp_settings, project, [
            _response(_call("data.inspect", path=str(csv))),
            _response(text="inspected"),
        ])
        agent.respond(f"inspect {csv}")

        node = _datasets(project, tmp_settings)["sales.csv"]
        recorded = str(node["metadata"].get("path", "")).replace("\\", "/")
        assert recorded == "data/sales.csv", (
            f"dataset recorded as {recorded!r}, expected a project-relative path")


class TestIdentityIsNotOverreaching:
    def test_an_observation_outside_the_project_is_not_folded_in(self, tmp_settings,
                                                                 tmp_path: Path):
        """The fix must not sweep unrelated data into the current project.

        Preferring the active project is only correct while the observation is
        actually inside it. Something outside keeps its own identity, or nothing
        is recorded, rather than being misattributed.
        """
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        other_csv = outside / "other.csv"
        other_csv.write_text("k,v\nx,1\n", encoding="utf-8")

        knowledge_index(project_path=str(project), settings=tmp_settings)
        agent = _agent(tmp_settings, project, [
            _response(_call("data.inspect", path=str(other_csv))),
            _response(text="inspected"),
        ])
        agent.respond(f"inspect {other_csv}")

        # Either it was not recorded against this project, or it was refused.
        # What must never happen is another project's file appearing here.
        datasets = _datasets(project, tmp_settings)
        assert "other.csv" not in datasets, (
            "a dataset from outside the project was attributed to it")

    def test_scoping_for_change_tracking_is_unchanged(self, tmp_settings,
                                                      project: Path):
        """The shared helper still answers the scoping question it always did.

        The correction is confined to knowledge projection. Change tracking and
        Git still see the directory an operation is about, because that is what
        the ``_within_project`` guard is written against.
        """
        agent = _agent(tmp_settings, project, [])
        nested = project / "data" / "sales.csv"
        assert agent._project_root_from_args({"path": str(nested)}, "data.inspect") \
            == str((project / "data").resolve())
        # And the knowledge identity is the project.
        assert agent._knowledge_project_root({"path": str(nested)}, "data.inspect") \
            == str(project.resolve())

    def test_without_a_project_the_derived_root_is_still_used(self, tmp_settings,
                                                              tmp_path: Path):
        """No project context means the old behaviour, unchanged."""
        prefs = PreferencesStore(tmp_settings.memory_dir)
        agent = Agent(
            provider=ScriptedProvider([]), settings=tmp_settings,
            registry=build_registry(tmp_settings),
            permissions=PermissionManager(tmp_settings, prefs),
            events=EventBus(), confirmer=lambda t, d: "allow",
            project=None, memory_preferences=prefs, model_name="",
        )
        csv = tmp_path / "loose" / "data.csv"
        (tmp_path / "loose").mkdir(exist_ok=True)
        csv.write_text("k,v\nx,1\n", encoding="utf-8")
        assert agent._knowledge_project_root({"path": str(csv)}, "data.inspect") \
            == str(csv.parent.resolve())


class TestNoSecondAuthorityFromIdentity:
    def test_identity_cannot_be_supplied_by_the_model(self, tmp_settings,
                                                      project: Path):
        """A model-named identity is a path, and it is resolved like any other.

        The model does not get to say which project it is writing into; it
        names a path, and the path is resolved and authorized as one.
        """
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path="C:/Windows/System32",
                                 node_type="file", limit=10)
        assert not result.success
        assert "authorized" in result.error.lower()

    def test_a_traversal_in_a_named_project_creates_nothing_outside(self,
                                                                   tmp_settings,
                                                                   tmp_path: Path,
                                                                   project: Path):
        """Naming a traversing project path must not create a database outside."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        before = {p.resolve() for p in tmp_path.rglob("*.db")}
        result = knowledge_index(project_path=f"{project}/../../escaped",
                                 settings=tmp_settings)
        after = {p.resolve() for p in tmp_path.rglob("*.db")}
        created = after - before
        if created:
            kd = knowledge_dir(tmp_settings).resolve()
            for path in created:
                assert kd in path.parents, (
                    f"a traversing project path created {path} outside "
                    f"{kd}")
        else:
            # Refusing outright is the other acceptable answer.
            assert not result.success

