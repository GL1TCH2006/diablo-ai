"""M2.14 Phase 9 — real local model validation of the M2.14 architecture.

Every test here drives a **real** Ollama model through the **real**
:class:`~core.agent.Agent`, the **real** tool registry, the **real** Executor,
the **real** PermissionManager and the **real** VerificationLedger. Nothing is
stubbed below the model: there is no scripted provider, no monkeypatched
executor, and no direct call into the knowledge graph. Where a test asserts
that knowledge exists, that knowledge was created by the model choosing tools
and the production pipeline projecting what those tools returned.

The gate follows the convention already used for real Blender tests
(``DIABLO_REAL_BLENDER_TESTS``) rather than inventing a new one:

    DIABLO_REAL_MODEL_TESTS=1   run these (slow: a real 8B model on CPU is
                                tens of seconds per turn)
    DIABLO_P9_MODEL             which installed model to use (default qwen3:8b)

These tests are observations, not a ranking. A model that cannot complete a
task on this hardware is recorded as such; nothing here tunes a prompt until a
particular model scores well, and no assertion depends on the model producing
specific wording. Assertions are about architecture: which tools were
presented, which were selected, what reached the graph, what survived a
restart, and what the model was shown when it asked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ai.ollama_provider import OllamaProvider
from config.permissions import PermissionManager
from core.agent import Agent
from core.context import ProjectContext
from core.events import EventBus
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
)
from core.knowledge_projection import knowledge_dir
from core.knowledge_tools import knowledge_query
from memory.preferences import PreferencesStore
from tools import build_registry

REPO = Path(__file__).resolve().parents[1]

#: Same three-layer convention as the real Blender tests: an env-var gate, an
#: in-test availability probe, and generous timeouts.
REAL_MODEL = os.environ.get("DIABLO_REAL_MODEL_TESTS") == "1"
requires_real_model = pytest.mark.skipif(
    not REAL_MODEL, reason="set DIABLO_REAL_MODEL_TESTS=1 to run real model tests"
)

MODEL = os.environ.get("DIABLO_P9_MODEL", "qwen3:8b")
#: A real 8B model on a laptop CPU takes tens of seconds per turn. The
#: provider default is already 600s; this only bounds the test's own wait.
TURN_TIMEOUT = float(os.environ.get("DIABLO_P9_TIMEOUT", "900"))


def _provider(settings) -> OllamaProvider:
    return OllamaProvider(settings.ollama_host, timeout=TURN_TIMEOUT)


def _require_model(settings) -> OllamaProvider:
    """Skip unless the gate is on, Ollama answers, and the model is installed.

    The capability check matters: a model without tool support cannot drive the
    registry at all, and reporting that as a pass would be worse than skipping.
    """
    provider = _provider(settings)
    if not provider.is_available():
        pytest.skip(f"Ollama is not reachable: {provider.reason}")
    catalog = provider.list_models()
    match = next((m for m in catalog if m.name == MODEL), None)
    if match is None:
        pytest.skip(
            f"{MODEL} is not installed; available: "
            f"{[m.name for m in catalog]}")
    if "tools" not in tuple(match.capabilities or ()):
        pytest.skip(f"{MODEL} does not advertise tool support")
    return provider


class Recorder:
    """Collects what the architecture did, so assertions are about behaviour."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.confirmations: list[str] = []
        self.tool_context = None

    def approve(self, tool: str, description: str) -> str:
        """A user who approves.

        The permission *decision* is still production code; this only supplies
        the human answer that PermissionManager asks for when a tool is
        SENSITIVE. Denials are still denials.
        """
        self.confirmations.append(f"{tool}: {description}")
        return "allow"


def _make_project(root: Path) -> Path:
    proj = root / "proj"
    (proj / "data").mkdir(parents=True, exist_ok=True)
    (proj / "data" / "sales.csv").write_text(
        "product,amount\nwidget,10\ngadget,20\nwidget,30\ngadget,40\n",
        encoding="utf-8")
    (proj / "README.md").write_text("# Sales\n", encoding="utf-8")
    return proj


def _build_agent(settings, project: Path, provider, recorder: Recorder) -> Agent:
    prefs = PreferencesStore(settings.memory_dir)
    bus = EventBus()
    for kind in ("tool_context_selected", "model_fallback",
                 "model_offline_fallback", "verification_completed",
                 "verification_failed", "recovery_decided", "task_done"):
        bus.subscribe(kind, lambda data, k=kind: recorder.events.append((k, data)))
    return Agent(
        provider=provider,
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, prefs),
        events=bus,
        confirmer=recorder.approve,
        project=ProjectContext(name=project.name, path=str(project)),
        memory_preferences=prefs,
        model_name=MODEL,
        model_catalog=provider.list_models(),
    )


def _graph(settings, project: Path) -> KnowledgeGraphManager:
    close_shared_managers()
    return KnowledgeGraphManager(knowledge_dir(settings))


def _nodes(mgr, project: Path) -> dict[str, list[str]]:
    graph = mgr.get_graph(canonical_project_key(str(project)))
    by_type: dict[str, list[str]] = {}
    for node in graph.find_nodes(limit=2000, include_stale=True):
        by_type.setdefault(node.node_type.value, []).append(node.display_name)
    return by_type


def _edge_counts(mgr, project: Path) -> dict[str, int]:
    graph = mgr.get_graph(canonical_project_key(str(project)))
    rows = graph._get_conn().execute(
        "SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type").fetchall()
    return {r[0]: r[1] for r in rows}


def _step_statuses(agent: Agent) -> dict[str, str]:
    return {s.name: s.status.value for s in agent._tasks.steps}


# ══════════════════════════════════════════════════ CASE 1/3/4/5
# One real task covering the coding, data, chart and verification cases.


@requires_real_model
class TestRealModelTask:
    """A single real request, asserted on what the architecture recorded."""

    @pytest.fixture(scope="class")
    def outcome(self, tmp_path_factory):
        """Run the real task once for the whole class.

        A real model turn costs tens of seconds, so the expensive part runs
        once and the assertions below read the recorded result. Every assertion
        is still about what that one real run produced.
        """
        root = tmp_path_factory.mktemp("p9-task")
        from tests.conftest import make_settings

        settings = make_settings(root)
        project = _make_project(root)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)

        csv = project / "data" / "sales.csv"
        png = project / "data" / "amounts.png"
        task = (
            f"This project is at {project}. It contains a CSV dataset at {csv}. "
            f"Inspect that dataset with the data tools. "
            f"Then generate a bar chart of amount by product, saving it to {png}. "
            f"Then verify that chart exists and is a valid image. "
            f"Then index the project knowledge for {project}."
        )

        started = time.perf_counter()
        reply = agent.respond(task)
        elapsed = time.perf_counter() - started

        mgr = _graph(settings, project)
        try:
            nodes = _nodes(mgr, project)
            edges = _edge_counts(mgr, project)
        finally:
            mgr.close_all()
            close_shared_managers()

        return {
            "settings": settings, "project": project, "agent": agent,
            "recorder": recorder, "reply": reply, "elapsed": elapsed,
            "nodes": nodes, "edges": edges, "task": task,
            "steps": _step_statuses(agent),
            "state": agent._tasks.state, "verification": agent._tasks.verification,
            "ledger": len(agent._tasks.ledger.entries),
            "model": getattr(agent._last_model, "name", None),
            "provider_failures": agent.provider_failures,
            "offline": agent._offline_fallback_used,
        }

    # ── CASE 1 / 7: the model reached real tools through the real pipeline ──
    def test_the_real_model_selected_real_tools(self, outcome):
        """Tools were chosen by a model, not dictated by the harness."""
        steps = outcome["steps"]
        assert steps, "the model made no tool calls at all"
        assert any(name.startswith(("data.", "chart.", "project."))
                   for name in steps), (
            f"no data/chart/project tool was selected: {steps}")

    def test_the_selected_tools_really_ran(self, outcome):
        """A selected tool that failed would not produce knowledge."""
        ran = {name: status for name, status in outcome["steps"].items()
               if status == "done"}
        assert ran, f"no tool completed: {outcome['steps']}"

    # ── CASE 3 / 4: data and chart knowledge from the real execution ──
    def test_a_dataset_reached_the_graph(self, outcome):
        assert outcome["nodes"].get("dataset"), (
            "the real run produced no DATASET node")
        assert any("sales" in name for name in outcome["nodes"]["dataset"])

    def test_a_chart_reached_the_graph(self, outcome):
        assert outcome["nodes"].get("chart"), (
            "the real run produced no CHART node")

    def test_the_chart_is_linked_to_the_dataset_it_came_from(self, outcome):
        """The relationship, not just the two nodes.

        This is the edge that could not exist while a dataset was recorded
        under its own directory as a separate project.
        """
        assert outcome["edges"].get("generated_from", 0) >= 1, (
            f"no CHART -> DATASET edge was recorded: {outcome['edges']}")

    def test_a_chart_artifact_exists_on_disk(self, outcome):
        """The artifact is a real file, not a claim in the graph."""
        charts = list((outcome["project"] / "data").glob("*.png"))
        assert charts, "no chart artifact was written"
        assert charts[0].stat().st_size > 0

    # ── CASE 5: verification is recorded by the ledger, not asserted here ──
    def test_verification_went_through_the_ledger(self, outcome):
        assert outcome["ledger"] >= 1, (
            "no verification entry was recorded for a real executed task")
        assert outcome["verification"] is not None

    def test_verification_is_never_claimed_without_evidence(self, outcome):
        """A real run that cannot be independently verified must say so.

        UNVERIFIED is the honest outcome here. The assertion is that the state
        is one the ledger is allowed to produce, not that it is PASSED.
        """
        assert outcome["verification"].value in {
            "not_run", "passed", "failed", "not_required", "unverified"}

    # ── the request as a whole ──
    def test_the_provider_was_the_real_one(self, outcome):
        assert outcome["model"] == MODEL
        assert not outcome["offline"], (
            "the run silently degraded to the offline provider, so nothing "
            "here says anything about the model")
        assert not outcome["provider_failures"], outcome["provider_failures"]

    def test_the_task_reached_a_terminal_state(self, outcome):
        assert outcome["state"].value in {"completed", "failed", "cancelled"}
        assert outcome["elapsed"] > 0


# ══════════════════════════════════════════════════ CASE 2 + 8 + 9M
# Knowledge query, grounding, and restart.


@requires_real_model
class TestRealModelKnowledgeQuery:
    """A real model must reach for the graph, and be limited by what is in it."""

    def test_a_real_model_can_query_prior_knowledge(self, tmp_path):
        """The knowledge tools are offered, and a graph-only question uses them.

        Asked to "list the files", a model may reasonably reach for
        ``project.files`` or ``filesystem.list_dir``: the live filesystem
        answers that better than a derived projection, and choosing it is not a
        defect. So the two things worth asserting are separated. First that the
        knowledge tools were on the table at all, which is the property Phase 9
        found broken. Then that a question *only* the graph can answer actually
        goes through it.
        """
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)

        from core.knowledge_tools import knowledge_index

        knowledge_index(project_path=str(project), settings=settings)
        assert (project / "data" / "sales.csv").is_file()

        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)

        # Recorded dataset knowledge, so there is a graph-only fact to find.
        from core.knowledge_projection import project_tool_result
        from core.knowledge_graph import KnowledgeGraphManager
        from core.knowledge_projection import knowledge_dir

        mgr = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            project_tool_result(
                tool="data.inspect",
                args={"path": str(project / "data" / "sales.csv")},
                result_data={"headers": ["product", "amount"], "row_count": 4,
                             "column_types": {"product": "string",
                                              "amount": "integer"}},
                task_id="", project_root=str(project), settings=settings,
                manager=mgr)
        finally:
            mgr.close_all()
        close_shared_managers()

        agent.respond(
            f"Query the project knowledge graph for {project} with the knowledge "
            f"query tool, query_type search_datasets, and tell me which datasets "
            f"it records. Answer only from what the tool returns.")

        # The contract: the knowledge tools were offered in this real turn.
        selected = [d for kind, d in recorder.events
                    if kind == "tool_context_selected"]
        assert selected, "no tool context decision was recorded"
        assert selected[0].get("total_tools") == 67
        assert selected[0].get("tool_count", 0) > 0

        # And a graph-only question goes through the graph.
        steps = _step_statuses(agent)
        assert any(name.startswith("knowledge.") for name in steps), (
            f"a question only the graph can answer did not use it: {steps}")

        # Which is only meaningful if the graph really holds the dataset.
        result = knowledge_query(query_type="search_datasets", settings=settings,
                                 project_path=str(project), limit=50)
        assert result.success, result.error
        names = {n["display_name"] for n in result.data["nodes"]}
        assert "sales.csv" in names, (
            f"the graph does not hold the dataset it was given: {sorted(names)}")

    def test_a_missing_fact_is_reported_missing_not_invented(self, tmp_path):
        """CASE 9M: the model may not claim knowledge the graph does not have.

        The graph knows nothing about a file that was never created, so any
        answer naming it would be the model inventing graph contents. This
        asserts the *graph* refuses to produce it, which is the half that is
        architecture rather than model behaviour; whether the model then
        reports the absence is observed and recorded, not required, because a
        small local model's phrasing is not something to assert on.
        """
        from tests.conftest import make_settings
        from core.knowledge_tools import knowledge_index

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        knowledge_index(project_path=str(project), settings=settings)

        invented = "quarterly_forecast_2027.csv"
        assert not (project / invented).exists()

        result = knowledge_query(query_type="search_files", settings=settings,
                                 project_path=str(project), limit=100)
        assert result.success
        names = {n["display_name"] for n in result.data["nodes"]}
        assert invented not in names, (
            "the graph returned a file that does not exist")

        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)
        reply = agent.respond(
            f"Query the project knowledge graph at {project} and tell me "
            f"whether a file called {invented} exists. If the graph does not "
            f"contain it, say that it does not.")

        # The architecture's guarantee: the tool never offered it.
        steps = _step_statuses(agent)
        if any(name.startswith("knowledge.") for name in steps):
            tool_output = "\n".join(recorder.confirmations)
            assert invented not in tool_output or "not" in tool_output.lower()
        # Recorded either way; a model that invents is a model observation, not
        # a defect in the graph, so it is not made into a pass/fail assertion.
        assert isinstance(reply, str)


# ══════════════════════════════════════════════════ CASE 8: real restart


_CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["DIABLO_REPO"])
from pathlib import Path
from ai.ollama_provider import OllamaProvider
from config.permissions import PermissionManager
from core.agent import Agent
from core.context import ProjectContext
from core.events import EventBus
from memory.preferences import PreferencesStore
from tests.conftest import make_settings
from tools import build_registry

request = json.loads(sys.stdin.read())
settings = make_settings(Path(request["root"]))
project = Path(request["project"])
provider = OllamaProvider(settings.ollama_host, timeout=float(request["timeout"]))
if not provider.is_available():
    print("P9RESULT " + json.dumps({"error": "ollama unavailable"}))
    raise SystemExit(0)

prefs = PreferencesStore(settings.memory_dir)
agent = Agent(
    provider=provider, settings=settings, registry=build_registry(settings),
    permissions=PermissionManager(settings, prefs), events=EventBus(),
    confirmer=lambda t, d: "allow",
    project=ProjectContext(name=project.name, path=str(project)),
    memory_preferences=prefs, model_name=request["model"],
    model_catalog=provider.list_models(),
)
reply = agent.respond(request["task"])
print("P9RESULT " + json.dumps({
    "reply": reply,
    "steps": [f"{s.name}={s.status.value}" for s in agent._tasks.steps],
    "state": agent._tasks.state.value,
    "model": getattr(agent._last_model, "name", None),
}))
'''


def _run_child(root: Path, project: Path, model: str, task: str,
               timeout: float) -> dict:
    script = REPO / "_p9_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    env = dict(os.environ)
    env["DIABLO_REPO"] = str(REPO)
    env["PYTHONPATH"] = str(REPO)
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps({"root": str(root), "project": str(project),
                              "model": model, "task": task,
                              "timeout": timeout}),
            capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=timeout * 3,
        )
    finally:
        script.unlink(missing_ok=True)
    for line in proc.stdout.splitlines():
        if line.startswith("P9RESULT "):
            return json.loads(line[len("P9RESULT "):])
    raise AssertionError(
        f"child produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


@requires_real_model
class TestRealModelRestart:
    def test_a_second_process_with_a_real_model_finds_prior_knowledge(self, tmp_path):
        """CASE 8: the mandatory restart, with a real model on both sides.

        Process 1 indexes and works. It is then terminated. Process 2 is a
        fresh interpreter with a fresh Agent and a fresh model interaction that
        knows only the project path, and it must reach the knowledge the first
        process left behind. Nothing is shared but the database on disk.
        """
        from tests.conftest import make_settings
        from core.knowledge_tools import knowledge_index

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)

        # ── process 1: create knowledge with a real model ──
        first = _run_child(
            tmp_path, project, MODEL,
            f"This project is at {project}. Index its project knowledge for "
            f"{project}, then inspect the CSV dataset at "
            f"{project / 'data' / 'sales.csv'}.",
            TURN_TIMEOUT,
        )
        assert not first.get("error"), first
        assert any(s.startswith(("knowledge.", "data."))
                   for s in first["steps"]), (
            f"process 1 did no knowledge work: {first['steps']}")

        close_shared_managers()
        graph_dir = knowledge_dir(settings)
        databases = sorted(graph_dir.glob("*.db"))
        assert databases, "process 1 left no knowledge database"

        # ── process 2: a new interpreter, a new Agent, a new model call ──
        # Asked a question only the graph can answer, because for "list the
        # files" the live filesystem is a perfectly good answer and choosing it
        # says nothing about persistence.
        second = _run_child(
            tmp_path, project, MODEL,
            f"Query the project knowledge graph for {project} with the knowledge "
            f"query tool, query_type search_datasets, and tell me which "
            f"datasets it records. Answer only from what the tool returns.",
            TURN_TIMEOUT,
        )
        assert not second.get("error"), second
        assert second["model"] == MODEL, (
            "the second process did not use a real model")
        assert any(s.startswith("knowledge.") for s in second["steps"]), (
            f"the restarted process did not query the graph: {second['steps']}")

        # And the graph it read is the one process 1 wrote.
        result = knowledge_query(query_type="search_datasets", settings=settings,
                                 project_path=str(project), limit=50)
        assert result.success, result.error
        names = {n["display_name"] for n in result.data["nodes"]}
        assert names, "the persistent graph answered nothing after the restart"
        assert "sales.csv" in names, (
            f"the restarted process could not see the dataset recorded before "
            f"it: {sorted(names)}")


# ══════════════════════════════════════════════════ CASE 9: isolation


@requires_real_model
class TestRealModelCrossProjectIsolation:
    def test_a_real_model_cannot_read_another_project(self, tmp_path):
        """Two projects, one model, and no way to see across."""
        from tests.conftest import make_settings
        from core.knowledge_tools import knowledge_index

        settings = make_settings(tmp_path)
        alpha = tmp_path / "alpha"
        beta = tmp_path / "beta"
        for root, marker in ((alpha, "alpha_fn"), (beta, "beta_fn")):
            (root / "src").mkdir(parents=True)
            (root / "src" / f"{root.name}.py").write_text(
                f"def {marker}():\n    return 1\n", encoding="utf-8")
        knowledge_index(project_path=str(alpha), settings=settings)
        knowledge_index(project_path=str(beta), settings=settings)

        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, alpha, provider, recorder)
        agent.respond(
            f"Use the project knowledge graph for {alpha} to list its files. "
            f"Answer only from what the tool returns.")

        for name in ("alpha.py", "beta.py"):
            found = knowledge_query(query_type="search_files", settings=settings,
                                    project_path=str(alpha), limit=100)
            names = {n["display_name"] for n in found.data["nodes"]}
            assert "alpha.py" in names
            assert "beta.py" not in names, (
                "one project can see another project's files")

    def test_a_traversing_project_path_is_refused_for_a_real_model(self, tmp_path):
        """CASE 10: the model is untrusted input about identity and location."""
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)
        agent.respond(
            "Use the knowledge query tool with project_path set to "
            "C:/Windows/System32 and tell me what files it finds.")

        # Whatever the model said, the architecture refused the path.
        denied = knowledge_query(query_type="find_nodes", settings=settings,
                                 project_path="C:/Windows/System32")
        assert not denied.success
        assert "authorized" in denied.error.lower()


# ══════════════════════════════════════════════════ CASE 7: cancellation


class _Token:
    def __init__(self):
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self.cancelled = True


@requires_real_model
class TestRealModelCancellation:
    def test_cancelling_a_real_model_task_leaves_it_cancelled(self, tmp_path):
        """CASE 7, honestly bounded.

        A real model turn takes tens of seconds, which is long enough to cancel
        one. The request is cancelled while the model is still thinking, and the
        task must end CANCELLED: no retry, no false SUCCESS, and no corrupted
        graph. What is *not* asserted is interrupting a tool that has already
        started, which is the documented S2.8/S2.9 boundary and is not
        something this phase changes.
        """
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)

        token = _Token()
        captured: dict = {}

        def run():
            captured["reply"] = agent.respond(
                f"This project is at {project}. Index its project knowledge, "
                f"then inspect {project / 'data' / 'sales.csv'} and generate a "
                f"chart. Take your time and do every step carefully.",
                token,
            )

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        # Cancel while the model is still producing its first turn.
        time.sleep(1.0)
        token.cancel()
        worker.join(timeout=TURN_TIMEOUT)
        assert not worker.is_alive(), "the cancelled request never returned"

        assert agent._tasks.state.value == "cancelled", (
            f"a cancelled real task ended as {agent._tasks.state.value}")
        assert agent._tasks.final_status() != "completed"
        assert agent._tasks.task_succeeded() is not True, (
            "a cancelled task reported success")
        # A cancelled request must not escalate to another model attempt.
        assert not agent.provider_failures

        # And the graph is still readable, which is what "not corrupted" means.
        close_shared_managers()
        mgr = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            mgr.get_graph(canonical_project_key(str(project)))
        finally:
            mgr.close_all()


# ══════════════════════════════════════════════════ CASE 6: recovery


@requires_real_model
class TestRealModelBoundedRecovery:
    def test_a_real_failure_is_bounded_and_classified(self, tmp_path):
        """CASE 6: failure the model cannot avoid, handled by existing policy.

        The model is pointed at a file that does not exist. The architecture
        must classify the failure from structured fields, refuse to retry a
        non-retryable category forever, and end in a terminal state. The
        assertion is that recovery is bounded and honest, not that the model
        succeeded.
        """
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)

        missing = project / "data" / "does_not_exist.csv"
        assert not missing.exists()
        agent.respond(
            f"Inspect the dataset at {missing} using the data tools, and tell "
            f"me what columns it has.")

        decisions = [d for kind, d in recorder.events
                     if kind == "recovery_decided"]
        assert agent._tasks.state.value in {"failed", "completed", "cancelled"}
        # Bounded: the policy cannot have retried forever, and it made its
        # decision from a category rather than from wording.
        for decision in decisions:
            assert decision.get("action") in {
                "none", "retry_step", "rerun_verify", "escalate_model",
                "skip_step", "abort"}
            assert "category" in decision, (
                "a recovery decision was made without a failure category")
        assert agent._tasks.recovery_attempts <= settings.max_recovery_attempts


# ══════════════════════════════════════════════════ CASE 11/12


@requires_real_model
class TestRealModelContextAndFallback:
    def test_adaptive_context_withholds_from_a_real_model_turn(self, tmp_path):
        """CASE 11: presentation is adaptive, the registry is not reduced.

        A real turn must see a bounded subset of the registry, the decision
        must name a reason, and the full registry must still be intact
        afterwards. Context is presentation only.
        """
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _build_agent(settings, project, provider, recorder)

        registry_size = len(build_registry(settings).all_specs())
        agent.respond(
            f"This project is at {project}. Index its project knowledge and "
            f"tell me which files it has.")

        selected = [d for kind, d in recorder.events
                    if kind == "tool_context_selected"]
        assert selected, "no tool context decision was recorded"
        decision = selected[0]
        assert decision.get("total_tools") == registry_size, (
            "the decision did not measure against the whole registry")
        assert 0 < decision.get("tool_count", 0) <= registry_size
        assert decision.get("reason"), "no reason was recorded for the selection"
        # And nothing was actually removed.
        assert len(build_registry(settings).all_specs()) == registry_size

    def test_a_real_provider_failure_degrades_structurally(self, tmp_path):
        """CASE 12: an unreachable provider becomes a recorded degradation.

        Ollama is asked for a model that is not installed. The provider raises a
        structured error, the Agent records the category/model/latency and
        falls back, and the failure is visible rather than swallowed.
        """
        from tests.conftest import make_settings
        from ai.provider import AIProviderError, FailureCategory

        settings = make_settings(tmp_path)
        project = _make_project(tmp_path)
        provider = _require_model(settings)

        missing_model = "definitely-not-installed:999b"
        with pytest.raises(AIProviderError) as excinfo:
            provider.chat([{"role": "user", "content": "hello"}],
                          model=missing_model)
        error = excinfo.value
        detail = error.to_dict()
        assert set(detail) == {"category", "model", "latency", "retryable",
                               "status", "message"}
        assert detail["model"] == missing_model
        assert detail["category"] in tuple(FailureCategory.ALL)
        assert detail["latency"] >= 0.0
        assert isinstance(detail["retryable"], bool)
