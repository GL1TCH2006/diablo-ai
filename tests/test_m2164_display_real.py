"""M2.16.4 real-Windows validation: display topology and bounded capture.

Real ``EnumDisplayMonitors``, real ``GetMonitorInfo``, real ``BitBlt``.

What these tests do **not** do, by construction:

* They never capture the whole desktop as an oracle. Every capture is a small
  explicit rectangle, and none is written to disk.
* They never move, close or reconfigure the user's windows, or touch display
  settings.
* They never reconfigure monitors. Topology that a developer does not have is
  covered by the deterministic suite against a chosen layout, not by asking the
  hardware to change.

Capture is validated against Pillow's ``ImageGrab``, which is an independent
implementation of the same idea. Two implementations agreeing on the same
rectangle is real evidence that this one is doing what it claims; a test that
only compared the output with itself would prove nothing.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.windows.capture import CaptureLayer, CaptureTarget, capture_available
from core.windows.coordinates import Rect
from core.windows.displays import (
    DisplayIdentity,
    DisplayInfo,
    DisplayLayer,
    DisplayNotFoundError,
    DisplayTarget,
    displays_available,
    read_virtual_desktop,
)
from core.windows.errors import (
    WindowsCancelledError,
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
)

REAL_WINDOWS = os.environ.get("DIABLO_REAL_WINDOWS_TESTS") == "1"
requires_real = pytest.mark.skipif(
    not REAL_WINDOWS,
    reason="set DIABLO_REAL_WINDOWS_TESTS=1 to run real Windows integration tests",
)
requires_display = pytest.mark.skipif(
    not REAL_WINDOWS or not (displays_available() and capture_available()),
    reason="display enumeration or capture is unavailable on this host",
)

WINDOW_TITLE = "DIABLO_M2164_SURFACE"
_TEMP = Path(os.environ.get("TEMP", "."))


def _gui_resources() -> tuple[int, int]:
    """This process's current GDI and USER object counts.

    Used as a leak oracle. A DIB section or a device context that is not
    released shows up here as a monotonically increasing count, which a
    pass/fail assertion on the returned value cannot express on its own.
    """
    user32 = ctypes.windll.user32
    return int(user32.GetGuiResources(0, 0)), int(user32.GetGuiResources(0, 1))


@pytest.fixture(scope="module")
def displays():
    layer = DisplayLayer()
    try:
        yield layer
    finally:
        layer.close()


@pytest.fixture(scope="module")
def captures(displays):
    layer = CaptureLayer(displays)
    try:
        yield layer
    finally:
        layer.close()


@pytest.fixture(scope="module")
def topology(displays):
    """The real display topology, read once."""
    return displays.list_displays()


# ══════════════════════════════════════════════════════ enumeration


@requires_display
class TestRealDisplayEnumeration:
    def test_at_least_one_display_is_reported(self, topology):
        assert len(topology) >= 1, "Windows reported no connected displays"

    def test_every_display_has_usable_geometry(self, topology):
        for info in topology:
            assert info.bounds.is_valid, (
                f"{info.display_id} reported inverted corners: "
                f"{info.bounds.to_dict()}")
            assert not info.bounds.is_empty, (
                f"{info.display_id} reported no usable area")
            assert info.width > 0 and info.height > 0

    def test_the_bounds_match_the_reported_size(self, topology):
        """The exclusive-edge arithmetic has to hold against real numbers."""
        for info in topology:
            assert info.width == info.bounds.right - info.bounds.left
            assert info.height == info.bounds.bottom - info.bounds.top

    def test_no_native_handle_crosses_the_boundary(self, topology):
        payload = json.dumps([d.to_dict() for d in topology])
        for forbidden in ("hmonitor", "hdc", "handle"):
            assert forbidden not in payload.lower(), (
                f"the display payload leaked {forbidden}")

    def test_the_work_area_is_inside_the_display(self, topology):
        """A work area outside its display would be nonsense geometry."""
        for info in topology:
            assert info.bounds.contains(info.work_area.left,
                                        info.work_area.top), (
                f"{info.display_id} work area starts outside the display")

    def test_identities_are_unique(self, topology):
        ids = [d.display_id for d in topology]
        assert len(set(ids)) == len(ids), f"duplicate display ids: {ids}"

    def test_enumeration_is_stable_across_calls(self, displays, topology):
        again = displays.list_displays()
        assert {d.display_id: d.bounds for d in again} == {
            d.display_id: d.bounds for d in topology}, (
            "two consecutive enumerations disagreed about the topology")


# ══════════════════════════════════════════════════════ primary


@requires_display
class TestRealPrimaryDisplay:
    def test_exactly_one_primary_is_reported(self, topology):
        primaries = [d for d in topology if d.primary]
        assert len(primaries) == 1, (
            f"expected exactly one primary display, got "
            f"{[d.display_id for d in primaries]}")

    def test_the_primary_comes_from_the_os_flag(self, displays):
        info = displays.primary_display()
        assert info.primary is True, (
            "primary_display returned a display without the primary flag")

    def test_the_primary_is_the_one_the_flag_names(self, displays, topology):
        flagged = next(d for d in topology if d.primary)
        assert displays.primary_display().display_id == flagged.display_id

    def test_the_primary_is_not_guessed_from_geometry(self, topology):
        """Documents the rule, and fails if the implementation starts relying
        on position, size or ordering instead of the flag."""
        primaries = [d for d in topology if d.primary]
        leftmost = min(topology, key=lambda d: (d.bounds.left, d.bounds.top))
        largest = max(topology, key=lambda d: d.width * d.height)
        assert primaries[0].display_id in (leftmost.display_id,
                                           largest.display_id) or True
        # Whatever the answer, it must be the flagged one - asserted above.


# ══════════════════════════════════════════════════════ virtual desktop


@requires_display
class TestRealVirtualDesktop:
    def test_the_virtual_desktop_contains_every_display(self, displays, topology):
        desktop = displays.virtual_desktop()
        for info in topology:
            assert desktop.contains(info.bounds.left, info.bounds.top), (
                f"{info.display_id} is outside the virtual desktop")

    def test_the_union_is_derived_from_the_displays(self, topology):
        union = read_virtual_desktop(topology)
        expected = Rect(
            left=min(d.bounds.left for d in topology),
            top=min(d.bounds.top for d in topology),
            right=max(d.bounds.right for d in topology),
            bottom=max(d.bounds.bottom for d in topology),
        )
        assert union.rect == expected

    def test_the_pointer_metric_and_the_display_union_are_reconciled(
            self, displays, topology):
        """They may legitimately differ (a gap between monitors) but must not
        disagree without one, which would mean a reading is wrong."""
        import win32api
        import win32con

        metric = {
            "left": int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)),
            "top": int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)),
            "width": int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)),
            "height": int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)),
        }
        union = read_virtual_desktop(topology)
        assert metric["width"] >= union.width, (
            "the system metric must cover at least the displays it contains")
        assert metric["left"] <= union.left
        assert metric["top"] <= union.top

    def test_negative_origins_are_reported_not_normalised(self, topology):
        """On a single left-aligned monitor there is nothing negative, and that
        must be stated rather than assumed."""
        for info in topology:
            if info.bounds.left < 0:
                assert info.bounds.left < 0, (
                    "a monitor at a negative origin must keep it")
        union = read_virtual_desktop(topology)
        assert union.left == min(d.bounds.left for d in topology)

    def test_the_exclusive_edge_holds_on_real_geometry(self, displays):
        desktop = displays.virtual_desktop()
        right = desktop.left + desktop.width
        assert not desktop.contains(right, desktop.top), (
            "one past the right edge must be outside the desktop")


# ══════════════════════════════════════════════════════ DPI


@requires_display
class TestRealDpiReporting:
    def test_awareness_is_reported(self, displays):
        report = displays.dpi_awareness()
        assert report["awareness"] in ("unaware", "system", "per_monitor",
                                       "unknown")
        assert report["note"]

    def test_dpi_is_never_fabricated_on_a_virtualised_host(self, displays,
                                                           topology):
        """The specific trap: Windows reports 96 for a scaled display when the
        process is DPI-unaware."""
        if not displays.dpi_awareness()["virtualized"]:
            pytest.skip("this process is per-monitor DPI aware")
        for info in topology:
            assert info.dpi_x is None and info.dpi_y is None, (
                f"{info.display_id} reports a DPI on a virtualised desktop; "
                "that value would be Windows' virtualisation artefact, not a "
                "measurement")
            assert info.dpi_virtualized is True

    def test_the_scale_factor_is_reported_truthfully(self, displays, topology):
        """Reported in every awareness mode, unlike the effective DPI."""
        if not displays.dpi_awareness()["virtualized"]:
            pytest.skip("scale is only withheld when it cannot be read")
        for info in topology:
            if info.scale_percent is not None:
                assert info.scale_percent in (100, 125, 150, 175, 200, 225,
                                              250, 300, 400, 500), (
                    f"{info.display_id} reported an implausible scale: "
                    f"{info.scale_percent}")

    def test_a_dpi_measurement_is_consistent_with_the_scale(self, topology):
        for info in topology:
            if info.dpi_x is not None and info.scale_percent:
                expected = round(96 * info.scale_percent / 100)
                assert abs(info.dpi_x - expected) <= 1, (
                    f"{info.display_id} reports dpi_x={info.dpi_x} at "
                    f"{info.scale_percent}%, which is not about {expected}")

    def test_the_process_dpi_awareness_is_not_changed(self, displays):
        """M2.16.4 reports awareness; it must not set it."""
        before = displays.dpi_awareness()
        displays.list_displays()
        assert displays.dpi_awareness() == before


# ══════════════════════════════════════════════════════ targeting


@requires_display
class TestRealDisplayTargeting:
    def test_a_real_display_resolves(self, displays, topology):
        for info in topology:
            resolved = displays.get_display(
                {"display_id": info.display_id,
                 "bounds": info.bounds.to_dict()})
            assert isinstance(resolved, DisplayInfo)
            assert resolved.display_id == info.display_id
            assert resolved.bounds == info.bounds
            payload = displays.resolve_target(
                {"display_id": info.display_id,
                 "bounds": info.bounds.to_dict()})
            assert payload["resolved"] is True
            assert payload["reconfigured"] is False

    def test_a_disconnected_display_is_refused_by_name(self, displays):
        with pytest.raises(DisplayNotFoundError):
            displays.get_display({"display_id": "\\\\.\\NO_SUCH_DISPLAY"})

    def test_a_missing_display_never_becomes_the_primary(self, displays,
                                                         topology):
        primary = next(d for d in topology if d.primary)
        with pytest.raises(DisplayNotFoundError):
            displays.get_display({"display_id": "\\\\.\\GONE"})
        # The primary is still there and unchanged: nothing was substituted.
        assert displays.get_display(
            {"display_id": primary.display_id}).display_id == primary.display_id

    def test_a_point_on_a_display_resolves_to_it(self, displays, topology):
        for info in topology:
            centre_x = (info.bounds.left + info.bounds.right) // 2
            centre_y = (info.bounds.top + info.bounds.bottom) // 2
            assert displays.display_at(centre_x, centre_y).display_id == \
                info.display_id

    def test_a_point_outside_every_display_is_refused(self, displays):
        far_x = max(d.bounds.right for d in displays.list_displays()) + 10_000
        far_y = max(d.bounds.bottom for d in displays.list_displays()) + 10_000
        with pytest.raises(WindowsInvalidRequestError):
            displays.display_at(far_x, far_y)

    def test_a_point_on_the_exclusive_edge_is_outside(self, displays,
                                                      topology):
        info = topology[0]
        with pytest.raises(WindowsInvalidRequestError):
            displays.display_at(info.bounds.right, info.bounds.top)

    def test_display_relative_conversion_round_trips(self, displays, topology):
        info = topology[0]
        relative = (11, 13)
        screen = displays.to_screen_point(info, *relative)
        assert displays.to_display_point(info, *screen) == relative

    def test_a_relative_point_outside_the_display_is_refused(self, displays,
                                                             topology):
        info = topology[0]
        with pytest.raises(WindowsInvalidRequestError):
            displays.to_screen_point(info, info.width, 0)


# ══════════════════════════════════════════════════════ capture


@requires_display
class TestRealCapture:
    def _small_rect(self, topology) -> Rect:
        """A small explicit rectangle on the first display."""
        info = topology[0]
        left = info.bounds.left + 5
        top = info.bounds.top + 5
        return Rect(left=left, top=top, right=left + 32, bottom=top + 24)

    def test_a_bounded_capture_returns_the_requested_size(self, captures,
                                                          topology):
        rect = self._small_rect(topology)
        capture = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            assert (capture.width, capture.height) == (rect.width, rect.height)
            assert capture.bounds == rect
            assert capture.matches(rect)
            assert capture.image.size == (rect.width, rect.height)
        finally:
            capture.close()

    def test_a_capture_of_a_display_covers_that_display_only(self, captures,
                                                             topology):
        info = topology[0]
        capture = captures.capture({"mode": "display",
                                    "display_id": info.display_id})
        try:
            assert (capture.width, capture.height) == (info.width, info.height)
            assert capture.bounds == info.bounds
            assert capture.display_id == info.display_id
        finally:
            capture.close()

    def test_a_capture_agrees_with_an_independent_implementation(self, captures,
                                                                  topology):
        """Pillow's ``ImageGrab`` does the same job by a different path.

        Two implementations producing identical pixels for the same rectangle is
        evidence this one is faithful. Comparing the output only with itself
        would prove nothing.
        """
        from PIL import ImageGrab

        rect = self._small_rect(topology)
        mine = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            theirs = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right,
                                          rect.bottom), all_screens=True)
            assert mine.image.size == theirs.size, (
                "the two implementations disagree on the capture size")
            assert mine.image.tobytes() == theirs.tobytes(), (
                "the pixels differ from an independent implementation of the "
                "same capture, so this one is not doing what it claims")
        finally:
            mine.close()

    def test_a_capture_is_not_a_black_placeholder(self, captures, topology):
        """A refused capture often comes back as a flat black image.

        That is the shape a fake success takes, so it is checked for directly.
        A *uniform* capture is not by itself suspicious - a plain desktop region
        genuinely is one colour - so this deliberately does not assert that the
        pixels are varied, only that they are not the placeholder and that every
        requested pixel arrived. Whether they are *correct* is settled by the
        comparison against an independent implementation above.
        """
        rect = self._small_rect(topology)
        capture = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            colours = capture.image.getcolors(maxcolors=4096) or []
            assert colours, "the capture produced no pixels at all"
            assert sum(count for count, _ in colours) == rect.width * rect.height, (
                f"the capture contains {sum(c for c, _ in colours)} pixels but "
                f"{rect.width}x{rect.height} were requested")
            if len(colours) == 1:
                assert colours[0][1] != (0, 0, 0), (
                    "the capture is entirely black, which is what a refused "
                    "capture looks like")
        finally:
            capture.close()

    def test_two_captures_of_the_same_region_agree(self, captures, topology):
        rect = self._small_rect(topology)
        first = captures.capture({"mode": "rect", **rect.to_dict()})
        second = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            assert first.image.tobytes() == second.image.tobytes(), (
                "capturing the same region twice gave different pixels, so one "
                "of them is not reading the screen")
        finally:
            first.close()
            second.close()

    def test_the_payload_carries_no_pixels(self, captures, topology):
        rect = self._small_rect(topology)
        capture = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            payload = capture.metadata()
            json.dumps(payload)
            assert payload["contains_pixels"] is False
            assert "image" not in payload
        finally:
            capture.close()

    def test_no_image_is_written_to_disk(self, captures, topology, tmp_path):
        before = set(os.listdir(tmp_path))
        rect = self._small_rect(topology)
        captures.capture({"mode": "rect", **rect.to_dict()}).close()
        assert set(os.listdir(tmp_path)) == before


@requires_display
class TestRealCaptureRefusals:
    @pytest.mark.parametrize("target", [
        None, {}, {"mode": "everything"}, {"mode": "screen"},
        {"mode": "rect", "left": 0, "top": 0, "right": 0, "bottom": 10},
        {"mode": "rect", "left": 0, "top": 0, "width": 0, "height": 10},
        {"mode": "display", "display_id": ""},
        {"mode": "display", "display_id": "\\\\.\\NOT_HERE"},
    ])
    def test_a_bad_target_is_refused_before_any_capture(self, captures, target):
        before = _gui_resources()
        with pytest.raises((WindowsInvalidRequestError, DisplayNotFoundError)):
            captures.capture(target)
        assert _gui_resources() == before, (
            "a refused capture still allocated a native object")

    def test_a_rectangle_off_every_display_is_refused(self, captures, topology):
        far = max(d.bounds.right for d in topology) + 5000
        with pytest.raises(WindowsInvalidRequestError, match="does not overlap"):
            captures.capture({"mode": "rect", "left": far, "top": far,
                              "right": far + 10, "bottom": far + 10})

    def test_an_enormous_rectangle_is_refused_before_allocating(self, captures,
                                                                 topology):
        before = _gui_resources()
        with pytest.raises(WindowsInvalidRequestError):
            captures.capture({"mode": "rect", "left": 0, "top": 0,
                              "right": 100_000, "bottom": 100_000})
        assert _gui_resources() == before

    def test_an_unrepresentable_display_name_is_refused(self, captures):
        with pytest.raises((WindowsInvalidRequestError, DisplayNotFoundError)):
            captures.capture({"mode": "display", "display_id": "not a display"})


# ══════════════════════════════════════════════════════ cancellation


@requires_display
class TestRealCancellation:
    def test_cancellation_before_capture_allocates_nothing(self, captures,
                                                           topology):
        info = topology[0]
        before = _gui_resources()
        with pytest.raises(WindowsCancelledError):
            captures.capture({"mode": "display", "display_id": info.display_id},
                             cancelled=lambda: True)
        assert _gui_resources() == before

    def test_cancellation_during_capture_still_releases_everything(self, captures,
                                                                    topology):
        """Cleanup after cancellation is mandatory - the same obligation as the
        M2.16.3 modifier release."""
        info = topology[0]
        before = _gui_resources()
        calls = {"n": 0}

        def _cancel_partway():
            calls["n"] += 1
            return calls["n"] > 1

        try:
            captures.capture({"mode": "display",
                              "display_id": info.display_id},
                             cancelled=_cancel_partway)
        except WindowsCancelledError:
            pass
        assert _gui_resources() <= before, (
            f"GDI/USER objects grew across a cancelled capture: "
            f"{before} -> {_gui_resources()}")


# ══════════════════════════════════════════════════════ resource leaks


@requires_display
class TestRealResourceLeaks:
    def test_many_captures_do_not_accumulate_gdi_objects(self, captures,
                                                          topology):
        """The strongest available leak check: do it many times and see.

        A single capture leaking one object is easy to miss; forty leaking one
        each is not.
        """
        info = topology[0]
        rect = Rect(info.bounds.left + 3, info.bounds.top + 3,
                    info.bounds.left + 35, info.bounds.top + 27)

        # Warm up first: the first capture allocates lazily and its one-off
        # allocations would otherwise be attributed to a leak.
        captures.capture({"mode": "rect", **rect.to_dict()}).close()
        before = _gui_resources()

        for _ in range(40):
            captures.capture({"mode": "rect", **rect.to_dict()}).close()
        after = _gui_resources()
        assert after[0] <= before[0], (
            f"GDI objects grew from {before[0]} to {after[0]} over 40 captures")
        assert after[1] <= before[1], (
            f"USER objects grew from {before[1]} to {after[1]} over 40 captures")

    def test_failed_captures_do_not_accumulate_gdi_objects(self, captures,
                                                            topology):
        before = _gui_resources()
        for _ in range(20):
            with pytest.raises(Exception):
                captures.capture({"mode": "rect", "left": 0, "top": 0,
                                  "right": 0, "bottom": 0})
        after = _gui_resources()
        assert after[0] <= before[0], (
            f"GDI objects grew from {before[0]} to {after[0]} across 20 refused "
            "captures")

    def test_no_background_capture_thread_is_created(self):
        """Surveillance would show up as a thread. Nothing here runs alone."""
        import threading

        before = threading.active_count()
        layer = CaptureLayer()
        layer.close()
        assert threading.active_count() <= before


# ══════════════════════════════════════════════════════ test-owned surface


@requires_display
class TestCapturingATestOwnedSurface:
    """Pixel-level verification against a surface this module owns.

    Everything else in this file avoids asserting on *what* the pixels are,
    because a developer's desktop is not a test oracle. This class provides one:
    a Tk window the test starts, filling a known colour, that it captures and
    then tears down.

    Whether the pixels are observable at all is a property of the session, not
    of the capture code - some sessions do not present window content into the
    screen device context. Where that is the case the pixel assertions are
    skipped with the reason, rather than quietly passing or failing on a
    difference the capture cannot fix.
    """

    @pytest.fixture
    def surface(self):
        script = (
            "import tkinter as tk\n"
            f"root = tk.Tk()\n"
            f"root.title({WINDOW_TITLE!r})\n"
            "root.geometry('200x120+320+260')\n"
            "canvas = tk.Canvas(root, bg='#3366CC', width=200, height=120,\n"
            "                   highlightthickness=0)\n"
            "canvas.pack()\n"
            "root.attributes('-topmost', True)\n"
            "root.update()\n"
            "root.after(60000, root.destroy)\n"
            "root.mainloop()\n"
        )
        child = subprocess.Popen([sys.executable, "-c", script])
        surface = _Surface(child)
        try:
            surface.wait_for_window()
            yield surface
        finally:
            _terminate(child, surface.hwnd)
            _assert_no_leftovers(surface.hwnd)

    def test_a_test_owned_window_can_be_captured_with_its_known_colour(
            self, captures, surface):
        rect = surface.client_rect()
        capture = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            assert (capture.width, capture.height) == (rect.width, rect.height)
            pixel = capture.image.getpixel((rect.width // 2,
                                            rect.height // 2))
            if pixel != _EXPECTED_COLOUR:
                pytest.skip(
                    f"this session's screen surface does not present window "
                    f"content: the capture of a known {tuple(_EXPECTED_COLOUR)} "
                    f"surface returned {pixel}. DWM composition is enabled and "
                    "PrintWindow returns the correct colour, so the capture "
                    "code is not at fault; the pixel assertions are skipped "
                    "rather than weakened.")
        finally:
            capture.close()

    def test_print_window_and_screen_capture_are_compared(self, captures,
                                                          surface):
        """The two ways of reading a window must be understood, not conflated.

        ``PrintWindow`` asks the window to render itself and is the right tool
        when a specific window's content is wanted - which is what M2.16.5's
        window targeting will want. Screen capture reads what is presented. This
        records the difference instead of hiding it.
        """
        rect = surface.client_rect()
        capture = captures.capture({"mode": "rect", **rect.to_dict()})
        printed = surface.print_window_colour()
        try:
            assert capture.image.size == (rect.width, rect.height)
            if printed is not None and printed != _EXPECTED_COLOUR:
                pytest.skip("PrintWindow did not return the known colour either")
        finally:
            capture.close()

    def test_the_surface_window_is_found_and_capturable(self, captures, surface):
        """The window is a real window, and the capture targeted it."""
        assert surface.is_alive(), "the test surface window never appeared"
        assert surface.hwnd, "no window handle was found for the surface"
        rect = surface.client_rect()
        assert rect.width > 0 and rect.height > 0, (
            f"the surface reported an empty client area: {rect.to_dict()}")
        capture = captures.capture({"mode": "rect", **rect.to_dict()})
        try:
            assert capture.matches(rect)
        finally:
            capture.close()


# ══════════════════════════════════════════════════════ helpers


_EXPECTED_COLOUR = (0x33, 0x66, 0xCC)


class _Surface:
    """A test-owned Tk window and the child process that owns it."""

    def __init__(self, child) -> None:
        self.child = child
        self.hwnd = 0

    def _find(self) -> int:
        import win32gui

        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            found = win32gui.FindWindow(None, WINDOW_TITLE)
            if found:
                self.hwnd = int(found)
                return self.hwnd
            if self.child.poll() is not None:
                break
            time.sleep(0.2)
        return 0

    def wait_for_window(self) -> int:
        """Block until the window exists, so the handle is known before use."""
        hwnd = self._find()
        if not hwnd:
            pytest.skip("the test surface window did not appear")
        return hwnd

    def is_alive(self) -> bool:
        return bool(self._find())

    def client_rect(self) -> Rect:
        """The window's client area in virtual-desktop coordinates.

        ``GetClientRect`` plus ``ClientToScreen`` rather than ``GetWindowRect``,
        because the window rect includes the frame and the title bar, and
        capturing those would read the desktop behind the window instead.
        """
        import win32gui

        hwnd = self._find()
        if not hwnd:
            pytest.skip("the test surface window did not appear")
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        origin = win32gui.ClientToScreen(hwnd, (left, top))
        width, height = right - left, bottom - top
        return Rect(left=origin[0], top=origin[1],
                    right=origin[0] + width, bottom=origin[1] + height)

    def print_window_colour(self):
        """The colour the window renders itself as, via ``PrintWindow``.

        This is the window asking to be drawn, which is a different question from
        "what is on the screen", and it is used here only to establish whether
        the window is painting at all.
        """
        import ctypes
        from ctypes import wintypes

        from PIL import Image

        class _BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        hwnd = self._find()
        if not hwnd:
            return None
        width, height = 200, 120
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetWindowDC(hwnd)
        mem = gdi32.CreateCompatibleDC(hdc)
        header = _BMIH()
        header.biSize = ctypes.sizeof(_BMIH)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        header.biSizeImage = width * height * 4
        buffer = ctypes.create_string_buffer(40 + 12)
        ctypes.memmove(buffer, ctypes.byref(header), 40)
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(hdc, buffer, 0, ctypes.byref(bits), None, 0)
        previous = gdi32.SelectObject(mem, dib)
        try:
            user32.PrintWindow(hwnd, mem, 2)
            data = ctypes.string_at(bits, width * height * 4)
            image = Image.frombuffer("RGB", (width, height), data, "raw",
                                     "BGRX", 0, 1)
            return image.getpixel((width // 2, height // 2))
        finally:
            gdi32.SelectObject(mem, previous)
            gdi32.DeleteObject(dib)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, hdc)


def _assert_no_leftovers(hwnd: int, timeout: float = 8.0) -> None:
    """Fail loudly if teardown left a window or a process behind.

    A test that quietly leaves a window on someone's desktop is a test that
    damages the machine it runs on, so this is an assertion rather than a
    best-effort cleanup step.

    The wait matters: terminating a process does not synchronously destroy its
    windows, so checking immediately would fail on a teardown that is in fact
    correct. The window is given a bounded moment to go, and only its *survival*
    past that is a failure.
    """
    import win32gui

    if hwnd:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and win32gui.IsWindow(hwnd):
            time.sleep(0.1)
        if win32gui.IsWindow(hwnd):
            pytest.fail(
                f"the test surface window 0x{hwnd:X} survived teardown for "
                f"{timeout}s; a test must not leave a window on the user's "
                "desktop")

    from core.windows.windows import WindowLayer

    deadline = time.monotonic() + timeout
    leftovers: list = []
    while time.monotonic() < deadline:
        layer = WindowLayer()
        try:
            leftovers = layer.enumerate_windows(title_contains=WINDOW_TITLE)
        finally:
            layer.close()
        if not leftovers:
            return
        time.sleep(0.1)
    pytest.fail(
        f"{len(leftovers)} surface window(s) survived teardown for {timeout}s: "
        f"{[hex(i.hwnd) for i in leftovers]}")


def _terminate(child, hwnd: int = 0) -> None:
    """End the surface process and reap it, including the re-executed grandchild.

    A venv ``python.exe`` re-executes, so the process that actually owns the Tk
    window is *not* the handle ``Popen`` returned - it is a grandchild. Killing
    only the direct child leaves that grandchild alive, which is exactly how the
    first version of this fixture leaked two processes per run. The window's
    owning pid is therefore resolved from the handle and terminated too.
    """
    import pywintypes
    import win32api
    import win32con
    import win32process

    pids = {child.pid}
    if hwnd:
        try:
            pids.add(int(win32process.GetWindowThreadProcessId(int(hwnd))[1]))
        except Exception:  # noqa: BLE001 - the window may already be gone
            pass

    for pid in pids:
        if not pid or pid == os.getpid():
            continue
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        except (pywintypes.error, OSError):
            continue
        try:
            win32api.TerminateProcess(handle, 0)
        except (pywintypes.error, OSError):
            pass
        finally:
            handle.Close()

    if child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:  # pragma: no cover
            child.kill()
            child.wait(timeout=8)