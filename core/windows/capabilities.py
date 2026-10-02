"""M2.15 - the Windows capability layer.

The model reaches the native Windows layer only through here, and only through
``capability.execute``. That is deliberate: this module exposes a small, typed set
of named operations, and there is no path by which the model can name a Windows
API, pass a handle, or reach ``ctypes``.

Operations
----------
``list``
    Enumerate processes. Read-only.
``get``
    One process by PID, with details.
``find``
    Filter by name or parent PID.
``system_info``
    Native OS, CPU and memory snapshot.
``terminate``
    Terminate a process. The only mutating operation, and the only one that
    carries an explicit confirmation requirement.

Permission
----------
Termination is ``SENSITIVE`` with ``explicit_confirmation`` set, so
``PermissionManager`` already forces a user confirmation before it runs — the
existing ladder and the existing confirmation framework, unmodified. Everything
else is ``SAFE``. The model cannot escalate from a query to a process-control
primitive: ``terminate`` is a different operation on a different permission tier,
and the permission check is not optional.

Cancellation and correlation
----------------------------
:meth:`WindowsCapabilityLayer.cancellation_scope` and
:meth:`request_correlation` are the same hooks the Blender layer implements, so
``_LayerAdapter._scopes`` binds the trusted ``request_id`` and the request's
cancellation token without any Windows-specific plumbing in the adapter. Neither
can replace ``request_id``: this layer only ever *reads* it.

Verification evidence
---------------------
Every operation returns evidence through the existing
:class:`~core.verification_ledger.VerificationEntry` vocabulary, decided by
:mod:`integration.capability_bridge` from the structured result. This layer does
not adjudicate anything itself; it reports what it observed and the ledger decides
whether that supports a ``PASSED``.

* ``terminate`` reports ``requested`` and ``confirmed`` separately, and the
  bridge refuses to let a merely-*requested* termination read as a pass.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

from core.logging_setup import get_logger

from .errors import (
    WindowsCancelledError,
    WindowsInvalidRequestError,
    WindowsNotFoundError,
    WindowsWindowIdentityMismatchError,
)
from .coordinates import VirtualScreen, require_anchored
from .capture import CaptureLayer
from .displays import (
    DisplayInfo,
    DisplayLayer,
    displays_available,
)
from .uia import UIALayer, uia_available
from .visual import VisualTarget
from .processes import ProcessLayer
from .system import SystemLayer, system_available
from .window_models import WindowIdentity
from .windows import WindowLayer, windows_available
from .input import InputLayer

log = get_logger("windows.capabilities")

__all__ = ["WindowsCapabilityLayer", "WINDOWS_OPERATIONS", "build_windows_capability",
           "MAX_WINDOW_RESULTS", "WINDOW_RESULT_LIMIT"]

#: The complete, closed set of operations the model may request. Anything not in
#: here is refused by the adapter before any native call is attempted.
#:
#: M2.16 added the ``window_*`` reads and the four lifecycle verbs. The lifecycle
#: verbs change what the user sees and are therefore in
#: :data:`_MUTATING_OPERATIONS`, which is what the bridge reads to escalate them
#: to an explicit confirmation.
WINDOWS_OPERATIONS = (
    # process and system queries (M2.15)
    "list", "get", "find", "system_info",
    # window queries (M2.16.1)
    "list_windows", "get_window",
    # mutations (M2.15 process, M2.16.1 window)
    "terminate",
    "window_focus", "window_minimize", "window_restore", "window_close",
    # pointer queries (M2.16.2)
    "get_cursor_pos", "get_foreground_window",
    # pointer mutations (M2.16.2)
    "mouse_move", "mouse_left_click", "mouse_right_click", "mouse_double_click",
    # keyboard mutations (M2.16.3)
    "key_press", "key_release", "key_tap", "hotkey", "type_text",
    # display queries (M2.16.4)
    "list_displays", "primary_display", "virtual_desktop", "get_display",
    "point_to_display",
    # capture (M2.16.4). One operation, and the only mutating one in this block.
    "capture_screen",
    # UI Automation queries (M2.16.5)
    "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
    "uia_target_point",
    # UI Automation actions (M2.16.5)
    "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
    "uia_expand_collapse",
    # Visual target evaluation (M2.16.5). Read-only: it validates a claim and
    # never produces a dispatchable coordinate.
    "visual_resolve_target",
)

#: Operations that need the display layer. Withheld on a host that cannot read
#: display topology, since offering them would advertise operations that can only
#: refuse.
_DISPLAY_OPERATIONS = frozenset({
    "list_displays", "primary_display", "virtual_desktop", "get_display",
    "point_to_display", "capture_screen",
})

#: Operations that need UI Automation. Withheld on a host without COM or without a
#: registered ``UIAutomationCore``, for the same reason: advertising a verb that can
#: only report the provider missing would be worse than not offering it.
_UIA_OPERATIONS = frozenset({
    "uia_inspect_window", "uia_find_elements", "uia_resolve_target",
    "uia_target_point", "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
    "uia_expand_collapse",
})

#: Operations that change machine state. Everything else is a query.
#:
#: This set is the single authority for mutation classification. The bridge reads
#: it through the adapter's ``is_mutating``; nothing anywhere else decides
#: mutation from an operation's name.
#:
#: M2.16.2/3 added every pointer and keyboard verb. All of them mutate: they
#: change what the user's machine does, in front of the user, and are not
#: reversible by the product. Note that none of these names contains a keyword
#: from the bridge's legacy word list - which is exactly why the declaration
#: exists rather than name matching.
_MUTATING_OPERATIONS = frozenset({
    "terminate",
    "window_focus",
    "window_minimize",
    "window_restore",
    "window_close",
    "mouse_move",
    "mouse_left_click",
    "mouse_right_click",
    "mouse_double_click",
    "key_press",
    "key_release",
    "key_tap",
    "hotkey",
    "type_text",
# capture is the only M2.16.4 mutation.
    "capture_screen",
    # M2.16.5. Every UIA action mutates another application's UI, so all five are
    # declared mutating and reach the bridge already escalated. None of them is
    # "just a button click" from this system's point of view: pressing it changes
    # state the user did not ask this process to change. The four query verbs and
    # ``visual_resolve_target`` only read, and stay SAFE.
    "uia_invoke", "uia_set_value", "uia_toggle", "uia_select",
    "uia_expand_collapse",
})

#: Input operations that cannot work without the window layer, because each one
#: resolves and re-verifies a target through it. ``get_cursor_pos`` and
#: ``get_foreground_window`` are reads; the first needs only the pointer and the
#: virtual screen, and the second is how a host without windows would establish
#: that there is no target at all.
_WINDOW_DEPENDENT_INPUT_OPERATIONS = frozenset({
    "mouse_move", "mouse_left_click", "mouse_right_click", "mouse_double_click",
    "key_press", "key_release", "key_tap", "hotkey", "type_text",
    # M2.16.4: screen capture is classified mutating because that is this
    # codebase's term for "requires elevated permission", and capturing the screen
    # is the first operation in M2.16 that can expose something the user did not
    # intend to show. Every other display operation is a read and stays SAFE.
    "capture_screen",
})

#: Upper bound on returned processes, so one call cannot flood the context.
MAX_PROCESS_RESULTS = 200
DEFAULT_RESULT_LIMIT = 50

#: Same reasoning for windows. A desktop carries hundreds of helper windows, so
#: an unbounded enumeration would push a very large list into a model context for
#: no benefit.
MAX_WINDOW_RESULTS = 200
WINDOW_RESULT_LIMIT = 50


class WindowsCapabilityLayer:
    """Typed operations over the native Windows layer."""

    name = "windows"

    def __init__(self, *, process_layer: ProcessLayer | None = None,
                 system_layer: SystemLayer | None = None,
                 window_layer: WindowLayer | None = None,
                 default_timeout: float = 5.0) -> None:
        self._processes = process_layer or ProcessLayer(default_timeout=default_timeout)
        self._system = system_layer or SystemLayer()
        self._default_timeout = float(default_timeout)
        # M2.16. Optional so the process/system half still works on a host with
        # no window APIs at all; the window operations refuse with a typed
        # layer-unavailable error rather than the whole capability disappearing.
        self._windows = window_layer
        self._lock = threading.RLock()
        self._correlation = threading.local()
        self._closed = False
        self._last_error: dict | None = None

    # ------------------------------------------------------ capability hooks

    def operations(self) -> tuple[str, ...]:
        return WINDOWS_OPERATIONS

    def detect_capabilities(self) -> dict:
        """What this layer can actually do on this machine.

        Not a static claim: the native backend is probed, so a machine without
        pywin32 reports ``available: False`` rather than advertising operations
        that would raise.

        M2.16: window support is reported separately from process support, because
        they can differ - a host can have the process APIs without the window
        ones. The operation list reflects that: an unavailable sub-layer's
        operations are withheld rather than advertised and then refused.
        """
        native = system_available()
        window_native = windows_available()
        display_native = displays_available()
        operations = list(WINDOWS_OPERATIONS) if native else []
        if native and not window_native:
            # Both halves depend on the window layer, not just the ones named
            # "window". The pointer and keyboard verbs resolve and re-verify a
            # target through ``WindowLayer``, so advertising them on a host that
            # cannot read windows would offer operations that can only refuse.
            # ``get_cursor_pos`` is the exception: it reads only the pointer and
            # the virtual screen, neither of which needs the window APIs.
            operations = [op for op in operations
                          if not op.startswith("window_")
                          and op not in _WINDOW_DEPENDENT_INPUT_OPERATIONS]
        if native and not display_native:
            operations = [op for op in operations if op not in _DISPLAY_OPERATIONS]
        uia_native = uia_available()
        if native and not uia_native:
            # UIA actions need a window to anchor to, so they are withheld with
            # the rest of the window-dependent set above.
            #
            # ``visual_resolve_target`` is deliberately *not* withheld. Its whole
            # point is to describe a rectangle an application drew, and the
            # ``geometry`` source needs no UI Automation at all - it is plain
            # window-rectangle arithmetic. Gating it on UIA would mean a host
            # without comtypes quietly lost a capability it can genuinely perform,
            # and a ``uia``-sourced target simply reports its own source
            # unavailable at resolve time instead.
            operations = [op for op in operations if op not in _UIA_OPERATIONS]
        return {
            "available": native,
            "operations": operations,
            "mutating_operations": sorted(_MUTATING_OPERATIONS),
            "window_available": window_native,
            "display_available": display_native,
            "uia_available": uia_native,
            "backend": "native",
            "detail": ("native Windows APIs via pywin32"
                       if native else "pywin32 is not installed"),
        }

    def last_error(self) -> dict | None:
        return dict(self._last_error) if self._last_error else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            # UIA before capture/display: the UIA layer holds a reference to the
            # window layer and is closed first so it is not left pointing at a
            # closed dependency.
            for layer in (self._processes, self._system, self._windows,
                           getattr(self, "_capture", None),
                           getattr(self, "_input", None),
                           getattr(self, "_uia", None),
                           getattr(self, "_displays", None)):
                closer = getattr(layer, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("windows layer close failed: %s", exc)

    @contextmanager
    def request_correlation(self, request_id: str = "", context: dict | None = None):
        """Expose the trusted ``request_id`` for the duration of one call.

        Read-only. This layer cannot mint or replace an identity; it only makes
        the one S1 owns available for logging and diagnostics.
        """
        previous = getattr(self._correlation, "request_id", "")
        self._correlation.request_id = str(request_id or "")
        self._correlation.context = dict(context or {})
        try:
            yield self
        finally:
            self._correlation.request_id = previous

    @contextmanager
    def cancellation_scope(self, token=None):
        """Bind the request's cancellation token for the duration of one call."""
        previous = getattr(self._correlation, "token", None)
        self._correlation.token = token
        try:
            yield self
        finally:
            self._correlation.token = previous

    def trusted_request_id(self) -> str:
        return str(getattr(self._correlation, "request_id", "") or "")

    def _cancelled(self) -> bool:
        """Cancellation check for the native call about to be made."""
        token = getattr(self._correlation, "token", None)
        if token is None:
            return False
        checker = getattr(token, "is_cancelled", None)
        if callable(checker):
            return bool(checker())
        return False

    def _record_error(self, exc: BaseException) -> None:
        self._last_error = {
            "code": getattr(exc, "code", type(exc).__name__),
            "message": str(exc),
        }

    # ----------------------------------------------------------- operations

    def list(self, limit: int = DEFAULT_RESULT_LIMIT, **_ignored: Any) -> dict:
        """Enumerate processes.

        Metadata beyond the process table is deliberately not fetched here:
        opening a handle per process would be slow and would produce an access
        error per protected process. The table fields - pid, parent, name,
        threads, handles, memory, creation time - need no handle at all.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before enumeration.", diagnostics={"cancellation_terminated": False})
        cap = self._bounded_limit(limit, MAX_PROCESS_RESULTS)
        try:
            processes = self._processes.enumerate_processes(cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        rows = [p.to_dict() for p in processes[:cap]]
        return {
            "processes": rows,
            "count": len(rows),
            "total_visible": len(processes),
            "truncated": len(processes) > cap,
            "limit": cap,
        }

    def get(self, pid: Any, **_ignored: Any) -> dict:
        """One process by PID, with path, command line and owner."""
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the process lookup.", diagnostics={"cancellation_terminated": False})
        value = self._require_pid(pid)
        try:
            return self._processes.get_process(value, cancelled=self._cancelled).to_dict()
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise

    def find(self, name: str = "", parent_pid: Any = None,
             limit: int = DEFAULT_RESULT_LIMIT, **_ignored: Any) -> dict:
        """Filter by image name or parent PID."""
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the process search.", diagnostics={"cancellation_terminated": False})
        cap = self._bounded_limit(limit, MAX_PROCESS_RESULTS)
        parent = None
        if parent_pid is not None and parent_pid != "":
            parent = self._require_pid(parent_pid)
        try:
            found = self._processes.find_processes(
                str(name or ""), parent, limit=cap, cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        rows = [p.to_dict() for p in found]
        return {
            "processes": rows,
            "count": len(rows),
            "filters": {"name": str(name or ""), "parent_pid": parent},
            "limit": cap,
        }

    def system_info(self, **_ignored: Any) -> dict:
        """Native OS, CPU and memory snapshot."""
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the system query.", diagnostics={"cancellation_terminated": False})
        try:
            info = self._system.system_info(cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        # The trusted id rides along for diagnostics only. It is the same value
        # S1 owns, echoed, never replaced.
        info["trusted_request_id"] = self.trusted_request_id()
        return info

    def terminate(self, pid: Any, timeout: float = 5.0, **_ignored: Any) -> dict:
        """Terminate a process, then confirm it.

        Returns ``requested`` and ``confirmed`` as separate fields. A termination
        that was requested but not observed leaves ``confirmed`` false; the
        capability bridge is what decides whether that can ever support a
        ``PASSED``, and it cannot.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the termination request.",
                diagnostics={"cancellation_terminated": False})
        value = self._require_pid(pid)
        try:
            budget = max(0.0, min(float(timeout or 5.0), 30.0))
        except (TypeError, ValueError):
            raise WindowsInvalidRequestError(f"timeout must be a number, got {timeout!r}") from None
        try:
            result = self._processes.terminate_process(
                value, timeout=budget, cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        payload = result.to_dict()
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    # ------------------------------------------------------- M2.16 windows

    def _window_layer(self) -> WindowLayer:
        """The window layer, or a typed refusal that it is unavailable here.

        Separate from ``_guard`` so a host without the window APIs loses only the
        window operations rather than the whole Windows capability.
        """
        layer = getattr(self, "_windows", None)
        if layer is None:
            layer = self._windows = WindowLayer(process_layer=self._processes)
        if not windows_available():
            raise WindowsNotFoundError(
                "Native window APIs are unavailable on this host.")
        return layer

    def list_windows(self, limit: int = WINDOW_RESULT_LIMIT,
                     visible_only: bool = False,
                     include_untitled: bool = True,
                     pid: Any = None,
                     title_contains: str = "",
                     **_ignored: Any) -> dict:
        """Enumerate top-level windows, optionally filtered.

        M2.16. Read-only. Every row carries a :class:`WindowIdentity`, which is
        what a later lifecycle call has to quote back.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the window query.",
                diagnostics={"cancellation_terminated": False})
        cap = self._bounded_limit(limit, MAX_WINDOW_RESULTS)
        try:
            found = self._window_layer().enumerate_windows(
                visible_only=bool(visible_only),
                include_untitled=bool(include_untitled),
                pid=self._window_layer()._coerce_pid(pid),
                title_contains=str(title_contains or ""),
                cancelled=self._cancelled,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        rows = [w.to_dict() for w in found[:cap]]
        payload = {
            "windows": rows,
            "count": len(rows),
            "total_found": len(found),
            "truncated": len(found) > len(rows),
            "trusted_request_id": self.trusted_request_id(),
        }
        return payload

    def get_window(self, window: Any = None, **_ignored: Any) -> dict:
        """Re-read one window and prove it is the one the identity names.

        M2.16. This is the verification primitive every lifecycle verb relies on.
        It raises rather than returning a mismatched window, so a caller cannot
        act on a recycled handle by skipping the check.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the window query.",
                diagnostics={"cancellation_terminated": False})
        try:
            info = self._window_layer().get_window(window, cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        payload = dict(info.to_dict())
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    def _window_mutation(self, method: str, window: Any, **kwargs: Any) -> dict:
        """Shared shape for the four lifecycle verbs.

        Preflight cancellation, then the caller's own verify-then-act-then-re-read
        sequence, which is where the identity check lives. The cancellation token
        reaches the poll loops inside those methods through ``self._cancelled``.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the window operation.",
                diagnostics={"cancellation_terminated": False})
        layer = self._window_layer()
        try:
            payload = getattr(layer, method)(window, cancelled=self._cancelled,
                                            **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    def window_focus(self, window: Any = None, **_ignored: Any) -> dict:
        """Bring a window to the foreground. Mutating."""
        return self._window_mutation("focus_window", window)

    def window_minimize(self, window: Any = None, **_ignored: Any) -> dict:
        """Minimize a window. Mutating."""
        return self._window_mutation("minimize_window", window)

    def window_restore(self, window: Any = None, **_ignored: Any) -> dict:
        """Restore a window. Mutating."""
        return self._window_mutation("restore_window", window)

    def window_close(self, window: Any = None, **_ignored: Any) -> dict:
        """Close a window and confirm it went away. Mutating."""
        return self._window_mutation("close_window", window)

    # ------------------------------- M2.16.2 pointer and M2.16.3 keyboard

    def _input_layer(self) -> InputLayer:
        """The native input layer, built on the same window layer.

        Deliberately shares the window layer: resolving a target and confirming a
        foreground window are that layer's job, and a second copy of that logic
        here is how the two would drift apart and start disagreeing about whether
        a target is valid.
        """
        layer = getattr(self, "_input", None)
        if layer is None:
            layer = self._input = InputLayer(self._window_layer())
        return layer

    def get_cursor_pos(self, **_ignored: Any) -> dict:
        """Read the pointer position and the virtual desktop geometry.

        Read-only.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the pointer query.",
                diagnostics={"cancellation_terminated": False})
        try:
            payload = self._input_layer().get_cursor_pos()
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    def get_foreground_window(self, **_ignored: Any) -> dict:
        """Read the foreground window as a logical identity.

        Read-only, and returns the same identity every other window operation
        uses so a caller can hand it straight back as an input target.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the foreground query.",
                diagnostics={"cancellation_terminated": False})
        try:
            payload = self._input_layer().get_foreground_window()
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        return {"window": payload, "has_foreground": payload is not None,
                "trusted_request_id": self.trusted_request_id()}

    def _input_mutation(self, method: str, *, allow_cancelled: bool = False,
                        **kwargs: Any) -> dict:
        """Shared shape for every pointer and keyboard verb.

        Preflight cancellation, then the caller's own resolve/verify/dispatch
        sequence, which is where target verification lives. The token reaches the
        settle polls through ``self._cancelled``.

        ``allow_cancelled`` exists for release-only cleanup. It waives the *token*
        check and nothing else - the operation is still classified as mutating, so
        it still requires permission. What it deliberately does not waive is target
        verification: freeing a key is the one thing that must keep working when
        everything else has already failed.
        """
        self._guard()
        if not allow_cancelled and self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the input dispatch.",
                diagnostics={"cancellation_terminated": False})
        try:
            result = getattr(self._input_layer(), method)(
                cancelled=self._cancelled, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        payload = result.to_dict()
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    def _resolve_for_input(self, target: Any, *, window: Any = None,
                           require_foreground: bool = True):
        """Resolve a target and confirm it, in that order.

        A caller may name the target either as a point carrying its own window
        identity, or as a separate ``window`` plus a point. Both are accepted;
        neither skips verification.

        Every input verb routes through here, so this is also where anchoring is
        enforced for the whole surface: a resolved point that is not anchored to a
        verified window is refused here rather than by each verb separately. One
        place to get wrong is better than twelve.

        The separate-window form requires a *window point*, so the coordinates are
        resolved against freshly-read bounds. A bare screen point given alongside a
        verified window is refused rather than quietly trusted because a window
        happened to be supplied - that combination says "somewhere", and
        "somewhere" is exactly what this layer exists to replace.
        """
        inputs = self._input_layer()
        if window is not None:
            confirmed = inputs.confirm_target(window)
            point = inputs.resolve(target, require_foreground=False)
            if not point.verified_target:
                raise WindowsInvalidRequestError(
                    "a screen point was given alongside a target window; use a "
                    "window point so the coordinates are resolved against that "
                    "window's verified bounds.",
                    diagnostics=point.to_dict())
            if not point.identity.matches(confirmed.identity):
                # The two names have to be the same window. Overwriting the
                # resolved provenance with the confirmed one would launder a
                # mismatch into a plausible-looking point: the coordinates would
                # be reported against a window they were never resolved from.
                raise WindowsWindowIdentityMismatchError(
                    "the window and the target point name different windows, so "
                    "the coordinates cannot be resolved against the target.",
                    diagnostics={
                        "window_key": confirmed.identity.key,
                        "point_window_key": point.window_key,
                    })
            return point

        point = inputs.resolve(target, require_foreground=require_foreground)
        if require_foreground and point.verified_target:
            # Re-confirm after resolution: the window was read once during the
            # resolve and the foreground could have changed since.
            inputs.confirm_target(point.identity)
        require_anchored(point)
        return point

    def mouse_move(self, target: Any = None, window: Any = None,
                   **_ignored: Any) -> dict:
        """Move the pointer to a verified target. Mutating.

        Foreground confirmation is required here exactly as it is for a click.
        A pointer moved into a background window's rectangle lands on whatever is
        actually on top there, so a move that skipped the foreground check would
        be a click aimed at an unknown window wearing a move's clothes.
        """
        point = self._resolve_for_input(target, window=window,
                                        require_foreground=True)
        return self._input_mutation("move_cursor", point=point)

    def _click(self, button: str, count: int, target: Any,
               window: Any) -> dict:
        point = self._resolve_for_input(target, window=window)
        return self._input_mutation("move_and_click", point=point,
                                    button=button, count=count)

    def mouse_left_click(self, target: Any = None, window: Any = None,
                         **_ignored: Any) -> dict:
        """Left-click a verified target. Mutating."""
        return self._click("left", 1, target, window)

    def mouse_right_click(self, target: Any = None, window: Any = None,
                          **_ignored: Any) -> dict:
        """Right-click a verified target. Mutating."""
        return self._click("right", 1, target, window)

    def mouse_double_click(self, target: Any = None, window: Any = None,
                           **_ignored: Any) -> dict:
        """Double-click a verified target. Mutating."""
        return self._click("left", 2, target, window)

    def _keyboard(self, method: str, target: Any, window: Any, *,
                    allow_cancelled: bool = False, **kwargs: Any) -> dict:
        # Keyboard input is only received by the focused window, so foreground
        # confirmation is mandatory rather than advisory here. The resolved point
        # is threaded into the dispatch as well as checked here, so the native
        # layer re-verifies the target immediately before SendInput rather than
        # trusting a check that happened one layer up.
        point = self._resolve_for_input(target, window=window,
                                        require_foreground=True)
        return self._input_mutation(method, point=point,
                                    allow_cancelled=allow_cancelled, **kwargs)

    def key_press(self, key: Any = None, target: Any = None, window: Any = None,
                  **_ignored: Any) -> dict:
        """Hold a named key down in a verified target. Mutating."""
        return self._keyboard("press_key", target, window, key=key)

    def key_release(self, key: Any = None, target: Any = None, window: Any = None,
                    **_ignored: Any) -> dict:
        """Release a held key. Mutating.

        Waives the cancellation token on purpose. A cancelled request is exactly
        the state in which a key may have been left held, so refusing to release
        because of cancellation would convert a recoverable interruption into a
        machine stuck with Ctrl down until the user intervenes.
        """
        return self._keyboard("release_key", target, window, key=key,
                              allow_cancelled=True)

    def key_tap(self, key: Any = None, target: Any = None, window: Any = None,
                **_ignored: Any) -> dict:
        """Press and release one key in a verified target. Mutating."""
        return self._keyboard("tap_key", target, window, key=key)

    def hotkey(self, keys: Any = None, target: Any = None, window: Any = None,
               **_ignored: Any) -> dict:
        """Dispatch a combination in a verified target. Mutating."""
        return self._keyboard("hotkey", target, window, keys=list(keys or []))

    def type_text(self, text: Any = None, target: Any = None, window: Any = None,
                  **_ignored: Any) -> dict:
        """Type text into a verified target. Mutating."""
        return self._keyboard("write_text", target, window, text=text)

    # ------------------------- M2.16.4 display topology and bounded capture

    def _display_layer(self) -> DisplayLayer:
        """The native display layer, built lazily like the others.

        Separate from the window layer on purpose. Windows does have per-window
        DPI, but a *display*'s identity is not a process, so it cannot be anchored
        the way an M2.16.1 window identity is. Keeping the two layers separate
        stops that difference being blurred - a display is identified by device
        name and re-read from live topology, not by a handle it can be pinned to.
        """
        layer = getattr(self, "_displays", None)
        if layer is None:
            layer = self._displays = DisplayLayer(
                default_timeout=self._default_timeout)
        return layer

    def _capture_layer(self) -> CaptureLayer:
        """The native capture layer, sharing the display layer's topology read.

        Shared on purpose: a capture has to agree with what enumeration reported,
        and two independently-read topologies could disagree between the two calls
        of a single request.
        """
        layer = getattr(self, "_capture", None)
        if layer is None:
            layer = self._capture = CaptureLayer(self._display_layer())
        return layer

    def _display_read(self, method: str, **kwargs: Any) -> dict:
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the display query.",
                diagnostics={"cancellation_terminated": False})
        try:
            payload = getattr(self._display_layer(), method)(
                cancelled=self._cancelled, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        if isinstance(payload, (DisplayInfo, VirtualScreen)):
            payload = payload.to_dict()
        result = dict(payload)
        result["trusted_request_id"] = self.trusted_request_id()
        return result

    def list_displays(self, **_ignored: Any) -> dict:
        """Every connected display, with bounds, work area, scale and DPI. Read-only."""
        self._guard()
        displays = self._display_layer().list_displays(cancelled=self._cancelled)
        try:
            primary = next(d for d in displays if d.primary)
            primary_id = primary.display_id
        except StopIteration:
            primary_id = ""
        return {
            "displays": [d.to_dict() for d in displays],
            "display_ids": [d.display_id for d in displays],
            "primary_display_id": primary_id,
            "count": len(displays),
            "dpi_awareness": self._display_layer().dpi_awareness(),
            "trusted_request_id": self.trusted_request_id(),
        }

    def primary_display(self, **_ignored: Any) -> dict:
        """The display Windows reports as primary. Read-only.

        Taken from the primary flag rather than from ordering, position or size.
        """
        payload = self._display_read("primary_display")
        return {"display": payload, "display_id": payload.get("display_id", ""),
                "trusted_request_id": self.trusted_request_id()}

    def virtual_desktop(self, **_ignored: Any) -> dict:
        """The bounding rectangle of every connected display. Read-only.

        The union of the display rectangles, which is not necessarily
        ``SM_CXVIRTUALSCREEN``: that metric spans any gap between monitors, and a
        capture has to be able to name the pixels that exist.
        """
        return self._display_read("virtual_desktop")

    def get_display(self, display: Any = None, target: Any = None,
                    **_ignored: Any) -> dict:
        """Resolve one display by id against live topology. Read-only.

        Accepts either a bare device name or ``{"display_id": ...}``. A display
        that is no longer connected is refused by name; it is never quietly
        replaced with the primary one.
        """
        wanted = display if display is not None else target
        if wanted is None:
            raise WindowsInvalidRequestError(
                "get_display requires a display_id.")
        payload = self._display_read("resolve_target", target=wanted)
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    def point_to_display(self, x: Any = None, y: Any = None,
                         **_ignored: Any) -> dict:
        """Which display contains a virtual-desktop point. Read-only.

        Refuses when the point is outside every display or in a gap between them.
        Answering with the nearest display would send the caller to a screen the
        point is not on.
        """
        if x is None or y is None:
            raise WindowsInvalidRequestError(
                "point_to_display requires both x and y.")
        try:
            ix, iy = int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise WindowsInvalidRequestError(
                "point_to_display requires integer x and y.") from exc
        info = self._display_layer().display_at(ix, iy, cancelled=self._cancelled)
        return {
            "display": info.to_dict(),
            "display_id": info.display_id,
            "point": {"x": ix, "y": iy},
            "relative": {"x": ix - info.bounds.left, "y": iy - info.bounds.top},
            "trusted_request_id": self.trusted_request_id(),
        }

    def capture_screen(self, target: Any = None, **_ignored: Any) -> dict:
        """Capture one explicit, bounded region. Mutating.

        Classified mutating because that is this codebase's term for "requires
        elevated permission", and a screen capture is the first M2.16 operation
        that can expose something the user did not intend to show. Leaving it
        unclassified would put a privacy-sensitive read at SAFE, which is exactly
        the failure the mutating declaration exists to prevent.

        The target is mandatory. There is no default meaning "the whole desktop",
        and no way to request a capture by omitting the target.

        The payload carries **metadata only**. The pixels stay in the
        :class:`~core.windows.capture.ScreenCapture` object, which an in-process
        caller can hold, and never enter a result that gets logged, cached or sent
        to a model. Handing screen contents to a model is a later milestone's
        decision to make explicitly, not a side effect of taking a picture.
        """
        if target is None:
            raise WindowsInvalidRequestError(
                "capture_screen requires an explicit target: a display_id, or "
                "{mode: 'rect', left, top, right, bottom}, or "
                "{mode: 'virtual_desktop'}. An untargeted capture is refused.")
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the capture.",
                diagnostics={"cancellation_terminated": False})
        try:
            capture = self._capture_layer().capture(target,
                                                    cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        payload = {"capture": capture.metadata()}
        payload["trusted_request_id"] = self.trusted_request_id()
        return payload

    # ------------------ M2.16.5 UI Automation and visual targeting

    def _uia_layer(self) -> UIALayer:
        """The UI Automation adapter, sharing the window layer.

        The same window layer is passed in because every element is anchored
        through it, and a second window layer could disagree about whether a window
        is still valid.
        """
        layer = getattr(self, "_uia", None)
        if layer is None:
            layer = self._uia = UIALayer(self._window_layer())
        return layer

    def _uia_read(self, method: str, **kwargs: Any) -> dict[str, Any]:
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the UI Automation query.",
                diagnostics={"cancellation_terminated": False})
        try:
            payload = getattr(self._uia_layer(), method)(
                cancelled=self._cancelled, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        result = dict(payload)
        result["trusted_request_id"] = self.trusted_request_id()
        return result

    def uia_inspect_window(self, window: Any = None, **_ignored: Any) -> dict:
        """Describe one window's root UI element. Read-only."""
        if window is None:
            raise WindowsInvalidRequestError(
                "uia_inspect_window requires a window identity.")
        return self._uia_read("inspect_window", window=window)

    def uia_find_elements(self, window: Any = None, automation_id: Any = None,
                          name: Any = None, control_type: Any = None,
                          max_results: Any = None, max_depth: Any = None,
                          **_ignored: Any) -> dict:
        """Find elements under a window, bounded and typed. Read-only.

        Four optional equality filters, never a predicate language: a model that can
        express an arbitrary condition can express one this layer cannot bound or
        cancel. Every response reports whether it was exhaustive.
        """
        if window is None:
            raise WindowsInvalidRequestError(
                "uia_find_elements requires a window identity.")
        return self._uia_read(
            "find_elements", window=window, automation_id=automation_id,
            name=name, control_type=control_type, max_results=max_results,
            max_depth=max_depth)

    def uia_resolve_target(self, target: Any = None, window: Any = None,
                           **_ignored: Any) -> dict:
        """Re-resolve a recorded element and report whether it is still the same.

        This is the stale-target gate. Read-only, and deliberately the *only* way
        to convert a stored target into a live one.
        """
        if target is None:
            raise WindowsInvalidRequestError(
                "uia_resolve_target requires an element descriptor.")
        return self._uia_read("resolve", target=target, window=window)

    def uia_target_point(self, target: Any = None, window: Any = None,
                         **_ignored: Any) -> dict:
        """Convert an element to an anchored, window-relative point. Read-only.

        The result is produced by the M2.16.2 coordinate resolver, so it carries
        that provenance and can be handed to the existing pointer capability. It is
        still not a dispatch: the pointer layer re-verifies foreground itself.
        """
        if target is None:
            raise WindowsInvalidRequestError(
                "uia_target_point requires an element descriptor.")
        return self._uia_read("target_point", target=target, window=window)

    def _uia_action(self, action: str, target: Any, window: Any,
                    **kwargs: Any) -> dict[str, Any]:
        """Shared shape for the five semantic actions.

        Preflight cancellation here, then the adapter's resolve-verify-act-
        re-read sequence. The permission check has already happened above this
        layer, in the bridge.
        """
        self._guard()
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the UI Automation action.",
                diagnostics={"cancellation_terminated": False})
        try:
            payload = self._uia_layer().act(action, target, window,
                                            cancelled=self._cancelled,
                                            **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        result = dict(payload)
        result["trusted_request_id"] = self.trusted_request_id()
        return result

    def uia_invoke(self, target: Any = None, window: Any = None,
                   observe: Any = None, settle: Any = None,
                   **_ignored: Any) -> dict:
        """Invoke an element's Invoke pattern. Mutating.

        ``Invoke`` has no state of its own, so with no ``observe`` element the
        result is honestly UNVERIFIED: Windows accepting the call is not evidence
        that anything happened. Naming a second element whose visible state should
        change turns it into a verifiable action, and that is the difference
        between a button press that is checked and one that is merely attempted.
        """
        return self._uia_action("invoke", target, window, observe=observe,
                                settle=settle)

    def uia_set_value(self, target: Any = None, window: Any = None,
                      value: Any = None, settle: Any = None,
                      **_ignored: Any) -> dict:
        """Set an element's value. Mutating.

        Verified by reading the value back, so a provider that silently refuses
        reports FAILED rather than applied.
        """
        return self._uia_action("set_value", target, window,
                                value=str(value or ""), settle=settle)

    def uia_toggle(self, target: Any = None, window: Any = None,
                   settle: Any = None, **_ignored: Any) -> dict:
        """Toggle an element. Mutating. Verified by re-reading the toggle state."""
        return self._uia_action("toggle", target, window, settle=settle)

    def uia_select(self, target: Any = None, window: Any = None,
                   settle: Any = None, **_ignored: Any) -> dict:
        """Select an element. Mutating. Verified by re-reading the selected flag."""
        return self._uia_action("select", target, window, settle=settle)

    def uia_expand_collapse(self, target: Any = None, window: Any = None,
                            state: Any = None, settle: Any = None,
                            **_ignored: Any) -> dict:
        """Expand or collapse an element. Mutating.

        Verified by re-reading the expand state.
        """
        return self._uia_action("expand_collapse", target, window,
                                state=str(state or "expand"), settle=settle)

    def visual_resolve_target(self, target: Any = None, **_ignored: Any) -> dict:
        """Validate a visual-target claim against current desktop state. Read-only.

        Never returns an actionable point. A visual target is a *claim*; turning it
        into a place to click is the pointer layer's job, and it re-resolves and
        re-checks foreground when it does. This exists so the claim is tested
        against real state before anything downstream is allowed to rely on it.
        """
        self._guard()
        if target is None:
            raise WindowsInvalidRequestError(
                "visual_resolve_target requires a visual target descriptor.")
        if self._cancelled():
            raise WindowsCancelledError(
                "Cancelled before the visual target check.",
                diagnostics={"cancellation_terminated": False})
        try:
            visual = VisualTarget.from_dict(target)
            verdict = visual.revalidate(
                display_layer=self._display_layer(),
                window_layer=self._window_layer(),
                cancelled=self._cancelled)
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            raise
        verdict["trusted_request_id"] = self.trusted_request_id()
        verdict["untrusted"] = True
        return verdict

    # ---------------------------------------------------------------- utils

    def _guard(self) -> None:
        if self._closed:
            raise WindowsNotFoundError("the Windows capability layer is closed.")

    @staticmethod
    def _require_pid(pid: Any) -> int:
        if pid is None or pid == "":
            raise WindowsInvalidRequestError("pid is required.")
        # A non-integral float is rejected rather than truncated. int(3.7) == 3
        # would silently act on a *different* process than the caller named,
        # which for `terminate` means killing something nobody asked to kill.
        if isinstance(pid, bool):
            raise WindowsInvalidRequestError(f"pid must be an integer, got {pid!r}")
        if isinstance(pid, float) and not pid.is_integer():
            raise WindowsInvalidRequestError(
                f"pid must be a whole number, got {pid!r}; truncating it would "
                f"target process {int(pid)}")
        try:
            value = int(pid)
        except (TypeError, ValueError):
            raise WindowsInvalidRequestError(f"pid must be an integer, got {pid!r}") from None
        if value < 0:
            raise WindowsInvalidRequestError(f"pid must not be negative, got {value}")
        return value

    @staticmethod
    def _bounded_limit(limit: Any, maximum: int) -> int:
        """Clamp a caller-supplied result limit into ``[1, maximum]``.

        A hostile limit falls back to the default rather than failing the query.
        Failing would make the operation unusable against a bad argument, and a
        limit is a hint about how much to return, not a correctness condition.

        ``float("inf")`` and ``nan`` are handled explicitly: ``int(inf)`` raises
        ``OverflowError`` and ``int(nan)`` raises ``ValueError``, neither of which
        is in the tuple below, so a caller could turn a hint into a traceback.
        """
        try:
            value = int(limit)
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_RESULT_LIMIT
        return max(1, min(value, maximum))


def build_windows_capability(**kwargs: Any) -> WindowsCapabilityLayer:
    """Factory, matching the other capability-layer construction sites."""
    return WindowsCapabilityLayer(**kwargs)