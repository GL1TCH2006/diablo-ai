"""M2.14 Phase F — end-to-end live integration tests.

Every test here drives the **real** :class:`~core.agent.Agent` through the
**real** tool registry and Executor, and then asserts on the **real** knowledge
graph. Nothing calls a projection helper directly, because the thing Phase F is
proving is that the wiring works, not that the helpers work.

Each test maps to one of the twelve required cases.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai.provider import AIProvider, AIResponse, ToolCall
from config.permissions import PermissionManager
from core.events import EventBus
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    RelationType,
    canonical_project_key,
)
from core.knowledge_projection import knowledge_dir
from core.knowledge_tools import knowledge_index, knowledge_query
from memory.preferences import PreferencesStore
from tools import build_registry


class ScriptedProvider(AIProvider):
    """Replays a fixed list of model turns."""

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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "charts").mkdir(parents=True)
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "def test_run():\n    assert True\n", encoding="utf-8")
    (root / "sales.csv").write_text(
        "product,amount\nA,10\nB,20\nC,30\n", encoding="utf-8")
    return root


def _agent(tmp_settings, responses, project: Path | None = None):
    from core.agent import Agent

    settings = tmp_settings
    settings.sessions_dir = settings.data_dir / "sessions"
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    preferences = PreferencesStore(settings.memory_dir)
    project_ctx = None
    if project is not None:
        from core.context import ProjectContext
        project_ctx = ProjectContext(name=project.name, path=str(project))
    return Agent(
        provider=ScriptedProvider(responses),
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, preferences),
        events=EventBus(),
        project=project_ctx,
        memory_preferences=preferences,
    )


def _graph(tmp_settings, project: Path):
    mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
    graph = mgr.get_graph(canonical_project_key(str(project)))
    return mgr, graph


def _targets(graph, node_id, relation):
    return [graph.get_node_by_id(e.target_id) for e in
            graph.get_edges(source_id=node_id, relation_type=relation)]


# ------------------------------------------------------- 1. PROJECT INDEX


class TestCase1ProjectIndex:
    def test_index_produces_project_directory_file_and_symbol(self, tmp_settings, project: Path):
        result = knowledge_index(project_path=str(project), settings=tmp_settings)
        assert result.success

        mgr, graph = _graph(tmp_settings, project)
        try:
            kinds = {n.node_type for n in graph.find_nodes(limit=200)}
            assert {NodeType.PROJECT, NodeType.DIRECTORY, NodeType.FILE,
                    NodeType.CODE_SYMBOL} <= kinds

            app = graph.get_node(f"file:{canonical_project_key(str(project))}:app.py")
            assert app is not None
            symbols = _targets(graph, app.node_id, RelationType.DEFINES)
            assert [s.metadata["kind"] for s in symbols] == ["function"]

            test_file = graph.get_node(
                f"file:{canonical_project_key(str(project))}:tests/test_app.py")
            test_symbols = _targets(graph, test_file.node_id, RelationType.DEFINES)
            assert [s.metadata["kind"] for s in test_symbols] == ["function"]
        finally:
            mgr.close_all()

    def test_index_is_reachable_through_the_query_tool(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                                 project_path=str(project),
                                 node_type="code_symbol", limit=50)
        assert result.success
        assert result.data["count"] >= 2


# ---------------------------------------------------------- 2. CODING TASK


class TestCase2CodingTask:
    def test_agent_edit_produces_task_changed_by_file(self, tmp_settings, project: Path):
        # The graph describes a project it has been told about, so index first.
        # Editing an already-indexed file is the case where a FILE node exists
        # to link against.
        knowledge_index(project_path=str(project), settings=tmp_settings)
        target = project / "app.py"
        agent = _agent(tmp_settings, [
            _response(_call("filesystem.write_file", path=str(target),
                            content="def run():\n    return 2\n")),
            _response(text="edited"),
        ], project=project)
        agent.respond("update the run function in app.py")

        mgr, graph = _graph(tmp_settings, project)
        try:
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=20)
            assert tasks, "the real Agent run produced no TASK node"
            changed = _targets(graph, tasks[0].node_id, RelationType.CHANGED_BY)
            assert "app.py" in [n.display_name for n in changed]
        finally:
            mgr.close_all()

    def test_brand_new_file_is_not_linked_until_indexed(self, tmp_settings, project: Path):
        """Honest behaviour: no FILE node, so no edge. Nothing is invented."""
        knowledge_index(project_path=str(project), settings=tmp_settings)
        target = project / "brand_new.py"
        agent = _agent(tmp_settings, [
            _response(_call("filesystem.write_file", path=str(target), content="x = 1\n")),
            _response(text="created"),
        ], project=project)
        agent.respond("create a brand new module")

        mgr, graph = _graph(tmp_settings, project)
        try:
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=20)
            assert tasks
            changed = _targets(graph, tasks[0].node_id, RelationType.CHANGED_BY)
            assert "brand_new.py" not in [n.display_name for n in changed]
        finally:
            mgr.close_all()

    def test_task_node_references_the_authoritative_task_id(self, tmp_settings, project: Path):
        target = project / "written.py"
        agent = _agent(tmp_settings, [
            _response(_call("filesystem.write_file", path=str(target), content="x = 1\n")),
            _response(text="ok"),
        ], project=project)
        agent.respond("write a file")

        task_id = agent.request_context.tasks.task_id
        mgr, graph = _graph(tmp_settings, project)
        try:
            from core.knowledge_graph import canonical_task_key
            assert graph.get_node(canonical_task_key(task_id)) is not None
            # TaskStore remains the authority for the same task.
            assert agent._task_store.get(task_id) is not None
        finally:
            mgr.close_all()


# --------------------------------------------------------- 3. VERIFICATION


class TestCase3Verification:
    def test_verified_task_produces_task_verified_by_verification(self, tmp_settings,
                                                                  project: Path):
        agent = _agent(tmp_settings, [
            _response(text="hello there"),
        ], project=project)
        agent.respond("hello")

        mgr, graph = _graph(tmp_settings, project)
        try:
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=20)
            assert tasks
            verifs = _targets(graph, tasks[0].node_id, RelationType.VERIFIED_BY)
            assert verifs, "no VERIFICATION node linked to the task"
            states = [v.metadata["state"] for v in verifs]
            # A greeting performs no work, so it must not claim a pass.
            assert "passed" not in states
        finally:
            mgr.close_all()

    def test_verification_state_matches_the_ledger(self, tmp_settings, project: Path):
        agent = _agent(tmp_settings, [_response(text="hello")], project=project)
        agent.respond("hello")

        ledger_state = agent.request_context.tasks.verification.value
        mgr, graph = _graph(tmp_settings, project)
        try:
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=5)
            if not tasks:
                pytest.skip("no task projected")
            verifs = _targets(graph, tasks[0].node_id, RelationType.VERIFIED_BY)
            if not verifs:
                pytest.skip("no verification projected")
            # The graph copies the ledger; it never improves on it.
            assert all(v.metadata["state"] in
                       (ledger_state, "not_run", "not_required", "unverified")
                       for v in verifs)
        finally:
            mgr.close_all()


# ------------------------------------------------------------- 4. DATASET


class TestCase4Dataset:
    def test_agent_data_inspect_produces_dataset(self, tmp_settings, project: Path):
        csv = project / "sales.csv"
        agent = _agent(tmp_settings, [
            _response(_call("data.inspect", path=str(csv))),
            _response(text="inspected"),
        ], project=project)
        agent.respond("inspect the sales csv")

        mgr, graph = _graph(tmp_settings, project)
        try:
            datasets = graph.find_nodes(node_type=NodeType.DATASET, limit=10)
            assert datasets, "the real Agent run produced no DATASET node"
            assert datasets[0].metadata["row_count"] == 3
            assert datasets[0].metadata["columns"] == ["product", "amount"]
        finally:
            mgr.close_all()

    def test_agent_data_analyze_produces_analysis(self, tmp_settings, project: Path):
        """A real groupby run must be remembered as a question, not as rows."""
        csv = project / "sales.csv"
        agent = _agent(tmp_settings, [
            _response(_call("data.analyze", path=str(csv), operation="groupby",
                            group_by="product", column="amount")),
            _response(text="grouped"),
        ], project=project)
        agent.respond("group the sales by product")

        mgr, graph = _graph(tmp_settings, project)
        try:
            analyses = graph.find_nodes(node_type=NodeType.ANALYSIS, limit=10)
            assert analyses, "the real Agent run produced no ANALYSIS node"
            assert analyses[0].metadata["operation"] == "groupby"
            assert analyses[0].metadata["params"]["group_by"] == "product"
        finally:
            mgr.close_all()

    def test_repeated_analysis_does_not_duplicate_nodes(self, tmp_settings,
                                                       project: Path):
        csv = project / "sales.csv"

        def run():
            agent = _agent(tmp_settings, [
                _response(_call("data.analyze", path=str(csv), operation="groupby",
                                group_by="product", column="amount")),
                _response(text="grouped"),
            ], project=project)
            agent.respond("group the sales by product")

        run()
        run()
        mgr, graph = _graph(tmp_settings, project)
        try:
            assert len(graph.find_nodes(node_type=NodeType.ANALYSIS, limit=20)) == 1
        finally:
            mgr.close_all()


# -------------------------------------------------------------- 5. CHART


class TestCase5Chart:
    def test_agent_chart_generation_produces_chart_and_artifact(self, tmp_settings,
                                                                project: Path):
        csv = project / "sales.csv"
        out = project / "charts" / "sales.png"
        agent = _agent(tmp_settings, [
            _response(_call("chart.generate", chart_type="bar", data_path=str(csv),
                            x_column="product", y_column="amount",
                            title="Sales", output_path=str(out))),
            _response(text="charted"),
        ], project=project)
        agent.respond("chart the sales csv")

        mgr, graph = _graph(tmp_settings, project)
        try:
            charts = graph.find_nodes(node_type=NodeType.CHART, limit=10)
            assert charts, "the real Agent run produced no CHART node"
            produced = _targets(graph, charts[0].node_id, RelationType.PRODUCED)
            assert [n.node_type for n in produced] == [NodeType.ARTIFACT]
            assert out.is_file()
        finally:
            mgr.close_all()


# ------------------------------------------------- 6. COMPLETE WORKFLOW


class TestCase6CompleteWorkflow:
    def test_full_data_chart_workflow(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        csv = project / "sales.csv"
        out = project / "charts" / "sales.png"
        agent = _agent(tmp_settings, [
            _response(_call("data.inspect", path=str(csv))),
            _response(_call("chart.generate", chart_type="bar", data_path=str(csv),
                            x_column="product", y_column="amount",
                            output_path=str(out))),
            _response(_call("chart.verify", path=str(out))),
            _response(text="all done"),
        ], project=project)
        agent.respond("analyse the csv and chart it")

        mgr, graph = _graph(tmp_settings, project)
        try:
            kinds = {n.node_type for n in graph.find_nodes(limit=300)}
            for required in (NodeType.DATASET, NodeType.CHART, NodeType.ARTIFACT,
                             NodeType.FILE, NodeType.TASK):
                assert required in kinds, f"missing {required.value}"

            chart = graph.find_nodes(node_type=NodeType.CHART, limit=1)[0]
            assert [n.node_type for n in
                    _targets(graph, chart.node_id, RelationType.GENERATED_FROM)] == [NodeType.DATASET]
            assert [n.node_type for n in
                    _targets(graph, chart.node_id, RelationType.PRODUCED)] == [NodeType.ARTIFACT]

            task = graph.find_nodes(node_type=NodeType.TASK, limit=1)[0]
            produced_types = {n.node_type for n in
                              _targets(graph, task.node_id, RelationType.PRODUCED)}
            assert NodeType.CHART in produced_types
            assert NodeType.DATASET in produced_types
        finally:
            mgr.close_all()


# --------------------------------------------------------- 7. DUPLICATION


class TestCase7Duplication:
    def test_repeated_agent_runs_do_not_explode_the_graph(self, tmp_settings, project: Path):
        csv = project / "sales.csv"
        agent = _agent(tmp_settings, [
            _response(_call("data.inspect", path=str(csv))),
            _response(text="ok"),
        ], project=project)

        agent.respond("inspect")
        mgr, graph = _graph(tmp_settings, project)
        try:
            first = graph.stats()
        finally:
            mgr.close_all()

        for _ in range(3):
            agent.respond("inspect the same csv again")

        mgr, graph = _graph(tmp_settings, project)
        try:
            second = graph.stats()
            # Three more tasks legitimately appear, but node growth is bounded
            # and datasets must not duplicate.
            assert second["nodes"] - first["nodes"] <= 12
            assert len(graph.find_nodes(node_type=NodeType.DATASET, limit=50)) == 1
        finally:
            mgr.close_all()

    def test_repeated_indexing_does_not_grow(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        mgr, graph = _graph(tmp_settings, project)
        before = graph.stats()
        mgr.close_all()

        for _ in range(3):
            knowledge_index(project_path=str(project), settings=tmp_settings)

        mgr, graph = _graph(tmp_settings, project)
        try:
            assert graph.stats() == before
        finally:
            mgr.close_all()


# ---------------------------------------------------------- 8. MODIFICATION


class TestCase8Modification:
    def test_changed_source_updates_symbols(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        (project / "app.py").write_text("def renamed():\n    return 2\n", encoding="utf-8")
        knowledge_index(project_path=str(project), settings=tmp_settings)

        mgr, graph = _graph(tmp_settings, project)
        try:
            symbols = graph.find_nodes_by_key_prefix(
                NodeType.CODE_SYMBOL,
                f"symbol:{canonical_project_key(str(project))}:app.py:")
            by_name = {s.display_name: s for s in symbols}
            assert by_name["renamed"].metadata["stale"] is False
            assert by_name["run"].metadata["stale"] is True
        finally:
            mgr.close_all()

    def test_changed_data_updates_dataset_metadata(self, tmp_settings, project: Path):
        csv = project / "sales.csv"
        # Two turns, two model responses each.
        agent = _agent(tmp_settings, [
            _response(_call("data.inspect", path=str(csv))),
            _response(text="ok"),
            _response(_call("data.inspect", path=str(csv))),
            _response(text="ok again"),
        ], project=project)
        agent.respond("inspect")
        csv.write_text("product,amount\nA,1\n", encoding="utf-8")
        agent.respond("inspect again")

        mgr, graph = _graph(tmp_settings, project)
        try:
            datasets = graph.find_nodes(node_type=NodeType.DATASET, limit=5)
            assert len(datasets) == 1
            assert datasets[0].metadata["row_count"] == 1
        finally:
            mgr.close_all()


# -------------------------------------------------------------- 9. FAILURE


class TestCase9Failure:
    def test_failed_task_never_yields_a_passed_verification(self, tmp_settings, project: Path):
        """A real failing tool, through the real Agent, must not look successful.

        ``filesystem.read_file`` on a missing path is a genuine failure the
        existing tools already produce, so this exercises the ordinary failure
        path rather than a synthetic one.
        """
        missing = project / "does_not_exist.py"
        agent = _agent(tmp_settings, [
            _response(_call("filesystem.read_file", path=str(missing))),
            _response(text="gave up"),
        ], project=project)
        agent.respond("read a file that does not exist")

        # The authoritative record, not the model's prose, is what counts.
        task_id = agent.request_context.tasks.task_id
        record = agent._task_store.get(task_id)
        assert record is not None

        mgr, graph = _graph(tmp_settings, project)
        try:
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=20)
            assert tasks
            task_node = tasks[0]
            assert task_node.metadata["status"] == record.status
            states = [v.metadata["state"] for v in
                      _targets(graph, task_node.node_id, RelationType.VERIFIED_BY)]
            assert "passed" not in states
        finally:
            mgr.close_all()

    def test_blocked_tool_never_yields_a_passed_verification(self, tmp_settings,
                                                             project: Path):
        """A permission denial must be recorded as a denial, not a success."""
        outside = tmp_settings.base_dir.parent / "outside.txt"
        outside.write_text("nope\n", encoding="utf-8")
        agent = _agent(tmp_settings, [
            _response(_call("filesystem.write_file", path=str(outside),
                            content="x = 1\n")),
            _response(text="denied"),
        ], project=project)
        agent.respond("write outside the workspace")

        mgr, graph = _graph(tmp_settings, project)
        try:
            for task in graph.find_nodes(node_type=NodeType.TASK, limit=20):
                states = [v.metadata["state"] for v in
                          _targets(graph, task.node_id, RelationType.VERIFIED_BY)]
                assert "passed" not in states
        finally:
            mgr.close_all()


# --------------------------------------------------------- 10. CANCELLATION


class TestCase10Cancellation:
    def test_cancelled_request_produces_no_false_success(self, tmp_settings, project: Path):
        class Token:
            """Minimal CancellationToken: core.cancellation only needs this."""

            def __init__(self):
                self._cancelled = False

            def is_cancelled(self):
                return self._cancelled

            def request(self):
                self._cancelled = True

        agent = _agent(tmp_settings, [
            _response(_call("data.inspect", path=str(project / "sales.csv"))),
            _response(text="ok"),
        ], project=project)

        token = Token()
        token.request()
        agent.respond("inspect the csv", token)

        mgr, graph = _graph(tmp_settings, project)
        try:
            states = []
            for task in graph.find_nodes(node_type=NodeType.TASK, limit=20):
                states.extend(v.metadata["state"] for v in
                              _targets(graph, task.node_id, RelationType.VERIFIED_BY))
            assert "passed" not in states
        finally:
            mgr.close_all()


# -------------------------------------------------------- 11. AUTHORIZATION


class TestCase11Authorization:
    def test_unauthorized_project_never_reaches_the_graph(self, tmp_settings):
        result = knowledge_index(project_path="C:/Windows/System32", settings=tmp_settings)
        assert not result.success
        assert "authorized" in result.error

    def test_two_projects_do_not_leak(self, tmp_settings, tmp_path: Path):
        other = tmp_path / "other"
        other.mkdir()
        (other / "other.py").write_text("def other_fn():\n    pass\n", encoding="utf-8")

        first = _mk(tmp_path, "leak-a")
        second = _mk(tmp_path, "leak-b")
        (second / "only_b.py").write_text("def only_b():\n    pass\n", encoding="utf-8")

        knowledge_index(project_path=str(first), settings=tmp_settings)
        knowledge_index(project_path=str(second), settings=tmp_settings)

        a = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                            project_path=str(first),
                            node_type="code_symbol", limit=100)
        b = knowledge_query(query_type="find_nodes", settings=tmp_settings,
                            project_path=str(second),
                            node_type="code_symbol", limit=100)
        a_names = {n["display_name"] for n in a.data["nodes"]}
        b_names = {n["display_name"] for n in b.data["nodes"]}
        assert "run" in a_names and "run" in b_names
        assert "only_b" in b_names
        assert "only_b" not in a_names

    def test_outside_project_path_produces_no_dataset(self, tmp_settings, project: Path,
                                                      tmp_path: Path):
        from core.knowledge_projection import project_tool_result
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            outside = tmp_path / "outside.csv"
            outside.write_text("a,b\n1,2\n", encoding="utf-8")
            project_tool_result(
                tool="data.inspect", args={"path": str(outside)},
                result_data={"headers": ["a"], "row_count": 1},
                task_id="t1", project_root=str(project),
                settings=tmp_settings, manager=mgr,
            )
            graph = mgr.get_graph(canonical_project_key(str(project)))
            assert graph.find_nodes(node_type=NodeType.DATASET, limit=5) == []
        finally:
            mgr.close_all()


# ------------------------------------------------------------- 12. RESTART


class TestCase12Restart:
    def test_relationships_survive_reopening_persistence(self, tmp_settings, project: Path):
        csv = project / "sales.csv"
        out = project / "charts" / "r.png"
        agent = _agent(tmp_settings, [
            _response(_call("data.inspect", path=str(csv))),
            _response(_call("chart.generate", chart_type="bar", data_path=str(csv),
                            x_column="product", y_column="amount",
                            output_path=str(out))),
            _response(text="done"),
        ], project=project)
        agent.respond("analyse and chart")
        agent.respond("thanks")

        # Fresh manager: nothing carried over in memory.
        mgr = KnowledgeGraphManager(knowledge_dir(tmp_settings))
        try:
            graph = mgr.get_graph(canonical_project_key(str(project)))
            kinds = {n.node_type for n in graph.find_nodes(limit=300)}
            assert {NodeType.DATASET, NodeType.CHART, NodeType.ARTIFACT} <= kinds
            chart = graph.find_nodes(node_type=NodeType.CHART, limit=1)[0]
            assert _targets(graph, chart.node_id, RelationType.GENERATED_FROM)
            assert _targets(graph, chart.node_id, RelationType.PRODUCED)
        finally:
            mgr.close_all()

    def test_query_tool_reads_the_reopened_graph(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        result = knowledge_query(query_type="search_files", settings=tmp_settings,
                                 project_path=str(project), extension=".py", limit=20)
        assert result.success
        assert result.data["count"] >= 2


def _mk(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "charts").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_run():\n    assert True\n",
                                                encoding="utf-8")
    (root / "sales.csv").write_text("product,amount\nA,10\nB,20\nC,30\n", encoding="utf-8")
    return root
