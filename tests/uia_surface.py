"""A test-owned native Windows UI surface with real UI Automation semantics.

M2.16.5's real tests need a window whose controls UIA reports properly. Tk was
tried first and rejected on evidence: its controls arrive as generic ``TkChild``
panes with empty names, empty AutomationIds and no patterns at all, so nothing
about them can be identified, verified, or acted on.

Native Win32 standard controls are the opposite. ``BUTTON`` reports
``ControlType.Button`` with ``AutomationId`` set to the control id,
``EDIT`` reports ``Value``, ``BUTTON|BS_AUTOCHECKBOX`` reports ``Toggle``, and
``LISTBOX`` items report ``SelectionItem`` - each with a state this layer can read
back and verify against.

The surface is created in the test's own process and destroyed with it. Nothing
here belongs to the user: no existing window is touched, no display setting is
changed, and the previous foreground window and cursor position are restored.

One non-obvious requirement, learned the hard way: an in-process Win32 control only
reacts once its thread pumps messages. ``Toggle`` and ``Invoke`` post a
``BM_CLICK``, so without :meth:`Probe.pump` the call returns successfully, the
postcondition reads unchanged, and the action looks like it silently did nothing.
Every action test pumps.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

import win32con

from core.windows.errors import WindowsOperationFailedError

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p)]


# Declared explicitly. Without argtypes, ctypes passes Python ints as C ints,
# which truncates a 64-bit handle to 32 bits and the window never appears at all.
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                ctypes.c_void_p]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.SetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p]
user32.SetWindowTextW.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
# Declared for the same reason as above. ``DispatchMessageW`` returns the window
# procedure's LRESULT, which is pointer-sized on 64-bit; left undeclared ctypes
# reads it back as an int and truncates it. These three are called on every pump.
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

WS_POPUP = 0x80000000 | 0x00C00000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
BS_PUSHBUTTON = 0x00000000
BS_AUTOCHECKBOX = 0x00000003
ES_AUTOHSCROLL = 0x0080
LBS_NOTIFY = win32con.LBS_NOTIFY
#: Without LBS_HASSTRINGS a list box is owner-draw, and LB_ADDSTRING silently
#: populates nothing. The control then reports an empty list to UIA too, so this
#: looks like a UIA discovery failure rather than the style bug it is.
LBS_HASSTRINGS = win32con.LBS_HASSTRINGS
# Taken from ``win32con`` rather than written out here. Hand-copied constants
# were wrong by enough to be instructive: ``LB_ADDSTRING`` is 0x0180 (0x0189 is
# ``LB_GETTEXT``) and ``CB_ADDSTRING`` is 0x0143 (0x0403 is ``LB_ADDSTRINGW``'s
# neighbour in a different block entirely). Both wrong values return "refused"
# rather than raising, so the symptom is an empty control that looks like an
# application refusing to populate - not an obvious typo. pywin32 is already a
# runtime dependency, so the authoritative values were available all along.
CBS_DROPDOWNLIST = win32con.CBS_DROPDOWNLIST
BM_GETCHECK = win32con.BM_GETCHECK
BM_SETCHECK = win32con.BM_SETCHECK
LB_RESETCONTENT = win32con.LB_RESETCONTENT
LB_ADDSTRING = win32con.LB_ADDSTRING
LB_GETCOUNT = win32con.LB_GETCOUNT
LB_GETCURSEL = win32con.LB_GETCURSEL
CB_ADDSTRING = win32con.CB_ADDSTRING
CB_GETCOUNT = win32con.CB_GETCOUNT

#: ``pywin32`` exposes no wide list-box message ids, and the SDK header is not
#: installed here, so this one was **measured**: every message in the 0x019C-0x01AF
#: range was tried against a real list box and 0x01AA is the one that actually adds
#: a UTF-16 item. Using an ANSI message with a wide buffer returns "refused" and
#: leaves the control empty, which reads like an application that will not populate
#: rather than a wrong message id.
LB_ADDSTRINGW = 0x01AA
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002

CLASS_NAME = "DiabloM2165UiaSurface"
TITLE = "DIABLO_M2165_UIA_SURFACE"

#: Control ids, which UIA reports as ``AutomationId`` for native controls.
IDS = {"label": 101, "button": 102, "edit": 103, "check": 104,
       "combo": 106, "witness": 107}

#: The probe whose window is currently alive, and the one registered window class.
#:
#: The window class is process-wide and Windows holds a raw pointer to its
#: ``WNDPROC`` for as long as the class is registered. Registering per probe and
#: unregistering on close - the obvious arrangement - is unsafe in both
#: directions: the unregister races with messages still queued for the class, and
#: tolerating ``ERROR_CLASS_ALREADY_EXISTS`` instead silently reuses a callback
#: belonging to a closed probe, so Windows calls into freed memory and the
#: interpreter dies with an access violation.
#:
#: So the class is registered exactly once and its callback dispatches to whichever
#: probe is current. There is one class, one callback, and neither is ever torn
#: down while a message could reference it.
_current_probe: "UiaProbe | None" = None
_class_registered = False



def _dispatch(hwnd, msg, wparam, lparam):
    probe = _current_probe
    if probe is None:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return probe._window_proc(hwnd, msg, wparam, lparam)


_WND_PROC = WNDPROC(_dispatch)


def _ensure_class() -> None:
    global _class_registered
    if _class_registered:
        return
    window_class = WNDCLASS()
    window_class.lpfnWndProc = _WND_PROC
    window_class.hInstance = kernel32.GetModuleHandleW(None)
    window_class.lpszClassName = CLASS_NAME
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise WindowsOperationFailedError(
            "could not register the UI Automation test window class.",
            diagnostics={"error": kernel32.GetLastError()})
    _class_registered = True


class UiaProbe:
    """A native window with deterministic, UIA-visible controls."""

    def __init__(self, *, position: tuple[int, int] = (240, 180),
                 size: tuple[int, int] = (520, 340)) -> None:
        self.click_count = 0
        self.position = position
        self.size = size
        self.hwnd = 0
        self.controls: dict[str, int] = {}
        self._build()

    # ---------------------------------------------------------------- build

    def _window_proc(self, hwnd, msg, wparam, lparam):
        # The button's handler updating a label is what gives Invoke a genuinely
        # observable postcondition, so an invoke can be verified rather than
        # merely attempted.
        if msg == WM_COMMAND:
            # ``WM_COMMAND`` packs both halves into wParam: the **LOWORD** is the
            # control id and the HIWORD is the notification code. Reading it the
            # other way round compares against the notification (always
            # BN_CLICKED, i.e. 0) and so this handler silently never fires -
            # which shows up as an Invoke that "worked" and changed nothing.
            control_id = int(wparam) & 0xFFFF
            if control_id == IDS["button"]:
                self.click_count += 1
                user32.SetWindowTextW(self.controls.get("witness", 0),
                                      f"presses={self.click_count}")
                return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _build(self) -> None:
        global _current_probe
        _ensure_class()
        _current_probe = self
        hwnd = user32.CreateWindowExW(
            0, CLASS_NAME, TITLE, WS_POPUP | WS_VISIBLE,
            int(self.position[0]), int(self.position[1]),
            int(self.size[0]), int(self.size[1]), None, None,
            kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            _current_probe = None
            raise WindowsOperationFailedError(
                "could not create the UI Automation test window.",
                diagnostics={"error": kernel32.GetLastError()})
        self.hwnd = int(hwnd)
        user32.ShowWindow(self.hwnd, 5)
        self._control("STATIC", "presses=0", 0, 10, 8, 200, 18, IDS["label"])
        self._control("BUTTON", "Press Me", BS_PUSHBUTTON, 10, 30, 160, 26,
                      IDS["button"])
        self._control("EDIT", "seed", ES_AUTOHSCROLL, 10, 62, 300, 24,
                      IDS["edit"])
        self._control("BUTTON", "Toggle Me", BS_AUTOCHECKBOX, 10, 92, 160, 24,
                      IDS["check"])
        # A list box is deliberately absent. Its items came back upper-cased and
        # in reverse insertion order through every message id tried, which is not a
        # behaviour I could explain - and a fixture element whose behaviour is not
        # understood is not something to assert against. Button, Edit, CheckBox and
        # ComboBox cover every pattern this milestone acts through.
        combo = self._control("COMBOBOX", None, CBS_DROPDOWNLIST, 200, 122, 180,
                              90, IDS["combo"])
        for item in ("one", "two", "three"):
            self._send_text(combo, CB_ADDSTRING, item)
        # The witness the Invoke postcondition watches: a static whose text the
        # button's handler changes, so an invoke can be verified rather than
        # merely attempted.
        self._control("STATIC", "presses=0", 0, 200, 220, 200, 18,
                      IDS["witness"])
        self.pump()

    @staticmethod
    def _send_text(hwnd: int, message: int, text: str) -> None:
        """Send a message whose lParam is a string pointer.

        Two things matter here. The buffer is held in a local for the duration of
        the call - inlining it into the ``cast`` leaves a temporary with no
        reference, and if it is collected first the message goes out with a
        dangling pointer, which shows as an empty control rather than a crash. And
        the message is one of the ANSI ones from ``win32con``, so the buffer is
        built as bytes; the wide variants have different ids that are not worth
        guessing at for ASCII test content.
        """
        buffer = ctypes.create_string_buffer(text.encode("ascii", "replace"), 64)
        user32.SendMessageW(hwnd, message, 0, ctypes.cast(buffer, ctypes.c_void_p))

    @staticmethod
    def _send_text_wide(hwnd: int, message: int, text: str) -> None:
        """Send a wide-character list message. See ``LB_ADDSTRINGW``."""
        buffer = ctypes.create_unicode_buffer(text)
        user32.SendMessageW(hwnd, message, 0, ctypes.cast(buffer, ctypes.c_void_p))

    def _control(self, cls: str, text: str | None, style: int, x: int, y: int,
                 w: int, h: int, control_id: int) -> int:
        hwnd = user32.CreateWindowExW(
            0, cls, text, style | WS_CHILD | WS_VISIBLE, x, y, w, h, self.hwnd,
            control_id, kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            raise WindowsOperationFailedError(
                f"could not create the {cls} test control.",
                diagnostics={"control_id": control_id,
                             "error": kernel32.GetLastError()})
        for name, value in IDS.items():
            if value == control_id:
                self.controls[name] = int(hwnd)
        return int(hwnd)

    # ------------------------------------------------------------- lifecycle

    def pump(self, rounds: int = 6) -> None:
        """Run the message loop so posted control messages are processed."""
        message = wintypes.MSG()
        for _ in range(rounds):
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            user32.UpdateWindow(self.hwnd)
            time.sleep(0.01)

    def activate(self) -> bool:
        """Bring the probe forward, using the documented thread-attach technique.

        Test-harness only, exactly as in M2.16.2: Windows refuses activation from a
        process that does not own the foreground, so the fixture attaches to the
        foreground thread's input queue. The *product* still honours that refusal.
        """
        import win32process

        current = int(user32.GetForegroundWindow() or 0)
        if not current:
            return bool(user32.SetForegroundWindow(self.hwnd))
        fg_thread = int(win32process.GetWindowThreadProcessId(current)[0])
        our_thread = int(kernel32.GetCurrentThreadId())
        attached = bool(user32.AttachThreadInput(our_thread, fg_thread, True))
        try:
            user32.SetForegroundWindow(self.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(our_thread, fg_thread, False)
        return int(user32.GetForegroundWindow() or 0) == self.hwnd

    def close(self) -> None:
        global _current_probe
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
        self.hwnd = 0
        if _current_probe is self:
            # Only the live probe's callback is reachable, and it stays pointed at
            # the single registered class. Nothing is unregistered, so no queued
            # message can reference a class that is being torn down.
            _current_probe = None

    # ------------------------------------------------------------ inspection

    def control(self, name: str) -> int:
        return int(self.controls[name])

    def own_window_key(self) -> str:
        """This probe's key, resolved by handle rather than by title.

        ``window_key()`` searches by title, so with two probes sharing a title it
        may return the *other* one. When a test needs to name a specific window -
        the ambiguity test does - it has to ask by handle. The full identity is
        enumerated rather than assembled here, because ``get_window`` requires a
        creation time before it will treat an identity as verifiable.
        """
        from core.windows.windows import WindowLayer

        layer = WindowLayer()
        try:
            for info in layer.enumerate_windows(include_untitled=True):
                if int(info.hwnd) == int(self.hwnd):
                    return info.identity.key
        finally:
            layer.close()
        raise AssertionError(
            f"window 0x{self.hwnd:X} was not found among the enumerated windows")

    def window_key(self) -> str:
        """The M2.16.1 identity key for this window, as the capability sees it.

        The tests pass this string around rather than the whole identity on
        purpose: a stored element names its window by key, so exercising that path
        is the point.
        """
        from core.windows.windows import WindowLayer

        layer = WindowLayer()
        try:
            for info in layer.enumerate_windows(title_contains=TITLE):
                return info.identity.key
        finally:
            layer.close()
        raise AssertionError(f"no window titled {TITLE!r} was found")

    def window_text(self, name: str) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(self.controls[name], buffer, 256)
        return buffer.value

    def set_window_text(self, name: str, text: str) -> None:
        user32.SetWindowTextW(self.controls[name], text)

    def checkbox_state(self) -> int:
        """The checkbox's Win32 state: the ground truth a UIA read is checked against."""
        self.pump()
        return int(user32.SendMessageW(self.controls["check"], BM_GETCHECK, 0, None))

    def set_checkbox_state(self, state: int) -> None:
        user32.SendMessageW(self.controls["check"], BM_SETCHECK, int(state), None)
        self.pump()

    def list_item_count(self) -> int:
        """Always zero: this surface has no list box. See ``_build``."""
        return 0

    def recreate_child(self, name: str, cls: str = "STATIC", text: str = "rebuilt",
                       style: int = 0, box: tuple[int, int, int, int] | None = None,
                       ) -> int:
        """Destroy and recreate a child, to simulate a UI tree rebuild.

        The stale-target tests need the tree to change *underneath* a recorded
        target. Recreating a control with the same id is the realistic version of
        that, and it is the case a naive implementation gets wrong: the
        AutomationId is still there, so a recipe that only checks that would
        resolve to the replacement and act on it.
        """
        old = self.controls.get(name)
        if old and user32.IsWindow(old):
            user32.DestroyWindow(old)
            self.pump()
        x, y, w, h = box or (200, 60, 200, 20)
        return self._control(cls, text, style, x, y, w, h,
                             IDS.get(name, 900 + len(self.controls)))

    def destroy_window(self) -> None:
        """Close the window entirely, for the window-gone stale test."""
        global _current_probe
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
        self.hwnd = 0
        self.pump()
        if _current_probe is self:
            _current_probe = None