"""M2.14 Phase 8M — a real Agent, restarted, still knows the project.

Phase 8B proved that knowledge outlives a process. That is necessary but not
sufficient: it was shown with the tool functions called directly. What matters to
a user is the thing they actually restart. So both halves of this module drive
the **real** :class:`~core.agent.Agent` through the **real** tool registry and
Executor, in two separate interpreters, and the assertion is about what the
second agent's model was actually shown.

The proof is deliberately not "the graph still has the nodes". It is that the
restarted agent, with no conversation, no memory and no live handle from the
first session, issued a real ``knowledge.query`` tool call and the tool result
handed back to its model named a file that only the first session could have
indexed. Nothing is shared between the halves but the database on disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: A file that exists in the project but is never named in this module's own
#: assertions before session 1 indexes it. Its appearance in session 2's model
#: context is therefore evidence, not coincidence.
PROJECT_FILE = "app.py"
PROJECT_SYMBOL = "run"


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    return env


def _run_child(mode: str, root: str, project: str) -> dict:
    """Run one agent session in a separate interpreter."""
    payload = {"mode": mode, "root": root, "project": project}
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=str(REPO),
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"child {mode} failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    for line in proc.stdout.splitlines():
        if line.startswith("P8RESULT "):
            return json.loads(line[len("P8RESULT "):])
    raise AssertionError(
        f"child {mode} produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Child process: a real Agent, one session
# ──────────────────────────────────────────────────────────────────────────


def _settings(root: str):
    """The same Settings the rest of the suite uses.

    Reusing the suite's own factory keeps the child honest: it configures the
    agent exactly as every other test does, rather than through a private
    reconstruction that could drift from real configuration.
    """
    from tests.conftest import make_settings

    settings = make_settings(Path(root))
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _build_agent(settings, project: Path, responses):
    """A real Agent wired to the real registry, exactly as the app builds it."""
    from ai.provider import AIProvider, AIResponse
    from config.permissions import PermissionManager
    from core.agent import Agent
    from core.context import ProjectContext
    from core.events import EventBus
    from memory.preferences import PreferencesStore
    from tools import build_registry

    class ScriptedProvider(AIProvider):
        """Replays fixed turns and records every message it is shown.

        The recorded messages are the evidence: they are what the model would
        really have seen, so a claim that the restarted agent "knows" the
        project can be checked against them rather than assumed.
        """

        name = "scripted"
        model_label = "scripted/local"
        available = True

        def __init__(self, turns):
            self.turns = list(turns)
            self.seen: list[dict] = []

        def is_available(self):
            return True

        def list_models(self):
            return []

        def chat(self, messages, tools=None, model=None):
            for message in messages or []:
                try:
                    self.seen.append(dict(message))
                except (TypeError, ValueError):
                    continue
            if self.turns:
                return self.turns.pop(0)
            return AIResponse(text="done", tool_calls=[])

    preferences = PreferencesStore(settings.memory_dir)
    provider = ScriptedProvider(responses)
    agent = Agent(
        provider=provider,
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, preferences),
        events=EventBus(),
        project=ProjectContext(name=project.name, path=str(project)),
        memory_preferences=preferences,
    )
    return agent, provider


def _tool_texts(provider) -> list[str]:
    """Every tool result the model was shown, in order."""
    return [
        str(m.get("content") or "")
        for m in provider.seen
        if isinstance(m, dict) and m.get("role") == "tool"
    ]


#: Node types describing the *project*. Everything an agent session creates
#: about *itself* — its task, plan, verification, checkpoint — is deliberately
#: excluded from this set, because a session legitimately records its own
#: lifecycle. Asserting that two sessions produced byte-identical graphs would
#: therefore be asserting that the agent did no work, which is the opposite of
#: the property under test.
PROJECT_NODE_TYPES = frozenset({
    "project", "file", "directory", "code_symbol", "test",
    "dataset", "analysis", "chart", "artifact",
})


def _split_identity(graph) -> tuple[dict, dict]:
    """``(project identity, session identity)`` for a graph."""
    project: dict[str, str] = {}
    session: dict[str, str] = {}
    for node in graph.find_nodes(limit=10000, include_stale=True):
        target = project if node.node_type.value in PROJECT_NODE_TYPES else session
        target[node.canonical_key] = node.node_id
    return project, session


def _response(*calls, text=""):
    from ai.provider import AIResponse, ToolCall

    return AIResponse(text=text, tool_calls=list(calls))


def _call(name, **args):
    from ai.provider import ToolCall

    return ToolCall(name=name, arguments=args)


def _session_one(root: str, project_path: str) -> dict:
    """A first real agent session: index the project and inspect real data."""
    settings = _settings(root)
    project = Path(project_path)

    agent, provider = _build_agent(settings, project, [
        _response(_call("knowledge.index", project_path=str(project))),
        _response(_call("data.inspect", path=str(project / "sales.csv"))),
        _response(text="indexed"),
    ])
    reply = agent.respond("index this project and look at sales.csv")

    from core.knowledge_graph import (
        KnowledgeGraphManager,
        canonical_project_key,
        close_shared_managers,
    )
    from core.knowledge_projection import knowledge_dir

    mgr = KnowledgeGraphManager(knowledge_dir(settings))
    try:
        graph = mgr.get_graph(canonical_project_key(str(project)))
        identity, session = _split_identity(graph)
    finally:
        mgr.close_all()
        close_shared_managers()

    return {
        "reply": reply,
        "tool_texts": _tool_texts(provider),
        "identity": identity,
        "session_nodes": session,
    }


def _session_two(root: str, project_path: str) -> dict:
    """A second real agent session in a fresh interpreter, with no memory of
    the first beyond what is on disk."""
    settings = _settings(root)
    project = Path(project_path)

    agent, provider = _build_agent(settings, project, [
        _response(_call("knowledge.query", query_type="search_files",
                        project_path=str(project), limit=50)),
        _response(text="here are the files"),
    ])
    reply = agent.respond("what files are in this project?")

    from core.knowledge_graph import (
        KnowledgeGraphManager,
        canonical_project_key,
        close_shared_managers,
    )
    from core.knowledge_projection import knowledge_dir

    mgr = KnowledgeGraphManager(knowledge_dir(settings))
    try:
        graph = mgr.get_graph(canonical_project_key(str(project)))
        identity, session = _split_identity(graph)
    finally:
        mgr.close_all()
        close_shared_managers()

    return {
        "reply": reply,
        "tool_texts": _tool_texts(provider),
        "identity": identity,
        "session_nodes": session,
    }


# ──────────────────────────────────────────────────────────────────────────
# The scenario
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "restart-project"
    root.mkdir()
    (root / PROJECT_FILE).write_text(
        f"def {PROJECT_SYMBOL}():\n    return 1\n", encoding="utf-8")
    (root / "sales.csv").write_text(
        "product,amount\nA,10\nB,20\nA,30\n", encoding="utf-8")
    return root


def _both_sessions(tmp_path: Path, project: Path) -> tuple[dict, dict]:
    first = _run_child("session_one", str(tmp_path), str(project))
    second = _run_child("session_two", str(tmp_path), str(project))
    return first, second


class TestRealAgentRestart:
    def test_session_one_actually_indexed(self, tmp_path: Path, project: Path):
        """Guard: if session 1 did no work, every other test here is vacuous."""
        first = _run_child("session_one", str(tmp_path), str(project))
        assert first["identity"], "session 1 recorded no knowledge"
        assert any("index" in t for t in first["tool_texts"]), (
            f"the index tool never ran: {first['tool_texts']}")

    def test_restarted_agent_sees_the_previous_sessions_files(
            self, tmp_path: Path, project: Path):
        """The real point of the phase, stated as one assertion.

        The second agent is a different object in a different interpreter with
        an empty conversation. The only way it can name a file is if session 1
        left knowledge on disk and session 2 read it back.
        """
        _run_child("session_one", str(tmp_path), str(project))
        second = _run_child("session_two", str(tmp_path), str(project))

        assert second["tool_texts"], "the restarted agent never got a tool result"
        seen = "\n".join(second["tool_texts"])
        assert PROJECT_FILE in seen, (
            "the restarted agent was not shown the indexed file:\n" + seen)

    def test_restarted_agent_sees_symbols_recorded_before_the_restart(
            self, tmp_path: Path, project: Path):
        _run_child("session_one", str(tmp_path), str(project))
        second = _run_child("session_two", str(tmp_path), str(project))
        seen = "\n".join(second["tool_texts"])
        assert PROJECT_SYMBOL in seen, (
            "live code knowledge did not survive the restart:\n" + seen)

    def test_restarted_agent_can_query_symbols(self, tmp_path: Path, project: Path):
        _run_child("session_one", str(tmp_path), str(project))
        second = _run_child("session_two_symbols", str(tmp_path), str(project))
        seen = "\n".join(second["tool_texts"])
        assert PROJECT_SYMBOL in seen, (
            "a symbol query after restart returned nothing:\n" + seen)

    def test_identity_is_identical_across_the_restart(self, tmp_path: Path,
                                                      project: Path):
        first, second = _both_sessions(tmp_path, project)
        assert first["identity"], "session 1 recorded no knowledge"
        for key, node_id in first["identity"].items():
            assert second["identity"].get(key) == node_id, (
                f"{key} changed identity across a real agent restart")

    def test_a_query_only_restart_adds_no_knowledge(self, tmp_path: Path,
                                                    project: Path):
        """Reading after a restart must not mutate what is known about the project.

        Scoped to project knowledge deliberately. Each agent session records its
        own task, and that is the agent doing its job, not a read mutating
        state. What must hold is that asking a question changes nothing about
        the project itself.
        """
        _run_child("session_one", str(tmp_path), str(project))
        second = _run_child("session_two", str(tmp_path), str(project))
        again = _run_child("session_two", str(tmp_path), str(project))
        assert second["identity"], "session 1 recorded no project knowledge"
        assert second["identity"] == again["identity"], (
            "a read-only agent session changed the stored project knowledge")

    def test_two_restarts_keep_answering(self, tmp_path: Path, project: Path):
        """Persistence is not a one-shot: the third session must work too."""
        _run_child("session_one", str(tmp_path), str(project))
        for _ in range(2):
            later = _run_child("session_two", str(tmp_path), str(project))
            assert PROJECT_FILE in "\n".join(later["tool_texts"])

    def test_a_fresh_agent_with_no_prior_session_reports_nothing(
            self, tmp_path: Path, project: Path):
        """The negative control.

        Without session 1 there is nothing to remember, and the agent must say
        so rather than inventing a plausible file list.
        """
        second = _run_child("session_two", str(tmp_path), str(project))
        seen = "\n".join(second["tool_texts"])
        assert PROJECT_FILE not in seen, (
            "a project that was never indexed produced file knowledge:\n" + seen)


def _session_two_symbols(root: str, project_path: str) -> dict:
    """A restart that asks about symbols rather than files."""
    settings = _settings(root)
    project = Path(project_path)

    agent, provider = _build_agent(settings, project, [
        _response(_call("knowledge.query", query_type="find_nodes",
                        project_path=str(project), node_type="code_symbol",
                        limit=50)),
        _response(text="here are the symbols"),
    ])
    reply = agent.respond("what functions does this project define?")

    from core.knowledge_graph import close_shared_managers

    close_shared_managers()
    return {"reply": reply, "tool_texts": _tool_texts(provider), "identity": {}}


# ──────────────────────────────────────────────────────────────────────────
# Child entry point
# ──────────────────────────────────────────────────────────────────────────


def _main() -> int:
    if "--child" not in sys.argv:
        return 0
    request = json.loads(sys.stdin.read())
    mode = request["mode"]
    root = request["root"]
    project = request["project"]
    if mode == "session_one":
        result = _session_one(root, project)
    elif mode == "session_two":
        result = _session_two(root, project)
    elif mode == "session_two_symbols":
        result = _session_two_symbols(root, project)
    else:
        raise SystemExit(f"unknown mode {mode}")
    sys.stdout.write("P8RESULT " + json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
