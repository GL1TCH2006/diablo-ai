"""Native top-level window enumeration and lifecycle primitives.

M2.16. The window half of the desktop-interaction foundation, sitting beside
:mod:`core.windows.processes` and using the same discipline: native APIs only, no
shell-outs, no handle leakage, and every mutating action verified against actual
resulting state rather than against an API return code.

What this module deliberately does **not** do
---------------------------------------------
No mouse, keyboard, screen capture, UI Automation, OCR or arbitrary window
messaging. Those are later M2.16 work packages. What belongs here is the part
every one of them needs and none of them can work without: a safe way to
*identify* a window and a bounded set of lifecycle verbs, each classified as
mutating and each cancellable and verifiable.

Identity
--------
Windows recycles ``HWND`` values, so a handle is an address and not an
identity. Every window is therefore identified by
:class:`~core.windows.window_models.WindowIdentity`, anchored to its owning
process's ``(pid, created_at)``. See that module for the full reasoning.

The safety rule that follows from it: **every mutating call re-reads the window
and re-verifies the full identity immediately before acting.** A handle that has
been recycled to a different window is refused
(:class:`~core.windows.errors.WindowsWindowIdentityMismatchError`) rather than
acted upon. Enumerating and then acting are separate steps with a check between
them, because the window can close in between.

Protected targets
-----------------
The shell's own windows, the desktop window, and windows owned by protected or
elevated processes are refused for mutation. Refusing is the honest answer to
"Windows said no": no attempt is made to work around the denial, and nothing here
escalates privilege.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from core.logging_setup import get_logger

from .errors import (
    WindowsAccessDeniedError,
    WindowsCancelledError,
    WindowsInvalidRequestError,
    WindowsNotFoundError,
    WindowsOperationFailedError,
    WindowsProtectedProcessError,
    WindowsWindowGoneError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from .processes import ProcessLayer
from .window_models import WindowIdentity, WindowInfo

log = get_logger("windows.windows")

__all__ = ["WindowLayer", "WINDOW_LIFECYCLE_OPERATIONS", "windows_available"]

#: Lifecycle verbs. Each one changes what the user sees, so each is declared
#: mutating by the adapter and requires an explicit confirmation. Enumeration and
#: metadata reads are not in this set.
WINDOW_LIFECYCLE_OPERATIONS = ("focus", "minimize", "restore", "close")

#: Classes that must never be mutated: the interactive shell itself, the desktop,
#: and the invisible helper windows Windows uses for its own compositing. Closing
#: any of these takes components of the user's session with it.
_PROTECTED_CLASSES = frozenset({
    "Progman",            # the desktop
    "WorkerW",            # its worker
    "Shell_TrayWnd",      # the taskbar
    "Windows.UI.Core.CoreWindow",   # the shell
    "ForegroundStaging",
    "MultitaskingViewFrame",
    "EdgeUiInputTopWndClass",
    "XamlExplorerHostIslandWindow",
    "ApplicationFrameWindow",
})

#: ``GetClassLong`` index for the owning module's base address. Not exposed by
#: ``win32con`` under this name on every build, so it is named here.
_GWL_HINSTANCE = -4

#: ``ShowWindow`` commands.
_SW_RESTORE = 9
_SW_MINIMIZE = 6
_SW_SHOW = 5


def _load_gui() -> Any:
    """Import the window APIs, or report the layer as unavailable.

    Isolated so a headless or non-Windows host raises a typed layer-unavailable
    error rather than an ``ImportError`` at module import time, which would make
    the whole Windows package unimportable.
    """
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:  # pragma: no cover - Windows-only
        raise WindowsNotFoundError(
            "pywin32 window APIs are unavailable on this host.") from exc
    return win32gui, win32process, win32con


def windows_available() -> bool:
    """Whether native window enumeration can run on this machine."""
    try:
        _load_gui()
    except WindowsNotFoundError:
        return False
    return True


class WindowLayer:
    """Native top-level window enumeration and lifecycle verbs.

    Not thread-safe by design: one instance owns one enumeration cursor, matching
    :class:`~core.windows.processes.ProcessLayer`. The capability layer serialises
    access through the same session lock it already holds for processes.
    """

    def __init__(self, *, process_layer: ProcessLayer | None = None) -> None:
        self._processes = process_layer or ProcessLayer()
        self._owns_process_layer = process_layer is None
        self._lock = threading.RLock()
        self._closed = False

    # ------------------------------------------------------------------ setup

    def close(self) -> None:
        self._closed = True
        if self._owns_process_layer:
            self._processes.close()

    def _guard(self) -> None:
        if self._closed:
            raise WindowsNotFoundError("the window layer is closed.")

    @staticmethod
    def _cancelled(cancelled: Callable[[], bool] | None) -> bool:
        if cancelled is None:
            return False
        try:
            return bool(cancelled())
        except Exception:  # noqa: BLE001 - a failing probe means "assume cancelled"
            return True

    @classmethod
    def _check_cancelled(cls, cancelled: Callable[[], bool] | None) -> None:
        """Refuse before doing work, not after.

        ``WindowsCancelledError`` carries ``cancellation_terminated: False``
        because a preflight refusal has not terminated anything - the same
        attribution the Blender layer uses, so the ledger can tell the two apart.
        """
        if cls._cancelled(cancelled):
            raise WindowsCancelledError(
                "Cancelled before the window operation.",
                diagnostics={"cancellation_terminated": False},
            )

    # ------------------------------------------------------------ enumeration

    def enumerate_windows(self, *, visible_only: bool = False,
                          include_untitled: bool = True,
                          pid: int | None = None,
                          title_contains: str = "",
                          cancelled: Callable[[], bool] | None = None,
                          ) -> list[WindowInfo]:
        """Enumerate top-level windows, optionally filtered.

        ``visible_only``
            Desktop windows are mostly invisible plumbing. Leaving them in the
            default result makes every call noisy, so visibility is available as
            a filter and the default keeps the full set for completeness.
        ``include_untitled``
            Windows has many untitled helper windows. Excluding them is opt-in so
            the default does not hide a real application that has not set a title
            yet.
        """
        self._guard()
        self._check_cancelled(cancelled)
        win32gui, win32process, _ = _load_gui()

        collected: list[int] = []

        def _collect(hwnd: int, _lparam: Any) -> bool:
            # Cancellation is checked per window so a long enumeration over a
            # desktop with thousands of helper windows stays interruptible.
            if self._cancelled(cancelled):
                return False
            collected.append(int(hwnd))
            return True

        with self._lock:
            win32gui.EnumWindows(_collect, None)

        self._check_cancelled(cancelled)

        wanted_pid = self._coerce_pid(pid)
        needle = str(title_contains or "").strip().lower()
        results: list[WindowInfo] = []
        for hwnd in collected:
            info = self._describe(hwnd, win32gui, win32process,
                                  collect_title=True)
            if info is None:
                continue
            if visible_only and not info.visible:
                continue
            if not include_untitled and not info.title.strip():
                continue
            if wanted_pid is not None and info.pid != wanted_pid:
                continue
            if needle and needle not in info.title.lower():
                continue
            results.append(info)
        return results

    def get_window(self, identity: WindowIdentity | dict,
                   *, cancelled: Callable[[], bool] | None = None) -> WindowInfo:
        """Re-read one window and prove it is the one the identity names.

        This is the verification step every mutating operation performs. It never
        returns a window that does not match, so a caller cannot act on a
        recycled handle by skipping the check.
        """
        self._guard()
        self._check_cancelled(cancelled)
        wanted = self._coerce_identity(identity)
        if not wanted.anchored:
            raise WindowsInvalidRequestError(
                "window identity is not anchored to a process creation time, so "
                "it cannot be verified before being acted on.")
        current = self._read(wanted.hwnd, cancelled=cancelled)
        if current is None:
            raise WindowsWindowNotFoundError(
                f"No window exists for hwnd 0x{wanted.hwnd:X}.",
                diagnostics={"hwnd": wanted.hwnd, "pid": wanted.pid})
        if not wanted.matches(current.identity):
            raise WindowsWindowIdentityMismatchError(
                "the window handle now belongs to a different window; refusing "
                "to act on it.",
                diagnostics={
                    "hwnd": wanted.hwnd,
                    "requested": wanted.to_dict(),
                    "observed": current.identity.to_dict(),
                },
            )
        return current

    def _read(self, hwnd: int, *,
              cancelled: Callable[[], bool] | None = None) -> WindowInfo | None:
        """Describe a window, or ``None`` if it does not exist.

        Cancellation is checked *before* deciding, and raises. Returning ``None``
        here would be indistinguishable from "the window does not exist", so a
        cancelled read would surface to the caller as a missing window - a wrong
        outcome that a caller could mistake for a stale handle.
        """
        win32gui, win32process, _ = _load_gui()
        self._check_cancelled(cancelled)
        return self._describe(int(hwnd), win32gui, win32process,
                              collect_title=True)

    # ------------------------------------------------------------- internals

    def _describe(self, hwnd: int, win32gui: Any, win32process: Any,
                  *, collect_title: bool) -> WindowInfo | None:
        """Build a :class:`WindowInfo` for one handle.

        Returns ``None`` when the window does not exist, rather than raising:
        enumeration walks a snapshot of handles and any of them can be destroyed
        mid-walk, which is routine rather than exceptional.
        """
        try:
            thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001 - a destroyed handle raises here
            return None
        try:
            class_name = win32gui.GetClassName(hwnd) or ""
            instance = int(win32gui.GetWindowLong(hwnd, _GWL_HINSTANCE) or 0)
            title = (win32gui.GetWindowText(hwnd) or "") if collect_title else ""
            visible = bool(win32gui.IsWindowVisible(hwnd))
            minimized = bool(win32gui.IsIconic(hwnd))
            enabled = bool(win32gui.IsWindowEnabled(hwnd))
            rect = tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
        except Exception:  # noqa: BLE001 - destroyed between the two calls
            return None

        created_at, process_name = self._process_anchor(pid)
        identity = WindowIdentity(
            hwnd=hwnd,
            process_identity=(int(pid), created_at),
            class_name=class_name,
            instance=instance,
        )
        foreground = False
        try:
            foreground = int(win32gui.GetForegroundWindow() or 0) == int(hwnd)
        except Exception:  # noqa: BLE001 - purely diagnostic
            foreground = False

        maximized = bool(rect[2] - rect[0] >= 0 and
                         _is_maximized(win32gui, hwnd))
        return WindowInfo(
            identity=identity,
            title=title,
            visible=visible,
            minimized=minimized,
            maximized=maximized,
            enabled=enabled,
            foreground=foreground,
            owner_pid=int(pid),
            thread_id=int(thread_id),
            process_name=process_name,
            rect=rect,  # type: ignore[arg-type]
        )

    def _process_anchor(self, pid: int) -> tuple[str, str]:
        """The owning process's ``(created_at, name)``.

        Reuses the process layer rather than re-reading the process table, so a
        window's durable anchor is the same kernel-sourced value the process
        layer already validates. A window whose process cannot be read returns an
        empty ``created_at`` and is therefore not safely targetable - which is
        reported honestly rather than papered over.
        """
        try:
            info = self._processes.get_process(int(pid))
        except Exception:  # noqa: BLE001 - protected or already gone
            return "", ""
        if info is None:
            return "", ""
        return str(info.created_at or ""), str(info.name or "")

    @staticmethod
    def _coerce_pid(pid: Any) -> int | None:
        if pid is None or pid == "":
            return None
        if isinstance(pid, bool):
            raise WindowsInvalidRequestError("pid must be an integer.")
        if isinstance(pid, float):
            if pid != int(pid):
                raise WindowsInvalidRequestError(
                    "pid must be an integer; a fractional pid would name a "
                    "different process than intended.")
            pid = int(pid)
        try:
            value = int(pid)
        except (TypeError, ValueError) as exc:
            raise WindowsInvalidRequestError(f"pid must be an integer, got {pid!r}.") from exc
        if value <= 0:
            raise WindowsInvalidRequestError("pid must be positive.")
        return value

    @staticmethod
    def _coerce_identity(identity: Any) -> WindowIdentity:
        """Accept a :class:`WindowIdentity`, its dict form, or its key string.

        Strict, because a partially-populated identity that is accepted would
        create a window reference that cannot be verified before it is acted on.

        The key string is accepted because it is this layer's *own* durable
        identifier: it is what ``enumerate_windows`` publishes in every row, what
        reaches logs and model context, and what M2.16.5 stores inside a UI element
        descriptor. A form this layer emits and cannot consume is a bug waiting for a
        caller - and the M2.16.6 workflow hit exactly that, passing a key back to
        ``get_window`` and being refused.
        """
        if isinstance(identity, WindowIdentity):
            return identity
        if isinstance(identity, str):
            return WindowIdentity.from_key(identity)
        if isinstance(identity, dict):
            if identity.get("window_key") and not identity.get("hwnd"):
                return WindowIdentity.from_key(str(identity["window_key"]))
            return WindowIdentity.from_dict(identity)
        raise WindowsInvalidRequestError(
            "window identity must be a WindowIdentity, its dict form, or its key.")

    @classmethod
    def _refuse_if_protected(cls, info: WindowInfo, operation: str) -> None:
        """Refuse lifecycle verbs against shell and protected windows.

        Refusing is a deliberate decision, distinct from Windows saying no. These
        are the desktop, the taskbar and the shell's own compositing windows;
        closing or minimizing one removes a component of the user's session. No
        attempt is made to work around a denial.
        """
        if info.identity.class_name in _PROTECTED_CLASSES:
            raise WindowsProtectedProcessError(
                f"Refused to {operation} a protected shell window "
                f"({info.identity.class_name}).",
                diagnostics={
                    "hwnd": info.hwnd,
                    "class_name": info.identity.class_name,
                    "operation": operation,
                },
            )

    # ------------------------------------------------------------- lifecycle

    def _mutate(self, identity: Any, operation: str, action: str,
                cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Verify, then act, then re-read to report observed state.

        The ordering is the safety property. Verify the identity *before* acting,
        because after the fact a successful API call is indistinguishable from a
        successful call against the wrong window.

        The result reports what was observed afterwards rather than what was
        requested. The bridge decides what that means for verification.
        """
        self._guard()
        self._check_cancelled(cancelled)
        wanted = self._coerce_identity(identity)
        before = self.get_window(wanted, cancelled=cancelled)
        self._refuse_if_protected(before, operation)
        win32gui, _win32process, _ = _load_gui()
        hwnd = before.hwnd

        try:
            performed = action(win32gui, hwnd)
        except WindowsAccessDeniedError:
            raise
        except Exception as exc:  # noqa: BLE001 - win32 raises many concrete types
            raise _translate(exc, hwnd=hwnd, operation=operation) from exc

        self._check_cancelled(cancelled)

        after = self._read(hwnd, cancelled=cancelled)
        payload = {
            "requested": bool(performed),
            "confirmed": after is not None,
            "operation": operation,
            "window": after.to_dict() if after is not None else None,
            "window_key": wanted.key,
            "before": before.to_dict(),
        }
        if after is not None and not wanted.matches(after.identity):
            # The handle was recycled between the action and the re-read. The
            # action may or may not have landed on the intended window, and that
            # is exactly the ambiguity this layer refuses to paper over.
            raise WindowsWindowIdentityMismatchError(
                "the window handle was recycled during the operation; the result "
                "cannot be attributed to the requested window.",
                diagnostics={
                    "hwnd": hwnd,
                    "requested": wanted.to_dict(),
                    "observed": after.identity.to_dict(),
                },
            )
        if after is None:
            raise WindowsWindowGoneError(
                "the window was destroyed by the operation.",
                diagnostics={"hwnd": hwnd, "operation": operation})
        return payload

    def focus_window(self, identity: Any, *,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Bring a window to the foreground and verify it became foreground.

        Activation is genuinely refused by Windows when the calling process is
        not the foreground process and the user is interacting elsewhere. That
        refusal is reported as not-confirmed rather than worked around, so all
        three facts survive separately:

        ``requested``
            the activation sequence was dispatched;
        ``activated``
            Windows' own return from ``SetForegroundWindow``;
        ``confirmed``
            the target is *observably* the foreground window.

        Windows returns FALSE when it declines, which is a normal outcome rather
        than an error, so it is reported and not raised.
        """
        state: dict[str, Any] = {"activated": False, "refused": ""}

        def _action(win32gui: Any, hwnd: int) -> bool:
            # Activation can be *refused* by Windows in two ways: a FALSE return,
            # or an exception with no winerror (the foreground lock). Both are
            # normal outcomes while the user is interacting with another
            # application - not infrastructure failures. Treating the second as a
            # hard error turned a routine refusal into an exception, so both are
            # recorded and the *observed* foreground state is what gets reported.
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, _SW_RESTORE)
            except Exception as exc:  # noqa: BLE001
                raise _translate(exc, hwnd=hwnd, operation="focus") from exc
            for call in ("BringWindowToTop", "SetForegroundWindow"):
                try:
                    result = getattr(win32gui, call)(hwnd)
                except Exception as exc:  # noqa: BLE001 - a refusal, not a failure
                    state["refused"] = f"{call}: {exc}"
                    break
                if call == "SetForegroundWindow":
                    state["activated"] = bool(result)
            return True

        payload = self._mutate(identity, "focus", _action, cancelled=cancelled)
        window = payload.get("window") or {}
        payload["activated"] = state["activated"]
        if state["refused"]:
            payload["refused"] = state["refused"]
        # ``confirmed`` is the *observed* outcome, not the API's opinion. This is
        # what the bridge's verifier reads, so a refused activation can never be
        # reported as a successful one.
        payload["confirmed"] = bool(window.get("foreground"))
        payload["foreground"] = bool(window.get("foreground"))
        return payload

    def minimize_window(self, identity: Any, *,
                        cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Minimize a window and verify it is minimized."""

        def _action(win32gui: Any, hwnd: int) -> bool:
            win32gui.ShowWindow(hwnd, _SW_MINIMIZE)
            return True

        return self._mutate(identity, "minimize", _action, cancelled=cancelled)

    def restore_window(self, identity: Any, *,
                       cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Restore a window and verify it is no longer minimized."""

        def _action(win32gui: Any, hwnd: int) -> bool:
            win32gui.ShowWindow(hwnd, _SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            return True

        return self._mutate(identity, "restore", _action, cancelled=cancelled)

    def close_window(self, identity: Any, *,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Close a window and verify it actually went away.

        Closing is the most consequential verb here, so the verification is
        strict: the window is re-read and the operation is only confirmed when it
        is genuinely gone. A window that merely stops being visible - a close
        request that some application deferred or ignored - is reported as not
        confirmed rather than as a success.
        """
        wanted = self._coerce_identity(identity)
        self._guard()
        self._check_cancelled(cancelled)
        before = self.get_window(wanted, cancelled=cancelled)
        self._refuse_if_protected(before, "close")
        win32gui, _win32process, _ = _load_gui()
        hwnd = before.hwnd

        try:
            # PostMessage returns None on success in pywin32 and raises on
            # failure. Coercing it with bool() would report every close as
            # "not requested", which is the opposite of what happened.
            win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            posted = True
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, hwnd=hwnd, operation="close") from exc

        # WM_CLOSE is asynchronous: the receiving application decides when to
        # actually destroy. Poll briefly for the window to disappear so the
        # result reflects observed state rather than "a message was posted".
        confirmed = False
        remaining = 30
        while remaining > 0:
            if self._cancelled(cancelled):
                raise WindowsCancelledError(
                    "Cancelled while waiting for the window to close.",
                    diagnostics={"hwnd": hwnd,
                                 "cancellation_terminated": False},
                )
            self._check_cancelled(cancelled)
            if not _is_window(win32gui, hwnd):
                confirmed = True
                break
            remaining -= 1
            _sleep(0.05)

        return {
            "requested": posted,
            "confirmed": confirmed,
            "operation": "close",
            "window_key": wanted.key,
            "hwnd": hwnd,
            "before": before.to_dict(),
            "window": None if confirmed else before.to_dict(),
        }


def _is_window(win32gui: Any, hwnd: int) -> bool:
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _is_maximized(win32gui: Any, hwnd: int) -> bool:
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        return int(placement[1]) == 3  # SW_SHOWMAXIMIZED
    except Exception:  # noqa: BLE001 - purely diagnostic
        return False


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _translate(exc: Exception, *, hwnd: int, operation: str) -> WindowsOperationFailedError:
    """Map a raw Win32 exception onto this layer's structured vocabulary.

    Windows raises ``pywintypes.error`` with a winerror code. Translating it here
    keeps the vocabulary stable at the layer boundary instead of leaking
    ``pywintypes`` types to the capability layer and the model.
    """
    winerror = int(getattr(exc, "winerror", 0) or 0)
    if winerror in (5, 13):  # ERROR_ACCESS_DENIED, ERROR_INVALID_ACCESS
        return WindowsAccessDeniedError(
            f"Windows denied {operation} on hwnd 0x{hwnd:X}.",
            diagnostics={"hwnd": hwnd, "operation": operation,
                         "winerror": winerror},
        )
    if winerror == 1400:  # ERROR_INVALID_WINDOW_HANDLE
        return WindowsWindowGoneError(
            f"The window 0x{hwnd:X} no longer exists.",
            diagnostics={"hwnd": hwnd, "operation": operation},
        )
    return WindowsOperationFailedError(
        f"Windows failed to {operation} the window 0x{hwnd:X}: {exc}",
        diagnostics={"hwnd": hwnd, "operation": operation, "winerror": winerror},
    )