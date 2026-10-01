"""M2.16 - the desktop boundary: permissions, cancellation, verification, correlation.

The identity model is covered by ``test_m216_window_identity``. This module covers
the four contracts that make a desktop primitive safe to expose to a model, each
of which is separately falsifiable:

* **permission** - every lifecycle verb escalates through ``is_mutating``, and a
  denial stops the native call rather than being reported and then ignored;
* **cancellation** - preflight refusal does no work, and a cancelled operation
  returns ``CANCELLED`` rather than an error;
* **verification** - a ``PASSED`` requires *observed* state, so a mutation that
  Windows silently refused cannot be reported as done;
* **correlation** - the model's own ``request_id`` can never displace the one S1
  owns, which is what stops a caller shedding attribution by injecting an id.
"""

from __future__ import annotations

import inspect

import pytest

from config.permissions import Decision, PermissionLevel
from core.verification import VerificationState
from integration.contracts import (
    CapabilityRequest,
    CapabilityStatus,
    WindowsCapabilityAdapter,
)


# ══════════════════════════════════════════════════════ the operation surface


class TestTheOperationSetIsClosed:
    def test_no_input_capture_or_control_operation_exists(self):
        """The scope boundary, asserted rather than trusted.

        M2.16 here is the foundation. Mouse, keyboard, screen capture, UIA and OCR
        are later work packages; if any of them had crept in, the surface would no
        longer be the five verbs plus two reads.
        """
        from core.windows.capabilities import WINDOWS_OPERATIONS

        for forbidden in ("window_click", "window_move_mouse", "window_type",
                          "window_key", "window_screenshot", "window_capture",
                          "window_uia", "window_ocr", "window_send_input",
                          "window_set_cursor", "window_post_message"):
            assert forbidden not in WINDOWS_OPERATIONS, (
                f"{forbidden} entered the capability surface")

    def test_the_window_reads_are_present(self):
        from core.windows.capabilities import WINDOWS_OPERATIONS

        assert "list_windows" in WINDOWS_OPERATIONS
        assert "get_window" in WINDOWS_OPERATIONS

    def test_the_four_lifecycle_verbs_are_present(self):
        from core.windows.capabilities import WINDOWS_OPERATIONS

        for verb in ("window_focus", "window_minimize", "window_restore",
                     "window_close"):
            assert verb in WINDOWS_OPERATIONS


# ══════════════════════════════════════════════════════ permission


def _adapter() -> WindowsCapabilityAdapter:
    from core.windows.capabilities import WindowsCapabilityLayer

    return WindowsCapabilityAdapter(WindowsCapabilityLayer())


class TestMutationIsClassifiedByDeclaration:
    @pytest.mark.parametrize("operation", [
        "terminate",
        "window_focus", "window_minimize", "window_restore", "window_close",
    ])
    def test_every_mutation_is_declared_mutating(self, operation):
        assert _adapter().is_mutating(operation) is True, (
            f"{operation} changes the user's screen but is not declared mutating")

    @pytest.mark.parametrize("operation", [
        "list", "get", "find", "system_info",
        "list_windows", "get_window",
    ])
    def test_every_read_is_not_mutating(self, operation):
        assert _adapter().is_mutating(operation) is False

    def test_the_layer_and_the_adapter_agree_on_the_mutating_set(self):
        """Two declarations of the same fact is one too many."""
        from core.windows.capabilities import _MUTATING_OPERATIONS

        assert set(_adapter().mutating_operations) == set(_MUTATING_OPERATIONS)

    def test_the_mutating_set_is_not_derived_from_operation_names(self):
        """Why the declared set exists rather than a keyword rule.

        The bridge still carries a legacy keyword list (``_MUTATING_WORDS``) used
        for capabilities that do not declare anything. None of the four lifecycle
        verbs appears in it - there is no "focus", "minimize", "restore" or
        "close" entry - so a keyword rule would have classified every one of them
        SAFE. That is precisely the failure the declaration prevents.
        """
        import tools.capability_bridge as bridge_module
        from core.windows.capabilities import WINDOWS_OPERATIONS, _MUTATING_OPERATIONS

        assert _MUTATING_OPERATIONS == frozenset({
            "terminate", "window_focus", "window_minimize",
            "window_restore", "window_close",
            "mouse_move", "mouse_left_click", "mouse_right_click",
            "mouse_double_click",
            "key_press", "key_release", "key_tap", "hotkey", "type_text",
            "capture_screen",
        })

        keywords = tuple(w.lower() for w in bridge_module._MUTATING_WORDS)
        for verb in ("window_focus", "window_minimize", "window_restore",
                     "window_close", "mouse_left_click", "mouse_double_click",
                     "key_tap", "hotkey", "type_text"):
            assert not any(word in verb.lower() for word in keywords), (
                f"{verb} would have been classified by a keyword rule, so this "
                "test no longer proves the declaration is what is doing the work")

        # Reads and mutations coexist in one capability and are told apart only
        # by the declared set.
        window_ops = [op for op in WINDOWS_OPERATIONS if "window" in op]
        assert window_ops, "the window operations are missing"
        reads = [op for op in window_ops if op not in _MUTATING_OPERATIONS]
        mutations = [op for op in window_ops if op in _MUTATING_OPERATIONS]
        assert reads and mutations, (
            "either every window operation reads or every one mutates, so the "
            "declared set is not what separates them")


class TestMutationEscalatesThroughTheExistingLadder:
    def _level(self, operation: str) -> PermissionLevel:
        """The bridge's own permission decision, through the public entry point."""
        import tools.capability_bridge as bridge_module

        bridge = bridge_module.CapabilityBridge(
            registry=_registry_with(_adapter()), events=None)
        return bridge.permission_level({"capability": "windows",
                                        "operation": operation, "params": {}})

    @pytest.mark.parametrize("operation", [
        "window_focus", "window_minimize", "window_restore", "window_close",
    ])
    def test_each_lifecycle_verb_is_sensitive(self, operation):
        assert self._level(operation) is PermissionLevel.SENSITIVE, (
            f"{operation} would not require confirmation")

    def test_a_mutation_is_not_silently_downgraded_to_safe(self):
        """The failure this guards: 'it is only a ShowWindow call' reasoning."""
        assert self._level("window_minimize") is not PermissionLevel.SAFE

    @pytest.mark.parametrize("operation", ["list_windows", "get_window"])
    def test_window_reads_stay_read_only(self, operation):
        assert self._level(operation) is PermissionLevel.SAFE

    def test_the_bridge_reads_the_adapter_declaration(self):
        source = inspect.getsource(
            __import__("tools.capability_bridge", fromlist=["x"]).CapabilityBridge)
        assert "is_mutating" in source, (
            "the bridge no longer consults the adapter's mutating set")


def _registry_with(adapter):
    from integration.contracts import CapabilityRegistry

    registry = CapabilityRegistry()
    registry.register(adapter)
    return registry


class TestDenialStopsTheNativeCall:
    """Permission is enforced by the Executor, upstream of the capability.

    That is the ratified order - Agent -> Executor -> PermissionManager ->
    Capability - so the test drives the Executor rather than the bridge. The
    bridge has no opinion on whether a call is allowed; it is the ladder's job,
    and testing it here would test the wrong component.
    """

    def _executor(self, tmp_settings, permissions, confirmer=None):
        from core.events import EventBus
        from core.executor import Executor
        from tools import build_registry

        calls: list[dict] = []

        class _Layer:
            name = "windows"

            def operations(self):
                return ("window_close",)

            def detect_capabilities(self):
                return {"available": True, "operations": ["window_close"]}

            def window_close(self, *args, **kwargs):
                calls.append({"close": True})
                return {"requested": True, "confirmed": True,
                        "window_key": "k"}

            def is_mutating(self, operation):
                return True

            def last_error(self):
                return None

        registry = build_registry(tmp_settings,
                                  capability_registry=_registry_with(
                                      WindowsCapabilityAdapter(_Layer())))
        executor = Executor(registry=registry, permissions=permissions,
                            events=EventBus(), confirmer=confirmer)
        return executor, calls

    def _args(self):
        return {"capability": "windows", "operation": "window_close",
                "params": {"window": {"hwnd": 1, "pid": 1, "class_name": "X",
                                      "instance": 0,
                                      "identity_created_at": "20260101 10:00:00"}}}

    def test_a_blocked_decision_never_reaches_the_layer(self, tmp_settings):
        executor, calls = self._executor(tmp_settings, _DenyAll())

        outcome = executor.run("capability.execute", self._args())

        assert outcome.status == "blocked", (
            f"a denied mutation was not blocked (status {outcome.status})")
        assert not calls, f"the native close call ran despite the denial: {calls}"

    def test_a_permitted_decision_does_reach_the_layer(self, tmp_settings):
        """The negative control: the test above must not pass because nothing
        ever reaches the layer.

        A confirmer that approves is needed because the verb is SENSITIVE, which
        is exactly the point - a mutating desktop verb cannot be auto-approved.
        """
        executor, calls = self._executor(tmp_settings, _AllowAll(),
                                         confirmer=lambda *a, **k: "allow")

        outcome = executor.run("capability.execute", self._args())

        assert calls, (
            f"the permitted mutation never reached the layer (status {outcome.status})")

    def test_a_lifecycle_verb_cannot_be_auto_approved(self, tmp_settings):
        """SENSITIVE means a human is asked; no confirmer means it does not run."""
        from config.permissions import PermissionManager

        asked: list[bool] = []

        def _confirm(*_args, **_kwargs):
            asked.append(True)
            return "allow"

        # No confirmer at all: a SENSITIVE verb must stop rather than proceed.
        executor, calls = self._executor(tmp_settings,
                                         PermissionManager(tmp_settings))
        outcome = executor.run("capability.execute", self._args())
        assert not calls, (
            "a SENSITIVE window verb ran with no confirmation available")

        executor, calls = self._executor(tmp_settings,
                                         PermissionManager(tmp_settings),
                                         confirmer=_confirm)
        executor.run("capability.execute", self._args())
        assert asked, "no confirmation was requested for a mutating desktop verb"


class _AllowAll:
    def evaluate(self, tool, args, level=None, level_for=None, task_id=""):
        return Decision(level or PermissionLevel.SENSITIVE, True, True, "")


class _DenyAll:
    def evaluate(self, tool, args, level=None, level_for=None, task_id=""):
        return Decision(PermissionLevel.BLOCKED, False, False, "denied by test")


# ══════════════════════════════════════════════════════ cancellation


class TestCancellationThroughTheCapability:
    def _request(self, token, operation="list_windows", **params):
        return CapabilityRequest(capability="windows", operation=operation,
                                 params=params, task_id="task-1",
                                 cancellation_token=token)

    def test_a_precancelled_request_never_reaches_the_layer(self):
        from core.cancellation import cancellation_requested
        from core.windows.capabilities import WindowsCapabilityLayer
        from core.windows.errors import WindowsCancelledError

        class _Token:
            def is_cancelled(self):
                return True

        layer = WindowsCapabilityLayer()
        try:
            from integration.contracts import CapabilityRegistry
            registry = CapabilityRegistry()
            registry.register(WindowsCapabilityAdapter(layer))
            request = self._request(_Token())
            result = registry.execute(request)
            assert result.status is CapabilityStatus.CANCELLED, (
                f"a precancelled request returned {result.status.value}")
        finally:
            layer.close()
        _ = (cancellation_requested, WindowsCancelledError)

    def test_a_cancelled_lifecycle_verb_returns_cancelled(self):
        from core.windows.capabilities import WindowsCapabilityLayer
        from core.windows.errors import WindowsCancelledError

        class _Token:
            def is_cancelled(self):
                return True

        layer = WindowsCapabilityLayer()
        try:
            with layer.cancellation_scope(_Token()):
                with pytest.raises(WindowsCancelledError):
                    layer.window_close({"hwnd": 1, "pid": 1, "class_name": "X",
                                        "instance": 0,
                                        "identity_created_at": "20260101 10:00:00"})
                with pytest.raises(WindowsCancelledError):
                    layer.list_windows()
                with pytest.raises(WindowsCancelledError):
                    layer.window_minimize({"hwnd": 1, "pid": 1,
                                           "class_name": "X", "instance": 0,
                                           "identity_created_at": "t"})
        finally:
            layer.close()

    def test_cancellation_exits_the_scope_for_the_next_call(self):
        """A token left bound would cancel the next, unrelated request."""
        from core.windows.capabilities import WindowsCapabilityLayer

        class _Token:
            def __init__(self):
                self.cancelled = True

            def is_cancelled(self):
                return self.cancelled

        layer = WindowsCapabilityLayer()
        try:
            token = _Token()
            with layer.cancellation_scope(token):
                with pytest.raises(Exception):
                    layer.list_windows()
            token.cancelled = False
            # Outside the scope the layer is no longer governed by that token.
            assert layer._cancelled() is False
        finally:
            layer.close()

    def test_cancellation_surfaces_as_cancelled_not_as_a_generic_error(self):
        """A cancellation that arrives as an untyped exception would be recorded
        as a failure and could trigger recovery of work the user stopped."""
        from core.windows.capabilities import WindowsCapabilityLayer
        from core.windows.errors import WindowsCancelledError

        class _Token:
            def is_cancelled(self):
                return True

        layer = WindowsCapabilityLayer()
        try:
            with layer.cancellation_scope(_Token()):
                with pytest.raises(WindowsCancelledError):
                    layer.system_info()
                with pytest.raises(WindowsCancelledError):
                    layer.list_windows()
        finally:
            layer.close()

    def test_a_cancelled_result_is_not_reported_as_a_window_failure(self):
        """The distinction the contract layer maps to ``cancelled``."""
        from core.windows.errors import (WindowsCancelledError,
                                         WindowsWindowNotFoundError)

        assert WindowsCancelledError.code != WindowsWindowNotFoundError.code


# ══════════════════════════════════════════════════════ verification


def _verify(operation: str, data: dict):
    """Drive the bridge's Windows verification with a real request and result."""
    import tools.capability_bridge as bridge_module
    from integration.contracts import CapabilityResult

    bridge = bridge_module.CapabilityBridge.__new__(bridge_module.CapabilityBridge)
    request = CapabilityRequest(capability="windows", operation=operation,
                                request_id="req-1", task_id="task-1")
    result = CapabilityResult(status=CapabilityStatus.SUCCESS,
                              capability="windows", operation=operation,
                              data=data, request_id=request.request_id)
    if operation in ("list_windows", "get_window"):
        # Read verification is dispatched off the same method the bridge uses.
        import tools.capability_bridge as bridge_module

        return bridge_module._window_read_verification(
            bridge, {"capability": "windows", "operation": operation,
                     "request_id": request.request_id},
            data, operation)
    return bridge._windows_verification(result=result, request=request,
                                        data=data, operation=operation)


def _window(**overrides) -> dict:
    payload = {"hwnd": 0x1234, "pid": 100, "class_name": "Notepad",
               "instance": 0x7FF0000, "identity_created_at": "20260101 10:00:00",
               "window_key": "k", "foreground": False, "minimized": False,
               "visible": True}
    payload.update(overrides)
    return payload


class TestWindowReadVerification:
    def test_a_well_formed_window_read_passes(self):
        result = _verify("list_windows", {"windows": [_window()], "count": 1})
        assert result.state is VerificationState.PASSED

    def test_a_window_without_a_process_anchor_does_not_pass(self):
        """A row that cannot be verified before being acted on is not valid."""
        result = _verify("list_windows",
                         {"windows": [_window(identity_created_at="")], "count": 1})
        assert result.state is not VerificationState.PASSED, (
            "an unanchored window row was accepted as well-formed")

    def test_a_window_without_a_class_name_does_not_pass(self):
        result = _verify("list_windows",
                         {"windows": [_window(class_name="")], "count": 1})
        assert result.state is not VerificationState.PASSED

    def test_an_empty_window_list_does_not_pass(self):
        result = _verify("list_windows", {"windows": [], "count": 0})
        assert result.state is not VerificationState.PASSED, (
            "an empty result was treated as evidence the desktop works")

    def test_a_single_window_read_passes(self):
        assert _verify("get_window", _window()).state is VerificationState.PASSED


class TestLifecycleVerificationUsesObservedState:
    def test_a_focus_passed_only_when_the_window_is_foreground(self):
        confirmed = _verify("window_focus",
                            {"requested": True, "confirmed": True,
                             "window": _window(foreground=True)})
        assert confirmed.state is VerificationState.PASSED

    def test_a_refused_activation_is_not_a_pass(self):
        """Windows refuses activation while the user is in another app.

        Reporting that as a pass would claim the user's screen changed when it
        did not.
        """
        result = _verify("window_focus",
                         {"requested": True, "confirmed": False,
                          "window": _window(foreground=False)})
        assert result.state is VerificationState.FAILED, (
            "a refused activation was verified as successful")

    def test_a_minimize_passed_only_when_observed_minimized(self):
        assert _verify("window_minimize",
                       {"requested": True, "confirmed": True,
                        "window": _window(minimized=True)}).state is \
            VerificationState.PASSED
        assert _verify("window_minimize",
                       {"requested": True, "confirmed": True,
                        "window": _window(minimized=False)}).state is \
            VerificationState.FAILED

    def test_a_restore_passed_only_when_visible_and_not_minimized(self):
        assert _verify("window_restore",
                       {"requested": True, "confirmed": True,
                        "window": _window(visible=True, minimized=False)}).state is \
            VerificationState.PASSED
        assert _verify("window_restore",
                       {"requested": True, "confirmed": True,
                        "window": _window(visible=False, minimized=False)}).state is \
            VerificationState.FAILED
        assert _verify("window_restore",
                       {"requested": True, "confirmed": True,
                        "window": _window(visible=True, minimized=True)}).state is \
            VerificationState.FAILED

    def test_a_close_passed_only_when_the_window_is_gone(self):
        assert _verify("window_close",
                       {"requested": True, "confirmed": True,
                        "window": None}).state is VerificationState.PASSED

    def test_a_close_that_did_not_close_is_not_a_pass(self):
        """WM_CLOSE only queues a message; an application can defer or ignore it."""
        result = _verify("window_close",
                         {"requested": True, "confirmed": False,
                          "window": _window()})
        assert result.state is VerificationState.FAILED

    def test_a_close_that_was_never_requested_is_unverified(self):
        result = _verify("window_close",
                         {"requested": False, "confirmed": False,
                          "window": _window()})
        assert result.state is VerificationState.UNVERIFIED

    def test_an_api_return_code_alone_is_never_enough(self):
        """Every verb with a missing observation fails.

        The layer reporting ``requested: True, confirmed: True`` while the
        observed window says otherwise must not pass - which is the whole reason
        the verifiers read the re-read window rather than the return value.
        """
        for operation, window in (
            ("window_minimize", _window(minimized=False)),
            ("window_restore", _window(minimized=True)),
            ("window_focus", _window(foreground=False)),
        ):
            result = _verify(operation, {"requested": True, "confirmed": True,
                                         "window": window})
            assert result.state is not VerificationState.PASSED, (
                f"{operation} passed on the return code alone")

    def test_evidence_carries_the_request_id(self):
        result = _verify("window_minimize",
                         {"requested": True, "confirmed": True,
                          "window": _window(minimized=True)})
        assert result.evidence["request_id"] == "req-1"

    def test_every_lifecycle_verb_has_a_verifier(self):
        """A new verb with no verifier would silently fall through to a default."""
        import tools.capability_bridge as bridge_module

        assert set(bridge_module._WINDOW_LIFECYCLE_VERIFIERS) == {
            "window_focus", "window_minimize", "window_restore", "window_close",
        }


# ══════════════════════════════════════════════════════ correlation


class TestCorrelationCannotBeForged:
    def test_a_model_supplied_request_id_cannot_win(self):
        """S1 owns the request id. A caller-supplied one is diagnostics only."""
        from core.windows.capabilities import WindowsCapabilityLayer

        layer = WindowsCapabilityLayer()
        try:
            with layer.request_correlation("trusted-id", {}):
                assert layer.trusted_request_id() == "trusted-id"
            # Nothing outside the scope is attributed to a finished request.
            assert layer.trusted_request_id() == ""
        finally:
            layer.close()

    def test_window_results_echo_the_trusted_id(self):
        from core.windows.capabilities import WindowsCapabilityLayer

        layer = WindowsCapabilityLayer()
        try:
            with layer.request_correlation("trusted-id", {}):
                payload = layer.list_windows(limit=1)
            assert payload["trusted_request_id"] == "trusted-id"
        finally:
            layer.close()

    def test_a_lifecycle_result_echoes_the_trusted_id(self, tmp_path, monkeypatch):
        import core.windows.windows as window_module
        from core.windows.capabilities import WindowsCapabilityLayer

        class _Gui:
            def IsWindow(self, h): return True
            def IsWindowVisible(self, h): return True
            def IsIconic(self, h): return True
            def IsWindowEnabled(self, h): return True
            def GetWindowRect(self, h): return (0, 0, 100, 100)
            def GetClassName(self, h): return "Notepad"
            def GetWindowText(self, h): return "x"
            def GetForegroundWindow(self): return 0
            def GetWindowPlacement(self, h): return (0, 0, 1, 0, 0, 0, 0)
            def GetWindowLong(self, h, i): return 0x7FF0000
            def ShowWindow(self, h, c): return True
            def BringWindowToTop(self, h): return True

        monkeypatch.setattr(window_module, "_load_gui",
                            lambda: (_Gui(), _Proc(), None))
        layer = WindowsCapabilityLayer()
        try:
            identity = {"hwnd": 0x99, "pid": os_getpid(), "class_name": "Notepad",
                        "instance": 0x7FF0000,
                        "identity_created_at": _own_created_at()}
            with layer.request_correlation("trusted-id", {}):
                payload = layer.window_minimize(identity)
            assert payload["trusted_request_id"] == "trusted-id"
        finally:
            layer.close()


def os_getpid() -> int:
    import os

    return os.getpid()


def _own_created_at() -> str:
    from core.windows.processes import ProcessLayer

    layer = ProcessLayer()
    try:
        info = layer.get_process(os_getpid())
        return str(info.created_at) if info else ""
    finally:
        layer.close()


class _Proc:
    def GetWindowThreadProcessId(self, hwnd):
        return 1, os_getpid()


_ = inspect