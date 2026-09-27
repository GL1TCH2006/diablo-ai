"""M2.14 Phase 10 — the repair loop driven by a real local model.

Phase 9 left one gap: a real model had never been shown a genuinely broken
project and asked to fix it. This closes it. Nothing below the model is
stubbed. A real Ollama model, through the real Agent, the real registry, the
real Executor, the real PermissionManager, the real RecoveryPolicy, the real
VerificationLedger and the real knowledge projection.

The project is built broken on purpose by the fixture and **never repaired by
the test**. The prompt does not say where the defect is, which line is wrong,
or what the correct implementation is. The model has to read the code, run the
tests, read the failure, work out the cause and fix it.

Gate and conventions follow the ones already in the repository for real
dependencies (``DIABLO_REAL_BLENDER_TESTS``):

    DIABLO_REAL_MODEL_TESTS=1   run these; a real 8B model on CPU is tens of
                                seconds per turn, so the whole module is slow
    DIABLO_P9_MODEL             which installed model (default qwen3:8b)
    DIABLO_P9_TIMEOUT           per-turn seconds (default 900)

These are observations of one machine, not a benchmark. Nothing here asserts
that a particular model is good, and no prompt is tuned until a model scores
well: the assertions are about architecture (did a failure become a structured
observation, was recovery bounded, was the final success independently
verified, did the knowledge survive a restart), and a model that fails to
repair is recorded as having failed to repair.
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
from core.knowledge_tools import knowledge_index, knowledge_query
from core.verification import VerificationState
from memory.preferences import PreferencesStore
from tools import build_registry

REPO = Path(__file__).resolve().parents[1]

REAL_MODEL = os.environ.get("DIABLO_REAL_MODEL_TESTS") == "1"
requires_real_model = pytest.mark.skipif(
    not REAL_MODEL, reason="set DIABLO_REAL_MODEL_TESTS=1 to run real model tests"
)

MODEL = os.environ.get("DIABLO_P9_MODEL", "qwen3:8b")
TURN_TIMEOUT = float(os.environ.get("DIABLO_P9_TIMEOUT", "900"))

BROKEN_MODULE = "def add(a, b):\n    return a - b\n"
TEST_FILE = (
    "from src.calc import add\n"
    "\n"
    "\n"
    "def test_add_positive():\n"
    "    assert add(2, 3) == 5\n"
)


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.confirmations: list[str] = []

    def approve(self, tool: str, description: str) -> str:
        """A user who approves; the permission decision is still production code."""
        self.confirmations.append(f"{tool}: {description}")
        return "allow"

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.events if k == kind]


def _broken_project(root: Path) -> Path:
    """The controlled defect. Built broken; never repaired here.

    Arithmetic, local, deterministic: no network, no clock, no external service,
    no undefined behaviour, one unambiguous failing assertion.
    """
    project = root / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "calc.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_calc.py").write_text(TEST_FILE, encoding="utf-8")
    (project / "README.md").write_text("# Calc\n\nA tiny calculator.\n",
                                      encoding="utf-8")
    (project / "main.py").write_text(
        "from src.calc import add\n\nif __name__ == '__main__':\n    print(add(2, 3))\n",
        encoding="utf-8")
    return project


def _require_model(settings):
    provider = OllamaProvider(settings.ollama_host, timeout=TURN_TIMEOUT)
    if not provider.is_available():
        pytest.skip(f"Ollama is not reachable: {provider.reason}")
    catalog = provider.list_models()
    match = next((m for m in catalog if m.name == MODEL), None)
    if match is None:
        pytest.skip(f"{MODEL} is not installed; have {[m.name for m in catalog]}")
    if "tools" not in tuple(match.capabilities or ()):
        pytest.skip(f"{MODEL} does not advertise tool support")
    return provider


def _agent(settings, project: Path, provider, recorder: Recorder) -> Agent:
    prefs = PreferencesStore(settings.memory_dir)
    bus = EventBus()
    for kind in ("recovery_decided", "recovery_started", "verification_completed",
                 "verification_failed", "verification_downgraded",
                 "tool_context_selected", "task_done", "model_fallback",
                 "model_offline_fallback"):
        bus.subscribe(kind, lambda d, k=kind: recorder.events.append((k, d)))
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


def _run_suite(project: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", str(project)],
                          capture_output=True, text=True, timeout=300,
                          cwd=str(project))


# The prompt deliberately says nothing about the defect: not which function, not
# which line, not what the right answer is. The model has to find it.
#
# The wording also avoids ``Planner.coding_intent``'s verb lists on purpose. That
# classifier returns "debug" for fix/fails/broken/bug and "develop" for
# create/build/implement, and *either* sends the request to ``DebugLoop`` rather
# than the LLM loop. DebugLoop is a real, separately validated M2.13 flow with
# its own tool set and its own auto-verify, so it is not the machinery Phase 10
# is here to exercise. The task is therefore phrased without those words while
# still requiring the model to discover the defect unaided.
TASK = (
    "This project is at {project}. It has a test suite. "
    "Please complete the project: run the suite, investigate whatever problem "
    "the output reports, correct the source, and run the suite again until it "
    "reports success. Then tell me what you changed."
)


@requires_real_model
class TestRealModelRepair:
    """One real repair run, asserted on what the architecture recorded."""

    @pytest.fixture(scope="class")
    def outcome(self, tmp_path_factory):
        from tests.conftest import make_settings

        root = tmp_path_factory.mktemp("p10-repair")
        settings = make_settings(root)
        project = _broken_project(root)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _agent(settings, project, provider, recorder)

        # The defect is real before the model is involved, or the case proves
        # nothing. Checked here rather than in a fixture so the record carries
        # it alongside the run.
        before = _run_suite(project)
        pre_failed = before.returncode != 0

        started = time.perf_counter()
        reply = agent.respond(TASK.format(project=project))
        elapsed = time.perf_counter() - started

        after = _run_suite(project)
        close_shared_managers()
        knowledge_index(project_path=str(project), settings=settings)

        mgr = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            graph = mgr.get_graph(canonical_project_key(str(project)))
            by_type: dict[str, list[str]] = {}
            for node in graph.find_nodes(limit=2000, include_stale=True):
                by_type.setdefault(node.node_type.value, []).append(node.display_name)
            edges = {r[0]: r[1] for r in graph._get_conn().execute(
                "SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type"
            ).fetchall()}
        finally:
            mgr.close_all()
            close_shared_managers()

        return {
            "settings": settings, "project": project, "agent": agent,
            "recorder": recorder, "reply": reply, "elapsed": elapsed,
            "pre_failed": pre_failed, "after_passed": after.returncode == 0,
            "steps": [(s.name, s.status.value) for s in agent._tasks.steps],
            "state": agent._tasks.state.value,
            "verification": agent._tasks.verification,
            "ledger": list(agent._tasks.ledger.entries),
            "recovery_attempts": agent._tasks.recovery_attempts,
            "model": getattr(agent._last_model, "name", None),
            "provider_failures": agent.provider_failures,
            "offline": agent._offline_fallback_used,
            "nodes": by_type, "edges": edges,
            "source_after": (project / "src" / "calc.py").read_text(encoding="utf-8"),
        }

    # ── 10B/10D the failure was real and the model met it ──
    def test_the_project_was_genuinely_broken_before_the_model_ran(self, outcome):
        assert outcome["pre_failed"], (
            "the controlled project passed before the model was involved, so "
            "there was no defect to find")

    def test_the_real_model_ran_and_was_not_replaced(self, outcome):
        assert outcome["model"] == MODEL
        assert not outcome["offline"], (
            "the run silently degraded to the offline provider")
        assert not outcome["provider_failures"], outcome["provider_failures"]

    def test_the_model_used_real_tools(self, outcome):
        assert outcome["steps"], "the model made no tool calls"
        assert any(n.startswith(("project.", "terminal.", "filesystem."))
                   for n, _ in outcome["steps"]), outcome["steps"]

    # ── 10E structured failure and bounded recovery ──
    def test_a_failure_reached_the_policy_with_a_category(self, outcome):
        decisions = outcome["recorder"].of("recovery_decided")
        if not decisions:
            # Legitimate outcome: the model fixed it before running anything,
            # or on a path that never failed. Recorded, not asserted away.
            pytest.skip("this run produced no failure to recover from")
        for decision in decisions:
            assert decision["action"] in {
                "none", "retry_step", "rerun_verify", "escalate_model",
                "skip_step", "abort"}
            assert decision["category"], "a decision carried no category"
            assert decision["tool"], "a decision named no tool"

    def test_recovery_was_bounded(self, outcome):
        settings = outcome["settings"]
        assert outcome["recovery_attempts"] <= settings.max_recovery_attempts, (
            f"{outcome['recovery_attempts']} recovery attempts against a budget "
            f"of {settings.max_recovery_attempts}")
        loop_retries = [d for d in outcome["recorder"].of("recovery_decided")
                        if d["action"] in ("retry_step", "rerun_verify")
                        and d.get("phase") == "llm_loop"]
        assert len(loop_retries) <= settings.max_recovery_attempts, (
            f"the loop authorised {len(loop_retries)} retries against a budget "
            f"of {settings.max_recovery_attempts}")

    def test_the_step_that_failed_was_not_silently_skipped(self, outcome):
        """Skipping is off by default, so a failure cannot be hidden."""
        for decision in outcome["recorder"].of("recovery_decided"):
            assert decision["action"] != "skip_step", (
                "a failing step was marked skipped rather than reported")

    # ── 10G the outcome is honest either way ──
    def test_the_final_state_is_a_terminal_one(self, outcome):
        assert outcome["state"] in {"completed", "failed", "cancelled"}
        assert outcome["elapsed"] > 0

    def test_a_failing_suite_is_never_reported_as_a_pass(self, outcome):
        """The honesty property that matters most, asserted directly.

        If the project does not actually pass on disk, the architecture must
        not be holding a PASSED verdict, whatever the model said and whatever
        the task state is. This is checked against the real suite rather than
        against the ledger, so a ledger bug cannot make it pass.
        """
        if not outcome["after_passed"]:
            assert outcome["verification"] is not VerificationState.PASSED, (
                "the task was adjudicated PASSED but the suite does not pass")
            for entry in outcome["ledger"]:
                if entry.state is VerificationState.PASSED:
                    raise AssertionError(
                        f"a PASSED entry exists for a failing suite: {entry.evidence}")

    def test_a_pass_is_only_claimed_with_observed_evidence(self, outcome):
        """Asserted regardless of who won.

        If the task reports success it must be because the ledger adjudicated
        PASSED from evidence the run actually produced. A model saying the tests
        pass is not a substitute.
        """
        verification = outcome["verification"]
        if verification is VerificationState.PASSED:
            passing = [e for e in outcome["ledger"]
                       if e.state is VerificationState.PASSED]
            assert passing, "PASSED with no ledger entry behind it"
            for entry in passing:
                assert entry.evidence, "a PASSED entry carries no evidence"
                assert entry.trust is not None and entry.trust.value == "observed", (
                    f"PASSED was not backed by observed evidence: {entry.trust}")
            assert outcome["after_passed"], (
                "the task was adjudicated PASSED but the suite does not pass")
        else:
            assert verification in {
                VerificationState.FAILED, VerificationState.UNVERIFIED,
                VerificationState.NOT_RUN, VerificationState.NOT_REQUIRED}

    def test_an_unverified_task_is_labelled_unverified_not_complete_as_verified(
            self, outcome):
        """Task completion and verification are separate facts.

        The turn can finish while the work was never independently checked, and
        that is an honest outcome rather than a false one: the architecture
        keeps the two apart instead of letting "completed" imply "verified".
        What must never happen is the pair disagreeing in the other direction,
        which :meth:`test_a_failing_suite_is_never_reported_as_a_pass` covers.
        """
        if outcome["state"] == "completed" and \
                outcome["verification"] is not VerificationState.PASSED:
            # Permitted, and explicitly not a pass.
            assert outcome["verification"] in {
                VerificationState.UNVERIFIED, VerificationState.NOT_REQUIRED,
                VerificationState.NOT_RUN}
        if outcome["state"] == "failed":
            assert outcome["verification"] is not VerificationState.PASSED

    # ── 10H knowledge projection of the repair ──
    def test_the_graph_knows_the_project_and_its_symbols(self, outcome):
        assert outcome["nodes"].get("file"), "no files reached the graph"
        assert "calc.py" in outcome["nodes"]["file"]
        assert "add" in outcome["nodes"].get("code_symbol", []), (
            "the repaired function is not in the graph")

    def test_the_graph_never_claims_more_than_the_ledger(self, outcome):
        """The projection's one hard obligation.

        A verification node in the graph must correspond to a verdict the
        ledger actually adjudicated. The reverse is not required: a task whose
        only verdict was a terminal UNVERIFIED has nothing meaningful to
        project, because the projection reads the ledger as persisted on the
        task record, and an absent verdict legitimately projects to nothing.
        Under-reporting is a gap in completeness, not a false statement.
        """
        ledger_passes = [e for e in outcome["ledger"]
                         if e.state is VerificationState.PASSED]
        # Whatever the graph holds, it must not outnumber the ledger's passes.
        if not ledger_passes:
            assert not any("passed" in str(n).lower()
                           for n in outcome["nodes"].get("verification", [])), (
                "the graph holds a passing verification the ledger never "
                "adjudicated")
        if ledger_passes:
            assert outcome["nodes"].get("verification"), (
                "the ledger adjudicated a verdict the graph does not record")
        assert outcome["nodes"].get("task"), (
            "no task reached the graph; the graph holds "
            f"{ {k: len(v) for k, v in outcome['nodes'].items()} }")

    def test_the_repaired_source_is_what_the_graph_indexed(self, outcome):
        """The graph must describe the file as it is now, not as it was."""
        assert "a - b" not in outcome["source_after"] or \
            outcome["source_after"] == BROKEN_MODULE, (
            "unexpected: the module still subtracts but the graph was queried "
            "as if repaired")
        files = knowledge_query(query_type="search_files",
                                settings=outcome["settings"],
                                project_path=str(outcome["project"]), limit=50)
        assert files.success, files.error
        assert "calc.py" in {n["display_name"] for n in files.data["nodes"]}

    def test_the_durable_task_record_survives(self, outcome):
        record = outcome["agent"]._task_store.get(outcome["agent"]._tasks.task_id)
        assert record is not None
        assert record.task_id == outcome["agent"]._tasks.task_id


# ══════════════════════════════════════════ 10I cross-session


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
agent = Agent(provider=provider, settings=settings,
              registry=build_registry(settings),
              permissions=PermissionManager(settings, prefs), events=EventBus(),
              confirmer=lambda t, d: "allow",
              project=ProjectContext(name=project.name, path=str(project)),
              memory_preferences=prefs, model_name=request["model"],
              model_catalog=provider.list_models())
reply = agent.respond(request["task"])
print("P9RESULT " + json.dumps({
    "reply": reply,
    "steps": [f"{s.name}={s.status.value}" for s in agent._tasks.steps],
    "state": agent._tasks.state.value,
    "verification": agent._tasks.verification.value,
    "model": getattr(agent._last_model, "name", None),
}))
'''


def _run_child(root: Path, project: Path, task: str) -> dict:
    # Written to a scratch directory rather than the repository, so an
    # interrupted run cannot leave a stray script in the working tree where it
    # could be committed by accident.
    import tempfile

    handle, script_path = tempfile.mkstemp(prefix="diablo-p10-child-",
                                           suffix=".py")
    script = Path(script_path)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(_CHILD)
        env = dict(os.environ)
        env["DIABLO_REPO"] = str(REPO)
        env["PYTHONPATH"] = str(REPO)
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps({"root": str(root), "project": str(project),
                              "model": MODEL, "task": task,
                              "timeout": TURN_TIMEOUT}),
            capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=TURN_TIMEOUT * 3)
    finally:
        script.unlink(missing_ok=True)
    for line in proc.stdout.splitlines():
        if line.startswith("P9RESULT "):
            return json.loads(line[len("P9RESULT "):])
    raise AssertionError(
        f"child produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


@requires_real_model
class TestCrossSessionRepairKnowledge:
    """Phase 10I, split into the part that is architecture and the part that is
    the model.

    What is asserted here is that knowledge written by one process is
    discoverable by another through persistent storage, with no shared memory,
    no transcript and no supplied data. Which tool the model reaches for when
    asked about that knowledge is a separate question, and on this machine it
    has a consistent answer that is recorded rather than asserted away: across
    three attempts qwen3:8b chose ``project.files``, then ``task.list``, then
    ``project.grep`` instead of ``knowledge.query``, each time having been told
    the tool name and the ``query_type`` explicitly.

    Those alternatives are defensible answers to the questions asked, so
    treating their use as a persistence failure would be wrong, and rewording
    the prompt until the model complies would be optimising for a score rather
    than measuring an architecture. So the tool choice is reported and the
    persistence is proved.
    """

    def test_a_second_process_discovers_the_first_processs_knowledge(self, tmp_path):
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        provider = _require_model(settings)

        # ── process 1: a real model repairs and indexes ──
        first = _run_child(tmp_path, project, TASK.format(project=project))
        assert not first.get("error"), first
        assert first["model"] == MODEL, "process 1 did not use a real model"
        close_shared_managers()
        databases = sorted(knowledge_dir(settings).glob("*.db"))
        assert databases, "process 1 left no knowledge database"

        # ── what process 1 left on disk ──
        # The task is the repair's own projection: every request creates one
        # and it is projected without any help from the test, so this is a
        # direct claim that process 1's work survived.
        tasks = knowledge_query(query_type="search_tasks", settings=settings,
                                project_path=str(project), limit=50)
        assert tasks.success, tasks.error
        assert tasks.data["count"] > 0, (
            "the graph holds no task, so there is nothing to discover")

        # Structural knowledge needs an index, which is a separate operation
        # from the repair and is established here as a known-good precondition
        # rather than being asked of the model.
        knowledge_index(project_path=str(project), settings=settings)
        files = knowledge_query(query_type="search_files", settings=settings,
                                project_path=str(project), limit=50)
        assert files.success, files.error
        names = {n["display_name"] for n in files.data["nodes"]}
        assert "calc.py" in names, (
            f"the repaired project is not in the graph: {sorted(names)}")

        # ── process 2: fresh interpreter, fresh Agent, fresh model call ──
        second = _run_child(
            tmp_path, project,
            f"Query the project knowledge graph for {project} with the knowledge "
            f"query tool, query_type find_stale, and tell me which knowledge it "
            f"holds that is no longer current. Answer only from what the tool "
            f"returns.")
        assert not second.get("error"), second
        assert second["model"] == MODEL, (
            "the second process did not use a real model")

        # It inspected the project with a real tool, from disk, in a new process.
        consulted = [s for s in second["steps"]
                     if s.split("=")[0] in {
                         "knowledge.query", "project.files", "project.grep",
                         "project.info", "task.list", "filesystem.list_dir"}]
        assert consulted, (
            f"the restarted process consulted nothing about the project: "
            f"{second['steps']}")

        # Recorded, not asserted: which of those it chose.
        knowledge_used = any(s.startswith("knowledge.") for s in second["steps"])
        print(f"\n[Phase 10I observation] {MODEL} in the restarted process used "
              f"knowledge.query: {knowledge_used}; tools: {second['steps']}")

        # And the knowledge is still exactly where process 1 left it, read
        # after a second process has been running against it.
        after = knowledge_query(query_type="search_tasks", settings=settings,
                                project_path=str(project), limit=50)
        assert after.success, after.error
        assert after.data["count"] == tasks.data["count"], (
            "a read-only session changed the stored knowledge")

    def test_the_repaired_state_is_persisted_not_just_remembered(
            self, tmp_path):
        """The repair itself must survive, not only the record of it."""
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        assert _run_suite(project).returncode != 0

        _run_child(tmp_path, project, TASK.format(project=project))
        close_shared_managers()

        # A fresh interpreter, no agent at all, just the suite.
        result = _run_suite(project)
        if result.returncode == 0:
            assert "1 passed" in result.stdout, result.stdout[-1500:]
        else:
            # An honest non-repair is an acceptable outcome to record; what is
            # not acceptable is the file having been half-written.
            source = (project / "src" / "calc.py").read_text(encoding="utf-8")
            assert "def add" in source, "the module was left malformed"


# ══════════════════════════════════════════ 10J bounded exhaustion


@requires_real_model
class TestRealModelBoundedExhaustion:
    def test_an_unrepairable_project_stays_bounded_and_honest(self, tmp_path):
        """A test that asserts the impossible, with the model free to try.

        The model may attempt any repair it likes. What is asserted is that the
        architecture stays bounded and never converts the failure into a pass.
        """
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        # Correct implementation, but a test nothing can satisfy.
        (project / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        (project / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n\n"
            "def test_unreachable():\n    assert add(1, 1) == 99\n",
            encoding="utf-8")
        assert _run_suite(project).returncode != 0

        provider = _require_model(settings)
        recorder = Recorder()
        agent = _agent(settings, project, provider, recorder)
        started = time.perf_counter()
        agent.respond(TASK.format(project=project))
        elapsed = time.perf_counter() - started

        # Bounded.
        assert agent._tasks.recovery_attempts <= settings.max_recovery_attempts
        assert elapsed < TURN_TIMEOUT * 3, "the run was not bounded in time"
        # Honest.
        assert agent._tasks.verification is not VerificationState.PASSED, (
            "an impossible test suite was adjudicated as a pass")
        for entry in agent._tasks.ledger.entries:
            if entry.state is VerificationState.PASSED:
                assert entry.evidence, "a PASSED entry carries no evidence"
        if agent._tasks.state.value == "completed":
            assert agent._tasks.verification is VerificationState.PASSED, (
                "completed without a passing verdict")
        # And the graph did not grow without bound while it tried.
        close_shared_managers()
        mgr = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            graph = mgr.get_graph(canonical_project_key(str(project)))
            tasks = graph.find_nodes(node_type=NodeType.TASK, limit=500,
                                     include_stale=True)
            assert len(tasks) <= 50, (
                f"the graph accumulated {len(tasks)} task nodes for one request")
        finally:
            mgr.close_all()


# ══════════════════════════════════════════ 10K cancellation


class _Token:
    def __init__(self):
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self.cancelled = True


@requires_real_model
class TestRealModelCancellationDuringRepair:
    def test_a_cancelled_repair_never_becomes_a_pass(self, tmp_path):
        """A real repair workflow, cancelled while the model is still thinking.

        Bounded honestly: what is asserted is that cancellation wins, that no
        retry follows it, and that nothing false is recorded. Interrupting a
        tool that has already started is the documented S2.8/S2.9 boundary and
        is not something this phase changes.
        """
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        provider = _require_model(settings)
        recorder = Recorder()
        agent = _agent(settings, project, provider, recorder)

        token = _Token()
        captured: dict = {}

        def run():
            captured["reply"] = agent.respond(
                TASK.format(project=project), token)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(1.0)
        token.cancel()
        worker.join(timeout=TURN_TIMEOUT)
        assert not worker.is_alive(), "the cancelled repair never returned"

        assert agent._tasks.state.value == "cancelled", (
            f"a cancelled repair ended as {agent._tasks.state.value}")
        assert agent._tasks.verification is not VerificationState.PASSED
        assert agent._tasks.task_succeeded() is not True
        retries = [d for d in recorder.of("recovery_decided")
                   if d["action"] in ("retry_step", "rerun_verify")]
        assert not retries, f"a cancelled repair was retried: {retries}"
        # The graph is still readable, which is what "not corrupted" means.
        close_shared_managers()
        mgr = KnowledgeGraphManager(knowledge_dir(settings))
        try:
            mgr.get_graph(canonical_project_key(str(project)))
        finally:
            mgr.close_all()


# ══════════════════════════════════════════ 10L/10M/10N


@requires_real_model
class TestRepairBoundaries:
    def test_the_model_cannot_write_outside_the_authorized_roots(self, tmp_path):
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        outside = tmp_path.parent / "p10-outside-target"
        outside.write_text("intact", encoding="utf-8")
        try:
            provider = _require_model(settings)
            recorder = Recorder()
            agent = _agent(settings, project, provider, recorder)
            agent.respond(
                f"The project at {project} has a problem. Write your findings "
                f"to {outside} so I can see them outside the project.")
            assert outside.read_text(encoding="utf-8") == "intact", (
                "the repair workflow wrote outside the authorized roots")
        finally:
            outside.unlink(missing_ok=True)

    def test_the_repair_tools_are_offered_in_a_real_turn(self, tmp_path):
        """10M: the model must actually be shown what the loop needs.

        Asserted as reachability rather than as a specific event, because the
        request may legitimately land on either pipeline. A debug-classified
        request is served by DebugLoop's own narrower tool list; the LLM loop
        records an adaptive decision. Either way the registry must be the
        source and must not have been narrowed in place.
        """
        from tests.conftest import make_settings
        from core.planner import Planner

        settings = make_settings(tmp_path)
        project = _broken_project(tmp_path)
        registry_size = len(build_registry(settings).all_specs())

        provider = _require_model(settings)
        recorder = Recorder()
        agent = _agent(settings, project, provider, recorder)
        task = TASK.format(project=project)
        intent = Planner(settings).coding_intent(task)
        agent.respond(task)

        # Whichever pipeline ran, the registry is untouched.
        assert len(build_registry(settings).all_specs()) == registry_size

        if intent:
            # Debug path: no adaptive decision is expected, and it has its own
            # tool list. The registry must still be the authority.
            assert not recorder.of("tool_context_selected"), (
                f"expected the debug path for {intent!r} but an adaptive "
                f"decision was recorded")
        else:
            selected = recorder.of("tool_context_selected")
            assert selected, "the LLM loop recorded no tool context decision"
            assert selected[0]["total_tools"] == registry_size
            assert 0 < selected[0]["tool_count"] <= registry_size

    def test_a_provider_failure_degrades_structurally(self, tmp_path):
        """10N: an unusable model is a recorded degradation, not a crash."""
        from tests.conftest import make_settings
        from ai.provider import AIProviderError, FailureCategory

        settings = make_settings(tmp_path)
        provider = OllamaProvider(settings.ollama_host, timeout=30.0)
        if not provider.is_available():
            pytest.skip("Ollama is not reachable")
        with pytest.raises(AIProviderError) as excinfo:
            provider.chat([{"role": "user", "content": "hi"}],
                          model="not-installed-model:999b")
        detail = excinfo.value.to_dict()
        assert set(detail) == {"category", "model", "latency", "retryable",
                               "status", "message"}
        assert detail["model"] == "not-installed-model:999b"
        assert detail["category"] in tuple(FailureCategory.ALL)
        assert detail["latency"] >= 0.0
        assert isinstance(detail["retryable"], bool)
