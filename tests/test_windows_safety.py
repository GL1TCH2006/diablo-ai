"""M2.15 - what the Windows layer will and will not do to a user's machine.

The brief was "make Windows operations native instead of shelling out to
PowerShell". Enumerating 300 processes through ``Get-CimInstance`` takes seconds
and leaks a shell; ``NtQuerySystemInformation`` takes milliseconds and leaks
nothing.

Shelling out also quietly widened what the product could do. PowerShell can
express *anything*, so a capability described as "process info" inherited the
reach of the whole language. These tests pin the surface down: a closed set of
named operations, no invented identity, termination that cannot read as success
unless the exit was observed, and refusals that happen before any native call.
"""

from __future__ import annotations

import ast
import json

import pytest

from core.verification import VerificationState
from config.permissions import PermissionLevel
from core.windows.capabilities import (
    _MUTATING_OPERATIONS,
    MAX_PROCESS_RESULTS,
    WINDOWS_OPERATIONS,
    WindowsCapabilityLayer,
)
from core.windows.errors import (
    WindowsAccessDeniedError,
    WindowsCancelledError,
    WindowsError,
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
    WindowsProcessGoneError,
    WindowsProcessNotFoundError,
    WindowsProtectedProcessError,
    windows_error_status_table,
)
from core.windows.models import (
    ProcessInfo,
    TerminationResult,
    process_identity,
)


@pytest.fixture
def layer():
    native = WindowsCapabilityLayer()
    try:
        yield native
    finally:
        native.close()


# ══════════════════════════════════════════════════════ the surface is closed


class TestTheOperationSurfaceIsClosed:
    def test_the_operations_are_exactly_the_documented_ones(self):
        # M2.16 took the surface from five to eleven, and M2.16.2/.3 took it to
        # twenty-two. Enumerated rather than counted, because the property under
        # test is that the set is known and deliberate, not that it has a length.
        assert WINDOWS_OPERATIONS == (
            "list", "get", "find", "system_info",
            "list_windows", "get_window",
            "terminate",
            "window_focus", "window_minimize", "window_restore", "window_close",
            # M2.16.2 pointer.
            "get_cursor_pos", "get_foreground_window",
            "mouse_move", "mouse_left_click", "mouse_right_click",
            "mouse_double_click",
            # M2.16.3 keyboard.
            "key_press", "key_release", "key_tap", "hotkey", "type_text",
            # M2.16.4 display topology and bounded capture.
            "list_displays", "primary_display", "virtual_desktop", "get_display",
            "point_to_display", "capture_screen",
        )

    def test_every_mutation_is_declared(self):
        """Nothing joins the mutating set without a test and a permission level.

        The M2.15 wording was "terminate is the only one", which was true then
        and is not now. The property it guarded is still worth guarding, over all
        fourteen.
        """
        assert _MUTATING_OPERATIONS == frozenset({
            "terminate", "window_focus", "window_minimize",
            "window_restore", "window_close",
            "mouse_move", "mouse_left_click", "mouse_right_click",
            "mouse_double_click",
            "key_press", "key_release", "key_tap", "hotkey", "type_text",
            "capture_screen",
        }), (
            "the mutating set changed; a new mutation needs a test and a "
            "permission level like the others have")
        assert _MUTATING_OPERATIONS <= set(WINDOWS_OPERATIONS), (
            "an operation is declared mutating but is not on the surface")

    def test_reading_the_pointer_and_the_foreground_is_not_a_mutation(self):
        """Both only read OS state. Nothing about them needs permission beyond
        the read itself, and classifying them as mutating would make asking where
        the pointer is a privileged act.

        The five M2.16.4 display queries are reads too and belong here: knowing
        what displays exist is not itself privacy-sensitive. Only ``capture_screen``
        is, and it is asserted separately.
        """
        for operation in ("get_cursor_pos", "get_foreground_window",
                          "list_displays", "primary_display", "virtual_desktop",
                          "get_display", "point_to_display"):
            assert operation not in _MUTATING_OPERATIONS, (
                f"{operation} was declared mutating; it only reads")

    def test_a_screen_capture_is_declared_mutating(self):
        """The one M2.16.4 operation that is privacy-sensitive.

        Screen capture is the first operation in M2.16 that can expose something
        the user did not intend to show. It is classified mutating so that it
        reaches the bridge already escalated; leaving it unclassified would put a
        screenshot at SAFE.
        """
        assert "capture_screen" in _MUTATING_OPERATIONS, (
            "capture_screen was not declared mutating; a screenshot must never "
            "be reachable at SAFE")

    def test_an_unlisted_operation_is_refused(self):
        """A closed surface: PowerShell could express anything, so the surface
        is pinned to names rather than left open to the host language."""
        for operation in ("write_file", "eval", "shell", "spawn", "inject",
                          "enumerate_windows", "screenshot", "start_service"):
            assert not hasattr(WindowsCapabilityLayer, operation), (
                f"the layer gained a method named {operation}")
            assert operation not in WINDOWS_OPERATIONS

    def test_the_operations_property_is_a_tuple(self):
        assert isinstance(WINDOWS_OPERATIONS, tuple)

    def test_availability_is_reported_honestly(self, layer):
        details = layer.detect_capabilities()
        assert isinstance(details["available"], bool)
        if not details["available"]:
            assert details["operations"] == [], (
                "operations were advertised on a machine where the layer cannot work")


# ══════════════════════════════════════════════════════ arguments are not invented


class TestArgumentsAreValidatedBeforeAnythingNative:
    def test_a_missing_pid_is_a_caller_error(self, layer):
        with pytest.raises(WindowsInvalidRequestError):
            layer.get(None)

    def test_a_negative_pid_is_a_caller_error(self, layer):
        with pytest.raises(WindowsInvalidRequestError):
            layer.get(-1)

    def test_a_float_pid_is_refused_rather_than_truncated(self, layer):
        """``int(4.7)`` is 4. Truncating lets a caller aim at a different
        process than the one it named."""
        with pytest.raises(WindowsInvalidRequestError):
            layer.get(4.7)

    def test_a_protected_pid_is_refused_as_protected_not_as_bad_input(self, layer):
        """PID 4 is a real PID, so refusing it as 'invalid' would send the user
        looking for a typo that does not exist."""
        with pytest.raises(WindowsProtectedProcessError):
            layer.get(4)

    def test_a_pid_of_zero_is_refused(self, layer):
        with pytest.raises(WindowsError):
            layer.get(0)

    def test_a_non_numeric_pid_is_refused(self, layer):
        with pytest.raises(WindowsInvalidRequestError):
            layer.get("not a pid")

    def test_a_bad_timeout_is_a_caller_error(self, layer):
        with pytest.raises(WindowsInvalidRequestError):
            layer.terminate(os_placeholder(), timeout=-1)

    def test_an_unbounded_limit_is_clamped_not_trusted(self, layer):
        """An unbounded query is the cheapest way to exhaust the context."""
        result = layer.list(limit=10**9)
        assert len(result["processes"]) <= MAX_PROCESS_RESULTS

    def test_a_hostile_limit_does_not_escape_the_bound(self, layer):
        for limit in (-5, 0, "many", None, 10**12, float("inf")):
            result = layer.list(limit=limit)
            assert len(result["processes"]) <= MAX_PROCESS_RESULTS, limit

    def test_a_hostile_limit_never_raises(self, layer):
        """A bad limit falls back to the default rather than failing the query;
        failing would make the operation unusable against a hostile argument."""
        for limit in (-5, "many", None, float("inf")):
            assert layer.list(limit=limit)["processes"]


def os_placeholder() -> int:
    """A PID that exists but must never be terminated by the validator alone."""
    import os

    return os.getpid()


# ══════════════════════════════════════════════════════ protected processes


class TestProtectedProcessesAreRefusedOnPrinciple:
    @pytest.mark.parametrize("pid", [0, 1, 2, 4])
    def test_the_kernel_and_boot_processes_are_refused(self, layer, pid):
        with pytest.raises(WindowsProtectedProcessError):
            layer.terminate(pid)

    def test_protected_pids_are_refused_before_any_handle_is_opened(self, layer):
        """The refusal is a policy decision, not a failed ``OpenProcess``. The
        distinction matters: one says 'never', the other says 'not this time'."""
        assert layer._require_pid(4) == 4, "enumeration of a protected pid is fine"
        with pytest.raises(WindowsProtectedProcessError):
            layer.terminate(4)

    def test_self_termination_is_refused(self, layer):
        """The most obvious footgun: asking Diablo to end its own process."""
        import os

        with pytest.raises(WindowsError) as exc:
            layer.terminate(os.getpid())
        assert exc.value.code in (
            WindowsProtectedProcessError.code, WindowsInvalidRequestError.code), (
            f"self-termination failed as {exc.value.code}, which does not say "
            "the layer declined to kill itself")


# ══════════════════════════════════════════════════════ termination honesty


class TestTerminationReportsWhatActuallyHappened:
    def _result(self, **kwargs):
        defaults = {"pid": 100, "identity": process_identity(100, "20260101"),
                    "requested": True, "confirmed": False}
        return TerminationResult(**{**defaults, **kwargs})

    def test_only_both_together_is_success(self):
        assert self._result(confirmed=True).succeeded is True
        assert self._result(confirmed=False).succeeded is False
        assert self._result(requested=False, confirmed=True).succeeded is False, (
            "a confirmed exit that was never requested is incoherent, and reading "
            "as success would claim an action nobody took")

    def test_a_termination_against_a_missing_process_raises_rather_than_reporting(
        self, layer):
        """There is no process to terminate, so there is nothing to report about
        having terminated it. Raising is the honest outcome."""
        with pytest.raises(WindowsProcessNotFoundError):
            layer.terminate(10**9, timeout=0.1)

    def test_the_result_is_json_safe_and_carries_no_handle(self):
        """Handles are process-local; leaking one into JSON or a log is noise at
        best and an information leak at worst."""
        result = TerminationResult(
            pid=100, identity=process_identity(100, "20260101120000"),
            requested=True, confirmed=True, exit_code=1, waited_seconds=0.2)
        serialised = result.to_dict()
        assert "handle" not in serialised
        assert serialised["succeeded"] is True
        json.dumps(serialised)

    def test_a_deadline_that_expires_reports_no_exit_code(self):
        """The bounded wait is what makes 'requested' and 'confirmed' separable:
        if the wait expires, nothing is known about the exit."""
        result = TerminationResult(
            pid=100, identity=process_identity(100, "20260101120000"),
            requested=True, confirmed=False, waited_seconds=5.0,
            reason="deadline expired")
        assert result.succeeded is False
        assert result.to_dict()["exit_code"] is None
        assert result.to_dict()["reason"]


# ══════════════════════════════════════════════════════ identity


class TestProcessIdentityIsBothHalves:
    def test_the_same_pid_with_a_different_creation_time_is_a_different_process(self):
        a = process_identity(100, "20260101120000")
        b = process_identity(100, "20260101130000")
        assert a != b, (
            "PID reuse is the mechanism by which this layer could terminate an "
            "unrelated process; identity must include the creation time")

    def test_an_empty_creation_time_is_not_the_same_as_a_known_one(self):
        """A caller that could not read the creation time gets PID-only identity
        and must not match an identified process."""
        assert process_identity(100, "") != process_identity(100, "20260101120000")

    def test_both_halves_equal_is_a_match(self):
        assert process_identity(100, "20260101120000") == \
            process_identity(100, "20260101120000")


class TestProcessInfoSerialisesCleanly:
    def test_the_row_is_json_safe_and_carries_no_handle(self):
        info = ProcessInfo(pid=100, name="python.exe", created_at="20260101120000")
        data = info.to_dict()
        assert data["pid"] == 100
        assert "handle" not in data
        json.dumps(data)


# ══════════════════════════════════════════════════════ error vocabulary


class TestErrorsHaveAStableVocabulary:
    ALL_ERRORS = (
        WindowsError, WindowsProcessNotFoundError, WindowsProcessGoneError,
        WindowsAccessDeniedError, WindowsProtectedProcessError,
        WindowsCancelledError, WindowsInvalidRequestError,
        WindowsOperationFailedError,
    )

    def test_every_error_declares_a_distinct_code(self):
        codes = [error.code for error in self.ALL_ERRORS]
        assert len(set(codes)) == len(codes), "two errors share a code"

    def test_every_error_is_in_the_status_table(self):
        for error in self.ALL_ERRORS:
            assert error.code in windows_error_status_table, (
                f"{error.__name__} ({error.code}) has no registered status")

    def test_every_registered_status_is_a_known_contract_status(self, layer):
        from integration.contracts import CapabilityStatus

        valid = {status.value for status in CapabilityStatus}
        for code, status in windows_error_status_table.items():
            assert status in valid, f"{code} maps to unknown status {status!r}"

    def test_a_protected_process_is_a_permission_outcome(self):
        assert windows_error_status_table[WindowsProtectedProcessError.code] == \
            "permission_required"
        assert windows_error_status_table[WindowsAccessDeniedError.code] == \
            "permission_required"

    def test_an_infrastructure_failure_is_not_reported_as_bad_input(self):
        """The distinction a user acts on: 'you typed it wrong' vs 'the system
        failed'."""
        assert windows_error_status_table[WindowsOperationFailedError.code] == "failure"
        assert windows_error_status_table[WindowsInvalidRequestError.code] == \
            "invalid_request"

    def test_a_vanished_process_is_not_a_caller_error(self, layer):
        """A process that exits during a read is a real outcome of a valid
        request, not something the caller got wrong."""
        assert windows_error_status_table[WindowsProcessGoneError.code] != \
            windows_error_status_table[WindowsInvalidRequestError.code]

    def test_the_status_is_never_inferred_from_message_text(self, layer):
        """Substring matching on a message is how the word 'permission' in an
        unrelated sentence becomes a permission error."""
        error = WindowsOperationFailedError(
            "the operation was not permitted by policy discussion")
        assert error.code == WindowsOperationFailedError.code, (
            "the message changed the error's identity")
        assert windows_error_status_table[error.code] == "failure"

    def test_the_dict_is_json_safe(self):
        error = WindowsProcessGoneError("gone", diagnostics={"pid": 1})
        json.dumps(error.to_dict())

    def test_cancellation_reports_whether_it_terminated_anything(self):
        """A cancellation that could not stop its target must not read as one
        that did."""
        stopped = WindowsCancelledError("cancelled",
                                        diagnostics={"cancellation_terminated": True})
        assert stopped.terminated is True
        assert WindowsCancelledError("cancelled").terminated is None, (
            "an unrelated error must not claim it terminated anything")
        assert WindowsError("other").terminated is None


# ══════════════════════════════════════════════════════ cancellation


class TestCancellationStopsWorkBeforeItStarts:
    def test_a_precancelled_query_raises(self, layer):
        """Checking cancellation after the work is worthless. The native calls
        here cost real time, so the guard has to come first."""
        with layer.cancellation_scope(_Precancelled()):
            with pytest.raises(WindowsCancelledError):
                layer.list()

    def test_a_precancelled_termination_leaves_the_target_alone(self, layer):
        import os

        with layer.cancellation_scope(_Precancelled()):
            with pytest.raises(WindowsCancelledError):
                layer.terminate(os.getpid())

    def test_the_cancellation_reports_that_it_terminated_nothing(self, layer):
        with layer.cancellation_scope(_Precancelled()):
            with pytest.raises(WindowsCancelledError) as exc:
                layer.terminate(4321)
        assert exc.value.terminated is False, (
            "a cancellation that never reached the target claimed otherwise")

    def test_the_scope_is_restored_afterwards(self, layer):
        """A token leaking past its scope would cancel unrelated later calls."""
        with layer.cancellation_scope(_Precancelled()):
            pass
        assert layer._cancelled() is False
        assert layer.list()["processes"], "the layer is stuck cancelled"


class _Precancelled:
    def is_cancelled(self):
        return True

    def raise_if_cancelled(self):
        raise RuntimeError("cancelled")


# ══════════════════════════════════════════════════════ permission boundary


class TestThePermissionBoundaryIsDeclared:
    def _adapter(self):
        from integration.contracts import WindowsCapabilityAdapter

        return WindowsCapabilityAdapter(WindowsCapabilityLayer())

    def test_the_adapter_declares_terminate_as_mutating(self):
        """The bridge reads this. Without it, terminate would be judged SAFE
        because its name contains no mutating keyword."""
        assert self._adapter().is_mutating("terminate") is True

    def test_queries_are_not_declared_mutating(self):
        for operation in ("list", "get", "find", "system_info",
                          "list_windows", "get_window",
                          "get_cursor_pos", "get_foreground_window",
                          "list_displays", "primary_display", "virtual_desktop",
                          "get_display", "point_to_display"):
            assert self._adapter().is_mutating(operation) is False, (
                f"{operation} was declared mutating; it only reads")

    def test_the_mutating_set_matches_the_layer_declaration(self):
        """The adapter and the layer must not drift apart.

        Two declarations of one fact is one too many, and a silent drift would
        mean a mutation classified SAFE at the bridge.
        """
        from core.windows.capabilities import _MUTATING_OPERATIONS as layer_set

        assert set(self._adapter().mutating_operations) == set(layer_set)
        assert self._adapter().mutating_operations == tuple(sorted(layer_set)), (
            "the adapter's tuple is not the declared set, sorted; a drift here "
            "would mean the two orderings disagree about what it protects")

    def test_every_input_mutation_is_declared_mutating(self):
        """Input reaches another application on the user's machine.

        Each of these is a mutation of a window the user did not necessarily name,
        so each one has to arrive at the bridge already escalated. An input verb
        that read SAFE would need no permission at all.
        """
        for operation in ("mouse_move", "mouse_left_click", "mouse_right_click",
                          "mouse_double_click", "key_press", "key_release",
                          "key_tap", "hotkey", "type_text"):
            assert self._adapter().is_mutating(operation) is True, (
                f"{operation} was not declared mutating; it dispatches input")

    def test_an_unknown_operation_is_not_mutating(self):
        assert self._adapter().is_mutating("delete") is False

    def test_the_bridge_escalates_a_mutating_operation(self):
        """The adapter's own declaration is what makes terminate SENSITIVE."""
        import inspect

        from tools.capability_bridge import CapabilityBridge

        assert "is_mutating" in inspect.getsource(CapabilityBridge), (
            "the bridge no longer consults the adapter's mutating set")


def _is_docstring(tree, node) -> bool:
    """Whether ``node`` is the docstring of a module, class or function.

    Docstrings legitimately explain what code replaced; only prose-free code
    counts as an executable reference.
    """
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):  # noqa: F821
            body = getattr(candidate, "body", None)
            if not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and first.value is node):
                return True
    return False


def _evidence(operation: str, data: dict):
    """Call the bridge's Windows verification with a real request and result."""
    from integration.contracts import (
        CapabilityRequest,
        CapabilityResult,
        CapabilityStatus,
    )
    from tools.capability_bridge import CapabilityBridge

    from core.verification import VerificationState

    bridge = CapabilityBridge.__new__(CapabilityBridge)
    request = CapabilityRequest(capability="windows", operation=operation,
                                request_id="test-request")
    result = CapabilityResult(
        status=CapabilityStatus.SUCCESS, capability="windows", operation=operation,
        data=data, request_id=request.request_id)
    return bridge._windows_verification(result=result, request=request,
                                        data=data, operation=operation)


class TestVerificationEvidenceIsHonest:
    def test_a_termination_that_was_only_requested_cannot_pass(self):
        """The rule the whole verification story rests on: requested is not
        observed."""
        evidence = _evidence("terminate",
                             {"pid": 1, "requested": True, "confirmed": False})
        assert evidence is None or evidence.state is not VerificationState.PASSED, (
            "a requested-but-unconfirmed termination was verified as done")

    def test_a_termination_never_requested_cannot_pass(self):
        evidence = _evidence("terminate",
                             {"pid": 1, "requested": False, "confirmed": False})
        assert evidence is None or evidence.state is not VerificationState.PASSED

    def test_a_confirmed_termination_is_observed_evidence(self):
        evidence = _evidence(
            "terminate",
            {"pid": 1, "requested": True, "confirmed": True, "exit_code": 1})
        assert evidence is not None, (
            "an observed exit produced no verification evidence at all")
        assert evidence.state is VerificationState.PASSED

    def test_an_empty_process_list_is_not_a_pass(self):
        evidence = _evidence("list", {"processes": []})
        assert evidence is None or evidence.state is not VerificationState.PASSED, (
            "an empty result was treated as evidence the system works")

    def test_a_system_snapshot_with_no_memory_is_not_a_pass(self):
        evidence = _evidence("system_info", {"system": {}})
        assert evidence is None or evidence.state is not VerificationState.PASSED

    def _snapshot(self, **overrides):
        """A coherent native snapshot, in the shape the layer actually returns.

        ``SystemInfo.to_dict`` nests the memory and CPU readings under their own
        keys, so the fixture mirrors the real shape rather than a flattened one.
        """
        memory = {"total_physical_mb": 16384.0, "available_physical_mb": 8192.0}
        memory.update(overrides.pop("memory", {}))
        snapshot = {
            "backend": "native",
            "os_caption": "Windows 11 Pro",
            "memory": memory,
            "cpu": {"name": "Test CPU", "logical_cores": 16},
        }
        snapshot.update(overrides)
        return snapshot

    def test_a_consistent_system_snapshot_does_pass(self):
        """The positive case. Without it, "never passes" would look like a
        working check rather than a broken one."""
        evidence = _evidence("system_info", self._snapshot())
        assert evidence is not None and evidence.state is VerificationState.PASSED, (
            f"a coherent snapshot failed: {evidence.detail}")

    def test_an_inconsistent_snapshot_is_rejected(self):
        """More memory available than exists is a measurement that cannot be
        trusted, whatever else it reports."""
        evidence = _evidence("system_info", self._snapshot(
            memory={"total_physical_mb": 1024.0, "available_physical_mb": 4096.0}))
        assert evidence is not None
        assert evidence.state is VerificationState.FAILED

    def test_a_snapshot_with_no_total_memory_is_rejected(self):
        evidence = _evidence("system_info", self._snapshot(
            memory={"total_physical_mb": 0.0, "available_physical_mb": 0.0}))
        assert evidence.state is VerificationState.FAILED

    def test_a_snapshot_with_no_os_caption_is_rejected(self):
        evidence = _evidence("system_info", self._snapshot(os_caption=""))
        assert evidence.state is VerificationState.FAILED

    def test_a_real_snapshot_from_this_machine_verifies(self, layer):
        """End to end on real data, so the check is not only ever exercised
        against a fixture built to match its own expectations."""
        data = layer.system_info()
        assert data.get("backend") == "native", (
            f"the native backend is not in use: {data.get('backend')}")
        evidence = _evidence("system_info", data)
        assert evidence is not None and evidence.state is VerificationState.PASSED, (
            f"the machine's own snapshot failed verification: {evidence.detail}")

    def test_the_evidence_carries_the_trusted_request_id(self):
        """An evidence row that cannot be tied back to its request is an
        unciteable claim."""
        evidence = _evidence(
            "terminate",
            {"pid": 1, "requested": True, "confirmed": True, "exit_code": 1})
        assert evidence.evidence.get("request_id") == "test-request", (
            f"the evidence cannot be tied back to its request: {evidence.evidence}")

    def test_the_bridge_is_asked_to_verify_only_windows(self):
        """A mis-scoped dispatch would verify Blender work as Windows work."""
        import inspect

        from tools.capability_bridge import CapabilityBridge

        source = inspect.getsource(CapabilityBridge._verification_for
                                   if hasattr(CapabilityBridge, "_verification_for")
                                   else CapabilityBridge)
        assert 'capability == "windows"' in source


class TestTheRealLayerDoesNotShellOut:
    """The point of M2.15: no helper process is spawned to answer a query.

    A shell-out costs hundreds of milliseconds per call, leaks a console window,
    and - because PowerShell can express anything - silently widens what a
    capability described as 'process info' is able to reach.
    """

    def _package_files(self):
        from pathlib import Path

        return sorted((Path(__file__).resolve().parents[1] / "core" / "windows")
                      .glob("*.py"))

    def test_subprocess_is_never_imported(self):
        import ast

        for path in self._package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split(".")[0] != "subprocess", (
                            f"{path.name} imports subprocess")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] != "subprocess", (
                        f"{path.name} imports from {node.module}")

    def test_no_process_spawning_helper_is_referenced(self):
        """``os`` is imported for ``getpid``; the dangerous attributes are the
        spawn/exec family, which would reintroduce a child process."""
        import ast

        forbidden = {"system", "popen", "spawnv", "spawnl", "spawnve", "execv",
                     "execve", "execl", "execlp", "fork", "posix_spawn",
                     "startfile", "forkpty"}
        for path in self._package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in forbidden:
                    raise AssertionError(
                        f"{path.name} calls os.{node.attr}, which spawns a process")

    def test_powershell_is_never_executed(self):
        """The replaced implementation's marker.

        Checked against the *code*, not the prose: these modules legitimately
        name PowerShell in docstrings explaining what they replaced. Only a call
        or a command string would mean the shell was reinstated.
        """
        import ast

        for path in self._package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # A string literal that is not a bare docstring.
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    parent_is_docstring = False
                    lowered = node.value.lower()
                    if "powershell" in lowered or "pwsh" in lowered:
                        parent_is_docstring = _is_docstring(tree, node)
                        assert parent_is_docstring, (
                            f"{path.name}:{node.lineno} mentions PowerShell in "
                            f"executable code: {node.value[:80]!r}")
                # Any attribute call that could execute it.
                elif isinstance(node, ast.Attribute) and node.attr in {
                        "run", "Popen", "call", "check_output", "system"}:
                    segment = ast.dump(node)
                    assert "powershell" not in segment.lower(), (
                        f"{path.name}:{node.lineno} executes PowerShell")