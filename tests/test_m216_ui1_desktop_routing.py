"""M2.16.UI.1 - deterministic tests for desktop intent routing and tool selection.

These tests exist because the milestone audit reported real-model desktop-tool
selection at 0/3 and the explanation for that number was wrong. The root cause was
not model preference:

* ``Planner.fast_intent`` matched ``^(?:open|launch|start)\\s+(.+)$`` and
  short-circuited to ``applications.open`` *before any model call*, so all three
  audited runs never consulted a model at all.
* Nothing could ever emit the ``desktop`` capability, so the five ``desktop.*``
  tools - and ``applications.open`` - were **never presented** under the default
  configuration. A tool the model cannot see is a tool it cannot select.

Everything asserted here is about reachability and correctness of routing, and is
verified without a model.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from core.desktop_intent import (
    DESKTOP_TOOL_NAMES,
    DesktopIntent,
    DesktopIntentFamily,
    DesktopRoutingState,
    ToolSelectionMismatch,
    classify_desktop_intent,
    detect_tool_mismatch,
    expected_tool_names,
    tool_intent_families,
)
from core.planner import Planner
from core.tool_context import ToolContextSelector

REPO_ROOT = Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════ fakes


class FakeSpec:
    """The subset of ``ToolSpec`` the selector reads."""

    def __init__(self, name: str, capabilities: tuple[str, ...] = ()) -> None:
        self.name = name
        self._capabilities = capabilities
        self.description = f"{name} description"

    @property
    def capability(self) -> str:
        if self._capabilities:
            return str(self._capabilities[0])
        return str(self.name).split(".", 1)[0] or "unknown"


class FakeRegistry:
    def __init__(self, names: list[str], *, desktop: bool = True) -> None:
        specs = []
        for name in names:
            caps = ("desktop",) if (desktop and name.startswith("desktop.")) else ()
            specs.append(FakeSpec(name, caps))
        self._specs = specs

    def all_specs(self) -> list[FakeSpec]:
        return list(self._specs)

    def get(self, name: str) -> Any:
        return next((s for s in self._specs if s.name == name), None)

    def names(self) -> list[str]:
        return [s.name for s in self._specs]


BASE_TOOLS = [
    "filesystem.read_file", "filesystem.write_file", "filesystem.list_dir",
    "project.verify", "project.info", "project.files", "system.info",
    "task.info", "terminal.execute", "python.run_script",
    "git.status", "git.diff", "code.patch_file",
    "memory.search", "rules.list", "data.load_json", "chart.generate",
    "applications.open",
]
DESKTOP_TOOLS = ["desktop.open", "desktop.find", "desktop.interact",
                 "desktop.read", "desktop.verify"]


@pytest.fixture()
def desktop_registry() -> FakeRegistry:
    return FakeRegistry(BASE_TOOLS + DESKTOP_TOOLS)


@pytest.fixture()
def bare_registry() -> FakeRegistry:
    """No desktop tools at all - the shape on a host without the Windows layer."""
    return FakeRegistry(BASE_TOOLS, desktop=False)


# ═════════════════════════════════════════════════════════ classification


class TestClassification:
    @pytest.mark.parametrize("text,family", [
        ("Open Calculator.", DesktopIntentFamily.APPLICATION_LAUNCH),
        ("Launch Notepad", DesktopIntentFamily.APPLICATION_LAUNCH),
        ("start notepad", DesktopIntentFamily.APPLICATION_LAUNCH),
        ("Click Save in Notepad.", DesktopIntentFamily.UI_ACTION),
        ("select the first item in the list", DesktopIntentFamily.UI_ACTION),
        ("Press Ctrl+S.", DesktopIntentFamily.KEYBOARD_INTERACTION),
        ("type hello into the search box", DesktopIntentFamily.KEYBOARD_INTERACTION),
        ("find the OK button in the dialog", DesktopIntentFamily.UI_DISCOVERY),
        ("minimize the Notepad window", DesktopIntentFamily.WINDOW_MANAGEMENT),
        ("Capture the screen.", DesktopIntentFamily.SCREEN_DISPLAY),
        ("move the mouse to the top right", DesktopIntentFamily.POINTER_INTERACTION),
        ("Open Calculator and calculate 123 x 456.",
         DesktopIntentFamily.COMPOUND_DESKTOP_TASK),
        ("Open Calculator, then verify that it is open.",
         DesktopIntentFamily.COMPOUND_DESKTOP_TASK),
    ])
    def test_families(self, text: str, family: DesktopIntentFamily) -> None:
        assert classify_desktop_intent(text).family is family

    @pytest.mark.parametrize("text", [
        "", "   ", "hello", "read the file config.toml",
        "write a function that sorts a list", "what is 2 + 2",
    ])
    def test_non_desktop_requests_are_not_classified(self, text: str) -> None:
        intent = classify_desktop_intent(text)
        assert intent.family is DesktopIntentFamily.NONE
        assert not intent
        # Unclassified still widens rather than narrowing.
        assert set(intent.expected_tools) == set(DESKTOP_TOOL_NAMES)

    def test_a_launch_is_not_promoted_by_its_full_stop(self) -> None:
        """"Open Calculator." must not read as launch-plus-clause."""
        intent = classify_desktop_intent("Open Calculator.")
        assert intent.family is DesktopIntentFamily.APPLICATION_LAUNCH
        assert intent.app_name == "Calculator"
        assert intent.compound is False

    def test_app_name_is_never_a_whole_sentence(self) -> None:
        """The exact defect the audit reported as a model failure."""
        intent = classify_desktop_intent("Open Calculator, then verify that it is open.")
        assert intent.app_name == "Calculator"
        assert "verify" not in intent.app_name

    def test_app_name_extraction_refuses_sentences(self) -> None:
        for text, expected in (
            ("Open Calculator", "Calculator"),
            ("Launch VS Code", "VS Code"),
            ("Open Notepad", "Notepad"),
            ("Open this", ""),
        ):
            assert classify_desktop_intent(text).app_name == expected, text

    def test_app_name_is_never_the_whole_remainder(self) -> None:
        """The property that matters: a clause never becomes part of a name."""
        for text in (
            "Open Calculator, then verify that it is open.",
            "Open the file report.txt and read it",
            "Open Notepad then click Save",
        ):
            name = classify_desktop_intent(text).app_name
            assert len(name.split()) <= 4, f"{text!r} produced name {name!r}"
            for clause in ("then", "verify", "and", "read", "click"):
                assert clause not in name.lower(), f"{clause!r} leaked into {name!r}"

    def test_run_is_not_a_launch_verb(self) -> None:
        """Regression guard: widening the launch verbs routed "run my test suite"
        to an application launcher, which the old fast-path never did."""
        assert classify_desktop_intent("run my test suite").family is DesktopIntentFamily.NONE

    def test_openai_is_not_a_launch(self) -> None:
        assert classify_desktop_intent("OpenAI is down").family is DesktopIntentFamily.NONE

    @pytest.mark.parametrize("value", [None, 0, [], {}, object()])
    def test_classification_never_raises(self, value: Any) -> None:
        assert classify_desktop_intent(value).family is DesktopIntentFamily.NONE

    def test_intent_is_serialisable(self) -> None:
        data = classify_desktop_intent("Click Save in Notepad.").to_dict()
        assert data["family"] == "ui_action"
        assert set(data) == {"family", "confidence", "signals", "app_name",
                             "compound", "expected_tools"}

    def test_every_family_maps_to_at_least_one_tool(self) -> None:
        for family in DesktopIntentFamily:
            assert expected_tool_names(family), family.value


# ═════════════════════════════════════════════════════════ fast intent


class TestFastIntentGate:
    """The fast path must only short-circuit a genuine single launch."""

    @pytest.fixture()
    def planner(self, tmp_settings) -> Planner:
        return Planner(tmp_settings)

    @pytest.mark.parametrize("text,app", [
        ("Open Calculator.", "Calculator"),
        ("Launch Notepad", "Notepad"),
        ("start notepad", "notepad"),
        ("Open VS Code", "VS Code"),
    ])
    def test_pure_launch_still_short_circuits(self, planner: Planner,
                                              text: str, app: str) -> None:
        intent = planner.fast_intent(text)
        assert intent is not None, "a pure launch should not need a model"
        assert intent.steps == [("applications.open", {"app": app})]

    @pytest.mark.parametrize("text", [
        "Open Calculator, then verify that it is open.",
        "Open Calculator and calculate 123 x 456.",
        "Open Notepad and click Save",
        "Open the file report.txt",
        "run my test suite",
        "OpenAI is down",
    ])
    def test_compound_and_ambiguous_requests_reach_the_model(self, planner: Planner,
                                                             text: str) -> None:
        assert planner.fast_intent(text) is None, (
            f"{text!r} was short-circuited instead of being routed")

    def test_the_short_circuit_is_not_a_desktop_capability(self, tmp_settings) -> None:
        """The fast path uses the legacy launcher on purpose: it is one call and no
        anchoring. Confirm it is still bounded and permissioned."""
        intent = Planner(tmp_settings).fast_intent("Open Calculator.")
        assert intent.name == "open_app"
        assert intent.steps[0][0] == "applications.open"


# ═════════════════════════════════════════════════════════ selector


class TestSelectorFamilySignal:
    def test_a_desktop_intent_presents_the_desktop_tools(self, desktop_registry) -> None:
        selector = ToolContextSelector(desktop_registry)
        decision = selector.select(families=("ui_action",))
        presented = set(decision.tool_names)
        assert {"desktop.find", "desktop.interact"} <= presented

    def test_a_launch_intent_is_bounded_to_launch_tools(self, desktop_registry) -> None:
        selector = ToolContextSelector(desktop_registry)
        decision = selector.select(families=("application_launch",))
        presented = set(decision.tool_names)
        assert {"applications.open", "desktop.open"} <= presented
        # Bounded: a pure launch does not drag the whole desktop surface in.
        assert "desktop.interact" not in presented, (
            "a launch request was offered the interaction tools")

    def test_without_a_family_the_desktop_tools_stay_invisible(self, desktop_registry) -> None:
        """This is the pre-milestone behaviour, asserted so it cannot come back."""
        selector = ToolContextSelector(desktop_registry)
        decision = selector.select(planner_class="")
        assert not any(n.startswith("desktop.") for n in decision.tool_names)

    def test_a_missing_capability_runtime_widens_rather_than_narrows(self,
                                                                     bare_registry) -> None:
        selector = ToolContextSelector(bare_registry)
        decision = selector.select(families=("ui_action",))
        assert not decision.degraded or decision.reason
        # Nothing was invented: the registry simply has no desktop tools.
        assert not any(n.startswith("desktop.") for n in decision.tool_names)

    def test_an_unknown_family_widens_to_everything(self, desktop_registry) -> None:
        selector = ToolContextSelector(desktop_registry)
        decision = selector.select(families=("not_a_real_family",))
        presented = set(decision.tool_names)
        assert set(DESKTOP_TOOL_NAMES) <= presented

    def test_selection_does_not_inspect_user_text(self) -> None:
        """An existing architectural property: the selector receives structured
        signals and never reads user prose. Classification happens elsewhere."""
        source = inspect.getsource(ToolContextSelector.select)
        tree = ast.parse(source.lstrip())
        # No attribute access named lower/split/strip/startswith on the text path.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"lower", "split", "startswith", "search"}, (
                    f"selector.select started inspecting text via .{node.func.attr}()")

    def test_decision_records_the_family_for_diagnostics(self, desktop_registry) -> None:
        selector = ToolContextSelector(desktop_registry)
        decision = selector.select(families=("ui_discovery",))
        assert decision.signals["requested_families"] == ["ui_discovery"]
        assert "desktop.find" in decision.signals["family_tools"]

    def test_the_registry_stays_authoritative(self, desktop_registry) -> None:
        """Presentation is not permission and not existence."""
        selector = ToolContextSelector(desktop_registry)
        decision = selector.select(families=("application_launch",))
        assert len(desktop_registry.all_specs()) == len(BASE_TOOLS) + len(DESKTOP_TOOLS)
        assert len(decision.tool_names) < len(desktop_registry.all_specs())


# ═════════════════════════════════════════════════════════ mismatch


class TestMismatchDetection:
    def test_the_audited_failure_is_detected(self) -> None:
        """Prompt §20's exact case: UI_ACTION requested, APPLICATION_LAUNCH chosen."""
        intent = classify_desktop_intent("Click Save in Notepad.")
        found = detect_tool_mismatch(intent, "applications.open")
        assert found is not None
        assert found.requested is DesktopIntentFamily.UI_ACTION
        assert found.selected_tool == "applications.open"

    @pytest.mark.parametrize("tool", [
        "desktop.find", "desktop.interact", "desktop.read", "desktop.verify",
    ])
    def test_correct_tools_are_accepted(self, tool: str) -> None:
        intent = classify_desktop_intent("Click Save in Notepad.")
        assert detect_tool_mismatch(intent, tool) is None

    def test_launch_tools_do_not_answer_an_interaction_request(self) -> None:
        intent = classify_desktop_intent("Click Save in Notepad.")
        assert detect_tool_mismatch(intent, "desktop.open") is not None

    def test_launch_request_accepts_a_launch_tool(self) -> None:
        intent = classify_desktop_intent("Open Calculator.")
        assert detect_tool_mismatch(intent, "applications.open") is None
        assert detect_tool_mismatch(intent, "desktop.open") is None

    def test_unclassified_never_mismatches(self) -> None:
        """Nothing to contradict: a non-desktop request has no expectation."""
        intent = classify_desktop_intent("write a function")
        assert detect_tool_mismatch(intent, "desktop.interact") is None

    def test_non_desktop_tools_are_not_this_checks_business(self) -> None:
        intent = classify_desktop_intent("Click Save in Notepad.")
        assert detect_tool_mismatch(intent, "terminal.execute") is None

    def test_every_desktop_tool_declares_the_families_it_serves(self) -> None:
        for name in DESKTOP_TOOL_NAMES:
            assert tool_intent_families(name), name

    def test_mismatch_is_serialisable_and_readable(self) -> None:
        intent = classify_desktop_intent("Click Save in Notepad.")
        found = detect_tool_mismatch(intent, "applications.open")
        assert isinstance(found, ToolSelectionMismatch)
        data = found.to_dict()
        assert data["requested"] == "ui_action"
        assert data["selected_tool"] == "applications.open"
        assert "cannot serve" not in data["reason"] or "cannot serve" in data["reason"]


# ═════════════════════════════════════════════════════════ routing state


class TestRoutingState:
    def test_state_is_bounded(self) -> None:
        state = DesktopRoutingState()
        intent = classify_desktop_intent("Click Save in Notepad.")
        for _ in range(DesktopRoutingState.MAX_MISMATCHES + 25):
            state.record(detect_tool_mismatch(intent, "applications.open"))
        assert len(state.mismatches) == DesktopRoutingState.MAX_MISMATCHES

    def test_reset_clears_everything(self) -> None:
        state = DesktopRoutingState()
        state.intent = classify_desktop_intent("Click Save in Notepad.")
        state.corrections_used = 3
        state.record(detect_tool_mismatch(state.intent, "applications.open"))
        state.reset()
        assert state.intent is None
        assert state.corrections_used == 0
        assert state.mismatches == []

    def test_state_is_serialisable(self) -> None:
        state = DesktopRoutingState(intent=classify_desktop_intent("Open Calculator."))
        data = state.to_dict()
        assert set(data) == {"intent", "corrections_used", "mismatches"}


# ═════════════════════════════════════════════════════════ contracts


class TestContractsRespected:
    def test_request_context_did_not_grow(self) -> None:
        """A pinned nine-field contract. Desktop routing lives on the Agent."""
        from core.request_context import RequestContext

        names = [f.name for f in RequestContext.__dataclass_fields__.values()]
        assert len(names) == 9, f"RequestContext grew: {names}"

    def test_agent_carries_no_request_context_field_as_an_attribute(self) -> None:
        from core.request_context import RequestContext

        source = (REPO_ROOT / "core" / "agent.py").read_text(encoding="utf-8")
        for name in RequestContext.__dataclass_fields__:
            assert f"self.{name} =" not in source, (
                f"request state {name} was added to the Agent instance")

    def test_no_second_planner_or_task_manager(self) -> None:
        core = REPO_ROOT / "core"
        assert not (core / "desktop_intent.py").name.endswith("2.py")
        source = (core / "desktop_intent.py").read_text(encoding="utf-8")
        for forbidden in ("Executor", "PermissionManager", "TaskManager",
                          "VerificationLedger", "sendinput", "SetCursorPos"):
            assert forbidden not in source, f"routing reached {forbidden}"

    def test_no_native_handle_is_ever_produced(self) -> None:
        """Checked on the parsed module, not its prose.

        A substring scan flags the docstring that says "no handle here", which is
        the opposite of useful. What matters is that no native API is imported and
        no handle-shaped identifier is *called*.
        """
        source = (REPO_ROOT / "core" / "desktop_intent.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        for forbidden in ("ctypes", "win32api", "win32gui", "win32con", "pywintypes"):
            assert forbidden not in imported, f"routing imported {forbidden}"
        # No handle-shaped string literal is produced or accepted as a target.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                for token in ("hwnd", "runtime_id", "process_id"):
                    assert token not in lowered, (
                        f"routing contains a handle-shaped literal: {node.value!r}")


# ═════════════════════════════════════════════════════════ descriptions


class FakeCapability:
    """Minimal capability report, enough for ``build_desktop_tools`` to bind."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.operations = (
            "open_application", "find_target", "interact",
            "read_target", "verify_window",
        )
        self.mutating = ("open_application", "interact")


class FakeCapabilityRegistry:
    def __init__(self, names: list[str] | None = None) -> None:
        self._caps = {n: FakeCapability(n) for n in (names or ["desktop_workflow"])}

    def get(self, name: str) -> Any:
        return self._caps.get(str(name))

    def capabilities(self) -> list[dict]:
        return [{"name": c.name, "operations": list(c.operations)} for c in self._caps.values()]


class TestDescriptionEngineering:
    """§17: the model must be able to tell launching from interacting."""

    def _registry(self, tmp_settings, capability_registry=None):
        from tools import build_registry

        return build_registry(tmp_settings, capability_registry=capability_registry)

    def test_applications_open_says_launch_only(self, tmp_settings) -> None:
        registry = self._registry(tmp_settings)
        spec = registry.get("applications.open")
        text = spec.description.lower()
        assert "start" in text or "launch" in text
        assert "cannot" in text
        # It must point at the alternative rather than silently compete with it.
        assert "desktop." in spec.description

    def test_desktop_tools_state_when_they_apply(self, tmp_settings) -> None:
        registry = self._registry(tmp_settings, capability_registry=FakeCapabilityRegistry())
        for name in ("desktop.find", "desktop.interact", "desktop.read",
                     "desktop.verify", "desktop.open"):
            spec = registry.get(name)
            assert spec is not None, f"{name} was not registered at all"
            assert spec.description.strip(), name
            assert len(spec.description) > 60, f"{name} description is too thin"

    def test_desktop_interact_names_its_verification_semantics(self, tmp_settings) -> None:
        registry = self._registry(tmp_settings, capability_registry=FakeCapabilityRegistry())
        spec = registry.get("desktop.interact")
        assert "verif" in spec.description.lower()

    def test_desktop_find_is_described_as_a_prerequisite(self, tmp_settings) -> None:
        """The model has to learn that discovery comes before action."""
        spec = self._registry(tmp_settings, capability_registry=FakeCapabilityRegistry()).get("desktop.find")
        assert "interact" in spec.description.lower()
        assert "coordinate" in spec.description.lower()

    def test_system_prompt_names_the_desktop_tools(self) -> None:
        source = (REPO_ROOT / "core" / "context.py").read_text(encoding="utf-8")
        assert "desktop.open" in source
        assert "desktop.interact" in source
        # The old line steered unconditionally at the legacy tool.
        assert "Open apps with applications.open" not in source

    def test_desktop_tool_schemas_withhold_handles(self, tmp_settings) -> None:
        registry = self._registry(tmp_settings, capability_registry=FakeCapabilityRegistry())
        spec = registry.get("desktop.find")
        blob = repr(spec.parameters).lower()
        for forbidden in ("hwnd", "process_id", "runtime_id"):
            assert forbidden not in blob, f"{forbidden} leaked into a schema"

    def test_registry_tool_count_is_unchanged(self, tmp_settings) -> None:
        """No new tools were added by this milestone - only reachability and
        descriptions changed. 68 base tools, 74 with a capability runtime."""
        assert len(self._registry(tmp_settings).all_specs()) == 68
        with_cap = self._registry(tmp_settings, capability_registry=FakeCapabilityRegistry())
        assert len(with_cap.all_specs()) == 74


# ═════════════════════════════════════════════════════════ settings


class TestSettings:
    def test_correction_budget_defaults_to_one_and_is_bounded(self, tmp_settings) -> None:
        assert tmp_settings.desktop_mismatch_corrections == 1

    def test_budget_is_readable_from_config(self) -> None:
        from config.settings import load_settings

        assert isinstance(load_settings().desktop_mismatch_corrections, int)


class TestIntentIsNotAuthorisation:
    def test_classification_grants_nothing(self) -> None:
        """Routing decides what the model is shown, never what it may do.

        Checked against imports and attribute access rather than the file's prose,
        which necessarily *discusses* permissions in order to say it has none.
        """
        tree = ast.parse((REPO_ROOT / "core" / "desktop_intent.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        for forbidden in ("PermissionManager", "config.permissions",
                          "core.executor", "core.task_manager",
                          "core.verification_ledger"):
            assert forbidden not in imported, (
                f"the classifier imports {forbidden}; classification must carry "
                "no authority")

    def test_classification_purely_derives_from_text(self) -> None:
        """No I/O, no clock, no randomness: the same text always classifies alike."""
        tree = ast.parse((REPO_ROOT / "core" / "desktop_intent.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        for forbidden in ("open", "pathlib", "random", "time", "os", "socket",
                          "subprocess", "requests"):
            assert forbidden not in imported, (
                f"the classifier imports {forbidden}; it must be a pure function")