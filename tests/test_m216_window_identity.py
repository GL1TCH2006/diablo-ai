"""M2.16 - window identity, and the races it has to survive.

Windows recycles ``HWND`` values exactly as it recycles process ids. A handle is
an address, not an identity, and acting on one after it has been reassigned is how
"close that window" becomes closing something else.

:class:`~core.windows.window_models.WindowIdentity` anchors a window to its owning
process's ``(pid, created_at)`` instead. These tests pin the three properties that
makes safe:

* a recycled handle owned by a different process is **refused**, not acted on;
* a window whose process anchor could not be read is **not targetable at all**,
  because there is nothing to verify it against;
* every mutating path re-verifies the full identity *before* acting, so
  enumerating and acting are separate steps with a check between them.
"""

from __future__ import annotations

import pytest

from core.windows.errors import (
    WindowsInvalidRequestError,
    WindowsProtectedProcessError,
    WindowsWindowGoneError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from core.windows.window_models import WindowIdentity, WindowInfo


def _identity(**overrides) -> WindowIdentity:
    fields = dict(hwnd=0x1234, process_identity=(100, "20260101 10:00:00"),
                  class_name="Notepad", instance=0x7FF0000)
    fields.update(overrides)
    return WindowIdentity(**fields)


class TestIdentityIsMoreThanAHandle:
    def test_the_handle_alone_does_not_identify(self):
        """The core claim. Two windows, same handle number, different processes."""
        a = _identity(hwnd=0x1234, process_identity=(100, "20260101 10:00:00"))
        b = _identity(hwnd=0x1234, process_identity=(200, "20260101 10:00:00"))
        assert a.hwnd == b.hwnd
        assert not a.matches(b), (
            "two windows sharing an HWND but owned by different processes were "
            "treated as the same window")

    def test_the_same_pid_reused_is_a_different_window(self):
        """PID reuse is the mechanism; the creation time is the defence."""
        a = _identity(process_identity=(100, "20260101 10:00:00"))
        b = _identity(process_identity=(100, "20260101 11:00:00"))
        assert a.pid == b.pid
        assert not a.matches(b), (
            "a PID reused by a new process was treated as the same window")

    def test_a_different_class_is_not_the_same_window(self):
        assert not _identity(class_name="Notepad").matches(
            _identity(class_name="Chrome_WidgetWin_1"))

    def test_a_different_instance_is_not_the_same_window(self):
        assert not _identity(instance=0x7FF0000).matches(
            _identity(instance=0x1400000))

    def test_an_exact_match_is_recognised(self):
        a = _identity()
        assert a.matches(_identity())
        assert _identity().matches(a), "matching must be symmetric"

    def test_matching_nothing_is_false(self):
        assert _identity().matches(None) is False


class TestUnanchoredWindowsAreNotTargetable:
    def test_a_window_without_a_creation_time_is_unanchored(self):
        assert _identity(process_identity=(100, "")).anchored is False

    def test_an_unanchored_identity_matches_nothing(self):
        """Deliberate: there is nothing to verify it against.

        Silently treating an unanchored window as a match is precisely how the
        wrong window gets closed.
        """
        unanchored = _identity(process_identity=(100, ""))
        assert not unanchored.matches(_identity())
        assert not _identity().matches(unanchored)

    def test_a_zero_handle_is_not_anchored(self):
        assert _identity(hwnd=0).anchored is False

    def test_a_read_can_still_return_an_unanchored_window(self):
        """Reporting it for diagnosis is fine; acting on it is not.

        The distinction matters: refusing to *enumerate* would hide a real window
        from the user, whereas refusing to *act on* it is the safety property.
        """
        info = WindowInfo(identity=_identity(process_identity=(100, "")))
        assert info.to_dict()["anchored"] is False
        assert info.to_dict()["window_key"]


class TestTheKeyIsStableAndDiscriminating:
    def test_the_key_includes_the_creation_time(self):
        key = _identity().key
        assert "20260101 10:00:00" in key

    def test_different_processes_produce_different_keys(self):
        assert _identity(process_identity=(1, "t")).key != \
            _identity(process_identity=(2, "t")).key

    def test_the_same_window_produces_the_same_key(self):
        assert _identity().key == _identity().key

    def test_the_key_is_loggable_text(self):
        """It goes into evidence rows, logs and model context."""
        assert isinstance(_identity().key, str)
        assert "\n" not in _identity().key


class TestSerialisationRoundTrips:
    def test_a_dict_form_rebuilds_an_equivalent_identity(self):
        original = _identity()
        rebuilt = WindowIdentity.from_dict(original.to_dict())
        assert rebuilt.matches(original)
        assert rebuilt.key == original.key

    def test_the_dict_form_carries_no_native_handle_object(self):
        payload = _identity().to_dict()
        import json

        json.dumps(payload)
        assert all(isinstance(v, (str, int, bool, tuple, list)) or v is None
                   for v in payload.values())

    @pytest.mark.parametrize("bad", [
        None, "a string", 42, [], {},
        {"pid": 1},                       # no hwnd
        {"hwnd": 0, "pid": 1},            # invalid hwnd
        {"hwnd": "x", "pid": 1},          # non-numeric
        {"hwnd": 5, "pid": -1},           # negative pid
    ])
    def test_a_malformed_identity_is_refused(self, bad):
        """A partially-populated identity must never be accepted.

        Accepting one would create a window reference that cannot be verified
        before it is acted on - the failure this module exists to prevent.
        """
        with pytest.raises(WindowsInvalidRequestError):
            WindowIdentity.from_dict(bad)

    def test_an_unanchored_dict_form_is_accepted_but_stays_unanchored(self):
        rebuilt = WindowIdentity.from_dict(
            {"hwnd": 5, "pid": 7, "class_name": "X", "instance": 0})
        assert rebuilt.anchored is False


# ══════════════════════════════════════════════════════ verification-before-action


class _FakeGui:
    """A stand-in for the win32gui surface the layer uses.

    Models the property that makes this layer necessary: a handle is a slot, and
    the slot's occupant can be swapped between calls.
    """

    def __init__(self, occupants: dict):
        self.occupants = dict(occupants)   # hwnd -> identity-ish tuple
        self.calls: list[tuple] = []
        self.show_calls: list[tuple] = []

    def IsWindow(self, hwnd):
        return hwnd in self.occupants

    def IsWindowVisible(self, hwnd):
        return True

    def IsIconic(self, hwnd):
        return False

    def IsWindowEnabled(self, hwnd):
        return True

    def GetWindowRect(self, hwnd):
        return (0, 0, 800, 600)

    def GetClassName(self, hwnd):
        return self.occupants[hwnd][0]

    def GetWindowText(self, hwnd):
        return self.occupants[hwnd][1]

    def GetForegroundWindow(self):
        return 0

    def GetWindowPlacement(self, hwnd):
        return (0, 0, 1, 0, 0, 0, 0)

    def GetWindowLong(self, hwnd, index):
        return self.occupants[hwnd][2]

    def ShowWindow(self, hwnd, cmd):
        self.show_calls.append((hwnd, cmd))
        return True

    def BringWindowToTop(self, hwnd):
        self.calls.append(("top", hwnd))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("fg", hwnd))
        return True

    def PostMessage(self, hwnd, msg, wparam, lparam):
        self.calls.append(("close", hwnd))
        return True

    def GetWindowThreadProcessId(self, hwnd):
        return 1, self.occupants[hwnd][3]

    def EnumWindows(self, callback, lparam):
        for hwnd in list(self.occupants):
            if not callback(hwnd, lparam):
                break
        return True


class _FakeProcessLayer:
    """A process layer whose creation times the test controls."""

    def __init__(self, created: dict[int, str]):
        self._created = dict(created)
        self.closed = False

    def get_process(self, pid, **kwargs):
        from core.windows.models import ProcessInfo

        if int(pid) not in self._created:
            return None
        return ProcessInfo(pid=int(pid), name="app.exe",
                           created_at=self._created[int(pid)])

    def close(self):
        self.closed = True


def _layer(occupants, created):
    from core.windows.windows import WindowLayer

    layer = WindowLayer.__new__(WindowLayer)
    layer._processes = _FakeProcessLayer(created)
    layer._owns_process_layer = False
    layer._lock = __import__("threading").RLock()
    layer._closed = False
    layer._gui = _FakeGui(occupants)
    return layer


class _FakeProcesses:
    """Reports the occupant's pid from the fake GUI's table.

    Holding the GUI rather than returning a constant matters: the layer reads the
    owning pid from here, and a double that always answered 0 would make every
    window look like it belonged to the same process.
    """

    def __init__(self, gui):
        self._gui = gui

    def GetWindowThreadProcessId(self, hwnd):
        return 1, self._gui.occupants[hwnd][3]


def test_a_recycled_handle_is_refused_before_any_action(monkeypatch):
    """The most important refusal in the desktop layer.

    The caller holds an identity for window A. By the time the action is
    dispatched, the handle belongs to window B. Acting would close B.
    """
    import core.windows.windows as module

    created = {100: "20260101 10:00:00", 200: "20260101 12:00:00"}
    # hwnd 0x1234 is occupied by a Notepad owned by pid 100.
    layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
    monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

    wanted = layer.get_window(_identity().to_dict())
    assert wanted.identity.matches(_identity())

    # The handle is now a different window in a different process.
    layer._gui.occupants[0x1234] = ("Chrome_WidgetWin_1", "web", 0x1400000, 200)

    with pytest.raises(WindowsWindowIdentityMismatchError):
        layer.close_window(_identity().to_dict())

    assert ("close", 0x1234) not in layer._gui.calls, (
        "the close message was posted to a recycled handle")


def test_a_recycled_handle_within_the_same_pid_is_also_refused(monkeypatch):
    """Same process, different window class: still not the requested window."""
    import core.windows.windows as module

    created = {100: "20260101 10:00:00"}
    layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
    monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

    layer.get_window(_identity().to_dict())
    layer._gui.occupants[0x1234] = ("ConsoleWindowClass", "cmd", 0x7FF0000, 100)

    with pytest.raises(WindowsWindowIdentityMismatchError):
        layer.minimize_window(_identity().to_dict())
    assert layer._gui.show_calls == [], "an action ran against a swapped handle"


def test_a_destroyed_window_is_reported_as_gone_not_as_an_error(monkeypatch):
    import core.windows.windows as module

    created = {100: "20260101 10:00:00"}
    layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
    monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

    del layer._gui.occupants[0x1234]

    with pytest.raises(WindowsWindowNotFoundError):
        layer.get_window(_identity().to_dict())


def test_an_unanchored_window_cannot_be_acted_on(monkeypatch):
    import core.windows.windows as module

    created = {100: ""}     # creation time unreadable
    layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
    monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

    unanchored = _identity(process_identity=(100, ""))
    assert layer.enumerate_windows(), "the window should still be listed"

    with pytest.raises(WindowsInvalidRequestError):
        layer.focus_window(unanchored)
    assert layer._gui.calls == [], "an action ran against an unverifiable window"


def test_protected_shell_windows_are_refused_for_mutation(monkeypatch):
    """The desktop, taskbar and shell windows are not the user's to close."""
    import core.windows.windows as module

    created = {100: "20260101 10:00:00"}
    shell = WindowIdentity(hwnd=0x2000, process_identity=(100, "20260101 10:00:00"),
                           class_name="Shell_TrayWnd", instance=0)
    layer = _layer({0x2000: ("Shell_TrayWnd", "", 0, 100)}, created)
    monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

    assert layer.enumerate_windows(), "shell windows are still readable"
    with pytest.raises(WindowsProtectedProcessError):
        layer.close_window(shell)
    assert layer._gui.calls == []


# ══════════════════════════════════════════════════════ cancellation


class TestCancellationRefusesBeforeAnyWork:
    def test_a_precancelled_read_does_no_work(self, monkeypatch):
        import core.windows.windows as module
        from core.windows.errors import WindowsCancelledError

        created = {100: "20260101 10:00:00"}
        layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        with pytest.raises(WindowsCancelledError):
            layer.enumerate_windows(cancelled=lambda: True)
        assert layer._gui.calls == []

    def test_a_precancelled_mutation_does_no_work(self, monkeypatch):
        import core.windows.windows as module
        from core.windows.errors import WindowsCancelledError

        created = {100: "20260101 10:00:00"}
        layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        for verb in (layer.focus_window, layer.minimize_window,
                     layer.restore_window, layer.close_window):
            with pytest.raises(WindowsCancelledError):
                verb(_identity().to_dict(), cancelled=lambda: True)
        assert layer._gui.calls == []
        assert layer._gui.show_calls == []

    def test_a_preflight_cancellation_claims_to_have_terminated_nothing(
            self, monkeypatch):
        """The ratified attribution: refusing early did not stop a child."""
        import core.windows.windows as module
        from core.windows.errors import WindowsCancelledError

        created = {100: "20260101 10:00:00"}
        layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        with pytest.raises(WindowsCancelledError) as exc:
            layer.close_window(_identity().to_dict(), cancelled=lambda: True)
        assert exc.value.terminated is False

    def test_a_failing_cancellation_probe_is_treated_as_cancelled(self, monkeypatch):
        """A probe that raises must not be read as "carry on"."""
        import core.windows.windows as module
        from core.windows.errors import WindowsCancelledError

        created = {100: "20260101 10:00:00"}
        layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        def _boom():
            raise RuntimeError("token gone")

        with pytest.raises(WindowsCancelledError):
            layer.enumerate_windows(cancelled=_boom)


# ══════════════════════════════════════════════════════ process ↔ window


class TestProcessWindowAssociation:
    def test_a_window_reports_its_owning_process_creation_time(self, monkeypatch):
        import core.windows.windows as module

        created = {100: "20260101 10:00:00"}
        layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 100)}, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        found = layer.enumerate_windows()
        assert found[0].identity.created_at == "20260101 10:00:00"
        assert found[0].pid == 100

    def test_a_window_whose_process_cannot_be_read_is_unanchored(self, monkeypatch):
        """Access-denied or already-gone processes must be reported honestly."""
        import core.windows.windows as module

        layer = _layer({0x1234: ("Notepad", "notes.txt", 0x7FF0000, 999)}, created={})
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        found = layer.enumerate_windows()
        assert found, "the window should still be listed"
        assert found[0].identity.anchored is False, (
            "a window was reported as anchored without a readable process anchor")

    def test_filtering_by_pid_selects_the_right_windows(self, monkeypatch):
        import core.windows.windows as module

        created = {100: "20260101 10:00:00", 200: "20260101 11:00:00"}
        layer = _layer({
            0x1: ("Notepad", "a.txt", 0x7FF0000, 100),
            0x2: ("Chrome_WidgetWin_1", "web", 0x1400000, 200),
        }, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))

        found = layer.enumerate_windows(pid=200)
        assert len(found) == 1
        assert found[0].pid == 200

    @pytest.mark.parametrize("bad", [-1, 0, "abc", 4.7])
    def test_a_malformed_pid_filter_is_a_caller_error(self, monkeypatch, bad):
        import core.windows.windows as module

        layer = _layer({}, {})
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))
        with pytest.raises(WindowsInvalidRequestError):
            layer.enumerate_windows(pid=bad)

    def test_an_integral_float_pid_filter_is_accepted(self, monkeypatch):
        import core.windows.windows as module

        created = {100: "20260101 10:00:00"}
        layer = _layer({0x1: ("Notepad", "a.txt", 0x7FF0000, 100)}, created)
        monkeypatch.setattr(module, "_load_gui", lambda: (layer._gui, _FakeProcesses(layer._gui), None))
        assert len(layer.enumerate_windows(pid=100.0)) == 1


_ = (WindowInfo, WindowsWindowGoneError)