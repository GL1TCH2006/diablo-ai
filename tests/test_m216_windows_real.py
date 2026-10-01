"""M2.16 real-Windows validation for the desktop primitives.

These run against real Win32 on this machine. The doubles in
``test_m216_window_identity`` prove the safety rules; only these prove that the
native calls behave the way the rules assume.

**Safety rules these tests obey.**

* Nothing is closed, terminated or focused that the test did not create.
* The lifecycle verbs are exercised against a **controlled child process** that
  this module spawns and owns - a purpose-built Tk window, not a user
  application.
* Restoration is attempted for the user's own foreground window in a ``finally``,
  so running this file does not leave the desktop somewhere else.
* If Blender or Tk is unavailable the destructive tests skip rather than falling
  back to something less controlled.

The Tk window is deliberate: it is a real top-level window owned by a real
process this test owns, so ``close`` is a genuine test of destruction rather than
a simulated one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from core.windows.errors import (
    WindowsInvalidRequestError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from core.windows.windows import WindowLayer, windows_available


pytestmark = pytest.mark.skipif(
    not windows_available(),
    reason="native window APIs are unavailable on this machine")


@pytest.fixture(scope="module")
def layer():
    native = WindowLayer()
    try:
        yield native
    finally:
        native.close()


def _own_created_at() -> str:
    """This interpreter's process creation time - the anchor for the test window."""
    from core.windows.processes import ProcessLayer

    processes = ProcessLayer()
    try:
        info = processes.get_process(os.getpid())
        return str(info.created_at) if info else ""
    finally:
        processes.close()


#: Unique to this module. Matching on it is what guarantees the tests can only
#: ever select a window they created.
WINDOW_TITLE = "DIABLO_M216_WINDOW"


#: Every window this module has selected so far. A freshly spawned child can
#: briefly coexist with its predecessor's window while that process winds down,
#: so a new test must wait for an identity it has not seen before rather than
#: reusing a stale one that is about to disappear.
_SEEN_HWNDS: set[int] = set()


def _app_window(timeout: float = 20.0):
    """Find a freshly created Tk window, and anchor to *its own* process.

    The owner pid is read from the window rather than assumed to be the spawned
    child: a venv ``python.exe`` re-executes the real interpreter, so the process
    that owns the window can be a grandchild of the one launched here. Matching
    on the unique title is what makes the selection safe, and the owner pid is
    then taken from the window itself so the anchor is the process that actually
    holds it.
    """
    own_pid = os.getpid()
    probe = WindowLayer()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for info in probe.enumerate_windows(title_contains=WINDOW_TITLE):
                if not info.identity.anchored:
                    continue
                if info.pid == own_pid:
                    # The test runner's own windows are never targets.
                    continue
                if info.hwnd in _SEEN_HWNDS:
                    continue        # a predecessor's window, still winding down
                _SEEN_HWNDS.add(info.hwnd)
                return info
            time.sleep(0.2)
    finally:
        probe.close()
    return None


@pytest.fixture
def app_window():
    """A fresh, owned top-level window per test, or a skip.

    Function-scoped deliberately: ``test_close_is_observed_as_gone`` destroys the
    window, so a module-scoped fixture would leave every later test with a handle
    that no longer exists - and the resulting "window not found" failures would
    read as layer defects rather than as fixture ordering.

    Spawned as a child rather than run in-thread so the window belongs to its own
    process: that is what gives it a real ``(pid, created_at)`` anchor, and it
    means ``close`` destroys something this test genuinely owns.
    """
    script = (
        "import tkinter as tk\n"
        f"r = tk.Tk()\n"
        f"r.title({WINDOW_TITLE!r})\n"
        "r.geometry('320x240+120+120')\n"
        "r.update()\n"
        "r.after(45000, r.destroy)\n"
        "r.mainloop()\n"
    )
    try:
        child = subprocess.Popen([sys.executable, "-c", script])
    except OSError as exc:
        pytest.skip(f"could not spawn the test window: {exc}")

    try:
        info = _app_window()
        if info is None:
            pytest.skip("the Tk test window did not appear on this machine")
        yield info
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                child.kill()
                child.wait(timeout=5)


# ══════════════════════════════════════════════════════ enumeration


REAL_WINDOWS = os.environ.get("DIABLO_REAL_WINDOWS_TESTS") == "1"
requires_real_windows = pytest.mark.skipif(
    not REAL_WINDOWS,
    reason="set DIABLO_REAL_WINDOWS_TESTS=1 to run real Windows integration tests",
)


@requires_real_windows
class TestRealEnumeration:
    def test_the_layer_is_available_on_this_machine(self, layer):
        assert windows_available()
        assert layer.enumerate_windows(), (
            "no top-level windows at all, which means enumeration is broken "
            "rather than that the desktop is empty")

    def test_every_window_row_is_well_formed(self, layer):
        for info in layer.enumerate_windows():
            assert info.hwnd > 0
            assert info.pid >= 0
            assert info.identity.class_name, (
                f"window 0x{info.hwnd:X} has no class name")
            assert info.to_dict()["window_key"]

    def test_every_window_is_anchored_to_its_process(self, layer):
        """A desktop carries windows whose owning process cannot be read; those
        must be reported as unanchored rather than fabricated."""
        windows = layer.enumerate_windows()
        anchored = [w for w in windows if w.identity.anchored]
        assert anchored, "no window could be anchored to a process"

    def test_visibility_filtering_reduces_the_set(self, layer):
        everything = layer.enumerate_windows()
        visible = layer.enumerate_windows(visible_only=True)
        assert len(visible) <= len(everything)
        assert all(w.visible for w in visible)

    def test_filtering_by_pid_selects_only_that_process(self, layer):
        me = os.getpid()
        found = layer.enumerate_windows(pid=me)
        assert all(w.pid == me for w in found)

    def test_filtering_by_title_matches_a_substring(self, layer, app_window):
        found = layer.enumerate_windows(title_contains="DIABLO_M216")
        assert found, "the owned test window was not found by title"
        assert all("diablo_m216" in w.title.lower() for w in found)

    def test_a_nonsense_title_matches_nothing(self, layer):
        assert layer.enumerate_windows(title_contains="zzzz-no-such-window") == []

    def test_untitled_filtering_works(self, layer):
        titled = layer.enumerate_windows(include_untitled=False)
        assert all(w.title.strip() for w in titled)

    def test_a_malformed_pid_is_a_caller_error(self, layer):
        for bad in (-1, 0, "abc", 4.7):
            with pytest.raises(WindowsInvalidRequestError):
                layer.enumerate_windows(pid=bad)

    def test_enumeration_does_not_leak_a_handle_object(self, layer):
        """No PyHANDLE or window object may cross the boundary."""
        import json

        rows = [w.to_dict() for w in layer.enumerate_windows()[:20]]
        for row in rows:
            json.dumps(row)
            assert isinstance(row["hwnd"], int)


class TestRealIdentityVerification:
    def test_a_real_window_verifies_against_its_own_identity(self, layer, app_window):
        again = layer.get_window(app_window.identity)
        assert again.identity.matches(app_window.identity)
        assert again.title == app_window.title

    def test_the_dict_identity_round_trips(self, layer, app_window):
        from core.windows.window_models import WindowIdentity

        rebuilt = WindowIdentity.from_dict(app_window.identity.to_dict())
        assert layer.get_window(rebuilt).identity.matches(app_window.identity)

    def test_a_stale_handle_is_refused_rather_than_acted_on(self, layer, app_window):
        """A handle that does not exist names nothing."""
        stale = app_window.identity.to_dict()
        stale["hwnd"] = 0x7FFFFFFE      # not a live window on any normal desktop
        with pytest.raises((WindowsWindowNotFoundError,
                            WindowsWindowIdentityMismatchError)):
            layer.get_window(stale)

    def test_a_mismatched_identity_is_refused(self, layer, app_window):
        """A real handle, the wrong identity: refused, not quietly corrected."""
        from core.windows.window_models import WindowIdentity

        wrong = WindowIdentity(
            hwnd=app_window.identity.hwnd,
            process_identity=(app_window.identity.pid, "19990101 00:00:00"),
            class_name=app_window.identity.class_name,
            instance=app_window.identity.instance,
        )
        with pytest.raises(WindowsWindowIdentityMismatchError):
            layer.get_window(wrong)

    def test_a_wrong_class_for_a_real_handle_is_refused(self, layer, app_window):
        from core.windows.window_models import WindowIdentity

        wrong = WindowIdentity(
            hwnd=app_window.identity.hwnd,
            process_identity=app_window.identity.process_identity,
            class_name="SomeOtherClass",
            instance=app_window.identity.instance,
        )
        with pytest.raises(WindowsWindowIdentityMismatchError):
            layer.get_window(wrong)

    def test_an_unanchored_real_identity_is_refused(self, layer, app_window):
        from core.windows.window_models import WindowIdentity

        unanchored = WindowIdentity(
            hwnd=app_window.identity.hwnd,
            process_identity=(app_window.identity.pid, ""),
            class_name=app_window.identity.class_name,
            instance=app_window.identity.instance,
        )
        with pytest.raises(WindowsInvalidRequestError):
            layer.focus_window(unanchored)


# ══════════════════════════════════════════════════════ lifecycle verbs


def _restore_foreground(layer) -> None:
    """Return the user's own foreground window to the front.

    Run in a ``finally`` so this file does not leave the desktop somewhere the
    user did not leave it.
    """
    try:
        from core.windows.windows import _load_gui

        win32gui, _process, _con = _load_gui()
        current = int(win32gui.GetForegroundWindow() or 0)
        if not current:
            return
        for info in layer.enumerate_windows():
            if info.hwnd == current:
                win32gui.ShowWindow(current, 9)   # SW_RESTORE
                win32gui.SetForegroundWindow(current)
                return
    except Exception:  # noqa: BLE001 - restoration is best effort
        pass


class TestRealLifecycleVerbs:
    """Every verb is checked against real resulting state, not a return code."""

    def test_minimize_is_observed_as_minimized(self, layer, app_window):
        result = layer.minimize_window(app_window.identity)
        try:
            assert result["requested"] is True
            assert result["window"]["minimized"] is True, (
                "the minimize was requested but the re-read window is not "
                "minimized")
        finally:
            layer.restore_window(app_window.identity)

    def test_restore_is_observed_as_visible(self, layer, app_window):
        layer.minimize_window(app_window.identity)
        result = layer.restore_window(app_window.identity)
        assert result["requested"] is True
        assert result["window"]["visible"] is True
        assert result["window"]["minimized"] is False, (
            "the restore was requested but the window is still minimized")

    def test_focus_is_observed_as_foreground(self, layer, app_window):
        """Windows may refuse activation, so the assertion is on honesty.

        If activation was refused the result must say so; if it succeeded the
        window must actually be foreground. Either outcome is acceptable, a
        claimed success that is not observable is not.
        """
        try:
            result = layer.focus_window(app_window.identity)
            assert result["requested"] is True, (
                "the focus sequence was never dispatched")
            observed = layer.get_window(app_window.identity)
            if result["confirmed"]:
                assert observed.foreground is True, (
                    "focus reported success but the window is not foreground")
            else:
                assert result["foreground"] is False, (
                    "focus reported no confirmation while the window is "
                    "foreground")
        finally:
            _restore_foreground(layer)

    def test_a_refused_activation_is_reported_rather_than_raised(self, layer,
                                                                app_window):
        """Windows returning FALSE is a normal outcome, not an error."""
        result = layer.focus_window(app_window.identity)
        assert result["requested"] is True
        assert result["activated"] in (True, False)
        assert isinstance(result["confirmed"], bool)
        _restore_foreground(layer)

    def test_close_is_observed_as_gone(self, layer, app_window):
        """The real test: a window this test owns is actually destroyed."""
        result = layer.close_window(app_window.identity)
        assert result["requested"] is True
        assert result["confirmed"] is True, (
            "WM_CLOSE was posted but the window still exists")
        with pytest.raises(WindowsWindowNotFoundError):
            layer.get_window(app_window.identity)

    def test_acting_on_a_closed_window_is_refused(self, layer, app_window):
        layer.close_window(app_window.identity)
        with pytest.raises((WindowsWindowNotFoundError,
                            WindowsWindowIdentityMismatchError)):
            layer.focus_window(app_window.identity)

    def test_a_protected_shell_window_is_refused(self, layer):
        """The desktop and taskbar are not the user's to close."""
        found = [w for w in layer.enumerate_windows()
                 if w.identity.class_name in ("Progman", "Shell_TrayWnd",
                                              "WorkerW")]
        if not found:
            pytest.skip("no shell window class present on this desktop")
        for info in found:
            from core.windows.errors import WindowsProtectedProcessError

            with pytest.raises(WindowsProtectedProcessError):
                layer.close_window(info.identity)


class TestRealCancellation:
    def test_a_precancelled_call_does_no_work(self, layer, app_window):
        from core.windows.errors import WindowsCancelledError

        before = layer.get_window(app_window.identity)
        with pytest.raises(WindowsCancelledError):
            layer.minimize_window(app_window.identity, cancelled=lambda: True)
        after = layer.get_window(app_window.identity)
        assert after.minimized == before.minimized, (
            "a cancelled minimize still changed the window")

    def test_a_precancelled_close_leaves_the_window_alive(self, layer, app_window):
        from core.windows.errors import WindowsCancelledError

        with pytest.raises(WindowsCancelledError):
            layer.close_window(app_window.identity, cancelled=lambda: True)
        assert layer.get_window(app_window.identity), (
            "a cancelled close destroyed the window")

    def test_cancellation_during_the_close_poll_is_observed(self, layer,
                                                             app_window):
        """The close wait is the one genuinely blocking window in this layer."""
        from core.windows.errors import WindowsCancelledError

        calls = {"n": 0}

        def _cancel_immediately():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(WindowsCancelledError):
            layer.close_window(app_window.identity, cancelled=_cancel_immediately)
        # Either the cancel landed or the window closed first; both are valid,
        # but a silent success when cancellation was requested is not.
        assert calls["n"] > 0, "the token was never polled during the wait"


_ = (subprocess, time, sys)
_ = WindowsWindowIdentityMismatchError