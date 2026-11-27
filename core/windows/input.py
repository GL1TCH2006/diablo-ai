"""Native pointer and keyboard input dispatch.

M2.16.2 / M2.16.3. Everything that talks to Windows input lives here and nowhere
else. :mod:`core.windows.coordinates` decides *what* to hit; this module is the
only place that decides *how*, and no logic above the capability boundary ever
sees a virtual-key code, an ``INPUT`` struct or a handle.

Why ``SendInput``
-----------------
``mouse_event`` and ``keybd_event`` are the 16-bit-era calls: they truncate
coordinates, are unimplemented on modern Windows for several input types, and
cannot express Unicode. ``SendInput`` is the supported mechanism, it takes a
32-bit absolute coordinate, and it accepts ``KEYEVENTF_UNICODE`` so text can be
typed without depending on the active keyboard layout.

It is also the mechanism that injects input system-wide, which is why it is
confined here, reached only through the capability layer, and only after a target
has been resolved and verified. A user-installed ``LLMHF_INJECT`` block is
honoured by Windows itself: it makes ``SendInput`` return zero, which is treated
here as a refusal rather than a success.

What this layer does **not** claim
----------------------------------
A physical input event that has reached Windows cannot be recalled. Cancellation
is honoured before dispatch and at each cancellable boundary; once ``SendInput``
has been handed the event there is no further cancellation point. That is a
platform property rather than an omission, and results report ``dispatched``
without implying any confirmed effect.

Modifier cleanup
----------------
A modifier that is pressed and never released leaves the machine in a state the
user did not ask for: Ctrl stuck down, and the next keystroke interpreted as a
shortcut. Every sequence releases what it pressed in a ``finally``, so a native
failure, an exception or a cancellation cannot leave one logically held.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from core.logging_setup import get_logger

from .coordinates import (
    ResolvedPoint,
    VirtualScreen,
    require_anchored,
    resolve_point,
)
from .errors import (
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
)

log = get_logger("windows.input")

__all__ = ["InputLayer", "Keys", "InputDispatchResult", "CURSOR_TOLERANCE"]


# ══════════════════════════════════════════════════════════ key vocabulary


class Keys:
    """Normalised key names.

    A small named set rather than an enumeration of every virtual-key code. A
    caller names a key (``"enter"``, ``"ctrl"``); this maps it to the codes Windows
    wants. That is what stops a model passing an arbitrary integer as a virtual
    key: an unvalidated code is a way to press whatever the model likes, including
    system combinations.

    Text does not go through this table at all - it uses ``KEYEVENTF_UNICODE`` -
    which is what keeps typing independent of the active keyboard layout.
    """

    MODIFIERS = frozenset({"ctrl", "control", "alt", "shift", "win", "meta"})

    SPECIAL: dict[str, int] = {
        "backspace": 0x08, "tab": 0x09, "clear": 0x0C,
        "enter": 0x0D, "return": 0x0D, "pause": 0x13, "caps_lock": 0x14,
        "escape": 0x1B, "esc": 0x1B,
        "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "page_up": 0x21,
        "page_down": 0x22,
        "end": 0x23, "home": 0x24,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
        "select": 0x29, "print": 0x2A, "execute": 0x2B,
        "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
        "help": 0x2F,
        "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
        "numlock": 0x90, "scrolllock": 0x91,
        "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
        "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD,
        "period": 0xBE, "slash": 0xBF, "backtick": 0xC0,
        "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD, "quote": 0xDE,
        # Function keys. A bounded, well-known set rather than an enumeration of
        # every virtual key: F1-F24 are standard keys that a caller reasonably
        # expects, and refusing them would be an arbitrary cliff rather than a
        # safety boundary. Anything outside this table is still refused.
        "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
        "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
        "f11": 0x7A, "f12": 0x7B, "f13": 0x7C, "f14": 0x7D, "f15": 0x7E,
        "f16": 0x7F, "f17": 0x80, "f18": 0x81, "f19": 0x82, "f20": 0x83,
        "f21": 0x84, "f22": 0x85, "f23": 0x86, "f24": 0x87,
        # Numeric keypad. Also extended keys, so the keypad twin of an arrow
        # key is distinguishable from it.
        "num_0": 0x60, "num_1": 0x61, "num_2": 0x62, "num_3": 0x63,
        "num_4": 0x64, "num_5": 0x65, "num_6": 0x66, "num_7": 0x67,
        "num_8": 0x68, "num_9": 0x69,
        "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E,
        "divide": 0x6F,
    }

    #: Single characters map to their uppercase virtual key.
    PUNCTUATION: dict[str, int] = {
        "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
        "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
        "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
        "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
        "m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
        "s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
        "y": 0x59, "z": 0x5A,
        ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
        "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    }

    @classmethod
    def normalise(cls, key: Any) -> tuple[str, int]:
        """Map a caller-supplied key name to ``(canonical_name, virtual_key)``.

        Raises rather than guessing. An unknown name is refused because the
        alternative - accepting an integer - permits pressing any key at all.
        """
        if not isinstance(key, str):
            raise WindowsInvalidRequestError(
                f"key must be a name from the supported set, not "
                f"{type(key).__name__}.")
        name = key.strip().lower().replace(" ", "_").replace("-", "_")
        if not name:
            raise WindowsInvalidRequestError("key must not be empty.")
        if name in cls.SPECIAL:
            return name, cls.SPECIAL[name]
        if name in cls.PUNCTUATION:
            return name, cls.PUNCTUATION[name]
        if len(name) == 1 and name.isalpha():
            return name, cls.PUNCTUATION[name]
        raise WindowsInvalidRequestError(
            f"unsupported key {key!r}. Supported names are the special keys, the "
            f"modifiers {sorted(cls.MODIFIERS)}, and single characters.",
            diagnostics={"key": key},
        )

    @classmethod
    def is_modifier(cls, name: str) -> bool:
        return str(name or "").strip().lower() in cls.MODIFIERS


# ══════════════════════════════════════════════════════════ input structs


_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

#: Extended keys need the extended flag or Windows reads them as their numpad
#: twin: the arrows become keypad arrows, and Delete stops being Delete.
_EXTENDED_KEYS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x90, 0xA3, 0x6F,
})

#: Windows clamps a pointer to the edge of the virtual desktop, so a requested
#: position one pixel outside can legitimately land one pixel inside - or the
#: reverse. Pointer movement is therefore confirmed against this tolerance rather
#: than requiring exact equality, and the observed value is always reported so the
#: caller sees what actually happened.
CURSOR_TOLERANCE = 1

#: How long to wait for the pointer to arrive at a requested position before
#: reporting a dispatch that the OS did not visibly perform.
_CURSOR_SETTLE_TIMEOUT = 0.75


def _load_input_apis() -> tuple[Any, Any]:
    """Bind the pywin32 surface input depends on."""
    try:
        import win32api
        import win32con
    except ImportError as exc:  # pragma: no cover - Windows-only
        raise WindowsOperationFailedError(
            "pywin32 input APIs are unavailable on this host.") from exc
    return win32api, win32con


def _send_input(inputs: list[_INPUT]) -> int:
    """One ``SendInput`` call. Raises unless every event was injected.

    A short or zero return means the events were **not** injected - blocked by a
    low-level hook, or refused by the system. That is surfaced as an error rather
    than a count for the caller to interpret, because every caller would otherwise
    have to remember the check, and the one that forgot would report input as
    delivered when it never reached Windows at all.

    A short return is also not safely ignorable mid-sequence: if the third event of
    a modifier chord was dropped, the modifier really is down and has to be
    released. Raising lets the caller's ``finally`` do that.
    """
    if not inputs:
        return 0
    user32 = ctypes.windll.user32
    array = (_INPUT * len(inputs))(*inputs)
    sent = int(user32.SendInput(len(inputs), ctypes.byref(array),
                                ctypes.sizeof(_INPUT)))
    if sent != len(inputs):
        raise WindowsOperationFailedError(
            f"Windows injected {sent} of {len(inputs)} input events; the rest were "
            f"blocked or refused. Input was not delivered as requested.",
            diagnostics={"events_sent": int(sent), "events_requested": len(inputs)},
        )
    return sent


def _mouse_event(dx: int, dy: int, flags: int) -> _INPUT:
    event = _INPUT(type=_INPUT_MOUSE)
    event.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags,
                           time=0, dwExtraInfo=0)
    return event


def _key_event(vk: int, scan: int, flags: int) -> _INPUT:
    event = _INPUT(type=_INPUT_KEYBOARD)
    event.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                            dwExtraInfo=0)
    return event


def _unicode_event(char: str, key_up: bool) -> _INPUT:
    """One character, as a scan code with ``KEYEVENTF_UNICODE``.

    ``wVk`` is zero and the character goes in ``wScan``. That is what makes typing
    independent of the active keyboard layout: a Russian layout still produces the
    characters the caller asked for.
    """
    event = _INPUT(type=_INPUT_KEYBOARD)
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if key_up else 0)
    event.ki = _KEYBDINPUT(wVk=0, wScan=ord(char), dwFlags=flags, time=0,
                            dwExtraInfo=0)
    return event


# ══════════════════════════════════════════════════════════ results


class InputDispatchResult:
    """What was dispatched, and what could be independently observed.

    ``dispatched`` means Windows accepted the events. ``confirmed`` means this
    layer could *see* the result - possible for pointer movement, because the
    pointer is global state, but not for a click or a keystroke, whose effect lives
    inside an application this layer cannot observe.

    Keeping the two apart is what stops a click being reported as verified.
    """

    __slots__ = ("dispatched", "confirmed", "observed", "detail")

    def __init__(self, dispatched: bool, confirmed: bool = False,
                 observed: dict | None = None, detail: dict | None = None) -> None:
        self.dispatched = bool(dispatched)
        self.confirmed = bool(confirmed)
        self.observed = dict(observed or {})
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatched": self.dispatched,
            "confirmed": self.confirmed,
            "observed": dict(self.observed),
            "detail": dict(self.detail),
        }


# ══════════════════════════════════════════════════════════ the layer


class InputLayer:
    """Cursor inspection and native input dispatch.

    Holds a reference to the window layer rather than re-implementing target
    verification: resolving a point and confirming a foreground target is exactly
    what :class:`~core.windows.windows.WindowLayer` exists for, and duplicating
    that logic here is how the two would drift apart.
    """

    def __init__(self, window_layer: Any) -> None:
        self._windows = window_layer
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _guard(self) -> None:
        if self._closed:
            raise WindowsOperationFailedError("the input layer is closed.")

    # ------------------------------------------------------------ inspection

    def virtual_screen(self) -> VirtualScreen:
        """The union of every display, in virtual-desktop coordinates."""
        win32api, win32con = _load_input_apis()
        left = int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN))
        top = int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN))
        width = int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN))
        height = int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN))
        if width <= 0 or height <= 0:
            raise WindowsOperationFailedError(
                "the OS reported no virtual screen; there would be nothing to "
                "validate coordinates against.",
                diagnostics={"width": width, "height": height})
        return VirtualScreen(left=left, top=top, width=width, height=height)

    def get_cursor_pos(self) -> dict[str, Any]:
        """Read the pointer position and the virtual desktop geometry.

        Read-only. The virtual screen comes from the OS rather than being
        assembled from the primary monitor, because on a multi-display machine
        those differ, and validating against the wrong rectangle both rejects
        valid positions on a second display and accepts invalid ones above it.
        """
        self._guard()
        win32api, _win32con = _load_input_apis()
        try:
            x, y = win32api.GetCursorPos()
        except Exception as exc:  # noqa: BLE001
            raise WindowsOperationFailedError(
                f"the pointer position could not be read: {exc}") from exc
        return {"x": int(x), "y": int(y),
                "virtual_screen": self.virtual_screen().to_dict()}

    def get_foreground_window(self) -> dict[str, Any] | None:
        """The foreground window as a logical identity, or ``None``.

        M2.16.2. Returns the same :class:`WindowIdentity` every other window
        operation uses, so a caller can hand it straight back as an input target
        without a raw handle ever appearing.

        ``None`` is a real answer, not a failure: a locked or service desktop can
        genuinely have no foreground window, and fabricating one would give the
        caller a target that does not exist.
        """
        self._guard()
        info = self._foreground()
        return info.to_dict() if info is not None else None

    def _foreground(self) -> Any:
        from .windows import _load_gui

        win32gui, _win32process, _con = _load_gui()
        hwnd = int(win32gui.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        return self._windows._read(hwnd)

    def _foreground_key(self) -> str:
        info = self._foreground()
        return info.identity.key if info is not None else ""

    def confirm_target(self, identity: Any) -> Any:
        """Verify a target is valid and is the foreground window.

        Re-reads and re-verifies, so a handle recycled since the caller's last
        look is caught here rather than after input has already gone somewhere.
        """
        info = self._windows.get_window(identity)
        if info.minimized:
            raise WindowsInvalidRequestError(
                "the target window is minimized; it cannot receive input until it "
                "is restored.",
                diagnostics={"window_key": info.identity.key})
        observed = self._foreground()
        if observed is None or not observed.identity.matches(info.identity):
            raise WindowsInvalidRequestError(
                "the target window is not the foreground window, so input would "
                "not reach it. Activate it first.",
                diagnostics={
                    "window_key": info.identity.key,
                    "foreground_window_key": observed.identity.key if observed else "",
                })
        return info

    # ---------------------------------------------------------- coordinates

    def resolve(self, raw: Any, *, require_foreground: bool = True) -> ResolvedPoint:
        """Resolve a caller point to a verified screen coordinate."""
        self._guard()
        observed_key = self._foreground_key() if require_foreground else ""
        return resolve_point(
            raw,
            reader=self._windows.get_window,
            virtual_screen=self.virtual_screen(),
            require_foreground=require_foreground,
            observed_foreground_key=observed_key,
        )

    def _require_dispatch_target(self, point: ResolvedPoint, *,
                                 cancelled=None) -> Any:
        """The check every input verb makes immediately before dispatching.

        Two separate things, and the order matters:

        1. The point must be *anchored* - resolved against a verified window. A bare
           screen coordinate names a position, not a target, and there is nothing to
           re-read to discover that the window it was aimed at has gone.
        2. The target must *still* be valid and *still* be the foreground window.
           Resolution already verified this, but that happened earlier; the gap
           between resolving and dispatching is exactly where a window closes, gets
            recycled, or loses foreground. Re-reading here shrinks the race to the
           microseconds inside ``SendInput``, which is as close as a caller without
           an atomic kernel primitive can get.

        Returning the freshly-read window lets a caller act on current geometry.
        """
        require_anchored(point)
        self._windows._check_cancelled(cancelled)
        return self.confirm_target(point.identity)

    # ---------------------------------------------------------------- mouse

    def move_cursor(self, point: ResolvedPoint, *,
                    cancelled=None) -> InputDispatchResult:
        """Move the pointer, then read it back.

        The one mouse verb whose effect is observable: the pointer is global OS
        state, so the result can be confirmed by reading it rather than trusted.
        """
        self._guard()
        self._windows._check_cancelled(cancelled)
        self._require_dispatch_target(point, cancelled=cancelled)
        target = self._absolute(point)

        events: list[_INPUT] = []
        for _ in range(2):
            # Sent as a pair because a single absolute move occasionally lands a
            # pixel short; Windows' own cursor warping does the same.
            events.append(_mouse_event(target[0], target[1],
                                       _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
                                       | _MOUSEEVENTF_VIRTUALDESK))
        sent = _send_input(events)
        self._windows._check_cancelled(cancelled)

        observed = self._wait_for_cursor(point.x, point.y, cancelled=cancelled)
        within = (observed is not None
                  and abs(observed["x"] - point.x) <= CURSOR_TOLERANCE
                  and abs(observed["y"] - point.y) <= CURSOR_TOLERANCE)
        return InputDispatchResult(
            dispatched=sent > 0,
            confirmed=bool(within),
            observed=observed or {},
            detail={
                "requested": {"x": point.x, "y": point.y},
                "tolerance_px": CURSOR_TOLERANCE,
                "events_sent": sent,
                "window_key": point.window_key,
            },
        )

    def click(self, point: ResolvedPoint, *, button: str = "left",
              count: int = 1, cancelled=None) -> InputDispatchResult:
        """Click at a verified point.

        A click's effect lives inside the receiving application, which this layer
        cannot observe. The result therefore reports ``dispatched`` with
        ``confirmed=False``: acknowledging that Windows injected the events is not
        evidence that the application acted on them, and claiming otherwise would
        be inventing evidence.
        """
        self._guard()
        if button not in ("left", "right"):
            raise WindowsInvalidRequestError(
                f"button must be 'left' or 'right', got {button!r}.")
        if int(count) not in (1, 2):
            raise WindowsInvalidRequestError(
                "count must be 1 (single) or 2 (double).")

        self._windows._check_cancelled(cancelled)
        self._require_dispatch_target(point, cancelled=cancelled)
        down = _MOUSEEVENTF_LEFTDOWN if button == "left" else _MOUSEEVENTF_RIGHTDOWN
        up = _MOUSEEVENTF_LEFTUP if button == "left" else _MOUSEEVENTF_RIGHTUP

        events = [_mouse_event(0, 0, down), _mouse_event(0, 0, up)]
        if int(count) == 2:
            # A double click is two down/up pairs. Windows derives the
            # double-click itself from the interval, so no special flag exists
            # and inventing one would be meaningless.
            events = events + [_mouse_effect(0, 0, down), _mouse_effect(0, 0, up)]
        sent = _send_input(events)
        self._windows._check_cancelled(cancelled)
        return InputDispatchResult(
            dispatched=sent > 0,
            confirmed=False,
            observed={},
            detail={
                "button": button, "count": int(count), "events_sent": sent,
                "requested": {"x": point.x, "y": point.y},
                "window_key": point.window_key,
                "effect": "not independently observable from outside the target "
                          "application; dispatch acknowledgement only",
            },
        )

    def move_and_click(self, point: ResolvedPoint, *, button: str = "left",
                       count: int = 1, cancelled=None) -> InputDispatchResult:
        """Move to a point and click there in one dispatch."""
        self._guard()
        self._windows._check_cancelled(cancelled)
        self._require_dispatch_target(point, cancelled=cancelled)
        target = self._absolute(point)
        absolute = (_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
                    | _MOUSEEVENTF_VIRTUALDESK)
        down = _MOUSEEVENTF_LEFTDOWN if button == "left" else _MOUSEEVENTF_RIGHTDOWN
        up = _MOUSEEVENTF_LEFTUP if button == "left" else _MOUSEEVENTF_RIGHTUP

        events = [_mouse_event(target[0], target[1], absolute),
                  _mouse_event(0, 0, down), _mouse_event(0, 0, up)]
        if int(count) == 2:
            events += [_mouse_event(0, 0, down), _mouse_event(0, 0, up)]
        sent = _send_input(events)
        self._windows._check_cancelled(cancelled)
        observed = self._wait_for_cursor(point.x, point.y, cancelled=cancelled)
        within = (observed is not None
                  and abs(observed["x"] - point.x) <= CURSOR_TOLERANCE
                  and abs(observed["y"] - point.y) <= CURSOR_TOLERANCE)
        return InputDispatchResult(
            dispatched=sent > 0,
            # The click itself is unobservable, but the movement that preceded it
            # is - so this verb carries the strongest evidence available.
            confirmed=bool(within),
            observed=observed or {},
            detail={
                "button": button, "count": int(count), "events_sent": sent,
                "requested": {"x": point.x, "y": point.y},
                "window_key": point.window_key,
                "cursor_confirmed": bool(within),
                "click_confirmed": False,
                "effect": "the click is not independently observable from outside "
                          "the target application",
            },
        )

    # ------------------------------------------------------------- keyboard

    def tap_key(self, key: str, *, point: ResolvedPoint,
                cancelled=None) -> InputDispatchResult:
        """Press and release one key in a verified target.

        Explicit names only - no virtual-key code is accepted from a caller.

        ``point`` is required even though no coordinate is involved. Keystrokes go
        to whichever window holds foreground, so without naming a target there is
        no way to notice that foreground belongs to a different application than
        the one the caller meant. The point is used purely to prove *where* the
        keystroke is going.
        """
        self._guard()
        self._windows._check_cancelled(cancelled)
        name, vk = Keys.normalise(key)
        self._require_dispatch_target(point, cancelled=cancelled)
        flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0
        sent = _send_input([_key_event(vk, 0, flags),
                            _key_event(vk, 0, flags | _KEYEVENTF_KEYUP)])
        self._windows._check_cancelled(cancelled)
        return InputDispatchResult(
            dispatched=sent > 0, confirmed=False,
            detail={"key": name, "events_sent": sent,
                    "window_key": point.window_key,
                    "effect": "not independently observable from outside the "
                              "target application"},
        )

    def press_key(self, key: str, *, point: ResolvedPoint,
                  cancelled=None) -> InputDispatchResult:
        """Hold one key down in a verified target. The caller must release it."""
        self._guard()
        self._windows._check_cancelled(cancelled)
        name, vk = Keys.normalise(key)
        self._require_dispatch_target(point, cancelled=cancelled)
        flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0
        sent = _send_input([_key_event(vk, 0, flags)])
        self._windows._check_cancelled(cancelled)
        return InputDispatchResult(dispatched=sent > 0, confirmed=False,
                                   detail={"key": name, "held": True,
                                           "events_sent": sent,
                                           "window_key": point.window_key})

    def release_key(self, key: str, *, cancelled=None) -> InputDispatchResult:
        """Release a key this layer pressed.

        Deliberately the one verb with **no** target check and **no** cancellation
        check. Releasing is how a caller frees a key they are holding, and refusing
        to release because the target changed, or because the request was
        cancelled, would strand it down for the rest of the session. Both failures
        are exactly when release matters most: cleanup has to keep working when
        everything else has stopped working.
        """
        self._guard()
        name, vk = Keys.normalise(key)
        flags = (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0) \
            | _KEYEVENTF_KEYUP
        sent = _send_input([_key_event(vk, 0, flags)])
        return InputDispatchResult(dispatched=sent > 0, confirmed=False,
                                   detail={"key": name, "held": False,
                                           "events_sent": sent,
                                           "target_checked": False})

    def hotkey(self, keys: list[str], *, point: ResolvedPoint,
               cancelled=None) -> InputDispatchResult:
        """Dispatch a key combination in a verified target, releasing what it pressed.

        Modifiers go down first, the final key last, and every key pressed is
        released in reverse in a ``finally``. A modifier left held would corrupt
        every subsequent input on the machine, so cleanup runs even when a native
        call fails partway.
        """
        self._guard()
        if not isinstance(keys, (list, tuple)) or not keys:
            raise WindowsInvalidRequestError(
                "a hotkey needs a non-empty list of key names.")
        if len(keys) > 8:
            raise WindowsInvalidRequestError(
                "a hotkey may not exceed 8 keys; longer sequences are ambiguous.")

        normalised = [Keys.normalise(k) for k in keys]
        modifiers = [k for k in normalised if Keys.is_modifier(k[0])]
        main = [k for k in normalised if not Keys.is_modifier(k[0])]
        if len(main) != 1:
            raise WindowsInvalidRequestError(
                "a hotkey needs exactly one non-modifier key, with zero or more "
                "modifiers before it.",
                diagnostics={"keys": [k[0] for k in normalised]})

        self._windows._check_cancelled(cancelled)
        self._require_dispatch_target(point, cancelled=cancelled)
        events: list[_INPUT] = []
        for _name, vk in modifiers:
            events.append(_key_event(vk, 0, _KEYEVENTF_EXTENDEDKEY
                                     if vk in _EXTENDED_KEYS else 0))
        _name, vk = main[0]
        flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0
        events.append(_key_event(vk, 0, flags))

        pressed = [vk for _n, vk in modifiers]
        sent = 0
        try:
            sent = _send_input(events)
        finally:
            # Unconditional. A partial or failed dispatch must not leave a
            # modifier logically held for the rest of the session. This runs
            # without a target check on purpose - see ``release_key``.
            self._release_all(pressed + [vk])
        self._windows._check_cancelled(cancelled)
        return InputDispatchResult(
            dispatched=sent > 0, confirmed=False,
            detail={"keys": [k[0] for k in normalised], "events_sent": sent,
                    "modifiers_released": True,
                    "window_key": point.window_key,
                    "effect": "not independently observable from outside the "
                              "target application"},
        )

    def write_text(self, text: str, *, point: ResolvedPoint,
                   cancelled=None) -> InputDispatchResult:
        """Type text into a verified target through the Unicode scan-code path.

        Uses ``KEYEVENTF_UNICODE``, so the result does not depend on the active
        keyboard layout. There is deliberately no clipboard fallback: dropping a
        string into the clipboard to type it would overwrite something the user
        had copied, which is a side effect no one asked for.

        Characters Windows cannot represent - surrogates, control codes - are
        reported rather than silently substituted.
        """
        self._guard()
        if not isinstance(text, str):
            raise WindowsInvalidRequestError(
                f"text must be a string, not {type(text).__name__}.")
        if not text:
            raise WindowsInvalidRequestError("text must not be empty.")
        if len(text) > 4096:
            raise WindowsInvalidRequestError(
                "text is limited to 4096 characters per call; send longer text in "
                "several calls so progress and cancellation stay observable.")

        unsupported = [c for c in text if not _representable(c)]
        if unsupported:
            return InputDispatchResult(
                dispatched=False, confirmed=False,
                detail={
                    "unsupported_characters": unsupported[:16],
                    "reason": "these characters cannot be represented through the "
                              "Unicode scan-code path and were not substituted",
                },
            )

        self._windows._check_cancelled(cancelled)
        self._require_dispatch_target(point, cancelled=cancelled)
        events: list[_INPUT] = []
        for char in text:
            events.append(_unicode_event(char, False))
            events.append(_unicode_event(char, True))
        sent = _send_input(events)
        self._windows._check_cancelled(cancelled)
        return InputDispatchResult(
            dispatched=sent > 0, confirmed=False,
            detail={"characters": len(text), "events_sent": sent,
                    "unicode_path": True,
                    "window_key": point.window_key,
                    "effect": "not independently observable from outside the "
                              "target application"},
        )

    # ------------------------------------------------------------- internals

    def _release_all(self, virtual_keys: list[int]) -> None:
        """Best-effort release of every key in a sequence."""
        for vk in reversed(virtual_keys):
            flags = (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0) \
                | _KEYEVENTF_KEYUP
            try:
                _send_input([_key_event(vk, 0, flags)])
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                log.warning("could not release virtual key 0x%X: %s", vk, exc)

    def _absolute(self, point: ResolvedPoint) -> tuple[int, int]:
        """Virtual-desktop absolute coordinates in the 0..65535 space.

        ``SendInput`` takes absolute coordinates normalised to this range over the
        *virtual* desktop, not the primary monitor.
        """
        screen = self.virtual_screen()
        span_x = max(1, screen.width - 1)
        span_y = max(1, screen.height - 1)
        x = int(round((point.x - screen.left) * 65535 / span_x))
        y = int(round((point.y - screen.top) * 65535 / span_y))
        return max(0, min(65535, x)), max(0, min(65535, y))

    def _wait_for_cursor(self, x: int, y: int, *, cancelled=None) -> dict | None:
        """Poll the pointer until it arrives, or the budget runs out."""
        deadline = time.monotonic() + _CURSOR_SETTLE_TIMEOUT
        observed: dict | None = None
        while True:
            win32api, _con = _load_input_apis()
            cx, cy = win32api.GetCursorPos()
            observed = {"x": int(cx), "y": int(cy)}
            if (abs(cx - x) <= CURSOR_TOLERANCE
                    and abs(cy - y) <= CURSOR_TOLERANCE):
                return observed
            if time.monotonic() >= deadline:
                return observed
            self._windows._check_cancelled(cancelled)
            time.sleep(0.02)


def _representable(char: str) -> bool:
    """Whether a character can be sent through the Unicode scan-code path.

    Surrogates are the notable case: ``ord()`` produces a value outside the
    Unicode scalar range and Windows would either drop it or inject nonsense.
    """
    code = ord(char)
    if 0xD800 <= code <= 0xDFFF:          # surrogate half
        return False
    if code < 0x20 and char not in ("\t", "\n", "\r"):
        return False
    return True


def _mouse_effect(dx: int, dy: int, flags: int) -> _INPUT:
    return _mouse_event(dx, dy, flags)