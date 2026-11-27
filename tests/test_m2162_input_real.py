"""M2.16.2 / M2.16.3 real-Windows validation of pointer and keyboard input.

Real ``SendInput``, real Win32, real Tk. The doubles prove the logic; only these
prove that the injected events actually land in a window and that the safety
checks fire against a live desktop.

**What these tests will and will not do.**

* Every target is a window this module creates: a Tk window with a single
  ``Entry`` field, titled uniquely, matched through the M2.16.1 identity model.
* No input is ever sent to a user application. The previous foreground window is
  captured and restored in a ``finally``.
* Typed text is asserted by reading the ``Entry``'s own contents back through Tk -
  which is the only way to observe a keystroke's effect from outside, and exactly
  why a click or a key cannot be independently verified.
* The mouse is moved back to where it started, so running this file does not
  leave the user's pointer somewhere unexpected.

Race tests destroy or re-point the target *after* it was resolved, which is the
invariant that matters: input must never silently land on a different window.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.windows.errors import (
    WindowsInvalidRequestError,
    WindowsWindowGoneError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from core.windows.windows import WindowLayer, windows_available


REAL_WINDOWS = os.environ.get("DIABLO_REAL_WINDOWS_TESTS") == "1"
requires_real_windows = pytest.mark.skipif(
    not REAL_WINDOWS,
    reason="set DIABLO_REAL_WINDOWS_TESTS=1 to run real Windows integration tests",
)
requires_input = pytest.mark.skipif(
    not REAL_WINDOWS or not windows_available(),
    reason="native window and input APIs are unavailable on this machine",
)

WINDOW_TITLE = "DIABLO_M216_INPUT_PROBE"

#: The Tk child writes what it actually received here, so a keystroke's effect
#: can be observed from outside the target application. Per-instance, because a
#: test that spawns a second owned window needs the two not to overwrite each
#: other's reported state.
_STATE_DIR = Path(os.environ.get("TEMP", "."))

#: Windows holds the pointer in whole pixels and warps it in fractional steps,
#: so a confirmed move is compared against the layer's own tolerance rather than
#: demanding exact equality.
CURSOR_TOLERANCE = 2

#: Every window this module has selected, so a new test waits for a fresh one
#: rather than reusing a predecessor's while it winds down.
_SEEN_HWNDS: set[int] = set()

# Plain string, not an f-string: the script has its own braces and format
# placeholders, and interpolating it here would evaluate those against this
# module instead. The two values it needs are substituted explicitly.
_APP_SCRIPT = r'''
import json, os, tkinter as tk

STATE_PATH = "__STATE__"

def dump():
    try:
        root.update_idletasks()
        payload = {
            "text": entry.get(),
            "clicks": app.clicks,
            "button": app.last_button,
            "alive": True,
            # Reported so tests aim at the real widget instead of guessing
            # where a title bar and frame padding put it.
            "entry_rect": [
                entry.winfo_rootx(), entry.winfo_rooty(),
                entry.winfo_width(), entry.winfo_height(),
            ],
            "entry_focused": bool(root.focus_get() is entry),
        }
        # Written to a temporary file and moved into place. Truncating the real
        # file in place would let a reader catch it mid-write, and a state file
        # that intermittently reads back empty looks exactly like a flaky test.
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass

root = tk.Tk()
root.title("__TITLE__")
root.geometry("420x320+140+140")
root.resizable(False, False)

app = tk.Frame(root)
app.pack(fill="both", expand=True)
app.clicks = 0
app.last_button = ""

entry = tk.Entry(app, font=("Segoe UI", 14), width=30)
entry.pack(pady=18, padx=18, fill="x")

# A button with no side effect, so a click lands somewhere real without
# changing what the test asserts.
tk.Button(app, text="probe").pack(pady=6)

def on_click(which):
    app.clicks += 1
    app.last_button = which
    dump()

# Bound on the toplevel only, and deliberately not returning "break".
#
# Binding on the entry as well would run this once per ancestor in the bind chain
# for a single physical click, inflating the counter. Returning "break" would stop
# the duplicate - but it also stops the Entry's *default class binding*, which is
# what gives the field keyboard focus on click. One binding, no "break", and the
# click both counts once and focuses, which is what a real click does.
for _name, _number in (("left", 1), ("right", 3)):
    root.bind("<Button-%d>" % _number, lambda _e, n=_name: on_click(n))

def poll():
    # Dumped every UI turn so a test can read whatever has arrived.
    dump()
    root.after(120, poll)

def raise_self():
    # Windows refuses SetForegroundWindow from a process that does not own the
    # foreground, so the test harness cannot reliably activate this window. An
    # application raising *itself* is legitimate and is what a real one does, so
    # the probe does it from inside its own process rather than the harness
    # working around the foreground lock.
    try:
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(400, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    dump()

def save_and_quit(_event=None):
    dump()
    root.destroy()

root.bind("<Escape>", save_and_quit)
root.update()
root.after(150, raise_self)
root.after(100, poll)
root.after(120000, save_and_quit)
root.mainloop()
'''


def _script_for(title: str, state: Path) -> str:
    # Forward slashes in the substituted path: a Windows path with backslashes is
    # an invalid string literal once it lands inside the script (``\\Users`` reads
    # as a unicode escape). Windows accepts either separator.
    return (_APP_SCRIPT
            .replace("__TITLE__", title)
            .replace("__STATE__", str(state).replace("\\", "/")))


@pytest.fixture(scope="module")
def layer():
    native = WindowLayer()
    try:
        yield native
    finally:
        native.close()


@pytest.fixture(scope="module")
def input_layer(layer):
    from core.windows.input import InputLayer

    native = InputLayer(layer)
    try:
        yield native
    finally:
        native.close()


class _ProbeApp:
    """A running probe window and the child process that owns it.

    Held as one object so a test can never address a window without also owning
    it. ``state_path`` travels with the instance because two probes would
    otherwise report into the same file.
    """

    def __init__(self, child, info, state_path: Path) -> None:
        self.child = child
        self.info = info
        self.state_path = state_path

    @property
    def identity(self):
        return self.info.identity

    @property
    def rect(self):
        return self.info.rect

    def read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def wait_state(self, key: str, timeout: float = 8.0) -> dict:
        """Wait for the target to report ``key``, returning whatever it reported."""
        return self.wait_for(lambda state: state.get(key) not in (None, "", 0),
                             timeout)

    def wait_for(self, predicate, timeout: float = 8.0) -> dict:
        """Wait until the target's reported state satisfies ``predicate``.

        Waiting for the condition being *asserted* matters here. The child dumps
        every UI turn, so a request that waits only for "some text appeared" can
        return while the last characters are still being typed, and then assert
        against a half-typed string.
        """
        deadline = time.monotonic() + timeout
        state = self.read_state()
        while time.monotonic() < deadline:
            state = self.read_state()
            try:
                if predicate(state):
                    return state
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)
        return state

    def stop(self) -> None:
        """Terminate the window, and reap anything that re-executed.

        A venv ``python.exe`` re-executes, so the window's owning process can be a
        grandchild of the handle returned by ``Popen``. Terminating only the
        direct child would leave that grandchild - and its window - alive, so the
        owning pid is terminated explicitly. Both are processes this test started.

        The window is then waited out. A probe that outlives its test would sit at
        the same coordinates as the next test's probe and swallow its clicks, which
        shows up as an intermittent failure in a *later* test rather than here.
        """
        hwnd = int(self.info.hwnd)
        for pid in {int(self.info.pid) if self.info.pid else 0, self.child.pid}:
            if not pid or pid == os.getpid():
                continue
            _terminate_pid(pid)
        if self.child.poll() is None:
            try:
                self.child.terminate()
                self.child.wait(timeout=8)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.child.kill()
                self.child.wait(timeout=8)
        _wait_until(lambda: not _window_exists(hwnd), 8.0)


def _terminate_pid(pid: int) -> None:
    """Terminate a process this test started, tolerating one already gone."""
    import pywintypes
    import win32api
    import win32con

    try:
        handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
    except (pywintypes.error, OSError):
        return
    try:
        win32api.TerminateProcess(handle, 0)
    except (pywintypes.error, OSError):
        pass
    finally:
        handle.Close()


def _window_exists(hwnd: int) -> bool:
    from core.windows.windows import _load_gui

    win32gui, _process, _con = _load_gui()
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _find_probe_window(title: str, timeout: float = 25.0):
    """The probe window titled ``title``, matched through the identity model.

    The owner pid is read from the window rather than assumed to be the spawned
    child, because a venv ``python.exe`` re-executes and the real window may
    belong to a grandchild.
    """
    own_pid = os.getpid()
    probe = WindowLayer()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for info in probe.enumerate_windows(title_contains=title):
                if info.identity.anchored and info.pid != own_pid \
                        and info.hwnd not in _SEEN_HWNDS:
                    _SEEN_HWNDS.add(info.hwnd)
                    return info
            time.sleep(0.2)
    finally:
        probe.close()
    return None


def _start_app(title: str = WINDOW_TITLE) -> "_ProbeApp":
    state = _STATE_DIR / f"diablo_m216_input_{abs(hash(title))}.json"
    try:
        state.unlink()
    except OSError:
        pass
    try:
        child = subprocess.Popen([sys.executable, "-c", _script_for(title, state)])
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"could not start the probe app: {exc}")
    info = _find_probe_window(title)
    if info is None:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=8)
        pytest.skip(f"the probe window {title!r} did not appear on this machine")
    return _ProbeApp(child, info, state)


def _activate_for_test(layer, identity) -> bool:
    """Make the test-owned window the foreground one, and report whether it worked.

    Windows refuses ``SetForegroundWindow`` from a process that does not own the
    foreground, which a test harness never does. The documented way to arrange
    for a window to be activated in that situation is ``AttachThreadInput``:
    attach this thread's input queue to the foreground thread's, activate, detach.

    **This belongs to the harness and not to the product.** ``layer.focus_window``
    deliberately still honours Windows' refusal - that is M2.16.1 behaviour and
    there are tests asserting it. The product does not reach for
    ``AttachThreadInput`` anywhere; a test arranging for its *own* window to be on
    top is establishing a precondition, not bypassing a restriction so that
    input can be sent somewhere the user did not ask for.
    """
    import ctypes
    import win32gui
    import win32process

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = int(identity.hwnd)
    our_thread = int(kernel32.GetCurrentThreadId())
    current = int(win32gui.GetForegroundWindow() or 0)
    if not current:
        user32.SetForegroundWindow(hwnd)
        return int(win32gui.GetForegroundWindow() or 0) == hwnd
    fg_thread = int(win32process.GetWindowThreadProcessId(current)[0])
    attached = bool(user32.AttachThreadInput(our_thread, fg_thread, True))
    try:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)
    return int(win32gui.GetForegroundWindow() or 0) == hwnd


@contextlib.contextmanager
def _owned_window(layer, title: str = WINDOW_TITLE):
    """Start an owned probe, foreground it, and guarantee it is torn down.

    The previous foreground window and cursor position are captured first and
    restored in a ``finally``, so a failing test still hands the desktop back.
    """
    app = _start_app(title)
    foreground_before = _foreground_key()
    cursor_before = _cursor_pos()
    try:
        if not _activate_for_test(layer, app.identity):
            # Fall back to the product's own path first, so the refusal the
            # product reports is still observed before the harness intervenes.
            try:
                layer.focus_window(app.identity)
            except WindowsInvalidRequestError:
                pass
        if not _wait_until(lambda: _foreground_key() == app.identity.key, 8.0):
            if not _activate_for_test(layer, app.identity):
                pytest.skip(
                    "Windows would not make the test window the foreground "
                    "window on this session, so input cannot be tested here")
        _wait_until(lambda: app.read_state().get("alive") is True, 8.0)
        yield app
    finally:
        app.stop()
        _restore(layer, foreground_before, cursor_before)


@pytest.fixture
def probe(layer):
    """A test-owned window with an input field."""
    with _owned_window(layer) as app:
        yield app


def _entry_point(app: "_ProbeApp", input_layer, *,
                 require_foreground: bool = True):
    """A resolved point on the ``Entry``, aimed using the rect the app reported.

    Taken from the child's own geometry rather than a hard-coded offset. A title
    bar plus frame padding puts the field well below ``(30, 30)``, and a guess
    that lands on the frame instead of the field produces a test that silently
    never focuses anything.
    """
    state = app.read_state()
    rect = state.get("entry_rect")
    if not rect:
        pytest.skip("the probe did not report its entry geometry")
    left, top, width, height = (int(v) for v in rect)
    window_left, window_top = int(app.info.rect[0]), int(app.info.rect[1])
    return input_layer.resolve(
        {"mode": "window",
         "window": app.identity.to_dict(),
         "relative_x": (left + width // 2) - window_left,
         "relative_y": (top + height // 2) - window_top},
        require_foreground=require_foreground,
    )


def _focus_entry(app: "_ProbeApp", input_layer):
    """Click the field so it holds keyboard focus, and wait until it does."""
    point = _entry_point(app, input_layer)
    input_layer.move_and_click(point, button="left", count=1)
    assert _wait_until(
        lambda: app.read_state().get("entry_focused") is True, 8.0), (
        f"the entry never took keyboard focus: {app.read_state()!r}")
    return point


def _foreground_key() -> str:
    import win32gui

    from core.windows.windows import _load_gui

    win32gui, _process, _con = _load_gui()
    hwnd = int(win32gui.GetForegroundWindow() or 0)
    if not hwnd:
        return ""
    probe = WindowLayer()
    try:
        info = probe._read(hwnd)
        return info.identity.key if info else ""
    finally:
        probe.close()


def _cursor_pos() -> tuple[int, int]:
    import win32api

    return tuple(win32api.GetCursorPos())


def _restore(layer, foreground_key: str, cursor) -> None:
    """Put the desktop back the way it was found."""
    try:
        import win32api
        win32api.SetCursorPos(int(cursor[0]), int(cursor[1]))
    except Exception:  # noqa: BLE001 - restoration is best effort
        pass
    if not foreground_key:
        return
    try:
        for info in layer.enumerate_windows():
            if info.identity.key == foreground_key:
                layer.focus_window(info.identity)
                return
    except Exception:  # noqa: BLE001
        pass


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


    def wait_state(self, key: str, timeout: float = 8.0) -> dict:
        """Wait for the target to report ``key``, returning whatever it reported.

        Polls rather than sleeping a fixed interval: the child dumps after every
        UI turn, so this returns as soon as the effect is observable instead of
        always paying the full timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.read_state()
            if state.get(key) not in (None, "", 0):
                return state
            time.sleep(0.05)
        return self.read_state()


# ══════════════════════════════════════════════════════ reads


@requires_input
class TestRealPointerAndForegroundReads:
    def test_cursor_position_is_readable(self, input_layer):
        payload = input_layer.get_cursor_pos()
        assert isinstance(payload["x"], int)
        assert isinstance(payload["y"], int)

    def test_the_virtual_screen_is_real(self, input_layer):
        """Not fabricated, and not the primary monitor by assumption."""
        screen = input_layer.virtual_screen()
        assert screen.width > 0 and screen.height > 0
        assert screen.contains(screen.left, screen.top)

    def test_the_foreground_window_carries_a_logical_identity(self, input_layer):
        payload = input_layer.get_foreground_window()
        assert payload is not None, "a desktop session should have a foreground window"
        assert payload["hwnd"] > 0
        assert payload["class_name"], "the identity has no class name"
        assert payload["identity_created_at"], (
            "the foreground identity is not anchored to a process")
        assert payload["window_key"]
        json.dumps(payload)   # no native handle object in the payload

    def test_the_foreground_identity_can_be_used_as_a_target(self, probe,
                                                             input_layer):
        """The property that makes the two halves work together."""
        info = input_layer.get_foreground_window()
        assert info is not None
        assert info["window_key"] == probe.identity.key
        confirmed = input_layer.confirm_target(info)
        assert confirmed.identity.matches(probe.identity)


# ══════════════════════════════════════════════════════ mouse


@requires_input
class TestRealMouse:
    def test_the_pointer_moves_and_the_move_is_observed(self, probe, input_layer):
        """The one input whose effect is globally observable, so it is the one
        that can legitimately be verified."""
        rect = probe.rect
        target = {"mode": "window",
                  "window": probe.identity.to_dict(),
                  "relative_x": (rect[2] - rect[0]) // 2,
                  "relative_y": (rect[3] - rect[1]) // 2}
        point = input_layer.resolve(target, require_foreground=True)
        result = input_layer.move_cursor(point)
        assert result.dispatched is True, (
            f"the pointer move was not injected: {result.to_dict()}")
        observed = result.observed
        assert abs(observed["x"] - point.x) <= CURSOR_TOLERANCE
        assert abs(observed["y"] - point.y) <= CURSOR_TOLERANCE, (
            f"the pointer did not arrive: asked for ({point.x}, {point.y}), "
            f"observed ({observed['x']}, {observed['y']})")

    def test_a_click_reaches_the_target_window(self, probe, input_layer):
        """Read back out of the target, which is the only way to observe it."""
        rect = probe.rect
        target = {"mode": "window", "window": probe.identity.to_dict(),
                  "relative_x": (rect[2] - rect[0]) // 2,
                  "relative_y": 60}
        point = input_layer.resolve(target, require_foreground=True)
        result = input_layer.move_and_click(point, button="left", count=1)
        assert result.dispatched is True
        state = probe.wait_state("clicks")
        assert state.get("clicks", 0) >= 1, (
            f"the click never reached the target window: {state}")

    def test_a_left_click_reports_the_button(self, probe, input_layer):
        rect = probe.rect
        point = input_layer.resolve({"mode": "window",
                                     "window": probe.identity.to_dict(),
                                     "relative_x": 20, "relative_y": 60},
                                    require_foreground=True)
        input_layer.move_and_click(point, button="left", count=1)
        state = probe.wait_state("button")
        assert state.get("button") == "left", (
            f"the target recorded button={state.get('button')!r}")

    def test_a_right_click_reports_the_right_button(self, probe, input_layer):
        rect = probe.rect
        point = input_layer.resolve({"mode": "window",
                                     "window": probe.identity.to_dict(),
                                     "relative_x": 20, "relative_y": 60},
                                    require_foreground=True)
        input_layer.move_and_click(point, button="right", count=1)
        state = probe.wait_state("button")
        assert state.get("button") == "right", (
            f"the target recorded button={state.get('button')!r}; a left click "
            f"was recorded instead")

    def test_a_double_click_is_delivered_as_two(self, probe, input_layer):
        rect = probe.rect
        point = input_layer.resolve({"mode": "window",
                                     "window": probe.identity.to_dict(),
                                     "relative_x": 20, "relative_y": 60},
                                    require_foreground=True)
        before = probe.read_state().get("clicks", 0)
        input_layer.move_and_click(point, button="left", count=2)
        assert _wait_until(
            lambda: probe.read_state().get("clicks", 0) >= before + 2, 8.0), (
            f"a double click did not deliver two presses: {probe.read_state()}")

    def test_coordinates_outside_the_window_are_refused(self, probe, input_layer):
        with pytest.raises(WindowsInvalidRequestError, match="outside"):
            input_layer.resolve({"mode": "window",
                                 "window": probe.identity.to_dict(),
                                 "relative_x": 99999, "relative_y": 10},
                                require_foreground=True)

    def test_a_screen_point_outside_the_desktop_is_refused(self, input_layer):
        with pytest.raises(WindowsInvalidRequestError, match="outside"):
            input_layer.resolve({"mode": "screen", "x": -5000, "y": -5000},
                                require_foreground=False)

    def test_a_minimized_window_is_not_clicked(self, probe, input_layer,
                                                layer):
        """Its rectangle would resolve to somewhere on the desktop that is not
        the target."""
        layer.minimize_window(probe.identity)
        try:
            with pytest.raises(WindowsInvalidRequestError, match="minimized"):
                input_layer.resolve({"mode": "window",
                                     "window": probe.identity.to_dict(),
                                     "relative_x": 10, "relative_y": 10},
                                    require_foreground=False)
        finally:
            layer.restore_window(probe.identity)
            _wait_until(lambda: _foreground_key() == probe.identity.key, 8.0)


# ══════════════════════════════════════════════════════ keyboard


@requires_input
class TestRealKeyboard:
    def test_text_lands_in_the_target_field(self, probe, input_layer):
        """Observed by reading the ``Entry`` back - the only honest evidence."""
        point = _focus_entry(probe, input_layer)
        result = input_layer.write_text("diablo216", point=point)
        assert result.dispatched is True
        state = probe.wait_for(lambda s: s.get("text") == "diablo216")
        assert state.get("text") == "diablo216", (
            f"typed text did not reach the target field: {state!r}")

    def test_unicode_text_lands_intact(self, probe, input_layer):
        """Through ``KEYEVENTF_UNICODE``, so the active layout cannot matter."""
        point = _focus_entry(probe, input_layer)
        result = input_layer.write_text("café — ünï", point=point)
        assert result.dispatched is True
        state = probe.wait_for(lambda s: s.get("text") == "café — ünï")
        assert state.get("text") == "café — ünï", (
            f"Unicode text did not arrive intact: {state!r}")

    def test_unrepresentable_text_is_reported_not_substituted(self, probe,
                                                               input_layer):
        point = _entry_point(probe, input_layer)
        result = input_layer.write_text("ok\ud800bad", point=point)
        assert result.dispatched is False
        assert result.detail.get("unsupported_characters"), (
            "an unrepresentable character was silently substituted")

    def test_a_single_key_tap_is_delivered(self, probe, input_layer):
        point = _focus_entry(probe, input_layer)
        result = input_layer.tap_key("tab", point=point)
        assert result.dispatched is True

    def test_a_hotkey_reaches_the_target_and_releases_its_modifiers(self, probe,
                                                                    input_layer):
        """Ctrl+A selects the field; the text then goes to the end, which is only
        possible if Ctrl was pressed *and released*."""
        point = _focus_entry(probe, input_layer)
        input_layer.write_text("abcdef", point=point)
        assert probe.wait_for(lambda s: s.get("text") == "abcdef").get(
            "text") == "abcdef", "the seed text never landed"

        result = input_layer.hotkey(["ctrl", "a"], point=point)
        assert result.dispatched is True
        assert result.detail.get("modifiers_released") is True
        input_layer.write_text("X", point=point)
        state = probe.wait_for(
            lambda s: s.get("text") == "X")
        assert state.get("text") == "X", (
            f"Ctrl+A did not select the field: {state!r}")

    def test_shift_combination_is_dispatched(self, probe, input_layer):
        point = _focus_entry(probe, input_layer)
        result = input_layer.hotkey(["shift", "end"], point=point)
        assert result.dispatched is True

    def test_a_press_and_release_pair_does_not_leave_the_key_down(self, probe,
                                                                   input_layer):
        point = _focus_entry(probe, input_layer)
        input_layer.press_key("shift", point=point)
        input_layer.release_key("shift")
        # If Shift were still held, the next text would come out upper-cased.
        input_layer.write_text("plain", point=point)
        assert probe.wait_for(lambda s: s.get("text") == "plain").get(
            "text") == "plain", (
            f"a key was left held, so later text was modified: "
            f"{probe.read_state()!r}")

    def test_a_malformed_key_is_refused_before_dispatch(self, probe, input_layer):
        point = _entry_point(probe, input_layer)
        for bad in (5, None, "", "hyperkey", ["enter"]):
            with pytest.raises(WindowsInvalidRequestError):
                input_layer.tap_key(bad, point=point)

    def test_keyboard_input_is_refused_without_a_named_target(self, probe,
                                                               input_layer):
        """A keystroke with no target cannot be checked against anything."""
        with pytest.raises(TypeError):
            input_layer.tap_key("a")
        with pytest.raises(WindowsInvalidRequestError, match="target"):
            input_layer.write_text("a", point=None)


# ══════════════════════════════════════════════════════ races


@requires_input
class TestRealRacesNeverRetarget:
    """The invariant: input must never silently reach a different target."""

    def test_input_is_refused_when_the_target_disappeared(self, probe, input_layer,
                                                           layer):
        """The target is resolved, then destroyed before the input is dispatched.

        This is the race that matters: without a re-read, the coordinates would
        have been dispatched into whatever now occupies that part of the screen.
        """
        target = {"mode": "window", "window": probe.identity.to_dict(),
                  "relative_x": 20, "relative_y": 30}
        layer.close_window(probe.identity)
        assert _wait_until(
            lambda: _gone(layer, probe.identity), 8.0), (
            "the probe window did not actually close")

        with pytest.raises((WindowsWindowNotFoundError,
                            WindowsWindowGoneError,
                            WindowsWindowIdentityMismatchError,
                            WindowsInvalidRequestError)):
            point = input_layer.resolve(target, require_foreground=True)
            input_layer.move_and_click(point, button="left", count=1)

    def test_a_recycled_identity_is_refused(self, probe, input_layer):
        """The M2.16.1 refusal, reached through the input path."""
        stale = dict(probe.identity.to_dict())
        stale["identity_created_at"] = "19990101 00:00:00"
        with pytest.raises(WindowsWindowIdentityMismatchError):
            input_layer.resolve({"mode": "window", "window": stale,
                                 "relative_x": 10, "relative_y": 10},
                                require_foreground=True)

    def test_input_is_refused_when_the_target_is_not_foreground(self, probe,
                                                                input_layer,
                                                                layer):
        """Keyboard and click both need the target on top.

        The window that takes the foreground is a *second probe this test owns*.
        Borrowing an unrelated application would mean changing something the user
        is actually using, which is not a price worth paying for a test.
        """
        target = {"mode": "window", "window": probe.identity.to_dict(),
                  "relative_x": 20, "relative_y": 30}
        other = _start_app(f"{WINDOW_TITLE}_OTHER")
        try:
            assert _activate_for_test(layer, other.identity), (
                "the second owned window could not take the foreground")
            assert _wait_until(
                lambda: _foreground_key() == other.identity.key, 8.0), (
                "the second window never took the foreground")

            with pytest.raises(WindowsInvalidRequestError, match="foreground"):
                input_layer.resolve(target, require_foreground=True)

            # And the same refusal through a real dispatch attempt, not just
            # through resolve - the point could have been resolved while the probe
            # *was* foreground and then the desktop changed its mind.
            stale_point = input_layer.resolve(
                target, require_foreground=False)
            with pytest.raises(WindowsInvalidRequestError, match="foreground"):
                input_layer.tap_key("a", point=stale_point)
        finally:
            other.stop()
        _activate_for_test(layer, probe.identity)
        _wait_until(lambda: _foreground_key() == probe.identity.key, 8.0)

    def test_an_unanchored_identity_is_refused(self, probe, input_layer):
        stale = dict(probe.identity.to_dict())
        stale["identity_created_at"] = ""
        with pytest.raises((WindowsWindowIdentityMismatchError,
                            WindowsInvalidRequestError)):
            input_layer.resolve({"mode": "window", "window": stale,
                                 "relative_x": 10, "relative_y": 10},
                                require_foreground=True)

    def test_an_unanchored_point_cannot_reach_dispatch(self, probe, input_layer):
        """A bare screen coordinate names a position, not a target."""
        unanchored = input_layer.resolve({"mode": "screen",
                                          "x": int(probe.rect[0]) + 30,
                                          "y": int(probe.rect[1]) + 60},
                                         require_foreground=False)
        assert unanchored.anchored is False
        with pytest.raises(WindowsInvalidRequestError, match="target"):
            input_layer.move_cursor(unanchored)
        with pytest.raises(WindowsInvalidRequestError, match="target"):
            input_layer.move_and_click(unanchored, button="left", count=1)
        with pytest.raises(WindowsInvalidRequestError, match="target"):
            input_layer.tap_key("a", point=unanchored)
        with pytest.raises(WindowsInvalidRequestError, match="target"):
            input_layer.write_text("a", point=unanchored)
        with pytest.raises(WindowsInvalidRequestError, match="target"):
            input_layer.hotkey(["ctrl", "a"], point=unanchored)

    @pytest.mark.parametrize("verb", ["tap_key", "write_text", "hotkey"])
    def test_preflight_cancellation_dispatches_nothing(self, probe, input_layer,
                                                       verb):
        from core.windows.errors import WindowsCancelledError

        point = _focus_entry(probe, input_layer)
        before = probe.read_state().get("text", "")
        kwargs = {"key": "x"} if verb == "tap_key" else (
            {"text": "SHOULDNOTAPPEAR"} if verb == "write_text"
            else {"keys": ["ctrl", "a"]})
        with pytest.raises(WindowsCancelledError):
            getattr(input_layer, verb)(point=point, cancelled=lambda: True,
                                       **kwargs)
        assert probe.read_state().get("text", "") == before, (
            "input reached the target despite preflight cancellation")

    def test_a_release_still_works_after_cancellation(self, probe, input_layer):
        """Cleanup must not be blocked by the very cancellation that caused it."""
        from core.windows.errors import WindowsCancelledError

        point = _focus_entry(probe, input_layer)
        input_layer.press_key("shift", point=point)
        result = input_layer.release_key("shift", cancelled=lambda: True)
        assert result.dispatched is True

        with pytest.raises(WindowsCancelledError):
            input_layer.tap_key("a", point=point, cancelled=lambda: True)

        input_layer.release_key("shift", cancelled=lambda: True)
        input_layer.write_text("plain", point=point, cancelled=lambda: False)
        assert probe.wait_for(lambda s: s.get("text") == "plain").get(
            "text") == "plain", f"a key was left held: {probe.read_state()!r}"


def _gone(layer, identity) -> bool:
    try:
        layer.get_window(identity)
        return False
    except (WindowsWindowNotFoundError, WindowsWindowGoneError,
            WindowsWindowIdentityMismatchError):
        return True
    except WindowsInvalidRequestError:
        return True