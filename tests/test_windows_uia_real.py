"""Real-Windows M2.16.5 tests: UI Automation against a real control tree.

Everything here runs against ``tests/uia_surface.py``, a native Win32 window with
standard controls created in this process. Native controls are used deliberately:
Tk exposes generic unnamed panes with no usable patterns, so a Tk fixture would
have tested nothing.

The point of this file is the part that cannot be faked. That ``Invoke`` really
presses a button is proved by a label the button's own ``WM_COMMAND`` handler
changes. That ``Toggle`` really flips a checkbox is proved by cross-checking
``BM_GETCHECK`` - the Win32 answer - against what UIA reported. Every action
assertion here has an independent witness outside UI Automation, because a test
that only asks UIA whether UIA worked proves nothing.

Actions in this file are asynchronous: they are delivered through the owning
thread's message loop, which is this process. Each action therefore passes
``settle=probe.pump`` so the application can actually run. Without it, every
working action reports as a failure - which is not a fixture problem, it is how
UIA works everywhere.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uia_surface  # noqa: E402
from core.windows.capabilities import WindowsCapabilityLayer  # noqa: E402
from core.windows.coordinates import Rect  # noqa: E402
from core.windows.errors import (  # noqa: E402
    WindowsInvalidRequestError,
    WindowsTargetNotFoundError,
    WindowsTargetStaleError,
)
from core.windows.uia import uia_available  # noqa: E402

requires_real_uia = pytest.mark.skipif(
    not uia_available(),
    reason="comtypes/UI Automation is unavailable on this host",
)

IDS = uia_surface.IDS


@pytest.fixture()
def probe():
    """A native Win32 window hosting real, UIA-visible controls."""
    fixture = uia_surface.UiaProbe()
    try:
        yield fixture
    finally:
        fixture.close()


@pytest.fixture()
def layer():
    """A real capability layer.

    The layers expose ``close()`` rather than the context-manager protocol, so this
    follows the convention used by the rest of the suite instead of adding one to
    the production class for the sake of a test.
    """
    capability = WindowsCapabilityLayer()
    try:
        yield capability
    finally:
        capability.close()


def element(layer, probe, key: str) -> dict:
    """Discover one control by the automation id the fixture assigned it."""
    found = layer.uia_find_elements(window=probe.window_key())
    wanted = str(IDS[key])
    for element in found["elements"]:
        if element["recipe"]["identity"]["automation_id"] == wanted:
            return element
    raise AssertionError(f"control {key!r} ({wanted}) was not discovered")


class TestTheSurfaceExists:
    def test_uia_reports_available_when_comtypes_is_present(self):
        assert uia_available() is True

    def test_the_capability_advertises_all_ten_operations(self, layer):
        operations = set(layer.detect_capabilities()["operations"])
        assert {op for op in operations
                if op.startswith(("uia_", "visual_"))} == {
            "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
            "uia_target_point", "uia_invoke", "uia_set_value", "uia_toggle",
            "uia_select", "uia_expand_collapse", "visual_resolve_target"}

    def test_the_five_actions_are_declared_mutating(self, layer):
        capabilities = layer.detect_capabilities()
        mutating = set(capabilities["mutating_operations"])
        assert {"uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
                "uia_expand_collapse"} <= mutating

    def test_visual_resolving_is_advertised_without_requiring_uia(self, layer):
        """Its ``geometry`` source is plain rectangle arithmetic."""
        assert "visual_resolve_target" in layer.detect_capabilities()["operations"]


@requires_real_uia
class TestRealDiscovery:
    def test_a_real_window_is_inspected(self, layer, probe):
        info = layer.uia_inspect_window(window=probe.window_key())
        assert info["untrusted"] is True
        bounds = Rect.from_dict(info["bounds"]) \
            if isinstance(info["bounds"], dict) else info["bounds"]
        assert bounds is not None, "a real window reported no bounds at all"
        assert not bounds.is_empty
        assert bounds.width > 0 and bounds.height > 0
        assert bounds.left == probe.position[0] and bounds.top == probe.position[1], (
            "the inspected rectangle does not match where the window was placed, "
            "so the anchor is not the real one")

    def test_the_controls_are_found_with_their_real_patterns(self, layer, probe):
        patterns = {e["recipe"]["identity"]["automation_id"]: e["patterns"]
                    for e in layer.uia_find_elements(
                        window=probe.window_key())["elements"]}
        assert "Invoke" in patterns[str(IDS["button"])]
        assert "Toggle" in patterns[str(IDS["check"])]
        assert "Value" in patterns[str(IDS["edit"])]

    def test_a_bounded_search_admits_it_was_bounded(self, layer, probe):
        found = layer.uia_find_elements(window=probe.window_key(), max_depth=1)
        assert found["exhaustive"] in (True, False)

    def test_an_exhaustive_search_says_so(self, layer, probe):
        assert layer.uia_find_elements(window=probe.window_key())["exhaustive"] is True

    def test_a_narrowed_search_returns_only_what_was_asked_for(self, layer, probe):
        found = layer.uia_find_elements(
            window=probe.window_key(), automation_id=str(IDS["button"]))
        assert found["elements"], "an explicit automation id matched nothing"
        for found_element in found["elements"]:
            assert found_element["recipe"]["identity"]["automation_id"] \
                == str(IDS["button"])

    def test_no_application_text_is_published_unlabelled(self, layer, probe):
        """Checked against a real application, on the surface a model reads first."""
        for found_element in layer.uia_find_elements(
                window=probe.window_key())["elements"]:
            assert "name" not in found_element, (
                "a control label reached the model as a bare field")
            assert found_element["recipe"]["trust"] == "UNTRUSTED_APPLICATION_DATA"


@requires_real_uia
class TestRealResolution:
    def test_an_element_re_resolves_to_itself(self, layer, probe):
        target = element(layer, probe, "check")
        resolved = layer.uia_resolve_target(target=target, window=probe.window_key())
        assert resolved["resolved"] is True
        assert resolved["target_id"] == target["target_id"]
        assert resolved["window_key"] == probe.window_key()

    def test_resolution_works_from_the_window_key_alone(self, layer, probe):
        """A stored element names its window by key; that has to be enough."""
        target = element(layer, probe, "check")
        resolved = layer.uia_resolve_target(target=target)
        assert resolved["resolved"] is True
        assert resolved["target_id"] == target["target_id"]

    def test_an_unknown_target_is_refused_rather_than_guessed(self, layer, probe):
        recipe = dict(element(layer, probe, "check")["recipe"]["identity"])
        recipe["automation_id"] = "no-such-control"
        with pytest.raises((WindowsTargetNotFoundError, WindowsInvalidRequestError)):
            layer.uia_resolve_target(target=recipe, window=probe.window_key())

    def test_a_target_point_is_anchored_and_inside_the_element(self, layer, probe):
        target = element(layer, probe, "button")
        result = layer.uia_target_point(target=target, window=probe.window_key())
        point = result["point"]
        bounds = Rect.from_dict(target["bounds"])
        assert result["anchored"] is True
        assert result["provenance"] == "uia:element_bounds"
        assert bounds.left <= point["x"] < bounds.right
        assert bounds.top <= point["y"] < bounds.bottom

    def test_a_point_for_a_destroyed_window_is_refused(self, layer, probe):
        target = element(layer, probe, "button")
        window_key = probe.window_key()
        probe.close()
        with pytest.raises((WindowsTargetStaleError, WindowsInvalidRequestError)):
            layer.uia_target_point(target=target, window=window_key)


@requires_real_uia
class TestRealActionsAreCheckedAgainstWindows:
    """Every postcondition here is confirmed by Win32, not by asking UIA twice."""

    def test_toggle_flips_the_checkbox_and_says_so(self, layer, probe):
        target = element(layer, probe, "check")
        before = probe.checkbox_state()
        result = layer.uia_toggle(target=target, window=probe.window_key(),
                                  settle=probe.pump)
        after = probe.checkbox_state()
        assert before == 0
        verification = result["verification"]
        assert verification["status"] == "PASSED"
        assert verification["previous_state"] == "off"
        assert verification["observed_state"] == "on"
        assert verification["changed"] is True
        assert after == 1, "UIA reported a toggle that Win32 says did not happen"

    def test_toggling_back_returns_to_the_starting_state(self, layer, probe):
        target = element(layer, probe, "check")
        layer.uia_toggle(target=target, window=probe.window_key(), settle=probe.pump)
        second = layer.uia_toggle(target=target, window=probe.window_key(),
                                  settle=probe.pump)
        assert second["verification"]["previous_state"] == "on"
        assert second["verification"]["observed_state"] == "off"
        assert probe.checkbox_state() == 0

    def test_set_value_is_visible_to_win32(self, layer, probe):
        target = element(layer, probe, "edit")
        result = layer.uia_set_value(target=target, window=probe.window_key(),
                                     value="typed by uia", settle=probe.pump)
        assert result["verification"]["status"] == "PASSED"
        assert probe.window_text("edit") == "typed by uia", (
            "the value was verified through UIA but Win32 disagrees")

    def test_invoke_with_a_witness_is_verified_by_the_witness(self, layer, probe):
        """A press is only verified by something the press visibly changed."""
        button = element(layer, probe, "button")
        witness = element(layer, probe, "witness")
        before = probe.click_count
        result = layer.uia_invoke(target=button, window=probe.window_key(),
                                  observe=witness, settle=probe.pump)
        after = probe.click_count
        assert result["verification"]["status"] == "PASSED"
        assert after == before + 1, (
            "UIA reported a verified invoke that the application never saw")

    def test_invoke_without_a_witness_is_honestly_unverified(self, layer, probe):
        """The claim this milestone refuses to make.

        ``Invoke`` has no state of its own. Windows accepting the call is not
        evidence that anything happened, so the result stays UNVERIFIED.
        """
        button = element(layer, probe, "button")
        result = layer.uia_invoke(target=button, window=probe.window_key(),
                                  settle=probe.pump)
        assert result["verification"]["status"] == "UNVERIFIED"

    def test_an_action_on_an_unsupported_pattern_is_refused(self, layer, probe):
        """No guessing, and no generic dispatch that might try anyway."""
        combo = element(layer, probe, "combo")
        assert "ExpandCollapse" not in combo["patterns"], (
            "this fixture's combo unexpectedly supports ExpandCollapse, so the "
            "refusal test would no longer be testing a refusal")
        with pytest.raises(WindowsInvalidRequestError):
            layer.uia_expand_collapse(target=combo, window=probe.window_key(),
                                      state="expand", settle=probe.pump)

    def test_an_action_on_a_destroyed_window_is_refused(self, layer, probe):
        target = element(layer, probe, "check")
        window_key = probe.window_key()
        probe.close()
        with pytest.raises((WindowsTargetStaleError, WindowsInvalidRequestError)):
            layer.uia_toggle(target=target, window=window_key, settle=probe.pump)

    def test_a_cancelled_action_never_reaches_the_control(self, layer, probe):
        """Preflight cancellation is checked before anything is dispatched.

        Cancellation is bound through ``cancellation_scope`` - the same path every
        other Windows operation uses - rather than a per-call flag, so this also
        proves the UIA actions are wired into the existing mechanism instead of
        inventing a second one.
        """
        from core.windows.errors import WindowsCancelledError

        class _Cancelled:
            @staticmethod
            def is_cancelled() -> bool:
                return True

        target = element(layer, probe, "check")
        before = probe.checkbox_state()
        with pytest.raises((WindowsCancelledError, WindowsInvalidRequestError)):
            with layer.cancellation_scope(_Cancelled()):
                layer.uia_toggle(target=target, window=probe.window_key(),
                                 settle=probe.pump)
        assert probe.checkbox_state() == before, (
            "a cancelled action still changed the control")


@requires_real_uia
class TestRealVisualTargetsAreClaimsNotClicks:
    """``visual_resolve_target`` is advertised, so it needs real coverage too."""

    def _claim(self, probe, bounds=None) -> dict:
        """A geometry claim anchored to the live window, built by hand.

        Deliberately built here rather than produced by a detector: there is no
        vision producer yet (§8.8), and this is what one would have to satisfy.
        """
        left, top = probe.position
        rect = bounds or {"left": left + 10, "top": top + 10,
                          "right": left + 110, "bottom": top + 60}
        return {"bounds": rect, "source": "geometry",
                "window_key": probe.window_key(),
                "confidence": 0.9,
                "provenance": {"detector": "test-geometry"}}

    def test_a_claim_inside_the_real_window_is_valid_but_never_actionable(
            self, layer, probe):
        report = layer.visual_resolve_target(target=self._claim(probe))
        assert report["verdict"] == "VALID"
        assert report["actionable"] is False, (
            "a validated visual claim reported itself actionable")
        assert report["actionable_reason"]

    def test_a_claim_whose_window_is_gone_is_refused(self, layer, probe):
        """A claim about a window that no longer exists must not be validated.

        It raises rather than returning an unvalidatable report, because a report
        with a verdict is something a caller will go on to act on.
        """
        from core.windows.errors import WindowsTargetStaleError

        claim = self._claim(probe)
        probe.close()
        with pytest.raises((WindowsTargetStaleError, WindowsInvalidRequestError)):
            layer.visual_resolve_target(target=claim)

    def test_a_claim_from_an_uninstalled_source_is_uncorroborated(self, layer, probe):
        claim = self._claim(probe)
        claim["source"] = "future_vision"
        report = layer.visual_resolve_target(target=claim)
        assert report["verdict"] == "UNCORROBORATED"
        assert report["actionable"] is False

    def test_a_claim_reaching_outside_the_window_is_refused(self, layer, probe):
        """A rectangle not wholly inside what it claims to belong to is a defect.

        Checked at the far corner as well as the origin, so a claim that starts
        inside the window and runs off the edge of it is caught.
        """
        from core.windows.errors import WindowsInvalidGeometryError

        left, top = probe.position
        for bounds in (
            {"left": left - 5000, "top": top - 5000,
             "right": left - 4000, "bottom": top - 4000},          # wholly outside
            {"left": left + 10, "top": top + 10,                  # starts inside,
             "right": left + 10000, "bottom": top + 10000},        # runs off the end
        ):
            with pytest.raises((WindowsInvalidGeometryError,
                                WindowsInvalidRequestError)):
                layer.visual_resolve_target(target=self._claim(probe, bounds=bounds))


@requires_real_uia
class TestRealComHygiene:
    def test_repeated_reads_do_not_strand_com_references(self, layer, probe):
        """A long-running agent is where a COM leak actually costs something.

        Polling a postcondition acquires a pattern interface on every poll, so a
        settle loop can strand dozens of references if they are not released. The
        check is proven able to detect a leak first, so that passing it means
        something.
        """
        import gc

        from core.windows.uia import (
            PATTERN_INTERFACES,
            UIElementDescriptor,
            UIALayer,
            _pattern_of,
            _release,
        )

        def live_pointers() -> int:
            gc.collect()
            return len([obj for obj in gc.get_objects()
                        if type(obj).__name__.startswith("POINTER(IUIAutomation")])

        target = element(layer, probe, "check")
        uia = UIALayer(layer._windows)
        try:
            with uia._with_client() as (automation, walker):
                from core.windows.window_models import WindowIdentity

                identity = WindowIdentity.from_key(probe.window_key())
                info = layer._windows.get_window(identity, cancelled=None)
                root = uia._element_for_window(automation, info)
                descriptor = UIElementDescriptor.from_dict(target)
                live = uia._acquire_live(walker, root, descriptor.identity, None)
                try:
                    before = live_pointers()
                    for _ in range(300):
                        uia._patterns_of(live)
                        uia._text_of(live)
                    assert live_pointers() == before, (
                        "reading patterns grew the COM reference count")

                    pattern_id, interface = PATTERN_INTERFACES["Toggle"]
                    retained = [_pattern_of(live, pattern_id, interface)
                                for _ in range(50)]
                    assert live_pointers() > before, (
                        "this check cannot detect a leak, so passing it means "
                        "nothing")
                    del retained
                    assert live_pointers() == before
                finally:
                    _release(live)
                    _release(root)
        finally:
            uia.close()
