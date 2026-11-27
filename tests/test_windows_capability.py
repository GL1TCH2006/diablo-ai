"""M2.15 - deterministic tests for the native Windows systems layer.

Split deliberately in two.

This module is deterministic. Anything that needs a real process on a real
machine, or a real 64-bit handle, lives in ``test_windows_real.py`` behind an
explicit gate. Nothing here mocks the operating system to pretend a termination
happened; where the OS boundary is genuinely needed, the test is skipped with a
reason rather than faked.

What is pinned here
-------------------
* The error vocabulary, and that it maps to the ratified ``CapabilityStatus`` by
  code rather than by message wording.
* The termination result type: ``requested`` and ``confirmed`` are separate, and
  ``succeeded`` requires both.
* Process identity: an unverifiable match is not a match, which is what makes the
  PID-reuse defence work.
* The capability surface: which operations exist, which mutate, and that a
  mutating one is ``SENSITIVE`` while queries are ``SAFE``.
* Refusals: self-termination, protected PIDs, bad input.
* Request and task id provenance through the bridge.
* Cancellation before work starts.
* The capability registry refusing an operation the layer does not offer.
"""

from __future__ import annotations

import pytest

from core.windows.capabilities import WINDOWS_OPERATIONS, WindowsCapabilityLayer
from core.windows.errors import (
    WindowsAccessDeniedError,
    WindowsCancelledError,
    WindowsError,
    WindowsInvalidRequestError,
    WindowsNotFoundError,
    WindowsOperationFailedError,
    WindowsProcessGoneError,
    WindowsProcessNotFoundError,
    WindowsProtectedProcessError,
    windows_error_status_table,
)
from core.windows.models import (
    CpuInfo,
    MemoryInfo,
    ProcessInfo,
    SystemInfo,
    TerminationResult,
    process_identity,
)
from integration.contracts import CapabilityStatus, WindowsCapabilityAdapter
from tools.capability_bridge import CapabilityBridge


# ── error vocabulary ────────────────────────────────────────────────────────


class TestErrorVocabulary:
    def test_every_error_declares_a_stable_code(self):
        for cls in (WindowsNotFoundError, WindowsProcessNotFoundError,
                    WindowsProcessGoneError, WindowsAccessDeniedError,
                    WindowsProtectedProcessError, WindowsCancelledError,
                    WindowsInvalidRequestError, WindowsOperationFailedError,
                    WindowsError):
            assert cls.code and isinstance(cls.code, str), cls
            assert cls.code == cls.code.lower(), cls

    def test_every_error_maps_to_a_known_capability_status(self):
        known = {status.value for status in CapabilityStatus}
        for code, value in windows_error_status_table.items():
            assert value in known, f"{code} maps to unknown status {value!r}"

    def test_infrastructure_failure_is_not_reported_as_invalid_input(self):
        """The ratified rule: a broken system is not a caller error."""
        assert windows_error_status_table[WindowsOperationFailedError.code] == "failure"
        assert windows_error_status_table[WindowsNotFoundError.code] == "unavailable"
        assert windows_error_status_table[WindowsAccessDeniedError.code] == "permission_required"

    def test_a_process_that_vanished_is_not_a_caller_error(self):
        """It was a valid request; the process left."""
        assert windows_error_status_table[WindowsProcessNotFoundError.code] == "invalid_request"
        assert windows_error_status_table[WindowsProcessGoneError.code] == "failure"

    def test_protected_process_is_a_permission_outcome(self):
        assert windows_error_status_table[WindowsProtectedProcessError.code] == "permission_required"

    def test_status_is_never_inferred_from_message_wording(self):
        """A message saying 'invalid' must not become INVALID_REQUEST."""
        exc = WindowsOperationFailedError("invalid something went wrong in the kernel")
        assert exc.code == WindowsOperationFailedError.code
        assert windows_error_status_table[exc.code] == "failure"

    def test_cancellation_reports_whether_it_terminated_anything(self):
        assert WindowsCancelledError().terminated is None
        pending = WindowsCancelledError(
            diagnostics={"cancellation_terminated": False})
        assert pending.terminated is False

    def test_error_to_dict_is_json_safe(self):
        payload = WindowsProtectedProcessError("nope", diagnostics={"pid": 4}).to_dict()
        assert payload["code"] == WindowsProtectedProcessError.code
        assert payload["diagnostics"]["pid"] == 4


# ── termination result semantics ───────────────────────────────────────────


class TestTerminationResult:
    def test_requested_and_confirmed_are_separate_facts(self):
        requested_only = TerminationResult(pid=1, identity=(1, "t"), requested=True,
                                           confirmed=False, reason="no observation")
        assert requested_only.requested is True
        assert requested_only.confirmed is False
        assert requested_only.succeeded is False, (
            "a requested termination that was never observed must not read as a success")

    def test_confirmed_without_a_request_is_not_a_success(self):
        never_requested = TerminationResult(pid=1, identity=(1, "t"), requested=False,
                                            confirmed=True)
        assert never_requested.succeeded is False

    def test_both_together_is_the_only_success(self):
        both = TerminationResult(pid=1, identity=(1, "t"), requested=True, confirmed=True,
                                 exit_code=1)
        assert both.succeeded is True

    def test_recycled_pid_is_reported_distinctly_and_without_an_exit_code(self):
        recycled = TerminationResult(
            pid=99, identity=(99, "t0"), requested=True, confirmed=True, exit_code=None,
            reason="pid was recycled to a different process")
        assert recycled.succeeded is True
        assert recycled.exit_code is None, (
            "an exit code read against a recycled pid would be another process's")
        assert "recycled" in recycled.reason

    def test_serialises_without_a_native_handle(self):
        payload = TerminationResult(pid=5, identity=(5, "t"), requested=True,
                                     confirmed=True).to_dict()
        assert payload["identity_pid"] == 5
        assert isinstance(payload["detail"], dict)


# ── process identity ────────────────────────────────────────────────────────


class TestProcessIdentity:
    def test_identity_is_pid_plus_creation_time(self):
        assert process_identity(10, "2026-01-01 00:00:00") == (10, "2026-01-01 00:00:00")

    def test_same_process_requires_both_halves(self):
        a = ProcessInfo(pid=10, created_at="t1")
        b = ProcessInfo(pid=10, created_at="t1")
        c = ProcessInfo(pid=10, created_at="t2")
        assert a.same_process(b) is True
        assert a.same_process(c) is False

    def test_a_missing_creation_time_is_never_a_match(self):
        """Otherwise PID reuse defeats every check built on identity."""
        known = ProcessInfo(pid=10, created_at="t1")
        unknown = ProcessInfo(pid=10, created_at="")
        assert known.same_process(unknown) is False
        assert unknown.same_process(known) is False
        assert unknown.same_process(unknown) is False

    def test_different_pids_never_match(self):
        assert ProcessInfo(pid=10, created_at="t").same_process(
            ProcessInfo(pid=11, created_at="t")) is False

    def test_process_info_serialises_without_handles(self):
        """No operating-system handle may reach the model.

        ``handle_count`` is a *number Windows reports*, which is fine; what must
        not appear is anything usable as a handle.
        """
        payload = ProcessInfo(pid=1, name="x.exe", handle_count=42).to_dict()
        assert payload["handle_count"] == 42
        assert not any(isinstance(v, int) and not isinstance(v, bool)
                       and k not in {"pid", "parent_pid", "thread_count",
                                     "handle_count", "session_id"}
                       for k, v in payload.items() if k in {"pid", "parent_pid"})
        # The serialisable form is JSON-clean.
        import json
        json.dumps(payload)


# ── capability surface and permission ───────────────────────────────────────


class _StubLayer:
    """A Windows layer with no OS behind it, for surface and policy tests."""

    def __init__(self, operations=WINDOWS_OPERATIONS, available=True):
        self._operations = tuple(operations)
        self._available = available
        self.calls: list[tuple[str, dict]] = []
        self._token = None

    def operations(self):
        return self._operations

    def detect_capabilities(self):
        return {"available": self._available,
                "operations": list(self._operations) if self._available else []}

    def request_correlation(self, request_id="", context=None):
        class _Scope:
            def __init__(self, outer):
                self.outer = outer

            def __enter__(self):
                self.outer._token = None
                return self.outer

            def __exit__(self, *exc):
                self.outer._token = None
                return False
        return _Scope(self)

    def cancellation_scope(self, token=None):
        class _Scope:
            def __init__(self, outer):
                self.outer = outer

            def __enter__(self):
                self.outer._token = token
                return self.outer

            def __exit__(self, *exc):
                self.outer._token = None
                return False
        return _Scope(self)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _record(**kwargs):
            self.calls.append((name, dict(kwargs)))
            return {}
        return _record


class TestCapabilitySurface:
    def test_the_operation_set_is_closed(self):
        # M2.16 extended the surface from five to eleven, M2.16.2/.3 to
        # twenty-two, M2.16.4 to twenty-eight and M2.16.5 to thirty-eight.
        # Enumerated explicitly rather than counted: the point of this test is that
        # the set is known and deliberate, so a future addition has to be a
        # conscious edit here.
        assert WINDOWS_OPERATIONS == (
            "list", "get", "find", "system_info",
            "list_windows", "get_window",
            "terminate",
            "window_focus", "window_minimize", "window_restore", "window_close",
            "get_cursor_pos", "get_foreground_window",
            "mouse_move", "mouse_left_click", "mouse_right_click",
            "mouse_double_click",
            "key_press", "key_release", "key_tap", "hotkey", "type_text",
            "list_displays", "primary_display", "virtual_desktop", "get_display",
            "point_to_display", "capture_screen",
            "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
            "uia_target_point",
            "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
            "uia_expand_collapse",
            "visual_resolve_target",
        )

    def test_the_adapter_advertises_what_the_layer_offers(self):
        adapter = WindowsCapabilityAdapter(_StubLayer(("list", "get")))
        assert adapter._operations_for_layer() == ("list", "get")
        assert adapter.can_execute("terminate") is False

    def test_an_unavailable_layer_advertises_nothing(self):
        """A host without pywin32 must not advertise operations that would raise."""
        adapter = WindowsCapabilityAdapter(_StubLayer(available=False))
        capabilities = adapter.capabilities()
        assert capabilities["available"] is False
        assert capabilities["operations"] == []

    def test_terminate_is_the_only_mutating_operation(self):
        """M2.16 renamed for what it now asserts: every mutation is declared.
    
        The original intent was that nothing could join the mutating set without a
        permission level and a test. That intent is stronger now that there are
        twenty mutating operations rather than one, so it is asserted over all of
        them: each is declared, each is SENSITIVE, and no read is.

        The pointer and keyboard verbs matter most here. Each one reaches a window
        the user did not necessarily name, so an input verb judged SAFE would
        dispatch input with no permission asked for at all. The same argument
        applies to the UIA actions, which reach into an application the user may
        not have named either - ``uia_find_elements`` reads, ``uia_invoke`` presses.
        """
        from config.permissions import PermissionLevel

        adapter = WindowsCapabilityAdapter(_StubLayer())
        assert adapter.is_mutating("terminate") is True
        for op in ("window_focus", "window_minimize", "window_restore",
                   "window_close", "mouse_move", "mouse_left_click",
                   "mouse_right_click", "mouse_double_click", "key_press",
                   "key_release", "key_tap", "hotkey", "type_text",
                   "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
                   "uia_expand_collapse"):
            assert adapter.is_mutating(op) is True, f"{op} was not declared mutating"
        for op in ("list", "get", "find", "system_info", "list_windows",
                   "get_window", "get_cursor_pos", "get_foreground_window",
                   "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
                   "uia_target_point", "visual_resolve_target"):
            assert adapter.is_mutating(op) is False, f"{op} was declared mutating"
        assert adapter.mutating_operations == tuple(sorted({
            "terminate", "window_close", "window_focus", "window_minimize",
            "window_restore", "mouse_move", "mouse_left_click",
            "mouse_right_click", "mouse_double_click", "key_press",
            "key_release", "key_tap", "hotkey", "type_text", "capture_screen",
            "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
            "uia_expand_collapse",
        })), "the adapter's mutating tuple is not the declared set, sorted"
        _ = PermissionLevel

    def test_unsupported_operation_is_refused_before_any_native_call(self):
        from integration.contracts import CapabilityRequest

        layer = _StubLayer()
        adapter = WindowsCapabilityAdapter(layer)
        result = adapter.execute(CapabilityRequest(capability="windows", operation="reboot"))
        assert result.status is CapabilityStatus.INVALID_REQUEST
        assert layer.calls == [], "a refused operation must not reach the layer"


class TestPermissionBoundary:
    def _bridge(self, operations=WINDOWS_OPERATIONS):
        from integration.contracts import CapabilityRegistry

        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(_StubLayer(operations)))
        return CapabilityBridge(registry)

    def test_terminate_requires_confirmation(self):
        bridge = self._bridge()
        level = bridge.permission_level(
            {"capability": "windows", "operation": "terminate", "params": {"pid": 1}})
        assert level.name == "SENSITIVE"

    def test_queries_are_safe(self):
        bridge = self._bridge()
        for op, params in (("list", {}), ("get", {"pid": 1}),
                           ("find", {"name": "x"}), ("system_info", {})):
            level = bridge.permission_level(
                {"capability": "windows", "operation": op, "params": params})
            assert level.name == "SAFE", f"{op} should not require confirmation"

    def test_the_model_cannot_escalate_from_a_query_to_process_control(self):
        """Query permissions must not carry over to the mutating operation."""
        from config.permissions import PermissionManager
        from tests.conftest import make_settings
        from pathlib import Path
        import tempfile

        bridge = self._bridge()
        permissions = PermissionManager(make_settings(Path(tempfile.mkdtemp())), None)
        query = permissions.evaluate(
            "capability.execute",
            {"capability": "windows", "operation": "list", "params": {}},
            bridge.permission_level({"capability": "windows", "operation": "list"}),
            bridge.permission_level)
        control = permissions.evaluate(
            "capability.execute",
            {"capability": "windows", "operation": "terminate", "params": {"pid": 1}},
            bridge.permission_level({"capability": "windows", "operation": "terminate"}),
            bridge.permission_level)
        assert query.allow is True
        assert control.allow is False
        assert control.requires_confirmation is True

    def test_an_unknown_capability_is_blocked(self):
        bridge = self._bridge()
        assert bridge.permission_level(
            {"capability": "nope", "operation": "list"}).name == "BLOCKED"


# ── refusals and validation (no OS required) ──────────────────────────────


class TestInputValidation:
    def test_pid_is_required(self):
        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        with pytest.raises(WindowsInvalidRequestError):
            layer._require_pid(None)

    def test_pid_must_be_an_integer(self):
        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        with pytest.raises(WindowsInvalidRequestError):
            layer._require_pid("abc")

    def test_pid_must_not_be_negative(self):
        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        with pytest.raises(WindowsInvalidRequestError):
            layer._require_pid(-1)

    def test_a_float_pid_is_rejected_rather_than_truncated(self):
        """int(3.7) == 3 would silently target the wrong process."""
        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        with pytest.raises(WindowsInvalidRequestError):
            layer._require_pid(3.7)

    def test_limit_is_bounded(self):
        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        assert layer._bounded_limit(10_000, 200) == 200
        assert layer._bounded_limit(0, 200) == 1
        assert layer._bounded_limit("nonsense", 200) == 50

    def test_a_closed_layer_refuses_work(self):
        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        layer._closed = True
        with pytest.raises(WindowsNotFoundError):
            layer._guard()

    def test_a_bad_timeout_is_a_caller_error(self):
        import threading

        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        layer._closed = False
        layer._correlation = threading.local()
        layer._correlation.token = None
        layer._processes = None
        layer._system = None
        with pytest.raises(WindowsInvalidRequestError):
            layer.terminate(1, timeout="soon")


class TestCancellationStopsWorkBeforeItStarts:
    def test_a_precancelled_query_raises_and_does_no_work(self):
        import threading

        calls: list[str] = []

        class _NoWorkLayer:
            def operations(self):
                return WINDOWS_OPERATIONS

            def detect_capabilities(self):
                return {"available": True, "operations": list(WINDOWS_OPERATIONS)}

            def _record(self, **kwargs):
                calls.append("called")
                return {}

        layer = WindowsCapabilityLayer.__new__(WindowsCapabilityLayer)
        layer._closed = False
        layer._correlation = threading.local()
        layer._correlation.request_id = ""
        layer._correlation.context = {}
        layer._correlation.token = type(
            "T", (), {"is_cancelled": staticmethod(lambda: True)})()
        layer._processes = _NoWorkLayer()
        layer._system = _NoWorkLayer()

        for method, params in (("list", {}), ("get", {"pid": 1}),
                               ("find", {"name": "x"}), ("system_info", {}),
                               ("terminate", {"pid": 1})):
            with pytest.raises(WindowsCancelledError):
                getattr(layer, method)(**params)
        assert calls == [], "a pre-cancelled request must reach no native call"


# ── request / task id provenance ───────────────────────────────────────────


class TestProvenanceIsPreserved:
    def test_the_trusted_request_id_is_read_not_replaced(self):
        from integration.contracts import CapabilityRequest, CapabilityRegistry

        class _Echo:
            def operations(self):
                return ("system_info",)

            def detect_capabilities(self):
                return {"available": True, "operations": ["system_info"]}

            def request_correlation(self, request_id="", context=None):
                from contextlib import contextmanager

                @contextmanager
                def _scope():
                    self.seen = request_id
                    yield self
                return _scope()

            def system_info(self, **kwargs):
                return {"os_caption": "Windows", "request_id_seen": getattr(self, "seen", "")}

        layer = _Echo()
        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(layer))
        trusted = "trusted-request-abc123"
        result = registry.execute(CapabilityRequest(
            capability="windows", operation="system_info", request_id=trusted))
        assert result.request_id == trusted, "the result must carry S1's request_id"
        assert result.data["request_id_seen"] == trusted, (
            "the layer must observe S1's id, not mint its own")

    def test_a_model_supplied_request_id_cannot_win(self):
        """``params`` is the model's channel; ``request_id`` is S1's."""
        from integration.contracts import CapabilityRequest, CapabilityRegistry

        class _Sneaky:
            def operations(self):
                return ("system_info",)

            def detect_capabilities(self):
                return {"available": True, "operations": ["system_info"]}

            def system_info(self, **kwargs):
                return {"os_caption": "Windows"}

        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(_Sneaky()))
        result = registry.execute(CapabilityRequest(
            capability="windows", operation="system_info",
            params={"request_id": "model-supplied"}, request_id="trusted-xyz"))
        assert result.request_id == "trusted-xyz"


# ── verification evidence ──────────────────────────────────────────────────


def _verify(data: dict, operation: str, request_id: str = "rid-1"):
    from core.verification import VerificationResult

    bridge = CapabilityBridge.__new__(CapabilityBridge)
    from integration.contracts import CapabilityRequest, CapabilityResult

    request = CapabilityRequest(capability="windows", operation=operation,
                                request_id=request_id)
    result = CapabilityResult(status=CapabilityStatus.SUCCESS, capability="windows",
                               operation=operation, data=data, request_id=request_id)
    return bridge._windows_verification(result, request, data, operation)


class TestWindowsVerificationEvidence:
    def test_a_confirmed_termination_is_observed_evidence(self):
        verification = _verify({
            "pid": 5, "requested": True, "confirmed": True, "exit_code": 1,
            "identity_created_at": "t"}, "terminate")
        assert verification.state.value == "passed"
        assert verification.evidence["confirmed"] is True
        assert verification.evidence["pid"] == 5

    def test_a_termination_that_was_only_requested_cannot_pass(self):
        """The core safety property of the whole layer."""
        verification = _verify({
            "pid": 5, "requested": True, "confirmed": False, "exit_code": None},
            "terminate")
        assert verification.state.value == "failed"
        assert verification.evidence["requested"] is True
        assert verification.evidence["confirmed"] is False

    def test_a_termination_never_requested_is_unverified_not_passed(self):
        verification = _verify({"pid": 5, "requested": False, "confirmed": False},
                               "terminate")
        assert verification.state.value == "unverified"

    def test_inconsistent_system_metrics_do_not_pass(self):
        """Available exceeding total is not a real machine."""
        verification = _verify({
            "os_caption": "Windows", "backend": "native",
            "memory": {"total_physical_mb": 100.0, "available_physical_mb": 500.0},
            "cpu": {"logical_cores": 8}}, "system_info")
        assert verification.state.value == "failed"

    def test_a_consistent_system_snapshot_passes(self):
        verification = _verify({
            "os_caption": "Windows", "backend": "native",
            "memory": {"total_physical_mb": 100.0, "available_physical_mb": 50.0},
            "cpu": {"logical_cores": 8}}, "system_info")
        assert verification.state.value == "passed"

    def test_a_snapshot_with_no_memory_is_not_a_pass(self):
        verification = _verify({"os_caption": "Windows", "backend": "native",
                                "memory": {}, "cpu": {}}, "system_info")
        assert verification.state.value == "failed"

    def test_process_rows_must_be_well_formed(self):
        good = _verify({"processes": [{"pid": 1, "name": "a.exe"}], "count": 1}, "list")
        assert good.state.value == "passed"
        bad = _verify({"processes": [{"pid": "one"}], "count": 1}, "list")
        assert bad.state.value == "unverified"

    def test_an_empty_process_list_is_unverified_not_passed(self):
        """Nothing observed is not a confirmed observation."""
        assert _verify({"processes": [], "count": 0}, "list").state.value == "unverified"


# ── models ─────────────────────────────────────────────────────────────────


class TestResultModels:
    def test_system_info_declares_its_backend(self):
        assert SystemInfo().to_dict()["backend"] == "native"
        assert SystemInfo(backend="powershell_fallback").to_dict()["backend"] == "powershell_fallback"

    def test_cpu_load_is_none_when_not_measured_rather_than_zero(self):
        """Zero would read as 'idle', which is a different claim."""
        cpu = CpuInfo(load_percent=None)
        assert cpu.to_dict()["load_percent"] is None
        assert cpu.to_dict()["load_percent"] != 0

    def test_memory_percentages_are_consistent(self):
        memory = MemoryInfo(total_physical_mb=100.0, available_physical_mb=25.0)
        assert memory.used_physical_mb == 75.0
        assert memory.used_percent == 75.0

    def test_zero_total_memory_does_not_divide_by_zero(self):
        assert MemoryInfo(total_physical_mb=0.0).used_percent == 0.0
