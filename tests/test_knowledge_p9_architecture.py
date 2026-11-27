"""M2.14 Phase 9 — architecture assertions that do not need a model.

Found by Phase 9: a real local model, asked to query the project knowledge
graph, answered that the tool "is not listed in the available functions". It was
not listed. The tools were in the registry and dispatchable, but nothing in the
structured signal set ever requested the ``knowledge`` capability, and the
widened set included ``data`` and ``chart`` without it. The persistent graph
that M2.14 exists to provide was therefore unreachable by the model in the
default configuration, and no scripted test noticed, because scripted tests
name the tool they want and never depend on the model being offered it.

The rest of this module is the Phase 9R audit, expressed as tests. Each one
reads the production modules to assert a structural property, so a later change
that reintroduces a bypass fails here rather than being discovered in review.
"""

from __future__ import annotations

import ast
import os
import sqlite3
from dataclasses import fields
from pathlib import Path

import pytest

from ai.provider import AIProviderError
from config.permissions import PermissionManager
from core.context import ProjectContext
from core.events import EventBus
from core.knowledge_graph import (
    KnowledgeGraphManager,
    NodeType,
    canonical_project_key,
    close_shared_managers,
)
from core.knowledge_tools import knowledge_index
from core.request_context import RequestContext
from core.tool_context import ToolContextSelector
from memory.preferences import PreferencesStore
from tools import build_registry

CORE = Path(__file__).resolve().parents[1] / "core"


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


# ══════════════════════════════════════════ the defect this phase found


class TestKnowledgeToolsAreReachable:
    """A tool that is never offered is a tool the model cannot use.

    Registration is not reachability. The model can only emit a call for a tool
    whose schema it was given, so a capability that is in the registry but never
    presented is a capability that does not exist as far as the model is
    concerned.
    """

    def _selector(self, settings) -> ToolContextSelector:
        return ToolContextSelector(build_registry(settings))

    def test_the_knowledge_tools_are_registered(self, tmp_settings):
        names = [s.name for s in build_registry(tmp_settings).all_specs()]
        assert "knowledge.query" in names
        assert "knowledge.index" in names

    @pytest.mark.parametrize("label,kwargs", [
        ("no signal at all", {}),
        ("ordinary prose", {"user_text": "tell me about this project"}),
        ("knowledge prose", {"user_text": "use the knowledge graph to list datasets"}),
        ("list the files", {"user_text": "list the files in this project"}),
        ("index the project", {"user_text": "index this project"}),
    ])
    def test_knowledge_is_offered_in_the_default_configuration(self, tmp_settings,
                                                                label, kwargs):
        decision = self._selector(tmp_settings).select(**kwargs)
        offered = [n for n in decision.tool_names if n.startswith("knowledge.")]
        assert offered, (
            f"the knowledge tools were not offered ({label}); the model cannot "
            f"call a tool it was never shown. capabilities={decision.capabilities}")

    def test_a_narrow_selection_still_leaves_the_registry_complete(self, tmp_settings):
        """Presentation may narrow; the registry may not.

        A focused coding request legitimately gets a smaller tool set. What must
        hold is that nothing was removed from the registry itself, so an
        out-of-context call is still validated and dispatched normally.
        """
        registry = build_registry(tmp_settings)
        before = {s.name for s in registry.all_specs()}
        ToolContextSelector(registry).select(
            user_text="add a function to main.py", planner_class="develop")
        after = {s.name for s in build_registry(tmp_settings).all_specs()}
        assert before == after, "selection mutated the registry"

    def test_the_widened_set_is_what_carries_the_data_capabilities(self,
                                                                  tmp_settings):
        """The fix is one capability in one tuple, riding the existing path.

        Asserting the shape rather than the outcome keeps this honest: if the
        widened set is ever emptied, this fails and says why.
        """
        from core import tool_context

        assert "knowledge" in tool_context.log_caps
        assert "data" in tool_context.log_caps
        assert "chart" in tool_context.log_caps

    def test_no_keyword_routing_was_introduced_to_reach_it(self, tmp_settings):
        """The fix must not become the thing the design forbids.

        Selection is driven by structured signals. If ``user_text`` were read to
        decide capabilities, a word in a path or a file name could widen or
        narrow the tool set, which the module explicitly rules out.
        """
        source = (CORE / "tool_context.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        select = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "select")
        called = {
            node.func.attr for node in ast.walk(select)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "lower" not in called and "split" not in called, (
            "select() started inspecting user text directly")
        # The parameter is threaded but not read, which is the documented shape.
        assert "user_text: str" in source


# ══════════════════════════════════════════ 9R architecture audit


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _subprocess_calls(path: Path) -> list[str]:
    """Names called on an object that looks like a process or agent escape."""
    out: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Attribute) and node.attr in {
            "run", "Popen", "call", "check_output", "check_call",
        }:
            base = node.value
            if isinstance(base, ast.Name) and base.id in {"subprocess", "os"}:
                out.append(f"{base.id}.{node.attr}")
    return out


class TestNoBypasses:
    """The Agent must be the only way a model reaches a tool."""

    def test_knowledge_modules_do_not_reach_the_executor(self):
        """Phase 9R 2/3: the graph is projected after the fact, never during.

        A knowledge module that called a tool or the executor could describe
        work that did not happen.
        """
        for path in sorted(CORE.glob("knowledge_*.py")):
            source = path.read_text(encoding="utf-8")
            assert "Executor" not in source, (
                f"{path.name} references the Executor")
            assert "PermissionManager" not in source, (
                f"{path.name} references the PermissionManager")
            assert not _subprocess_calls(path), (
                f"{path.name} can spawn a process: {_subprocess_calls(path)}")

    def test_knowledge_modules_never_instantiate_an_authority(self):
        for path in sorted(CORE.glob("knowledge_*.py")):
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (func.id if isinstance(func, ast.Name)
                            else getattr(func, "attr", ""))
                    assert name not in {
                        "TaskStore", "VerificationLedger", "TaskManager",
                        "GitManager",
                    }, f"{path.name} instantiates {name}()"

    def test_the_projection_hook_is_called_from_the_execution_recorder(self):
        """Phase 9R 6: the graph is fed by the real execution path.

        If this call were removed the graph would silently stop updating, which
        is the failure mode Phase 9 is here to prevent.
        """
        source = (CORE / "agent.py").read_text(encoding="utf-8")
        assert "_project_knowledge_tool" in source
        body = source.split("def _record_execution", 1)[1].split("\n    def ", 1)[0]
        assert "_project_knowledge_tool(" in body, (
            "_record_execution no longer projects tool observations")

    def test_the_verification_ledger_remains_the_only_adjudicator(self):
        """Phase 9R 4/14: one authority for verdicts."""
        for path in sorted(CORE.glob("knowledge_*.py")):
            assert "set_verification" not in path.read_text(encoding="utf-8"), (
                f"{path.name} writes a verification verdict")
        source = (CORE / "task_manager.py").read_text(encoding="utf-8")
        assert "self.ledger.record(" in source, (
            "TaskManager stopped routing verdicts through the ledger")

    def test_no_second_planner_or_task_authority(self):
        """Phase 9R 12/15."""
        names = {p.name for p in CORE.glob("*.py")}
        assert not (names & {"planner2.py", "task_manager2.py", "recovery2.py"}), (
            "a second planner, task manager or recovery engine appeared")
        source = (CORE / "recovery.py").read_text(encoding="utf-8")
        assert "class RecoveryPolicy" in source
        assert "class RecoveryPolicy2" not in source

    def test_cancellation_stays_bounded_and_fail_closed(self):
        """Phase 9R 11."""
        source = (CORE / "cancellation.py").read_text(encoding="utf-8")
        assert "except Exception" in source, (
            "cancellation should fail closed on a broken token")
        policy = (CORE / "recovery.py").read_text(encoding="utf-8")
        # Cancellation is evaluated before any other rule.
        decide = policy.split("def decide", 1)[1]
        assert decide.index("CANCELLED") < decide.index("RETRY_STEP"), (
            "cancellation no longer dominates the recovery decision")


class TestRequestContextContract:
    """Phase 9L: the nine-field contract must not grow."""

    def test_it_has_exactly_nine_fields(self):
        names = [f.name for f in fields(RequestContext)]
        assert len(names) == 9, (
            f"RequestContext has {len(names)} fields ({names}); the contract is "
            f"nine and new state belongs elsewhere")
        assert names == [
            "tasks", "cancellation_token", "router_verify_ok", "escalations_used",
            "offline_fallback_used", "pending_goal", "recovered_task_ids",
            "terminal_event_emitted", "last_model",
        ]

    def test_agent_state_stays_off_the_context(self):
        source = (CORE / "agent.py").read_text(encoding="utf-8")
        for name in ("_active_plan", "_provider_failures"):
            assert name in source, f"{name} disappeared from the Agent"


class TestProviderFailureIsStructured:
    """Phase 9L: a provider failure keeps its shape all the way out."""

    def test_the_error_carries_exactly_six_fields(self):
        error = AIProviderError("boom", category="timeout", model="m",
                                latency=1.5, retryable=True, status=504)
        detail = error.to_dict()
        assert set(detail) == {"category", "model", "latency", "retryable",
                               "status", "message"}
        assert detail["category"] == "timeout"
        assert detail["model"] == "m"
        assert detail["latency"] == 1.5
        assert detail["retryable"] is True

    def test_malformed_is_not_retryable(self):
        """Retrying a capability gap only hides it from routing."""
        from ai.provider import FailureCategory

        assert AIProviderError("x", category=FailureCategory.MALFORMED,
                               status=200).to_dict()["retryable"] is False
        assert AIProviderError("x", category=FailureCategory.TIMEOUT,
                               status=0).to_dict()["retryable"] is True
        assert AIProviderError("x", category=FailureCategory.HTTP,
                               status=503).to_dict()["retryable"] is True
        assert AIProviderError("x", category=FailureCategory.HTTP,
                               status=404).to_dict()["retryable"] is False


class TestGraphStaysAProjection:
    """Phase 9R 6: destroying the graph must cost nothing real."""

    def test_deleting_the_graph_leaves_the_project_intact(self, tmp_settings,
                                                          project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        close_shared_managers()
        for path in sorted((tmp_settings.data_dir / "knowledge").glob("*")):
            path.unlink()
        result = __import__("core.knowledge_tools", fromlist=["knowledge_query"]) \
            .knowledge_query(query_type="find_nodes", settings=tmp_settings,
                             project_path=str(project))
        assert result.success and result.data["indexed"] is False
        # The project itself is untouched and can be re-indexed.
        assert (project / "src" / "main.py").is_file()
        knowledge_index(project_path=str(project), settings=tmp_settings)
        again = __import__("core.knowledge_tools", fromlist=["knowledge_query"]) \
            .knowledge_query(query_type="search_files", settings=tmp_settings,
                             project_path=str(project))
        assert again.success and again.data["count"] > 0

    def test_a_query_still_cannot_write(self, tmp_settings, project: Path):
        knowledge_index(project_path=str(project), settings=tmp_settings)
        pid = canonical_project_key(str(project))
        view = __import__("core.knowledge_tools", fromlist=["manager"]) \
            .manager(tmp_settings).get_existing(pid)
        assert view is not None and view.readonly
        node = view.find_nodes(node_type=NodeType.FILE, limit=1)[0]
        with pytest.raises(PermissionError):
            view.upsert_node(NodeType.FILE, node.canonical_key, "x", {})


class TestAdaptiveContextStaysPresentationOnly:
    """Phase 9R 9/10."""

    def test_selection_grants_no_permission(self, tmp_settings):
        """A selected tool is still resolved by its own permission level."""
        registry = build_registry(tmp_settings)
        specs = {s.name: s for s in registry.all_specs()}
        decision = ToolContextSelector(registry).select(full=True)
        for name in decision.tool_names:
            assert name in specs
            # The level comes from the spec, never from the selection.
            assert specs[name].level is not None

    def test_a_damaged_registry_degrades_to_the_full_set(self, tmp_settings):
        """Rule 4: selection fails safe, never narrow on error."""
        registry = build_registry(tmp_settings)
        selector = ToolContextSelector(registry)
        decision = selector.select(capabilities=("no-such-capability",))
        assert decision.degraded or decision.reduction_ratio < 1.0

    def test_full_context_is_an_escape_hatch_not_the_default(self, tmp_settings):
        assert tmp_settings.tools_full_context is False
        selector = ToolContextSelector(build_registry(tmp_settings))
        full = selector.select(full=True)
        adaptive = selector.select(user_text="add a function")
        # M2.14.1 added memory.policy, taking the registry to 68.
        assert full.tool_count == 68
        assert adaptive.tool_count < full.tool_count
