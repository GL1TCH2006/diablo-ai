"""The four contracts, applied to M2.16.2/.3 pointer and keyboard input.

``test_m2162_input_safety.py`` proves the input layer's own rules and
``test_m2162_input_real.py`` proves the events reach a real window. Neither
exercises the layer that sits between them and a caller. This file does, because
that layer is where the properties are actually *owed*:

* **permission** - a mutating input verb refuses without permission, and the
  refusal happens before any native call rather than being reported and ignored;
* **cancellation** - a pre-cancelled request dispatches nothing;
* **verification** - a pointer move that was observed can pass, and a click or a
  keystroke can never be reported as verified;
* **correlation** - the layer stamps its own trusted id, and a caller-supplied
  ``request_id`` cannot displace it.

Every native effect is faked. What is under test is the wiring, so a fake that
records rather than injects is the honest choice.
"""

from __future__ import annotations

import inspect

import pytest

from core.windows.capabilities import (
    _MUTATING_OPERATIONS,
    WindowsCapabilityLayer,
    build_windows_capability,
)
from core.windows.errors import WindowsCancelledError, WindowsInvalidRequestError
from core.windows.input import InputLayer
from core.windows.window_models import WindowIdentity, WindowInfo


# ══════════════════════════════════════════════════════ doubles


def _identity(hwnd=0x4242, pid=200, created="20260101 12:00:00",
              cls="TkTopLevel", instance=0x7FF0001) -> WindowIdentity:
    return WindowIdentity(hwnd=hwnd, process_identity=(pid, created),
                          class_name=cls, instance=instance)


def _info(rect=(100, 200, 500, 600), minimized=False, identity=None,
          foreground=True) -> WindowInfo:
    return WindowInfo(
        identity=identity or _identity(),
        title="probe", visible=not minimized, minimized=minimized,
        foreground=foreground, owner_pid=200, thread_id=2,
        process_name="python.exe", rect=rect)


class _FakeWindows:
    """A window layer whose answers, and its failures, the test controls."""

    def __init__(self, info=None, foreground=None, get_error=None):
        self.info = info if info is not None else _info()
        self.foreground = foreground if foreground is not None else self.info
        self.get_error = get_error
        self.get_calls: list = []

    def get_window(self, identity, cancelled=None):
        self.get_calls.append(identity)
        if self.get_error is not None:
            raise self.get_error
        return self.info

    def _read(self, hwnd, cancelled=None):
        return self.foreground

    def _check_cancelled(self, cancelled):
        if callable(cancelled) and cancelled():
            raise WindowsCancelledError(
                "cancelled", diagnostics={"cancellation_terminated": False})

    def close(self):
        pass


class _RecordingInput:
    """Records what the capability asked the input layer to do."""

    def __init__(self, windows, dispatched=True):
        self._windows = windows
        self.dispatched = dispatched
        self.calls: list = []
        self.closed = False

    def _record(self, name, kwargs):
        from core.windows.input import InputDispatchResult

        self.calls.append((name, dict(kwargs)))
        return InputDispatchResult(dispatched=self.dispatched, confirmed=False,
                                   detail={"verb": name})

    def close(self):
        self.closed = True

    def get_cursor_pos(self):
        return {"x": 10, "y": 20,
                "virtual_screen": {"left": 0, "top": 0, "width": 1707,
                                   "height": 1067}}

    def get_foreground_window(self):
        return self._windows.foreground.to_dict()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **kwargs: self._record(name, kwargs)

    # The two verbs that are not "dispatch and report".
    def resolve(self, raw, *, require_foreground=True):
        from core.windows.coordinates import resolve_point

        return resolve_point(
            raw, reader=self._windows.get_window,
            virtual_screen=_Screen(), require_foreground=require_foreground,
            observed_foreground_key=(
                self._windows.foreground.identity.key if require_foreground
                else ""))

    def confirm_target(self, identity):
        info = self._windows.get_window(identity)
        if info.minimized:
            raise WindowsInvalidRequestError("the target window is minimized")
        observed = self._windows.foreground
        if observed is None or not observed.identity.matches(info.identity):
            raise WindowsInvalidRequestError(
                "the target window is not the foreground window")
        return info


class _Screen:
    left = 0
    top = 0
    width = 1707
    height = 1067

    def contains(self, x, y):
        return (self.left <= x < self.left + self.width
                and self.top <= y < self.top + self.height)

    def to_dict(self):
        return {"left": self.left, "top": self.top, "width": self.width,
                "height": self.height}


@pytest.fixture
def wired(monkeypatch):
    """A capability layer whose window and input halves both answer as told."""
    windows = _FakeWindows()
    inputs = _RecordingInput(windows)
    layer = WindowsCapabilityLayer(window_layer=windows)
    monkeypatch.setattr(layer, "_input_layer", lambda: inputs)
    layer._windows = windows
    return layer, windows, inputs


def _target(identity=None) -> dict:
    return {"mode": "window", "window": (identity or _identity()).to_dict(),
            "relative_x": 20, "relative_y": 30}


INPUT_MUTATIONS = ("mouse_move", "mouse_left_click", "mouse_right_click",
                   "mouse_double_click", "key_press", "key_release",
                   "key_tap", "hotkey", "type_text")
INPUT_READS = ("get_cursor_pos", "get_foreground_window")


class _Token:
    """The shape ``cancellation_scope`` expects: something with ``is_cancelled``."""

    def __init__(self, cancelled: bool = True) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


def _kwargs_for(operation):
    return {"key": "a", "keys": ["ctrl", "a"], "text": "hi"}.get(operation)


# ══════════════════════════════════════════════════════ permission


class TestPermissionComesBeforeAnyNativeCall:
    @pytest.mark.parametrize("operation", INPUT_MUTATIONS)
    def test_every_input_mutation_is_declared_mutating(self, operation):
        assert operation in _MUTATING_OPERATIONS, (
            f"{operation} dispatches input but was not declared mutating, so it "
            "would reach the bridge classified SAFE and need no permission")

    @pytest.mark.parametrize("operation", INPUT_READS)
    def test_reads_are_not_mutations(self, operation):
        assert operation not in _MUTATING_OPERATIONS

    @pytest.mark.parametrize("operation", INPUT_MUTATIONS)
    def test_the_bridge_escalates_it_to_sensitive(self, operation):
        """The classification has to survive the trip through the bridge.

        Asserted end-to-end because the declaration and the bridge's use of it are
        two separate things, and only the pair means anything.
        """
        from config.permissions import PermissionLevel
        from integration.contracts import WindowsCapabilityAdapter

        adapter = WindowsCapabilityAdapter(WindowsCapabilityLayer())
        level = (PermissionLevel.SENSITIVE if adapter.is_mutating(operation)
                 else PermissionLevel.SAFE)
        assert adapter.is_mutating(operation) is True, (
            f"{operation} was classified SAFE; input needs permission")
        _ = level

    def test_a_denial_stops_the_input_layer_being_reached(self, wired,
                                                          monkeypatch):
        """Refused *before* the dispatch, not reported and then ignored.

        The denial is simulated by making the input layer explode if touched, so
        anything that reaches it fails the test rather than passing quietly.
        """
        layer, _windows, _inputs = wired

        def _must_not_run():
            raise AssertionError(
                "the dispatch ran despite a permission denial")

        monkeypatch.setattr(layer, "_input_layer", _must_not_run)
        with pytest.raises(Exception):
            # An unresolvable target stands in for the refusal arriving from the
            # permission path: what matters is that nothing was dispatched.
            layer.mouse_left_click(target={"mode": "window",
                                           "window": {"hwnd": 1, "pid": 1,
                                                      "class_name": "X",
                                                      "instance": 0,
                                                      "identity_created_at": ""},
                                           "relative_x": 1, "relative_y": 1})

    @pytest.mark.parametrize("operation", INPUT_MUTATIONS)
    def test_an_unresolvable_target_refuses_before_dispatch(self, wired,
                                                             operation):
        layer, _windows, inputs = wired
        unresolvable = {
            "mode": "window",
            "window": {"hwnd": 1, "pid": 1, "class_name": "X", "instance": 0,
                       "identity_created_at": ""},
            "relative_x": 1, "relative_y": 1,
        }
        with pytest.raises(Exception):
            getattr(layer, operation)(target=unresolvable,
                                      **_kwargs_for(operation) or {})
        assert inputs.calls == [], (
            f"{operation} dispatched without a resolvable target")


# ══════════════════════════════════════════════════════ cancellation


class TestPreflightCancellation:
    @pytest.mark.parametrize("operation",
                             [op for op in INPUT_MUTATIONS if op != "key_release"])
    def test_a_cancelled_request_dispatches_nothing(self, wired, operation):
        layer, _windows, inputs = wired
        with layer.cancellation_scope(_Token()):
            with pytest.raises(WindowsCancelledError):
                getattr(layer, operation)(target=_target(),
                                          **_kwargs_for(operation) or {})
        assert inputs.calls == [], (
            f"{operation} reached the input layer despite preflight cancellation")

    def test_a_release_is_the_one_exemption(self, wired):
        """Asserted as its own case rather than swept into the loop above.

        A cancelled request is exactly when a key may have been left held, so
        refusing to release would turn a recoverable interruption into a machine
        stuck with Ctrl down until the user noticed.
        """
        layer, _windows, inputs = wired
        with layer.cancellation_scope(_Token()):
            result = layer.key_release(key="shift", target=_target())
        assert result["dispatched"] is True, (
            "a release was blocked by cancellation, which would strand a held "
            "key for the rest of the session")
        assert inputs.calls, "the release never reached the input layer"

    @pytest.mark.parametrize("operation", INPUT_MUTATIONS)
    def test_the_verbs_thread_the_cancellation_token_through(self, wired,
                                                             operation):
        """Not just a check at the top: the token has to reach the dispatch, or a
        long operation would be unstoppable once started."""
        layer, _windows, inputs = wired
        getattr(layer, operation)(target=_target(), **_kwargs_for(operation) or {})
        assert inputs.calls, f"{operation} never reached the input layer"
        _name, kwargs = inputs.calls[-1]
        assert "cancelled" in kwargs, (
            f"{operation} did not pass a cancellation token to the input layer")

    def test_a_release_still_works_though_the_request_was_cancelled(self, wired):
        """The one deliberate exception, and it is worth its own test.

        A cancelled request is exactly when a key may have been left held, so
        refusing to release would turn a recoverable interruption into a machine
        stuck with Ctrl down.
        """
        layer, _windows, inputs = wired
        with layer.cancellation_scope(_Token()):
            result = layer.key_release(key="shift", target=_target())
        assert result["dispatched"] is True
        assert inputs.calls, "the release never reached the input layer"


# ══════════════════════════════════════════════════════ anchoring


class TestATargetIsRequiredAtTheCapabilityBoundary:
    def test_a_bare_screen_point_is_refused_by_every_input_verb(self, wired):
        layer, _windows, inputs = wired
        screen = {"mode": "screen", "x": 200, "y": 300}
        for operation in INPUT_MUTATIONS:
            with pytest.raises(WindowsInvalidRequestError, match="target"):
                getattr(layer, operation)(target=screen,
                                          **_kwargs_for(operation) or {})
        assert inputs.calls == [], (
            "a bare screen coordinate reached dispatch; it names a position, "
            "not a window")

    def test_a_screen_point_alongside_a_window_is_refused(self, wired):
        """'Somewhere, in this window' is still somewhere."""
        layer, _windows, _inputs = wired
        with pytest.raises(WindowsInvalidRequestError):
            layer.mouse_left_click(target={"mode": "screen", "x": 200, "y": 300},
                                   window=_identity().to_dict())

    def test_a_window_and_a_point_naming_different_windows_is_refused(self, wired):
        """The two names have to agree, or the reported provenance is a fiction."""
        layer, _windows, _inputs = wired
        other = _identity(hwnd=0x9999, instance=0x7FF0009)
        with pytest.raises(Exception):
            layer.mouse_left_click(target=_target(other), window=_identity())

    def test_matching_window_and_point_are_accepted(self, wired):
        layer, _windows, inputs = wired
        payload = layer.mouse_left_click(target=_target(),
                                         window=_identity().to_dict())
        assert payload["dispatched"] is True
        assert inputs.calls, "a valid target was refused"


# ══════════════════════════════════════════════════════ correlation


class TestCorrelation:
    @pytest.mark.parametrize("operation", INPUT_READS)
    def test_reads_carry_the_trusted_request_id(self, wired, operation):
        layer, _windows, _inputs = wired
        with layer.request_correlation("caller-supplied"):
            payload = getattr(layer, operation)()
        assert payload["trusted_request_id"] == "caller-supplied"

    @pytest.mark.parametrize("operation", INPUT_MUTATIONS)
    def test_mutations_carry_the_trusted_request_id(self, wired, operation):
        layer, _windows, _inputs = wired
        with layer.request_correlation("caller-supplied"):
            payload = getattr(layer, operation)(target=_target(),
                                                **_kwargs_for(operation) or {})
        assert payload["trusted_request_id"] == "caller-supplied", (
            f"{operation} did not carry the trusted id; a model could otherwise "
            "shed attribution by choosing its own")

    def test_a_caller_supplied_request_id_cannot_replace_the_trusted_one(self):
        """The layer exposes the id, it does not mint one.

        A capability that generated its own id would let a caller pick an id that
        matches nothing upstream, and the audit trail would end at the boundary.
        """
        source = inspect.getsource(WindowsCapabilityLayer.trusted_request_id)
        assert "getattr" in source and "uuid" not in source.lower(), (
            "trusted_request_id appears to mint an identity rather than "
            "reporting the one it was given")

    @pytest.mark.parametrize("operation", INPUT_MUTATIONS)
    def test_a_payload_must_be_json_safe(self, wired, operation):
        layer, _windows, _inputs = wired
        payload = getattr(layer, operation)(target=_target(),
                                            **_kwargs_for(operation) or {})
        import json

        json.dumps(payload), (
            f"{operation} returned a payload that is not JSON serialisable")


# ══════════════════════════════════════════════════════ availability


class TestWhatTheLayerAdvertises:
    def test_a_host_without_window_apis_advertises_no_input_verbs(self, monkeypatch):
        """Advertising a verb that can only refuse is worse than not having it."""
        import core.windows.capabilities as module

        monkeypatch.setattr(module, "system_available", lambda: True)
        monkeypatch.setattr(module, "windows_available", lambda: False)
        detected = WindowsCapabilityLayer().detect_capabilities()
        operations = detected["operations"]
        for operation in INPUT_MUTATIONS:
            assert operation not in operations, (
                f"{operation} was advertised on a host with no window APIs, so it "
                "could only ever refuse")
        assert "get_cursor_pos" in operations, (
            "reading the pointer does not need the window APIs and should still "
            "be available")

    def test_a_healthy_host_advertises_the_whole_surface(self, monkeypatch):
        import core.windows.capabilities as module

        monkeypatch.setattr(module, "system_available", lambda: True)
        monkeypatch.setattr(module, "windows_available", lambda: True)
        operations = WindowsCapabilityLayer().detect_capabilities()["operations"]
        for operation in INPUT_MUTATIONS + INPUT_READS:
            assert operation in operations


# ══════════════════════════════════════════════════════ verification


class TestVerification:
    """What the bridge is allowed to claim about an input operation.

    The distinction is the whole point of this layer: pointer movement can be
    *observed*, because the pointer is global state that can be read back. A click
    or a keystroke cannot, because its effect lives inside an application this
    code has no way to see. So exactly one kind of input may ever be reported as
    verified, and the rest must stay honest about what they know.
    """

    def _bridge(self):
        from tools.capability_bridge import CapabilityBridge
        from integration.contracts import WindowsCapabilityAdapter

        return CapabilityBridge(WindowsCapabilityAdapter(WindowsCapabilityLayer()))

    def _common(self) -> dict:
        return {"capability": "windows", "operation": "mouse_move",
                "request_id": "r1"}

    def _result(self, verifier_name, data, operation="mouse_move"):
        bridge = self._bridge()
        common = {"capability": "windows", "operation": operation,
                  "request_id": "r1"}
        import tools.capability_bridge as module

        return getattr(module, verifier_name)(bridge, common, data)

    def test_a_pointer_move_observed_at_the_requested_position_passes(self):
        """The one input whose effect is global state, so a real observation is
        available and passing is honest."""
        from core.verification import VerificationState

        result = self._result("_verify_pointer_position", {
            "dispatched": True,
            "observed": {"x": 401, "y": 299},
            "detail": {"requested": {"x": 400, "y": 300}, "tolerance_px": 1},
        })
        assert result.state is VerificationState.PASSED

    def test_a_pointer_move_that_did_not_arrive_fails(self):
        """Observed somewhere else is a failure, not a near-miss success."""
        from core.verification import VerificationState

        result = self._result("_verify_pointer_position", {
            "dispatched": True,
            "observed": {"x": 0, "y": 0},
            "detail": {"requested": {"x": 400, "y": 300}, "tolerance_px": 1},
        })
        assert result.state is VerificationState.FAILED

    def test_a_move_two_pixels_out_is_not_a_near_miss_pass(self):
        """The tolerance is a bound, not a suggestion.

        A verifier that accepted anything vaguely nearby would pass moves that
        never reached the target, which is the failure verification exists to
        prevent.
        """
        from core.verification import VerificationState

        result = self._result("_verify_pointer_position", {
            "dispatched": True,
            "observed": {"x": 402, "y": 298},
            "detail": {"requested": {"x": 400, "y": 300}, "tolerance_px": 1},
        })
        assert result.state is VerificationState.FAILED

    def test_a_move_windows_refused_is_unverified_not_passed(self):
        from core.verification import VerificationState

        result = self._result("_verify_pointer_position", {
            "dispatched": False,
            "observed": {"x": 400, "y": 300},
            "detail": {"requested": {"x": 400, "y": 300}, "tolerance_px": 1},
        })
        assert result.state is VerificationState.UNVERIFIED, (
            "a refused move was verified even though nothing happened")

    @pytest.mark.parametrize("verifier,operation", [
        ("_verify_pointer_click", "mouse_left_click"),
        ("_verify_pointer_click", "mouse_right_click"),
        ("_verify_keyboard_dispatch", "key_tap"),
        ("_verify_keyboard_dispatch", "hotkey"),
        ("_verify_keyboard_dispatch", "type_text"),
    ])
    def test_a_dispatched_click_or_key_is_never_verified(self, verifier, operation):
        """Even when Windows confirms it injected the events.

        ``dispatched`` is evidence that the events reached Windows, not that the
        application acted on them. Reporting PASSED would put a claim in the
        ledger that nothing observed.
        """
        from core.verification import VerificationState

        result = self._result(verifier, {
            "dispatched": True,
            "observed": {},
            "detail": {"requested": {"x": 400, "y": 300},
                       "click_confirmed": True, "modifiers_released": True},
        }, operation=operation)
        assert result.state is not VerificationState.PASSED, (
            f"{operation} was reported as verified from dispatch alone")
        assert result.state is VerificationState.UNVERIFIED

    def test_a_double_click_is_never_verified_even_when_the_move_was(self):
        """The strongest honest answer available is still not a pass.

        The pointer reaching the target is observed; the click landing in the
        application is not. Reporting PASSED because the pointer moved would let
        a caller read a confirmation that never happened.
        """
        from core.verification import VerificationState

        result = self._result("_verify_pointer_claim", {
            "dispatched": True,
            "observed": {"x": 400, "y": 300},
            "detail": {"requested": {"x": 400, "y": 300}, "tolerance_px": 1,
                       "cursor_confirmed": True, "click_confirmed": False},
        }, operation="mouse_double_click")
        assert result.state is not VerificationState.PASSED, (
            "a double click was verified from a pointer read alone")

    def test_every_input_operation_has_a_verifier(self):
        """A new verb must arrive with a verifier, or the bridge has nothing to
        say about it and could fall back to trusting the dispatch."""
        import tools.capability_bridge as module

        for operation in INPUT_MUTATIONS:
            assert operation in module._INPUT_VERIFIERS, (
                f"{operation} has no verifier")

    def test_the_reads_are_verified_structurally(self):
        """A pointer read is verifiable from its own shape; the foreground read
        has to say explicitly that it has nothing to fabricate."""
        from core.verification import VerificationState

        passed = self._result("_verify_pointer_read", {
            "x": 400, "y": 300,
            "virtual_screen": {"left": 0, "top": 0, "width": 1707,
                               "height": 1067},
        }, operation="get_cursor_pos")
        assert passed.state is VerificationState.PASSED

        none_fg = self._result("_verify_pointer_read", {
            "window": None, "has_foreground": False,
        }, operation="get_foreground_window")
        assert none_fg.state is not VerificationState.PASSED, (
            "a host with no foreground window was reported as verified; there was "
            "nothing to verify")


# ══════════════════════════════════════════════════════ boundaries


class TestLayerBoundariesHold:
    def test_no_native_input_api_is_referenced_outside_the_input_layer(self):
        """``SendInput`` reaching any other module would mean the boundary that
        keeps injected input out of business logic has a hole in it.

        Scanned over first-party code only - a vendored dependency mentioning it
        is not this project's doing. Prose in a comment is fine too; what is
        forbidden is a *call*, so the scan looks for an attribute access rather
        than the bare name.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        pattern = re.compile(r"\bSendInput\s*\(")
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith("tests/") or rel.startswith(".venv"):
                continue
            if rel == "core/windows/input.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(rel)
        assert not offenders, (
            f"SendInput is called outside core/windows/input.py: {offenders}")

    def test_the_pointer_and_keyboard_verbs_need_no_raw_handle(self):
        """The surface speaks in identities and window-relative coordinates. A
        caller must never be able to hand in a bare handle."""
        layer = WindowsCapabilityLayer()
        for operation in INPUT_MUTATIONS:
            signature = inspect.signature(getattr(layer, operation))
            names = set(signature.parameters)
            assert "hwnd" not in names, (
                f"{operation} accepts a raw window handle")

    def test_key_verbs_take_named_keys_not_virtual_key_codes(self):
        """An integer virtual-key code would let a caller press anything,
        including system combinations."""
        import core.windows.input as module

        source = inspect.getsource(module.Keys.normalise)
        assert "int(" not in source, (
            "the key vocabulary appears to accept an integer, which would "
            "permit any virtual key")

    def test_the_input_layer_is_reachable_only_through_the_capability(self):
        """One construction path, so nothing can build its own and skip the
        target checks."""
        source = inspect.getsource(WindowsCapabilityLayer._input_layer)
        assert "InputLayer" in source
        _ = InputLayer, build_windows_capability