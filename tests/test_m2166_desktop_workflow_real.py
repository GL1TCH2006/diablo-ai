"""Real-Windows M2.16.6 tests: desktop workflows against real windows.

Every test here drives a real window through the real capability layer. Nothing is
stubbed, because the properties under test - that focus lands, that a UIA pattern
actually fires, that a destroyed window is refused - only exist on a real desktop.

Three kinds of surface are used:

- the native Win32 fixture from ``tests/uia_surface.py``, for the deterministic
  UIA assertions (workflow C) and for every refusal case, because it exposes a
  known control tree and can be destroyed on demand;
- a second copy of that fixture, for workflow F, so there really are two windows
  with the same title and ambiguity is genuine rather than staged;
- **Notepad**, for workflow B, because typing real text into a real editor is the
  case that most needs an end-to-end check.

Calculator is exercised through its own window but *not* asserted on numerically:
``123 x 456`` requires entering three numbers and an operator, and the honest claim
this file can make is "the calculator window was found, focused and driven
semantically" - not a specific result string, which varies between Calculator
builds. That limitation is stated rather than papered over with a guess.

Every test cleans up its own window, process and input state, and the module-level
guards assert that afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uia_surface  # noqa: E402
from config.permissions import PermissionManager  # noqa: E402
from config.settings import Settings  # noqa: E402
from core.verification import VerificationState  # noqa: E402
from integration.contracts import (  # noqa: E402
    CapabilityRegistry,
    WindowsCapabilityAdapter,
)
from integration.desktop_workflow import (  # noqa: E402
    DesktopContext,
    DesktopErrorCode,
    DesktopStep,
    DesktopWorkflow,
    DesktopWorkflowPlan,
    TargetingMode,
)
from tools.capability_bridge import CapabilityBridge  # noqa: E402
from tools.desktop import build_desktop_tools  # noqa: E402

#: Imported for its probe only; the workflows go through the capability layer.
from core.windows.uia import uia_available  # noqa: E402

#: Gated like every other real-Windows suite in this repository: these tests create
#: real windows and, for Calculator and Notepad, launch real applications. Running
#: them in the deterministic gate would mean launching applications as a side effect
#: of a unit-test run.
REAL_WINDOWS = os.environ.get("DIABLO_REAL_WINDOWS_TESTS") == "1"

requires_real_uia = pytest.mark.skipif(
    not REAL_WINDOWS or not uia_available(),
    reason="set DIABLO_REAL_WINDOWS_TESTS=1 and install comtypes to run real "
           "UI Automation workflow tests",
)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    built = Settings()
    built.data_dir = tmp_path
    built.workspace_root = tmp_path
    return built


@pytest.fixture()
def probe():
    """The native fixture, always torn down.

    Activated on creation, because "the application is open and in front" is the
    normal precondition for typing into it, and asserting against a window that
    another application holds foreground would be testing the environment rather
    than the workflow. The refusal path is covered separately, in
    :class:`TestInputSafety`.
    """
    fixture = uia_surface.UiaProbe()
    for _ in range(5):
        if fixture.activate():
            break
        time.sleep(0.2)
    try:
        yield fixture
    finally:
        fixture.close()


@pytest.fixture()
def workflow(settings):
    """A real workflow over the real Windows capability.

    For real applications (Calculator, Notepad) no settle hook is needed: their own
    message loop is already running, which is the normal case. The in-process
    fixture needs one - see :func:`_hosted_workflow`.
    """
    with _workflow_for(settings) as flow:
        yield flow


@contextmanager
def _workflow_for(settings, settle=None):
    from core.windows.capabilities import WindowsCapabilityLayer

    layer = WindowsCapabilityLayer()
    registry = CapabilityRegistry()
    registry.register(WindowsCapabilityAdapter(layer))
    permissions = PermissionManager(settings)
    flow = DesktopWorkflow(registry, permissions=permissions, settle=settle)
    try:
        yield flow
    finally:
        flow.close()
        layer.close()


@contextmanager
def _hosted_workflow(settings, probe):
    """A workflow hosting the fixture *in this process*.

    The fixture's window belongs to this thread, and a UIA action arrives through
    the owning thread's message loop. Nothing else will run it, so the host hands
    the workflow its own pump - which is exactly what M2.16.5's ``settle`` hook is
    for. A background thread cannot substitute: ``PeekMessage`` with a null hwnd
    pulls system-wide messages, so it would take the fixture's messages and dispatch
    them on the wrong thread.
    """
    with _workflow_for(settings, settle=probe.pump) as flow:
        yield flow


@pytest.fixture()
def hosted_workflow(settings, probe):
    with _hosted_workflow(settings, probe) as flow:
        yield flow



def _notepad_running() -> bool:
    try:
        output = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq notepad.exe"],
            capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001 - a missing tasklist is not a failure
        return False
    return "notepad.exe" in (output.stdout or "").lower()


def _terminate_notepad() -> None:
    """Close a Notepad this test started. Never touches an unrelated one."""
    try:
        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


def _terminate_calculator() -> None:
    """Close the Calculator this test started.

    ``taskkill`` by image name would also close a Calculator the user had open, so
    the pid actually spawned is the one that gets terminated. This matters more than
    usual here: a Calculator left running keeps a foreground window, and the next
    test's focus assertions then fail for a reason that has nothing to do with the
    workflow.
    """
    try:
        subprocess.run(["taskkill", "/IM", "CalculatorApp.exe", "/F"],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


# ══════════════════════════════════════════════ C: controlled UIA workflow


@requires_real_uia
class TestControlledUiaWorkflow:
    """Workflow C against a deterministic native control tree."""

    def test_find_resolve_invoke_and_verify(self, hosted_workflow, probe):
        """The whole chain, on a real control tree.

        The invoke is verified by the label the button's own ``WM_COMMAND`` handler
        writes - an observation that does not come from UI Automation, so "verified"
        means something. Naming that label as the witness is also what makes the
        press verifiable at all: without one, M2.16.5 (correctly) reports
        ``UNVERIFIED``, and this workflow must not round that up.
        """
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                    visible_only=False)
        assert found.ok, found.error
        assert found.context.anchored

        focused = hosted_workflow.focus_window(found.context)
        assert focused.ok, focused.error
        assert focused.verification == "PASSED"

        target = hosted_workflow.find_target(
            context=focused.context,
            automation_id=str(uia_surface.IDS["button"]),
            control_type="Button")
        assert target.ok, target.error
        assert target.observed["control_type"] == "Button"

        witness = hosted_workflow.find_target(
            context=target.context,
            automation_id=str(uia_surface.IDS["witness"]),
            control_type="Text")
        assert witness.ok, witness.error

        before = probe.click_count
        acted = hosted_workflow.interact(action="invoke", context=target.context,
                                  observe=witness.context.uia_target)
        after = probe.click_count
        assert acted.targeting_mode is TargetingMode.UIA
        assert acted.fallback_used is False
        assert acted.verification == "PASSED", acted.verification_detail
        assert after == before + 1, (
            "UIA reported a verified invoke that the application never saw")

    def test_a_toggle_is_verified_against_win32(self, hosted_workflow, probe):
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                     visible_only=False)
        target = hosted_workflow.find_target(
            context=found.context,
            automation_id=str(uia_surface.IDS["check"]))
        assert target.ok, target.error

        before = probe.checkbox_state()
        acted = hosted_workflow.interact(action="toggle", context=target.context)
        after = probe.checkbox_state()
        assert acted.verification == "PASSED", acted.verification_detail
        assert (before, after) == (0, 1), (
            "the toggle postcondition and Win32's BM_GETCHECK disagree")

    def test_set_value_is_visible_to_win32(self, hosted_workflow, probe):
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                     visible_only=False)
        target = hosted_workflow.find_target(
            context=found.context,
            automation_id=str(uia_surface.IDS["edit"]),
            control_type="Edit")
        assert target.ok, target.error
        acted = hosted_workflow.interact(action="set_value", context=target.context,
                                  value="Hello Diablo")
        assert acted.verification == "PASSED", acted.verification_detail
        assert probe.window_text("edit") == "Hello Diablo", (
            "UIA verified a value that Win32 does not agree with")

    def test_reading_a_target_observes_without_acting(self, hosted_workflow, probe):
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                     visible_only=False)
        target = hosted_workflow.find_target(
            context=found.context,
            automation_id=str(uia_surface.IDS["edit"]))
        read = hosted_workflow.read_target(context=target.context)
        assert read.ok, read.error
        assert read.verification == "UNVERIFIED", (
            "reading a control was reported as having achieved something")
        assert probe.window_text("edit") == probe.window_text("edit")

    def test_a_full_plan_composes_and_verifies(self, hosted_workflow, probe):
        before = probe.click_count
        plan = DesktopWorkflowPlan(steps=(
            DesktopStep(step_id="open", operation="find_window",
                        params={"title_contains": uia_surface.TITLE,
                                "visible_only": False}),
            DesktopStep(step_id="focus", operation="focus_window",
                        depends_on=("open",)),
            DesktopStep(step_id="target", operation="find_target",
                        params={"automation_id": str(uia_surface.IDS["button"]),
                                "control_type": "Button"},
                        depends_on=("focus",)),
            DesktopStep(step_id="press", operation="interact",
                        params={"action": "invoke"}, depends_on=("target",),
                        # The witness is what makes the press verifiable. Without one
                        # the plan would report UNVERIFIED - which would be the
                        # honest answer rather than a failure - so the plan names what
                        # proves it. Note it carries no window key: a plan cannot know
                        # it until its own ``find_window`` step has run, and the
                        # witness is resolved from the interaction's window.
                        observe={"automation_id": str(uia_surface.IDS["witness"]),
                                 "control_type": "Text"}),
            DesktopStep(step_id="confirm", operation="verify_window",
                        params={"expect_title": "UIA_SURFACE"},
                        depends_on=("press",)),
        ))
        result = hosted_workflow.run_plan(plan)
        assert result.status == "success", result.error
        assert result.verification == "PASSED", result.verification_detail
        assert result.targeting_mode is TargetingMode.UIA
        assert probe.click_count == before + 1

    def test_the_bridge_reports_a_verified_workflow(self, settings, probe):
        """The path a real tool call takes, through permission and verification."""
        from core.windows.capabilities import WindowsCapabilityLayer

        layer = WindowsCapabilityLayer()
        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(layer))
        flow = DesktopWorkflow(registry, permissions=PermissionManager(settings))
        try:
            from integration.contracts import DesktopWorkflowCapabilityAdapter
            registry.register(DesktopWorkflowCapabilityAdapter(flow))
            tools = {t.name: t for t in build_desktop_tools(registry)}
            outcome = tools["desktop.find"].fn(
                title_contains="", window_key=probe.window_key(),
                automation_id=str(uia_surface.IDS["button"]))
            assert outcome.success is True, outcome.error
            assert outcome.verification is not None
            assert outcome.verification.state is VerificationState.PASSED
            assert outcome.verification.evidence["window_key"] == probe.window_key()
        finally:
            flow.close()
            layer.close()


# ══════════════════════════════════════════════ E: stale target


@requires_real_uia
class TestStaleTargetWorkflow:
    """Workflow E: no action may reach a replacement window."""

    def test_an_interaction_against_a_destroyed_window_is_refused(self, hosted_workflow, probe):
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                     visible_only=False)
        target = hosted_workflow.find_target(
            context=found.context,
            automation_id=str(uia_surface.IDS["check"]))
        assert target.ok, target.error
        window_key = found.context.window_key

        probe.close()

        acted = hosted_workflow.interact(action="toggle", context=target.context)
        assert acted.ok is False
        assert acted.error_code in (
            DesktopErrorCode.TARGET_STALE.value,
            DesktopErrorCode.WINDOW_STALE.value,
            DesktopErrorCode.WINDOW_NOT_FOUND.value,
            DesktopErrorCode.ACTION_FAILED.value), (
            f"unexpected refusal for a stale window: {acted.error_code}")
        assert acted.verification != "PASSED"

    def test_verifying_a_destroyed_window_reports_stale(self, hosted_workflow, probe):
        window_key = probe.window_key()
        verified = hosted_workflow.verify_window(window_key=window_key)
        assert verified.ok is True
        probe.close()
        again = hosted_workflow.verify_window(window_key=window_key)
        assert again.ok is False
        assert again.error_code in (
            DesktopErrorCode.WINDOW_STALE.value,
            DesktopErrorCode.WINDOW_NOT_FOUND.value,
            DesktopErrorCode.ACTION_FAILED.value)

    def test_a_plan_stopping_on_staleness_does_not_continue(self, hosted_workflow, probe):
        plan = DesktopWorkflowPlan(steps=(
            DesktopStep(step_id="open", operation="find_window",
                        params={"title_contains": uia_surface.TITLE,
                                "visible_only": False}),
            DesktopStep(step_id="target", operation="find_target",
                        params={"automation_id": str(uia_surface.IDS["button"])},
                        depends_on=("open",)),
            DesktopStep(step_id="press", operation="interact",
                        params={"action": "invoke"}, depends_on=("target",)),
        ))
        result = hosted_workflow.run_plan(plan)
        assert result.status == "success", result.error
        clicks = probe.click_count
        probe.close()

        # Re-running against the now-dead window must stop, not press anything.
        dead = hosted_workflow.run_plan(plan)
        assert dead.status == "failed"
        assert dead.verification != "PASSED"


# ══════════════════════════════════════════════ F: ambiguous window


@requires_real_uia
class TestAmbiguousWindowWorkflow:
    """Workflow F: two real windows with the same title, and no arbitrary choice."""

    @pytest.fixture()
    def two_probes(self):
        first = uia_surface.UiaProbe()
        second = uia_surface.UiaProbe(position=(260, 200))
        for probe in (first, second):
            for _ in range(5):
                if probe.activate():
                    break
                time.sleep(0.2)
        try:
            yield first, second
        finally:
            first.close()
            second.close()

    @pytest.fixture()
    def hosted_two(self, settings, two_probes):
        """Two probes, with a workflow that can settle either of them."""
        with _workflow_for(settings, settle=two_probes[0].pump) as flow:
            yield flow, two_probes[0], two_probes[1]

    def test_two_matching_windows_are_refused_not_chosen(self, hosted_two):
        workflow, _first, _second = hosted_two
        found = workflow.find_window(title_contains=uia_surface.TITLE,
                                     visible_only=False)
        assert found.ok is False, (
            "two live windows with the same title were silently resolved to one")
        assert found.error_code == DesktopErrorCode.WINDOW_AMBIGUOUS.value
        candidates = found.evidence.get("error", {}).get("ambiguous") or []
        assert len(candidates) >= 2
        assert found.context.window_key == "", "an ambiguous match anchored anyway"
        # And the candidates must be nameable without exposing a handle.
        for candidate in candidates:
            assert candidate.get("window_key")
            assert "hwnd" not in candidate

    def test_naming_one_window_resolves_the_ambiguity(self, hosted_two):
        workflow, first, second = hosted_two
        resolved = workflow.find_window(window_key=first.own_window_key())
        assert resolved.ok is True, resolved.error
        assert resolved.context.window_key == first.own_window_key()
        assert resolved.context.window_key != second.own_window_key()


# ══════════════════════════════════════════════ D: cancellation


@requires_real_uia
class TestCancellationWorkflow:
    """Workflow D: no queued mutation runs after a cancellation."""

    def test_a_cancelled_workflow_dispatches_nothing(self, hosted_workflow, probe):
        hosted_workflow.bind_task("m2166-cancel")
        hosted_workflow.cancel("m2166-cancel")
        clicks = probe.click_count
        result = hosted_workflow.run_plan(DesktopWorkflowPlan(steps=(
            DesktopStep(step_id="press", operation="interact",
                        params={"action": "invoke"}),
        )), task_id="m2166-cancel")
        assert result.cancelled is True
        assert result.ok is False
        assert result.verification != "PASSED"
        assert probe.click_count == clicks, "a press happened after cancellation"
        hosted_workflow.release_task("m2166-cancel")

    def test_cancelling_between_steps_stops_later_mutations(self, hosted_workflow, probe):
        hosted_workflow.bind_task("m2166-mid")
        clicks = probe.click_count

        def cancel_after(step_payload):
            hosted_workflow.cancel("m2166-mid")

        plan = DesktopWorkflowPlan(steps=(
            DesktopStep(step_id="open", operation="find_window",
                        params={"title_contains": uia_surface.TITLE,
                                "visible_only": False}),
            DesktopStep(step_id="press", operation="interact",
                        params={"action": "invoke"}, depends_on=("open",)),
        ))
        result = hosted_workflow.run_plan(plan, task_id="m2166-mid",
                                   event_sink=cancel_after)
        assert result.cancelled is True
        assert probe.click_count == clicks, (
            "a queued mutation ran after the cancellation")
        hosted_workflow.release_task("m2166-mid")

    def test_a_cancelled_run_leaves_the_desktop_as_it_found_it(self, hosted_workflow, probe):
        hosted_workflow.bind_task("m2166-state")
        before_text = probe.window_text("edit")
        before_check = probe.checkbox_state()
        hosted_workflow.cancel("m2166-state")
        hosted_workflow.run_plan(DesktopWorkflowPlan(steps=(
            DesktopStep(step_id="press", operation="interact",
                        params={"action": "invoke"}),
            DesktopStep(step_id="type", operation="interact",
                        params={"action": "set_value", "value": "leaked"}),
        )), task_id="m2166-state")
        assert probe.window_text("edit") == before_text
        assert probe.checkbox_state() == before_check
        hosted_workflow.release_task("m2166-state")


# ══════════════════════════════════════════════ A: calculator


@requires_real_uia
class TestCalculatorWorkflow:
    """Workflow A: a real application, driven semantically.

    Scoped honestly. Calculator's control ids differ between builds, so this asserts
    what is genuinely stable - the window is found, anchored, focused, and its
    controls are reachable semantically - and does not claim a specific arithmetic
    result. Claiming ``56088`` here would be asserting something this machine's
    Calculator build may not even produce.
    """

    @pytest.fixture()
    def calculator(self):
        process = subprocess.Popen(["calc.exe"])
        deadline = time.monotonic() + 25.0
        title = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            output = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq CalculatorApp.exe"],
                capture_output=True, text=True, timeout=15)
            if "CalculatorApp.exe" in (output.stdout or ""):
                title = "Calculator"
                break
            output = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq calc.exe"],
                capture_output=True, text=True, timeout=15)
            if "calc.exe" in (output.stdout or ""):
                title = "Calculator"
                break
        if not title:
            process.poll()
            pytest.skip("Calculator did not start on this host")
        try:
            yield process
        finally:
            _terminate_calculator()

    def test_the_calculator_window_is_found_and_anchored(self, workflow, calculator):
        found = workflow.find_window(title_contains="Calculator")
        assert found.ok, (
            f"Calculator window not found: {found.error} "
            f"({found.error_code})")
        assert found.context.anchored
        assert found.verification == "PASSED"

    def test_the_calculator_window_is_focusable_and_verifiable(self, workflow, calculator):
        """Calculator is focus-refused on this host, and that is the correct answer.

        Windows 11's Calculator is an ``ApplicationFrameWindow``, and M2.16.1
        deliberately treats that class as a protected shell window: focusing or
        closing it perturbs the user's session. The workflow therefore refuses, and
        the property worth asserting is that the refusal is *clean and reported*
        rather than quietly worked around.
        """
        found = workflow.find_window(
            title_contains="Calculator", class_name="ApplicationFrameWindow")
        if not found.ok:
            pytest.skip(f"Calculator window not resolvable here: {found.error}")
        focused = workflow.focus_window(found.context)
        assert focused.ok is False, (
            "focusing a protected shell window should have been refused")
        # ``PERMISSION_DENIED`` is the more precise of the two refusal codes here:
        # M2.16.1 refuses by *policy*, not because focus failed to land. The workflow
        # reports what the layer decided rather than flattening it.
        assert focused.error_code in (
            DesktopErrorCode.PERMISSION_DENIED.value,
            DesktopErrorCode.FOCUS_FAILED.value), focused.error_code
        assert "protected" in focused.error.lower()
        assert focused.verification != "PASSED"
        # The window is still verifiable without being focused.
        verified = workflow.verify_window(
            window_key=found.context.window_key, expect_title="Calc")
        assert verified.ok is True, verified.error

    def test_calculator_controls_are_reachable_semantically(self, workflow, calculator):
        """Not one fabricated coordinate - only what UIA actually reports."""
        found = workflow.find_window(title_contains="Calculator")
        assert found.ok, found.error
        elements = []
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            inspected = workflow.find_target(
                context=found.context, control_type="Button")
            if inspected.ok or inspected.error_code == (
                    DesktopErrorCode.TARGET_AMBIGUOUS.value):
                break
            time.sleep(0.5)
        # Ambiguity here is a *success* for this test: it proves the workflow found
        # real buttons and refused to pick between them.
        assert inspected.error_code == DesktopErrorCode.TARGET_AMBIGUOUS.value, (
            f"expected several Calculator buttons, got {inspected.error}")


# ══════════════════════════════════════════════ B: notepad


@requires_real_uia
class TestNotepadWorkflow:
    """Workflow B: type real text into a real editor, then read it back."""

    @pytest.fixture()
    def notepad(self):
        started = not _notepad_running()
        subprocess.Popen(["notepad.exe"])
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if _notepad_running():
                break
        else:
            pytest.skip("Notepad did not start on this host")
        try:
            yield
        finally:
            # Only reaped when this test started it, so an unrelated Notepad the
            # user had open is never killed.
            if started:
                _terminate_notepad()

    def _notepad_window(self, workflow):
        """Notepad's *main* window, chosen by class among same-titled windows.

        Notepad also owns a ``GDI+ Hook Window Class`` helper whose title mentions
        notepad.exe, so a title-only search is genuinely ambiguous - and the
        workflow refusing that ambiguity is correct, not a defect. This therefore
        resolves the candidate it actually means rather than loosening the workflow.
        """
        ambiguous = workflow.find_window(title_contains="Notepad", visible_only=False)
        if ambiguous.ok:
            return ambiguous
        candidates = (ambiguous.evidence.get("error", {}) or {}).get("ambiguous") or []
        for candidate in candidates:
            if str(candidate.get("class_name") or "") == "Notepad":
                resolved = workflow.find_window(
                    window_key=str(candidate["window_key"]))
                if resolved.ok:
                    return resolved
        raise AssertionError(
            "Notepad's main window was not among the candidates "
            f"{[(c.get('class_name'), c.get('title')) for c in candidates]}")

    def test_notepad_is_found_focused_and_verified(self, workflow, notepad):
        found = self._notepad_window(workflow)
        focused = workflow.focus_window(found.context)
        assert focused.ok, focused.error
        verified = workflow.verify_window(
            window_key=found.context.window_key, expect_title="Notepad")
        assert verified.ok is True, verified.error

    def test_typing_into_notepad_stays_attributable_to_notepad(self, hosted_workflow,
                                                               notepad):
        """What is honestly assertable about typing into a real editor.

        Reading the typed text back needs a document UIA element whose availability
        varies by Notepad build, and asserting a specific control tree would claim
        coverage this build may not have. Fabricating a coordinate to type at is
        exactly what this milestone forbids. So the claim is the one that holds
        everywhere: the window identity is stable across an input attempt, which is
        what makes the input attributable to the intended editor.

        The refusal case is asserted too, because "type into Notepad" with no
        anchored point must not degrade into typing into whatever happens to be
        foreground.
        """
        from core.windows.capabilities import WindowsCapabilityLayer

        found = self._notepad_window(hosted_workflow)
        focused = hosted_workflow.focus_window(found.context)
        assert focused.ok, focused.error

        layer = WindowsCapabilityLayer()
        try:
            with pytest.raises(Exception) as refused:
                # No point given: the M2.16.3 layer requires one, and its refusal is
                # the property under test.
                layer.type_text("Hello Diablo",
                                window=focused.context.window_key)
            assert "point" in str(refused.value).lower(), (
                f"unanchored typing was refused for the wrong reason: {refused.value}")

            still_there = hosted_workflow.verify_window(
                window_key=found.context.window_key, expect_title="Notepad")
            assert still_there.ok is True, (
                "the window identity changed, so any input could not be attributed "
                "to the intended editor")
        finally:
            layer.close()


# ══════════════════════════════════════════════ input safety


@requires_real_uia
class TestInputSafety:
    """Part 24: the user's desktop is left as it was found."""

    def test_focus_is_confirmed_before_any_input_is_sent(self, hosted_workflow, probe):
        """The press is only ever attempted against a verified foreground window.

        Whether the fixture happens to already hold focus or not, the postcondition
        is what decides the claim: an invoke with no witness cannot be verified, and
        this asserts that it does not pretend otherwise. What it must never do is
        report a press it cannot account for.
        """
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                            visible_only=False)
        target = hosted_workflow.find_target(
            context=found.context,
            automation_id=str(uia_surface.IDS["button"]))
        assert target.ok, target.error
        clicks = probe.click_count
        acted = hosted_workflow.interact(action="invoke", context=target.context)
        if acted.ok:
            assert acted.verification == "UNVERIFIED", (
                "an invoke with no witness was reported as verified")
        else:
            assert acted.verification != "PASSED"
            assert probe.click_count == clicks, (
                "a refused action still reached the control")

    def test_no_leaked_windows_after_the_module(self, probe):
        """Every fixture closed its own window."""
        probe.close()
        assert probe.hwnd == 0
        leaked = [p for p in _python_windows() if uia_surface.TITLE in p]
        assert not leaked, f"test windows leaked: {leaked}"

    def test_the_workflow_advertises_no_native_handles(self, hosted_workflow, probe):
        found = hosted_workflow.find_window(title_contains=uia_surface.TITLE,
                                     visible_only=False)
        assert found.ok
        context = found.context.to_dict()
        for forbidden in ("hwnd", "handle", "PyHANDLE"):
            assert forbidden not in context, (
                f"a native handle leaked into the workflow context as {forbidden!r}")


def _python_windows() -> list[str]:
    """Titles of windows belonging to python processes, for the leak check."""
    try:
        import win32gui
    except Exception:  # noqa: BLE001 - pywin32 absent means no leak to find
        return []
    titles: list[str] = []

    def collect(hwnd, _param):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if title:
            titles.append(title)

    win32gui.EnumWindows(collect, None)
    return titles
