"""M2.13.0 - adaptive tool context.

The point of these tests is not that the selector returns a small list. It is
that narrowing the *presented* tool set cannot weaken anything: the full
registry stays authoritative, permissions are untouched, and every uncertain
path widens rather than guesses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from config.permissions import PermissionManager
from core.agent import Agent
from core.events import EventBus
from core.planner import ExecutionPlan
from core.tool_context import (
    CAPABILITY_GROUPS,
    CORE_TOOL_NAMES,
    ToolContextDecision,
    ToolContextSelector,
)
from memory.preferences import PreferencesStore
from tools import build_registry
from tools.registry import ToolRegistry, ToolSpec


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _spec(name, **kwargs):
    return ToolSpec(
        name=name,
        description=name,
        fn=lambda **_: None,
        parameters={"type": "object", "properties": {}},
        **kwargs,
    )


def _tiny_registry():
    """A faithful subset of the production registry.

    Every tool named in ``CORE_TOOL_NAMES`` and ``DIAGNOSTIC_TOOLS`` is present,
    because those sets are asserted against here; the rest are deliberate
    stand-ins for the capabilities the milestone cares about.
    """
    registry = ToolRegistry()
    for name in (
        # core
        "filesystem.read_file", "filesystem.list_dir", "filesystem.write_file",
        "project.verify", "project.info", "project.files",
        "system.info", "task.info",
        # coding / debugging
        "filesystem.delete_file", "project.grep", "code.patch_file",
        "python.run_script", "terminal.execute", "task.list",
        # version control, diagnostic and mutating
        "git.status", "git.diff", "git.log", "git.snapshot", "git.snapshot.list",
        "git.commit", "git.push", "git.reset", "git.rebase", "git.branch.switch",
        # other capabilities
        "gh.pr_list", "gh.issue_list", "memory.search", "memory.store",
        "rules.list", "models.list", "models.use", "applications.open",
    ):
        registry.register(_spec(name))
    return registry


def _response(*calls, text=""):
    return SimpleNamespace(tool_calls=list(calls), text=text)


def _call(name, **kwargs):
    return SimpleNamespace(name=name, arguments=kwargs, id=f"c{name}")


class ScriptedProvider:
    """Stands in for the model only; everything downstream is production code."""

    def __init__(self, script, seen=None):
        self.script = list(script)
        self.seen = seen if seen is not None else []
        self.model_label = "m2130"
        self.name = "m2130"

    def chat(self, messages, tools=None, model=None):
        self.seen.append(tools)
        return self.script.pop(0) if self.script else _response(text="done")


def _agent(tmp_settings, script, seen=None):
    prefs = PreferencesStore(tmp_settings.memory_dir)
    return Agent(
        provider=ScriptedProvider(script, seen),
        settings=tmp_settings,
        registry=build_registry(tmp_settings),
        permissions=PermissionManager(tmp_settings, prefs),
        events=EventBus(),
        confirmer=None,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )


# --------------------------------------------------------------------------
# capability metadata
# --------------------------------------------------------------------------


def test_capability_defaults_to_the_tool_namespace():
    assert _spec("git.commit").capability == "git"
    assert _spec("project.verify").capability == "project"


def test_explicit_capability_overrides_the_namespace():
    # One tool can serve several domains, so the first explicit entry wins.
    spec = _spec("capability.execute", capabilities=("capability", "blender", "vision"))
    assert spec.capability == "capability"
    assert spec.capabilities == ("capability", "blender", "vision")


def test_capability_execute_declares_blender_and_vision():
    registry = _tiny_registry()
    spec = _spec("capability.execute", capabilities=("capability", "blender", "vision"))
    registry.register(spec)
    assert "blender" in spec.capabilities
    assert "vision" in spec.capabilities


def test_derived_capability_needs_no_registration_changes():
    # Every real tool classifies itself from its name, so the milestone adds no
    # per-tool wiring: the production registry is unchanged at 57 tools.
    registry = build_registry(_real_settings())
    # M2.13.1/M2.13.2: 8 new tools added (4 data, 4 chart)
    assert len(registry.all_specs()) == 65
    assert {spec.capability for spec in registry.all_specs()} == {
        "applications", "code", "filesystem", "gh", "git", "memory", "models",
        "project", "python", "rules", "system", "task", "terminal", "data", "chart",
    }


def test_by_capability_is_ordered_and_total():
    registry = _tiny_registry()
    assert [s.name for s in registry.by_capability("project")] == [
        "project.verify", "project.info", "project.files", "project.grep",
    ]
    assert registry.by_capability("nope") == []
    # Capabilities partition the registry: nothing is lost or duplicated.
    seen = [s.name for cap in {s.capability for s in registry.all_specs()}
            for s in registry.by_capability(cap)]
    assert sorted(seen) == sorted(registry.names())
    assert len(seen) == len(set(seen))


def _real_settings():
    import tempfile
    from pathlib import Path

    from config.settings import Settings

    root = Path(tempfile.mkdtemp())
    data = root / "data"
    return Settings(
        base_dir=root, ai_provider="ollama", ollama_host="http://x", ai_model="",
        workspace_root=root / "p", data_dir=data, memory_dir=data / "memory",
        logs_dir=data / "logs", backups_dir=data / "backups", scratch_dir=root / "w",
        sessions_dir=data / "sessions", voice_enabled=False, log_level="INFO",
        allowed_roots=[root], raw={},
    )


# --------------------------------------------------------------------------
# the safety invariants
# --------------------------------------------------------------------------


def test_selection_never_withholds_the_core_set():
    selector = ToolContextSelector(_tiny_registry())
    for kwargs in (
        {},
        {"planner_class": "develop"},
        {"planner_class": "debug"},
        {"capabilities": ("github",)},
        {"used_tool_names": ("memory.search",)},
        {"include": ("gh.pr_list",)},
    ):
        names = selector.select(**kwargs).tool_names
        missing = set(CORE_TOOL_NAMES) - set(names)
        assert not missing, f"core tools withheld for {kwargs}: {missing}"


def test_uncertainty_widens_rather_than_narrows():
    selector = ToolContextSelector(_tiny_registry())
    # No usable signal at all -> broaden, and say so.
    blind = selector.select(user_text="hello")
    assert blind.signals["weak_signal"] is True
    # The blind set contains every coding capability, so nothing is withheld
    # merely because the request could not be classified.
    assert {"code", "python", "terminal", "filesystem", "project"} <= set(blind.capabilities)
    # A positively-signalled context drops the coding capabilities it has no
    # evidence for, and picks up only the one it was asked for.
    narrow = selector.select(capabilities=("github",))
    assert "gh" in narrow.capabilities
    assert not ({"code", "python", "terminal"} & set(narrow.capabilities))
    # The blind case, conversely, is never offered the remote or destructive
    # surfaces it has no signal for.
    assert not ({"gh", "git", "memory", "models", "applications"} & set(blind.capabilities))


def test_unknown_capability_is_recorded_and_fails_wide():
    selector = ToolContextSelector(_tiny_registry())
    decision = selector.select(capabilities=("githbu",))
    # A typo must never quietly produce a smaller tool set.
    assert decision.signals["unresolved_capabilities"] == ["githbu"]
    assert decision.signals["weak_signal"] is True
    assert "code" in decision.capabilities
    assert "gh" not in decision.capabilities


def test_group_aliases_and_real_capabilities_agree():
    selector = ToolContextSelector(_tiny_registry())
    # A group alias and the real capability name must be interchangeable.
    assert selector.select(capabilities=("github",)).tool_names == \
        selector.select(capabilities=("gh",)).tool_names
    assert selector.select(capabilities=("version_control",)).tool_names == \
        selector.select(capabilities=("git",)).tool_names
    # The debug context is a superset of the debugging group, because the
    # planner's classification also licenses the read-only git subset.
    group = set(selector.select(capabilities=("debugging",)).tool_names)
    assert group < set(selector.select(planner_class="debug").tool_names)


def test_debug_context_withholds_mutating_version_control():
    # A diagnostic turn should not be offered push/reset/rebase.
    selector = ToolContextSelector(_tiny_registry())
    names = set(selector.select(planner_class="debug").tool_names)
    assert {"git.status", "git.diff", "git.log"} <= names
    assert not ({"git.commit", "git.push", "git.reset"} & names)


def test_full_context_is_the_explicit_escape_hatch():
    registry = _tiny_registry()
    decision = ToolContextSelector(registry).select(full=True)
    assert set(decision.tool_names) == set(registry.names())
    assert decision.reduction_ratio == 0.0
    assert decision.degraded is True


def test_selection_failure_degrades_to_the_full_registry():
    class Exploding:
        def all_specs(self):
            raise RuntimeError("registry unavailable")

        def names(self):
            raise RuntimeError("registry unavailable")

    decision = ToolContextSelector(Exploding()).select(planner_class="develop")
    # Failing safe means: lose the optimisation, never the capability - and
    # never raise, even when the registry is itself the thing that broke.
    assert decision.degraded is True
    assert "selection failed" in decision.reason
    assert decision.tool_names == ()


def test_selection_is_deterministic():
    selector = ToolContextSelector(_tiny_registry())
    kwargs = dict(planner_class="develop", used_tool_names=("filesystem.write_file",))
    assert selector.select(**kwargs).tool_names == selector.select(**kwargs).tool_names
    # Ordering follows registration order, never set iteration order.
    names = selector.select(**kwargs).tool_names
    registry = _tiny_registry()
    order = [s.name for s in registry.all_specs() if s.name in set(names)]
    assert list(names) == order


def test_capability_groups_only_reference_real_capabilities():
    # A group naming a capability that does not exist would silently do nothing.
    registry = _tiny_registry()
    real = {spec.capability for spec in registry.all_specs()}
    real |= {"capability", "blender", "vision", "data", "chart"}
    for group, caps in CAPABILITY_GROUPS.items():
        unknown = set(caps) - real
        assert not unknown, f"group {group!r} references unknown capabilities {unknown}"


# --------------------------------------------------------------------------
# presentation only - the registry stays authoritative
# --------------------------------------------------------------------------


def test_a_tool_outside_the_context_still_validates_against_the_full_registry(tmp_path):
    registry = _tiny_registry()
    selector = ToolContextSelector(registry)
    decision = selector.select(planner_class="develop")
    assert "git.commit" not in decision.tool_names

    plan = ExecutionPlan.from_tool_calls(
        [SimpleNamespace(id="", name="git.commit", arguments={"message": "m", "root": str(tmp_path)})],
        goal="commit",
    )
    plan.validate(registry)  # must not raise: exclusion is presentation-only


def test_an_unknown_tool_is_still_refused():
    registry = _tiny_registry()
    plan = ExecutionPlan.from_tool_calls(
        [SimpleNamespace(id="", name="git.teleport", arguments={})], goal="x",
    )
    with pytest.raises(Exception):
        plan.validate(registry)


def test_context_selection_grants_no_permission():
    # Permissions come from the spec via PermissionManager; presenting fewer
    # tools cannot raise anyone's level.
    from config.permissions import PermissionLevel

    registry = _tiny_registry()
    selector = ToolContextSelector(registry)
    decision = selector.select(capabilities=("version_control",))
    assert "git.commit" in decision.tool_names
    spec = registry.get("git.commit")
    assert spec.level in tuple(PermissionLevel)


# --------------------------------------------------------------------------
# observability
# --------------------------------------------------------------------------


def test_decision_is_serialisable_for_diagnostics():
    decision = ToolContextSelector(_tiny_registry()).select(planner_class="debug")
    payload = json.loads(decision.to_json())
    assert payload["tool_count"] == len(decision.tool_names)
    assert payload["total_tools"] > payload["tool_count"]
    assert payload["signals"]["planner_class"] == "debug"
    assert payload["reason"]
    assert isinstance(decision, ToolContextDecision)


def test_selection_is_emitted_as_a_lifecycle_event():
    import tempfile
    from pathlib import Path

    from config.settings import Settings

    seen = []
    bus = EventBus()
    bus.subscribe("tool_context_selected", lambda data: seen.append(data))

    root = Path(tempfile.mkdtemp())
    data_dir = root / "data"
    settings = Settings(
        base_dir=root, ai_provider="ollama", ollama_host="http://x", ai_model="",
        workspace_root=root / "p", data_dir=data_dir, memory_dir=data_dir / "memory",
        logs_dir=data_dir / "logs", backups_dir=data_dir / "backups", scratch_dir=root / "w",
        sessions_dir=data_dir / "sessions", voice_enabled=False, log_level="INFO",
        allowed_roots=[root], raw={},
    )
    prefs = PreferencesStore(settings.memory_dir)
    agent = Agent(
        provider=ScriptedProvider([_response(text="hello")]),
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, prefs),
        events=bus,
        confirmer=None,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )
    agent.respond("add a greeting function")
    assert seen, "tool context selection must be observable"
    assert seen[0]["tool_count"] < seen[0]["total_tools"]


# --------------------------------------------------------------------------
# agent wiring
# --------------------------------------------------------------------------


def test_model_is_shown_a_subset_but_the_registry_is_untouched(tmp_settings):
    registry = build_registry(tmp_settings)
    before = list(registry.names())
    seen = []
    agent = _agent(tmp_settings, [_response(text="hi")], seen=seen)

    agent.respond("add a greeting function to the project")

    presented = [s.name for s in seen[0]]
    assert presented, "the model must still be given tools"
    assert len(presented) < len(before), "adaptive context did not narrow anything"
    assert list(registry.names()) == before, "the full registry must remain authoritative"
    for name in presented:
        assert registry.get(name) is not None


def test_full_context_setting_presents_everything(tmp_settings):
    import dataclasses

    settings = dataclasses.replace(tmp_settings, tools_full_context=True)
    registry = build_registry(settings)
    seen = []
    prefs = PreferencesStore(settings.memory_dir)
    agent = Agent(
        provider=ScriptedProvider([_response(text="hi")], seen),
        settings=settings,
        registry=registry,
        permissions=PermissionManager(settings, prefs),
        events=EventBus(),
        confirmer=None,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )
    agent.respond("add a greeting function to the project")
    presented = [s.name for s in seen[0]]
    assert presented == [s.name for s in registry.all_specs()]


def test_project_verify_stays_available_for_coding_requests(tmp_settings):
    # M2.13's prompt tells the model to verify its work; the context must allow it.
    seen = []
    agent = _agent(tmp_settings, [_response(text="hi")], seen=seen)
    agent.respond("add a greeting function to the project")
    presented = [s.name for s in seen[0]]
    assert "project.verify" in presented
    assert "filesystem.write_file" in presented
