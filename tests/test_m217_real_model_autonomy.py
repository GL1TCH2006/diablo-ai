"""M2.17 â€” a **pytest** gate for real-model autonomy evidence.

Why this file exists
====================

``docs/M2_17_TASK0_BASELINE_AND_SKIP_INVENTORY.txt`` Â§F recorded a structural gap
that no skip could reveal:

    "M2.17's real-model evidence currently lives entirely outside the test suite.
     Not skipped - absent. There is no pytest test, passing or skipped, that
     exercises the M2.17 autonomy path with a real model."

The three M2.17 real-model probes - ``tests/real_model_autonomy.py``,
``real_model_desktop_selection.py`` and ``real_model_edge_companion.py`` - are
*scripts*. None matches pytest's default collection patterns, so a reviewer
running the suite sees thousands of green tests and cannot tell that real-model
goal completion has never succeeded.

This file closes that gap. It is the first collected, gated pytest coverage of the
M2.17 autonomy path against a real model, and it asserts on **persisted state plus
an independently observed artifact** - never on the model's own claim of success.

The gate
========

The repo's existing convention (``test_knowledge_p9_real_model.py:58-66``,
``test_recovery_p10_real_model.py:62-68``), reused rather than reinvented:

===========================  ==================================================
``DIABLO_REAL_MODEL_TESTS``  set to ``1`` to run these
``DIABLO_P9_MODEL``          which installed model to use (default ``qwen3:8b``)
``DIABLO_P9_TIMEOUT``        per-turn provider timeout, seconds (default 900)
===========================  ==================================================

Three layers, as elsewhere: the env-var gate, an in-test availability probe that
skips with a **precise** reason, and generous timeouts.

Run it
======

::

    DIABLO_REAL_MODEL_TESTS=1 .venv\\Scripts\\python.exe -m pytest -q \\
        tests/test_m217_real_model_autonomy.py

Safety
======

Every run works inside a disposable pytest temp directory (``tmp_path_factory``),
built through ``tests.conftest.make_settings``, whose ``allowed_roots`` is that
directory. **The real project is never touched.** Side-effecting tools other than
``filesystem.write_file`` (which is confined to the disposable root by
containment) are session-blocked for the duration, so a real model cannot reach
git, the desktop, Blender or the shell.

Unverified in this run
======================

This file was authored by an unattended batch that is forbidden from contacting
Ollama. It was executed **with the gate OFF only**, which exercises the skip path
and nothing else. Its behaviour **with the gate ON is UNRUN-WITH-GATE**: no
assertion in it has ever executed. Treat a first green run as the first evidence,
not as a regression check.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from ai.ollama_provider import OllamaProvider

# â”€â”€ the gate, verbatim from the existing convention â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
REAL_MODEL = os.environ.get("DIABLO_REAL_MODEL_TESTS") == "1"
requires_real_model = pytest.mark.skipif(
    not REAL_MODEL, reason="set DIABLO_REAL_MODEL_TESTS=1 to run real model tests"
)

MODEL = os.environ.get("DIABLO_P9_MODEL", "qwen3:8b")
TURN_TIMEOUT = float(os.environ.get("DIABLO_P9_TIMEOUT", "900"))

# â”€â”€ bounds this file imposes on the model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#: Hard ceiling on tool calls for one goal. `Agent` has its own bound
#: (`settings.max_tool_calls`, default 32); this is the harness's own, tighter and
#: independent, so a change to that default cannot silently widen a real run.
MAX_TOOL_CALLS = 24

#: Hard ceiling on wall time for one goal. A real 8B model on CPU takes tens of
#: seconds per turn, so this is generous for ~4 turns and still finite.
WALL_TIME_LIMIT_SECONDS = float(os.environ.get("DIABLO_M217_WALL_LIMIT", "600"))

#: Tools session-blocked for the whole class. Containment already confines
#: `filesystem.write_file` to the disposable root; these are the ones that could
#: reach outside it or leave a mark anywhere that matters.
BLOCKED_TOOLS = (
    "filesystem.delete_file",
    "python.run_script",
    "terminal.execute",
    "applications.open",
    "capability.execute",
    "desktop.open",
    "desktop.find",
    "desktop.interact",
    "git.add",
    "git.commit",
    "git.push",
    "git.reset",
    "git.restore",
    "git.rebase",
    "git.pull",
    "project.verify",
)

#: The payload the model is asked to write, and that *this test* independently
#: verifies afterwards. Chosen to be unambiguous, ASCII, single-line, and to have
#: no trailing-whitespace ambiguity.
PAYLOAD = "diablo-real-model-autonomy-ok"


def _require_model(settings) -> OllamaProvider:
    """Skip unless the gate is on, Ollama answers, and the model can drive tools.

    Three separate skip reasons, because they call for different actions:
    not running, Ollama down, and a model that cannot do the job at all.
    """
    provider = OllamaProvider(settings.ollama_host, timeout=TURN_TIMEOUT)
    if not provider.is_available():
        pytest.skip(f"Ollama is not reachable at {settings.ollama_host}: {provider.reason}")
    try:
        catalog = provider.list_models()
    except Exception as exc:  # noqa: BLE001 - a failure here is an unavailable backend
        pytest.skip(f"Ollama did not return a model catalog: {exc}")
    match = next((m for m in catalog if m.name == MODEL), None)
    if match is None:
        pytest.skip(f"{MODEL} is not installed; available: {[m.name for m in catalog]}")
    if "tools" not in tuple(match.capabilities or ()):
        pytest.skip(
            f"{MODEL} does not advertise tool support; it cannot drive the "
            f"registry, so a result here would be meaningless")
    return provider


class _CountingProvider:
    """Wraps the real provider and records which tools were actually dispatched.

    Counts *dispatches*, not the model's claims about itself. A model that says
    "I wrote the file" without the tool having run leaves this empty, and the
    artifact assertion then fails on its own evidence.
    """

    def __init__(self, inner):
        self._inner = inner
        self.tool_names: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def chat(self, messages, tools=None, model=""):
        from ai.provider import AIResponse, ToolCall

        response = self._inner.chat(messages, tools=tools, model=model)
        calls = []
        for slot, call in enumerate(getattr(response, "tool_calls", []) or []):
            name = getattr(call, "name", "")
            self.tool_names.append(name)
            calls.append(ToolCall(id=f"c{slot}", name=name,
                                  arguments=getattr(call, "arguments", {}) or {}))
        return AIResponse(text=getattr(response, "text", ""), tool_calls=calls,
                          model=getattr(response, "model", ""))


def _build(root: Path, provider):
    """Assemble the production wiring over a disposable root.

    Everything below the model is real: the real Agent, registry, Executor,
    PermissionManager, VerificationLedger, TaskStore, GoalStore and
    AutonomyRuntime. Only the language model is the thing under test.
    """
    from config.permissions import PermissionManager
    from core.agent import Agent
    from core.events import EventBus
    from memory.conversation import ConversationStore
    from memory.preferences import PreferencesStore
    from memory.project_memory import ProjectMemory
    from tests.conftest import make_settings
    from tools import build_registry

    settings = make_settings(root)
    for directory in (settings.data_dir, settings.memory_dir, settings.logs_dir,
                      settings.backups_dir, settings.scratch_dir, settings.workspace_root):
        directory.mkdir(parents=True, exist_ok=True)

    prefs = PreferencesStore(settings.memory_dir)
    permissions = PermissionManager(settings, prefs)
    # No capability runtime: this file must not touch Blender, the desktop or any
    # native layer. `build_registry` therefore registers no `capability.execute`.
    registry = build_registry(settings)
    counting = _CountingProvider(provider)

    agent = Agent(
        provider=counting, settings=settings, registry=registry,
        permissions=permissions, events=EventBus(), confirmer=None, project=None,
        memory_conversation=ConversationStore(
            settings.memory_dir, settings.max_conversation_turns),
        memory_preferences=prefs,
        memory_project=ProjectMemory(settings.memory_dir, settings.workspace_root),
        model_name=MODEL, model_catalog=[],
    )
    for tool in BLOCKED_TOOLS:
        permissions.session_block(tool)
    return settings, agent, counting, permissions


def _run_goal(root: Path, provider, objective: str, goal_id: str):
    """Create a goal, run it through the real runtime, return what was recorded."""
    from core.autonomy import AutonomyRuntime, SchedulerLimits
    from core.goals import GoalState, GoalStore, create_goal

    settings, agent, counting, _permissions = _build(root, provider)
    goals = GoalStore(settings.sessions_dir)
    goal = create_goal(objective, goal_id=goal_id, required_evidence=1,
                       interval_seconds=60, next_run_at="20260101-000000")
    goals.create(goal)
    goal.transition(GoalState.ACTIVE)

    runtime = AutonomyRuntime(
        agent, goals, task_store=agent._task_store,
        limits=SchedulerLimits.from_settings(settings),
    )
    started = time.monotonic()
    task_id = runtime.submit_goal(goal)
    elapsed = time.monotonic() - started

    fresh = goals.get(goal_id)
    return {
        "settings": settings,
        "task_id": task_id,
        "elapsed": elapsed,
        "tool_names": list(counting.tool_names),
        "goal": fresh,
        "outcome": runtime.observe(fresh, task_id=task_id),
        "tasks": agent._tasks,
    }


@requires_real_model
class TestRealModelAutonomy:
    """One real goal, run once for the class; three properties asserted on it.

    The run is class-scoped on purpose. A real 8B model on CPU costs tens of
    seconds per turn, so three separate runs would be three times the cost for the
    same evidence. Each test below asserts a *different* property of the same run,
    which is what makes the cost acceptable.
    """

    @pytest.fixture(scope="class")
    def outcome(self, tmp_path_factory):
        from config.settings import Settings

        root = tmp_path_factory.mktemp("diablo-real-autonomy")
        settings = Settings()
        provider = _require_model(settings)

        target = root / "real_goal_artifact.txt"
        # The test's own copy of what the file should contain. Asserted after the
        # run; the model is never asked to confirm it.
        objective = (
            f"Create the file {target} whose entire content is exactly this single "
            f"line, followed by a newline: {PAYLOAD}"
        )
        result = _run_goal(root, provider, objective, "real-model-goal-1")
        result["target"] = target
        result["expected_digest"] = hashlib.sha256(
            (PAYLOAD + "\n").encode("utf-8")).hexdigest()
        return result

    def test_the_goal_completed_and_the_artifact_is_observed_independently(self, outcome):
        """Persisted completion **plus** an artifact this test observed itself.

        Neither half is sufficient on its own. The model asserting success proves
        nothing; the file existing proves the work happened but not that the goal
        layer recorded it. Both, and the sha256, are required.
        """
        goal = outcome["goal"]
        tasks = outcome["tasks"]

        # 1. the goal layer reached completion, from persisted state
        assert goal is not None, "the goal did not survive the run"
        assert goal.state.value == "completed", (
            f"a goal that wrote the artifact did not complete: {goal.state.value}")
        assert goal.has_required_evidence, (
            "the goal completed without recording verified evidence")
        assert goal.verified_task_ids, (
            "the goal completed with no verified task behind it")

        # 2. the task layer recorded a verified verdict, from persisted state
        assert tasks.state.value == "completed", (
            f"the task ended {tasks.state.value}")
        assert tasks.verification.value == "passed", (
            f"the task verified {tasks.verification.value}")
        assert tasks.ledger.latest_state.value == "passed"

        # 3. the model actually drove tools, rather than the harness doing the work
        assert outcome["tool_names"], (
            "no tool was dispatched, so nothing about autonomy was exercised")

        # 4. INDEPENDENT OBSERVATION - not the model's claim, this test reading disk
        target = outcome["target"]
        assert target.exists(), (
            f"the goal reports completed but {target.name} does not exist on disk")
        observed = target.read_bytes()
        assert hashlib.sha256(observed).hexdigest() == outcome["expected_digest"], (
            f"{target.name} exists but does not contain the requested content "
            f"(sha256 {hashlib.sha256(observed).hexdigest()[:16]}..., expected "
            f"{outcome['expected_digest'][:16]}...)")

    def test_the_run_stayed_within_its_tool_call_and_time_budgets(self, outcome):
        """A bounded harness must fail loudly if either bound is ever breached."""
        calls = outcome["tool_names"]
        assert calls, "no tool was dispatched, so the budget proves nothing"
        assert len(calls) <= MAX_TOOL_CALLS, (
            f"{len(calls)} tool calls exceeded the harness bound of "
            f"{MAX_TOOL_CALLS}: {calls}")
        assert outcome["elapsed"] <= WALL_TIME_LIMIT_SECONDS, (
            f"one goal took {outcome['elapsed']:.1f}s, over the "
            f"{WALL_TIME_LIMIT_SECONDS:.0f}s budget")


@requires_real_model
class TestRealModelUnverifiedGoal:
    """The negative case, in its own run.

    A goal whose objective cannot be satisfied must not complete. This is the
    invariant Batch 1's Task 0 report singled out as the closest thing the
    repository had to a real-model verification test, and it is the one that a
    model is most likely to try to talk its way past.
    """

    def test_a_goal_the_model_cannot_verify_does_not_report_completed(
            self, tmp_path, monkeypatch):
        from config.settings import Settings

        provider = _require_model(Settings())

        # A goal the model cannot honestly satisfy: the file does not exist and
        # nothing it can do will make the artifact appear, because the only tools
        # left available read. `project.verify` is session-blocked, so there is no
        # verification path to a PASSED verdict.
        missing = tmp_path / "never_created_by_anybody.txt"
        objective = (
            f"Read the file {missing} and report its exact contents."
        )
        result = _run_goal(tmp_path, provider, objective, "real-model-goal-unverified")

        goal = result["goal"]
        assert goal is not None
        assert goal.state.value != "completed", (
            "a goal completed on a request nothing could verify")
        if goal.state.value == "completed":
            # If it somehow did, the evidence claim must be false rather than silent.
            assert not goal.has_required_evidence, (
                "a goal completed while claiming verified evidence for an "
                "unverifiable objective")