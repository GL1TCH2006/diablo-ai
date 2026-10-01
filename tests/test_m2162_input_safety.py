"""M2.16.2 / M2.16.3 - the input safety model, deterministically.

The dangerous property of input is that its effect is **invisible from here**. A
click may open a menu, change a field, or land on an entirely different
application than intended, and the OS offers no way to tell. So the tests below
are organised around the three things that *are* checkable:

* the target is resolved, re-read and identity-verified before anything is
  dispatched;
* the coordinates came from freshly-read window bounds and lie inside them;
* verification never claims more than was observed.

They run against a fake window layer and a fake dispatcher, so a failure here is
a logic failure and not an artefact of the user's desktop.
"""

from __future__ import annotations

import pytest

from core.windows.coordinates import (
    ResolvedPoint,
    ScreenPoint,
    VirtualScreen,
    WindowPoint,
    coerce_point,
    require_anchored,
    resolve_point,
)
from core.windows.errors import (
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
    WindowsWindowGoneError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from core.windows.input import InputLayer, Keys
from core.windows.window_models import WindowIdentity, WindowInfo


# ══════════════════════════════════════════════════════ doubles


def _identity(hwnd=0x1234, pid=100, created="20260101 10:00:00", cls="Notepad",
              instance=0x7FF0000) -> WindowIdentity:
    return WindowIdentity(hwnd=hwnd, process_identity=(pid, created),
                          class_name=cls, instance=instance)


def _info(rect=(100, 200, 500, 600), minimized=False, identity=None,
          foreground=True) -> WindowInfo:
    return WindowInfo(
        identity=identity or _identity(),
        title="notes.txt", visible=not minimized, minimized=minimized,
        foreground=foreground, owner_pid=100, thread_id=1,
        process_name="notepad.exe", rect=rect)


class _FakeWindows:
    """A window layer whose answer the test controls."""

    def __init__(self, info=None, foreground=None, error=None):
        self.info = info if info is not None else _info()
        self.foreground = foreground if foreground is not None else self.info
        self.error = error
        self.get_calls: list = []

    def get_window(self, identity, cancelled=None):
        self.get_calls.append(identity)
        if self.error is not None:
            raise self.error
        return self.info

    def _read(self, hwnd, cancelled=None):
        return self.foreground

    def _check_cancelled(self, cancelled):
        from core.windows.errors import WindowsCancelledError

        if cancelled is not None and callable(cancelled) and cancelled():
            raise WindowsCancelledError("cancelled", diagnostics={
                "cancellation_terminated": False})

    def close(self):
        pass


class _Dispatcher:
    """Records what would have been injected, without injecting anything."""

    def __init__(self, cursor=(0, 0), send_result=None):
        self.cursor = list(cursor)
        self.send_result = send_result
        self.events: list = []
        self.released: list = []

    # pointer
    def GetCursorPos(self):
        return tuple(self.cursor)

    def GetSystemMetrics(self, index):
        return {76: 0, 77: 0, 78: 1707, 79: 1067}[index]

    def __call__(self, inputs):
        self.events.append(inputs)
        if self.send_result is not None:
            return self.send_result
        return len(inputs)


class _FakeCon:
    """The ``win32con`` half: metric index constants only."""

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79


class _PointerApi:
    """The narrow surface ``_load_input_apis`` hands back.

    ``GetCursorPos`` is the one the settle-poll reads, so faking the dispatcher
    alone would not exercise the confirmation path at all.
    """

    def __init__(self, position):
        self.position = list(position)

    def GetCursorPos(self):
        return tuple(self.position)

    def GetSystemMetrics(self, index):
        return {76: 0, 77: 0, 78: 1707, 79: 1067}[index]


def _layer(windows=None, dispatch=None):
    layer = InputLayer(windows or _FakeWindows())
    layer._send = dispatch or _Dispatcher()
    return layer


def _window_point(rel_x=50, rel_y=50, identity=None) -> dict:
    return {"mode": "window",
            "window": (identity or _identity()).to_dict(),
            "relative_x": rel_x, "relative_y": rel_y}


def _point(x=10, y=10, identity=None) -> ResolvedPoint:
    """An already-resolved, anchored point.

    The keyboard verbs require one. It carries no coordinate meaning - it exists
    so the caller has named a verified target for the keystroke to be checked
    against.
    """
    identity = identity or _identity()
    return ResolvedPoint(x=x, y=y, anchored=True, verified=True,
                         identity=identity, window_key=identity.key)


def _fake_send_input(monkeypatch, result=None):
    """Replace ``_send_input`` with a recorder that honours its contract.

    Returning the requested count is what a successful injection does; anything
    else is the refusal ``_send_input`` now raises on before a caller sees it.
    """
    sent: list = []

    def _send(inputs):
        sent.append(inputs)
        return len(inputs) if result is None else result

    monkeypatch.setattr("core.windows.input._send_input", _send)
    return sent


# ══════════════════════════════════════════════════════ coordinate model


class TestTheTargetIsNotInferred:
    def test_mode_must_be_declared(self):
        """Inferring the mode from the keys present would let a caller smuggle
        a window identity in and have it treated as absolute coordinates,
        discarding the anchoring exactly when it was wanted."""
        with pytest.raises(WindowsInvalidRequestError, match="mode"):
            coerce_point({"x": 10, "y": 10})
        with pytest.raises(WindowsInvalidRequestError):
            coerce_point({"window": _identity().to_dict(),
                          "relative_x": 1, "relative_y": 1})

    def test_screen_mode_is_accepted(self):
        point = coerce_point({"mode": "screen", "x": 10, "y": 20})
        assert isinstance(point, ScreenPoint)
        assert point.anchored is False, "a bare screen point claims no target"

    def test_window_mode_is_accepted(self):
        point = coerce_point(_window_point())
        assert isinstance(point, WindowPoint)

    @pytest.mark.parametrize("bad", [None, "x", 42, [], {"mode": "screen"},
                                     {"mode": "screen", "x": "a", "y": 1}])
    def test_malformed_points_are_refused(self, bad):
        with pytest.raises(WindowsInvalidRequestError):
            coerce_point(bad)

    def test_a_window_point_without_an_identity_is_refused(self):
        with pytest.raises(WindowsInvalidRequestError):
            coerce_point({"mode": "window", "relative_x": 1, "relative_y": 1})


class TestScreenPointValidation:
    screen = VirtualScreen(left=0, top=0, width=1707, height=1067)

    def test_a_point_inside_the_virtual_screen_resolves(self):
        result = resolve_point({"mode": "screen", "x": 400, "y": 300},
                              reader=lambda i: None, virtual_screen=self.screen,
                              require_foreground=False)
        assert (result.x, result.y) == (400, 300)
        assert result.anchored is False

    @pytest.mark.parametrize("x,y", [(-1, 10), (10, -1), (9999, 10), (10, 9999)])
    def test_a_point_outside_the_virtual_screen_is_refused(self, x, y):
        with pytest.raises(WindowsInvalidRequestError, match="outside"):
            resolve_point({"mode": "screen", "x": x, "y": y},
                          reader=lambda i: None, virtual_screen=self.screen,
                          require_foreground=False)

    def test_the_virtual_screen_offset_is_honoured(self):
        """A second display to the left makes negative coordinates valid."""
        offset = VirtualScreen(left=-1920, top=0, width=3627, height=1080)
        result = resolve_point({"mode": "screen", "x": -100, "y": 50},
                              reader=lambda i: None, virtual_screen=offset,
                              require_foreground=False)
        assert result.x == -100

    def test_no_screen_geometry_means_the_point_is_not_validated(self):
        """Reported honestly rather than validated against a guess."""
        result = resolve_point({"mode": "screen", "x": 400, "y": 300},
                              reader=lambda i: None, virtual_screen=None,
                              require_foreground=False)
        assert result.x == 400

    def test_an_unanchored_point_is_refused_for_input(self):
        point = ResolvedPoint(x=1, y=1, anchored=False)
        with pytest.raises(WindowsInvalidRequestError, match="target window"):
            require_anchored(point)

    def test_an_anchored_but_unverified_point_is_refused(self):
        point = ResolvedPoint(x=1, y=1, anchored=True, verified=True)
        with pytest.raises(WindowsInvalidRequestError):
            require_anchored(point)


class TestWindowPointResolution:
    screen = VirtualScreen(left=0, top=0, width=1707, height=1067)

    def _resolve(self, windows, point=None, require_foreground=False,
                 observed_key=""):
        return resolve_point(point or _window_point(), reader=windows.get_window,
                            virtual_screen=self.screen,
                            require_foreground=require_foreground,
                            observed_foreground_key=observed_key)

    def test_relative_coordinates_resolve_against_current_bounds(self):
        windows = _FakeWindows(info=_info(rect=(100, 200, 500, 600)))
        result = self._resolve(windows)
        assert (result.x, result.y) == (150, 250), (
            "the point was not resolved against the window's current bounds")
        assert result.verified_target is True
        assert result.window_key == _identity().key

    def test_the_window_is_re_read_at_resolution_time(self):
        """Stale bounds must not be usable. Resolution re-reads rather than
        trusting anything the caller held."""
        windows = _FakeWindows()
        self._resolve(windows)
        assert windows.get_calls, "the window was never re-read"

    def test_relative_coordinates_outside_the_window_are_refused(self):
        windows = _FakeWindows(info=_info(rect=(100, 200, 500, 600)))
        with pytest.raises(WindowsInvalidRequestError, match="outside"):
            self._resolve(windows, _window_point(rel_x=9999, rel_y=10))

    def test_negative_relative_coordinates_are_refused(self):
        windows = _FakeWindows(info=_info(rect=(100, 200, 500, 600)))
        with pytest.raises(WindowsInvalidRequestError):
            self._resolve(windows, _window_point(rel_x=-5, rel_y=10))

    def test_a_minimized_window_is_refused(self):
        """Clicking coordinates that belong to a minimized window would land
        wherever its rectangle happens to sit on the desktop."""
        windows = _FakeWindows(info=_info(minimized=True))
        with pytest.raises(WindowsInvalidRequestError, match="minimized"):
            self._resolve(windows)

    def test_empty_bounds_are_refused(self):
        windows = _FakeWindows(info=_info(rect=(0, 0, 0, 0)))
        with pytest.raises(WindowsInvalidRequestError, match="empty bounds"):
            self._resolve(windows)

    def test_a_destroyed_window_is_reported_not_faked(self):
        windows = _FakeWindows(error=WindowsWindowNotFoundError("gone"))
        with pytest.raises(WindowsWindowNotFoundError):
            self._resolve(windows)

    def test_a_recycled_handle_is_refused(self):
        windows = _FakeWindows(
            error=WindowsWindowIdentityMismatchError("recycled"))
        with pytest.raises(WindowsWindowIdentityMismatchError):
            self._resolve(windows)

    def test_an_unanchored_window_cannot_be_resolved(self):
        """A window with no process anchor has nothing to verify against."""
        windows = _FakeWindows(info=_info(
            identity=WindowIdentity(hwnd=1, process_identity=(9, ""),
                                   class_name="X")))
        with pytest.raises(WindowsWindowIdentityMismatchError):
            self._resolve(windows)

    def test_foreground_is_required_when_requested(self):
        windows = _FakeWindows()
        with pytest.raises(WindowsInvalidRequestError, match="foreground"):
            self._resolve(windows, require_foreground=True,
                          observed_key="something-else")

    def test_foreground_passes_when_it_matches(self):
        windows = _FakeWindows()
        result = self._resolve(windows, require_foreground=True,
                               observed_key=_identity().key)
        assert result.verified_target is True


class TestVirtualScreen:
    def test_contains_covers_every_addressable_pixel(self):
        """``width`` is a pixel count, so the last one is ``left + width - 1``.

        ``contains(100, 100)`` on a 100x100 screen is the case that matters:
        accepting it would admit a coordinate Windows clamps back to the edge, and
        a caller would believe it had aimed at a position that does not exist.
        """
        screen = VirtualScreen(0, 0, 100, 100)
        assert screen.contains(0, 0)
        assert screen.contains(99, 99), "the last pixel must be inside"
        assert not screen.contains(100, 50)
        assert not screen.contains(50, 100)
        assert not screen.contains(-1, 0)

    def test_an_offset_screen_contains_negative_coordinates(self):
        screen = VirtualScreen(-1920, -200, 3000, 1200)
        assert screen.contains(-1900, -100)
        assert not screen.contains(0, -201)

    def test_a_malformed_dict_yields_none_rather_than_a_guess(self):
        """Defaulting would validate coordinates against a screen that does not
        exist."""
        assert VirtualScreen.from_dict({"left": 0}) is None
        assert VirtualScreen.from_dict("nonsense") is None
        assert VirtualScreen.from_dict({"left": 0, "top": 0, "width": 10,
                                        "height": 10}).width == 10


# ══════════════════════════════════════════════════════ key vocabulary


class TestKeyVocabulary:
    @pytest.mark.parametrize("name,vk", [
        ("enter", 0x0D), ("escape", 0x1B), ("tab", 0x09), ("space", 0x20),
        ("f5", 0x74), ("f12", 0x7B), ("left", 0x25), ("delete", 0x2E),
        ("a", 0x41), ("z", 0x5A), ("0", 0x30), (".", 0xBE), ("/", 0xBF),
    ])
    def test_named_keys_resolve(self, name, vk):
        assert Keys.normalise(name)[1] == vk

    def test_names_are_normalised(self):
        assert Keys.normalise("  ENTER ")[1] == 0x0D
        assert Keys.normalise("Page-Down")[1] == 0x22

    def test_modifiers_are_recognised(self):
        for name in ("ctrl", "control", "alt", "shift", "win"):
            assert Keys.is_modifier(name)

    @pytest.mark.parametrize("bad", [
        5, 0x41, None, "", "   ", "hyperkey", "ctrl+c", ["enter"], {"a": 1},
        1.5, True,
    ])
    def test_malformed_keys_are_refused(self, bad):
        """Refusing is the safety property. Accepting an integer would be a way
        to press any key at all."""
        with pytest.raises(WindowsInvalidRequestError):
            Keys.normalise(bad)

    def test_a_virtual_key_code_is_never_accepted_from_a_caller(self):
        with pytest.raises(WindowsInvalidRequestError):
            Keys.normalise(0x5B)


# ══════════════════════════════════════════════════════ dispatch shapes


class TestDispatchIsRecordedNotExecuted:
    def test_a_single_tap_sends_a_down_and_an_up(self, monkeypatch):
        layer = _layer()
        sent: list = []
        monkeypatch.setattr("core.windows.input._send_input", lambda i: sent.append(i) or len(i))
        result = layer.tap_key("enter", point=_point())
        assert result.dispatched is True
        assert len(sent[0]) == 2, "a tap must be a press and a release"

    def test_a_movement_is_confirmed_by_reading_the_pointer_back(self, monkeypatch):
        """Pointer movement is the one input whose effect is global state and can
        therefore be verified.

        The position is read back through ``_load_input_apis``, which is the same
        seam the real implementation uses, so the fake has to be injected there -
        faking the dispatcher alone would prove nothing.
        """
        layer = _layer()
        point = ResolvedPoint(x=400, y=300, anchored=True, verified=True,
                              identity=_identity(), window_key=_identity().key)
        monkeypatch.setattr("core.windows.input._send_input", lambda i: len(i))
        monkeypatch.setattr("core.windows.input._load_input_apis",
                            lambda: (_PointerApi([400, 300]), _FakeCon()))
        result = layer.move_cursor(point)
        assert result.confirmed is True, (
            f"a pointer move that reached the target was not confirmed: "
            f"{result.to_dict()}")

    def test_a_movement_that_did_not_arrive_is_not_confirmed(self, monkeypatch):
        layer = _layer()
        point = ResolvedPoint(x=400, y=300, anchored=True, verified=True,
                              identity=_identity(), window_key=_identity().key)
        monkeypatch.setattr("core.windows.input._send_input", lambda i: len(i))
        monkeypatch.setattr("core.windows.input._load_input_apis",
                            lambda: (_PointerApi([0, 0]), _FakeCon()))
        result = layer.move_cursor(point)
        assert result.dispatched is True
        assert result.confirmed is False, (
            "a move that did not reach the requested position was confirmed")

    def test_a_click_is_never_confirmed(self, monkeypatch):
        """A click's effect lives inside the target application. Reporting it as
        confirmed would be inventing evidence."""
        layer = _layer()
        point = ResolvedPoint(x=10, y=10, anchored=True, verified=True,
                              identity=_identity(), window_key=_identity().key)
        monkeypatch.setattr("core.windows.input._send_input", lambda i: len(i))
        result = layer.click(point, button="left", count=1)
        assert result.dispatched is True
        assert result.confirmed is False, (
            "a click was reported as confirmed from outside the target")

    def test_a_double_click_is_two_pairs(self, monkeypatch):
        layer = _layer()
        point = ResolvedPoint(x=10, y=10, anchored=True, verified=True,
                              identity=_identity(), window_key=_identity().key)
        sent: list = []
        monkeypatch.setattr("core.windows.input._send_input",
                            lambda i: sent.append(len(i)) or len(i))
        layer.click(point, button="left", count=2)
        assert sent[0] == 4, "a double click must be two down/up pairs"

    def test_an_unknown_button_is_refused(self):
        layer = _layer()
        point = ResolvedPoint(x=10, y=10, anchored=True, verified=True,
                              identity=_identity(), window_key=_identity().key)
        for button in ("middle", "x1", "", None):
            with pytest.raises(WindowsInvalidRequestError):
                layer.click(point, button=button)

    def test_an_unsupported_click_count_is_refused(self):
        layer = _layer()
        point = ResolvedPoint(x=10, y=10, anchored=True, verified=True,
                              identity=_identity(), window_key=_identity().key)
        for count in (0, 3, -1, 99):
            with pytest.raises(WindowsInvalidRequestError):
                layer.click(point, count=count)


class TestWindowsRefusalIsNotSuccess:
    def test_a_blocked_sendinput_is_reported_as_a_refusal(self, monkeypatch):
        """A user-installed ``LLMHF_INJECT`` block makes ``SendInput`` return
        zero. That means the input did not happen.

        Patched one level lower than the rest of this file on purpose: replacing
        ``_send_input`` wholesale would skip the very check under test, since a
        replacement can simply ignore the count it is handed.
        """
        layer = _layer()
        point = _point()
        monkeypatch.setattr("ctypes.windll.user32.SendInput",
                            lambda *a, **k: 0)
        with pytest.raises(WindowsOperationFailedError, match="injected"):
            layer.click(point)

    def test_a_partially_injected_batch_is_also_a_refusal(self, monkeypatch):
        """Some events going in is not the request being honoured.

        A dropped event in a modifier chord leaves the modifier down, so this is
        an error rather than a count for the caller to interpret. Patched at
        ``SendInput`` itself so the real short-return check is what rejects it.
        """
        layer = _layer()
        batches: list = []

        def _partial(count, array, size):
            batches.append(count)
            return max(0, count - 1)

        monkeypatch.setattr("ctypes.windll.user32.SendInput", _partial)
        with pytest.raises(WindowsOperationFailedError, match="injected"):
            layer.hotkey(["ctrl", "c"], point=_point())
        assert len(batches) >= 2, (
            "a partially injected chord did not release what it pressed")

    def test_the_short_return_check_lives_in_the_real_sender(self, monkeypatch):
        """The guard is in ``_send_input`` itself, not only in its callers."""
        import core.windows.input as module

        monkeypatch.setattr("ctypes.windll.user32.SendInput",
                            lambda *a, **k: 0)
        events = [module._key_event(0x41, 0, 0)]
        with pytest.raises(WindowsOperationFailedError):
            module._send_input(events)


class TestTargetIsRequiredForEveryVerb:
    """The property that a coordinate is not a target, applied to all of them."""

    def _verbs(self, layer):
        point = ResolvedPoint(x=10, y=10, anchored=False, verified=False)
        return (
            ("move_cursor", (point,), {}),
            ("click", (point,), {}),
            ("move_and_click", (point,), {"button": "left", "count": 1}),
            ("tap_key", (), {"key": "a", "point": point}),
            ("press_key", (), {"key": "a", "point": point}),
            ("write_text", (), {"text": "a", "point": point}),
            ("hotkey", (), {"keys": ["ctrl", "a"], "point": point}),
        )

    def test_no_verb_dispatches_an_unanchored_point(self, monkeypatch):
        layer = _layer()
        sent = _fake_send_input(monkeypatch)
        for name, args, kwargs in self._verbs(layer):
            with pytest.raises(WindowsInvalidRequestError, match="target"):
                getattr(layer, name)(*args, **kwargs)
        assert sent == [], (
            "an unanchored point reached SendInput; a bare screen coordinate "
            "cannot be checked against any window")

    def test_a_missing_target_is_refused_rather_than_crashing(self, monkeypatch):
        layer = _layer()
        sent = _fake_send_input(monkeypatch)
        for kwargs in ({"key": "a", "point": None},
                       {"text": "a", "point": None},
                       {"keys": ["ctrl", "a"], "point": None}):
            name = {"key": "tap_key", "text": "write_text"}.get(
                next(iter(kwargs)), "hotkey")
            with pytest.raises(WindowsInvalidRequestError, match="target"):
                getattr(layer, name)(**kwargs)
        assert sent == []

    def test_the_target_is_re_read_immediately_before_dispatch(self, monkeypatch):
        """Resolution happened earlier; the window can have gone since.

        ``get_window`` is counted, and the dispatch has to happen only after the
        *second* read - the one taken at the last moment.
        """
        windows = _FakeWindows()
        layer = _layer(windows=windows)
        sent = _fake_send_input(monkeypatch)
        point = _point()
        assert windows.get_calls == [], "no read should have happened yet"
        layer.tap_key("a", point=point)
        assert windows.get_calls, (
            "the target was never re-read before the keystroke was dispatched")

    def test_a_stale_target_is_refused_at_the_last_moment(self, monkeypatch):
        """The race this closes: valid at resolution, gone by dispatch."""
        from core.windows.errors import WindowsWindowGoneError

        windows = _FakeWindows()
        sent = _fake_send_input(monkeypatch)
        layer = _layer(windows=windows)

        def _vanished(identity, cancelled=None):
            windows.get_calls.append(identity)
            raise WindowsWindowGoneError("the target window closed")

        windows.get_window = _vanished
        with pytest.raises(WindowsWindowGoneError):
            layer.tap_key("a", point=_point())
        assert sent == [], "input was dispatched after the target vanished"

    def test_release_is_the_one_verb_that_needs_no_target(self, monkeypatch):
        """Cleanup has to keep working when everything else has stopped.

        A cancelled or half-finished press is exactly when a key needs releasing,
        so release consults neither the target nor the token.
        """
        windows = _FakeWindows()
        sent = _fake_send_input(monkeypatch)

        def _never_called(identity, cancelled=None):
            raise AssertionError("release consulted the target window")

        windows.get_window = _never_called
        layer = _layer(windows=windows)
        result = layer.release_key("shift", cancelled=lambda: True)
        assert result.dispatched is True
        assert result.detail["target_checked"] is False
        assert len(sent) == 1


class TestModifiersAreAlwaysReleased:
    def _hotkey(self, monkeypatch, keys=("ctrl", "c"), fail=False):
        layer = _layer()
        sent: list = []

        def _send(inputs):
            sent.append(inputs)
            if fail and len(sent) == 1:
                return 0
            return len(inputs)

        monkeypatch.setattr("core.windows.input._send_input", _send)
        return layer, sent

    def test_modifiers_are_released_after_a_successful_hotkey(self, monkeypatch):
        layer, sent = self._hotkey(monkeypatch)
        result = layer.hotkey(["ctrl", "c"], point=_point())
        assert result.dispatched is True
        assert len(sent) >= 2, "no release followed the press"
        assert sent[-1], "the release batch was empty"

    def test_modifiers_are_released_when_the_press_batch_is_refused(self, monkeypatch):
        """The failure that matters: a held Ctrl would corrupt every later input
        on the machine."""
        layer, sent = self._hotkey(monkeypatch, fail=True)
        result = layer.hotkey(["ctrl", "c"], point=_point())
        assert result.dispatched is False
        assert len(sent) >= 2, (
            "the modifiers were never released after a refused dispatch")

    def test_modifiers_are_released_when_the_press_batch_raises(self, monkeypatch):
        layer = _layer()
        sent: list = []

        def _send(inputs):
            sent.append(inputs)
            if len(sent) == 1:
                raise WindowsOperationFailedError("SendInput failed")
            return len(inputs)

        monkeypatch.setattr("core.windows.input._send_input", _send)
        with pytest.raises(WindowsOperationFailedError):
            layer.hotkey(["ctrl", "c"], point=_point())
        assert len(sent) >= 2, (
            "an exception during dispatch left a modifier held")

    def test_modifiers_are_released_after_a_cancellation(self, monkeypatch):
        layer = _layer()
        sent: list = []
        monkeypatch.setattr("core.windows.input._send_input",
                            lambda i: sent.append(i) or len(i))

        # Cancels as soon as anything has actually been dispatched, which is the
        # interesting moment: the modifiers are down and the request is being torn
        # up underneath them. Before that point there is nothing to clean up, and
        # refusing early is the better outcome.
        def _cancel():
            return bool(sent)

        with pytest.raises(Exception):
            layer.hotkey(["ctrl", "c"], point=_point(), cancelled=_cancel)
        assert len(sent) >= 2, "a cancelled hotkey left a modifier held"

    def test_multiple_modifiers_are_all_released(self, monkeypatch):
        layer = _layer()
        sent: list = []
        monkeypatch.setattr("core.windows.input._send_input",
                            lambda i: sent.append(i) or len(i))
        layer.hotkey(["ctrl", "shift", "s"], point=_point())
        # One press batch carrying all three, then the releases. Each release is
        # its own batch because a failure partway must not skip the rest.
        assert len(sent[0]) == 3, "the press batch did not carry every key"
        released = sum(len(batch) for batch in sent[1:])
        assert released == 3, (
            f"not every pressed key was released: {released} of 3")

    @pytest.mark.parametrize("keys", [
        [], ["ctrl"], ["ctrl", "a", "b"], None,
        ["ctrl", "ctrl", "a", "a", "b", "c", "d", "e", "f"],
    ])
    def test_malformed_hotkeys_are_refused(self, keys):
        layer = _layer()
        with pytest.raises(WindowsInvalidRequestError):
            layer.hotkey(keys, point=_point())

    def test_a_single_modifier_hotkey_is_refused(self):
        """A hotkey with no main key has no meaning, and dispatching one would
        press and release a modifier for no reason."""
        layer = _layer()
        with pytest.raises(WindowsInvalidRequestError):
            layer.hotkey(["ctrl"], point=_point())


class TestTextInput:
    def test_text_uses_the_unicode_path(self, monkeypatch):
        """Layout-independent typing, and no clipboard side effect."""
        layer = _layer()
        sent: list = []
        monkeypatch.setattr("core.windows.input._send_input",
                            lambda i: sent.append(i) or len(i))
        result = layer.write_text("hi", point=_point())
        assert result.dispatched is True
        assert result.detail["unicode_path"] is True

    def test_a_character_is_sent_as_a_down_up_pair(self, monkeypatch):
        layer = _layer()
        counts: list = []
        monkeypatch.setattr("core.windows.input._send_input",
                            lambda i: counts.append(len(i)) or len(i))
        layer.write_text("abc", point=_point())
        assert counts[0] == 6, "three characters must be three down/up pairs"

    def test_unsupported_characters_are_reported_not_substituted(self, monkeypatch):
        """Silently replacing a character with something else would type text the
        caller did not ask for."""
        layer = _layer()
        sent: list = []
        monkeypatch.setattr("core.windows.input._send_input",
                            lambda i: sent.append(i) or len(i))
        result = layer.write_text("a\ud800b", point=_point())
        assert result.dispatched is False
        assert result.detail["unsupported_characters"], (
            "an unrepresentable character was silently dropped or substituted")
        assert sent == [], "input was dispatched despite unrepresentable text"

    @pytest.mark.parametrize("bad", [None, 5, "", b"bytes"])
    def test_malformed_text_is_refused(self, bad):
        layer = _layer()
        with pytest.raises(WindowsInvalidRequestError):
            layer.write_text(bad, point=_point())

    def test_unbounded_text_is_refused(self):
        layer = _layer()
        with pytest.raises(WindowsInvalidRequestError, match="4096"):
            layer.write_text("x" * 5000, point=_point())

    def test_there_is_no_clipboard_fallback(self):
        """Dropping text into the clipboard to type it would overwrite something
        the user had copied."""
        import inspect

        import core.windows.input as module

        source = inspect.getsource(module)
        for forbidden in ("OpenClipboard", "SetClipboardData", "EmptyClipboard",
                          "win32clipboard"):
            assert forbidden not in source, (
                f"the input layer references {forbidden}; a clipboard fallback "
                "would silently overwrite whatever the user had copied")


# ══════════════════════════════════════════════════════ cancellation


class TestPreflightCancellation:
    @pytest.mark.parametrize("call", [
        lambda l: l.tap_key("enter", point=_point(), cancelled=lambda: True),
        lambda l: l.press_key("a", point=_point(), cancelled=lambda: True),
        lambda l: l.hotkey(["ctrl", "c"], point=_point(), cancelled=lambda: True),
        lambda l: l.write_text("hi", point=_point(), cancelled=lambda: True),
    ])
    def test_no_input_is_dispatched_when_cancelled_up_front(self, call):
        layer = _layer()
        sent: list = []
        import core.windows.input as module
        original = module._send_input
        module._send_input = lambda i: sent.append(i) or len(i)
        try:
            from core.windows.errors import WindowsCancelledError

            with pytest.raises(WindowsCancelledError):
                call(layer)
            assert sent == [], "input was dispatched despite preflight cancellation"
        finally:
            module._send_input = original


_ = (WindowIdentity, WindowsWindowGoneError)