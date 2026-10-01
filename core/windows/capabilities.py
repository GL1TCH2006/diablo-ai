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
)
from .processes import ProcessLayer
from .system import SystemLayer, system_available
from .window_models import WindowIdentity
from .windows import WindowLayer, windows_available

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
    # window queries (M2.16)
    "list_windows", "get_window",
    # mutations (M2.15 process, M2.16 window)
    "terminate",
    "window_focus", "window_minimize", "window_restore", "window_close",
)

#: Operations that change machine state. Everything else is a query.
#:
#: This set is the single authority for mutation classification. The bridge reads
#: it through the adapter's ``is_mutating``; nothing anywhere else decides
#: mutation from an operation's name.
_MUTATING_OPERATIONS = frozenset({
    "terminate",
    "window_focus",
    "window_minimize",
    "window_restore",
    "window_close",
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
        operations = list(WINDOWS_OPERATIONS) if native else []
        if native and not window_native:
            operations = [op for op in operations if not op.startswith("window_")]
        return {
            "available": native,
            "operations": operations,
            "mutating_operations": sorted(_MUTATING_OPERATIONS),
            "window_available": window_native,
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
            for layer in (self._processes, self._system, self._windows):
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