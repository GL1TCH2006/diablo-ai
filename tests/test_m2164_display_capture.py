"""M2.16.4 display topology and bounded capture - deterministic tests.

No real Win32 here. The native boundary is faked so that every rule below is
tested against a *chosen* topology: negative origins, monitor gaps, adjacent
monitors, mismatched sizes, degenerate geometry, and devices appearing and
disappearing. Those are the cases that matter and they cannot be produced by
reconfiguring a developer's monitors.

The fakes record rather than capture, so nothing here touches the screen.
"""

from __future__ import annotations

import json

import pytest

from core.verification import VerificationState
from core.windows.capture import (
    MAX_CAPTURE_PIXELS,
    CaptureLayer,
    CaptureTarget,
    ScreenCapture,
    capture_available,
)
from core.windows.coordinates import Point, Rect, Size, VirtualScreen
from core.windows.displays import (
    DPI_AWARENESS_PER_MONITOR,
    DPI_AWARENESS_UNAWARE,
    DisplayIdentity,
    DisplayInfo,
    DisplayLayer,
    DisplayNotFoundError,
    DisplayTarget,
    read_virtual_desktop,
)
from core.windows.errors import (
    WindowsCancelledError,
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
)


def _imported_names(module) -> set[str]:
    """Module names the source actually imports.

    Read from the AST rather than by scanning text, so a comment that happens to
    contain the word "requests" cannot be mistaken for a dependency - and so a
    dependency introduced through any binding form is caught.
    """
    import ast
    import inspect

    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names

# ══════════════════════════════════════════════════════ doubles


def _display(display_id="\\\\.\\DISPLAY1", bounds=(0, 0, 1920, 1080),
             work_area=None, primary=True, dpi_x=None, dpi_y=None,
             scale=100, orientation="landscape", virtualized=False):
    rect = Rect(*bounds)
    work = Rect(*work_area) if work_area else rect
    return DisplayInfo(
        identity=DisplayIdentity(display_id=display_id), bounds=rect,
        work_area=work, primary=primary, dpi_x=dpi_x, dpi_y=dpi_y,
        scale_percent=scale, orientation=orientation,
        dpi_virtualized=virtualized, device_name=display_id)


class _FakeDisplays:
    """A display topology the test decides, and can change between calls."""

    def __init__(self, displays=None, awareness=DPI_AWARENESS_UNAWARE):
        self.displays = list(displays or [_display()])
        self.awareness = awareness
        self.calls = 0

    def list_displays(self, cancelled=None):
        # Cancellation is checked before the call is recorded, matching the real
        # layer: a cancelled request must not do any work at all.
        if cancelled is not None and callable(cancelled) and cancelled():
            raise WindowsCancelledError("cancelled")
        self.calls += 1
        return list(self.displays)

    def get_display(self, target, cancelled=None):
        wanted = DisplayTarget.from_dict(target)
        for info in self.displays:
            if wanted.matches(info):
                return info
        raise DisplayNotFoundError(
            f"no connected display is named {wanted.identity.display_id!r}")

    def primary_display(self, cancelled=None):
        for info in self.list_displays(cancelled=cancelled):
            if info.primary:
                return info
        raise DisplayNotFoundError("no primary display")

    def virtual_desktop(self, cancelled=None):
        return read_virtual_desktop(self.list_displays(cancelled=cancelled))

    def dpi_awareness(self):
        virtualized = self.awareness != DPI_AWARENESS_PER_MONITOR
        return {"awareness": "per_monitor" if not virtualized else "unaware",
                "virtualized": virtualized, "note": "test"}

    def display_at(self, x, y, cancelled=None):
        point = (int(x), int(y))
        matches = [d for d in self.list_displays(cancelled=cancelled)
                   if d.contains(*point)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise WindowsInvalidRequestError(
                f"({x}, {y}) is not on any connected display")
        raise WindowsInvalidRequestError(
            f"({x}, {y}) falls inside {len(matches)} connected displays")

    def close(self):
        pass


class _RecordingGdi:
    """A GDI stand-in whose pixel content the test chooses.

    Verifies the parts of capture that are genuinely this project's logic - the
    BGRX unpacking, the top-down orientation, the dimension arithmetic, and that
    every acquired object is released - without needing a screen.
    """

    def __init__(self, width=4, height=3, colour=(10, 20, 30), fail_bitblt=False,
                 size_mismatch=False):
        self.width = width
        self.height = height
        self.colour = colour
        self.fail_bitblt = fail_bitblt
        self.size_mismatch = size_mismatch
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.bitblt_calls: list[tuple] = []
        self.dib = None
        self.bits = None

    # -- device contexts
    def GetDC(self, _hwnd):
        self.acquired.append("screen_dc")
        return ctypes_void(1)

    def ReleaseDC(self, _hwnd, _dc):
        self.released.append("screen_dc")

    def CreateCompatibleDC(self, _dc):
        self.acquired.append("memory_dc")
        return ctypes_void(2)

    def DeleteDC(self, _dc):
        self.released.append("memory_dc")

    def CreateDIBSection(self, _dc, _info, _usage, bits, _section, _offset):
        self.acquired.append("dib")
        # Real memory, because the caller checks that the returned pointer is
        # non-null before it will read anything. The fake backs that pointer with
        # an actual buffer so the check passes and the bytes have somewhere to go.
        import ctypes

        self._buffer = ctypes.create_string_buffer(self.width * self.height * 4)
        self.bits = self._buffer
        getattr(bits, "_obj", bits).value = ctypes.addressof(self._buffer)
        self.dib = ctypes_void(3)
        return self.dib

    def SelectObject(self, _dc, obj):
        self.selected = obj
        return ctypes_void(99)

    def DeleteObject(self, _obj):
        self.released.append("dib")

    def BitBlt(self, _dst, x, y, w, h, _src, left, top, _rop):
        self.bitblt_calls.append((x, y, w, h, left, top))
        if self.fail_bitblt:
            return 0
        red, green, blue = self.colour
        pixels = bytearray()
        for _ in range(w * h):
            pixels += bytes((blue, green, red, 0))     # BGRX
        _write_bits(self.bits, bytes(pixels))
        return 1

    def as_apis(self):
        return {
            "user32": None,
            "GetDC": self.GetDC,
            "ReleaseDC": self.ReleaseDC,
            "CreateCompatibleDC": self.CreateCompatibleDC,
            "DeleteDC": self.DeleteDC,
            "CreateDIBSection": self.CreateDIBSection,
            "SelectObject": self.SelectObject,
            "DeleteObject": self.DeleteObject,
            "BitBlt": self.BitBlt,
        }


def ctypes_void(value):
    import ctypes

    return ctypes.c_void_p(value)


def _write_bits(pointer, data):
    """Place known bytes where the fake said the DIB's pixels are."""
    import ctypes

    ctypes.memmove(pointer, data, len(data))


@pytest.fixture
def gdi(monkeypatch):
    """A capture layer wired to the recording GDI, not to a real screen."""
    fake = _RecordingGdi()
    import core.windows.capture as module

    monkeypatch.setattr(module, "_load_gdi_apis", lambda: fake.as_apis())
    monkeypatch.setattr(module, "MAX_CAPTURE_PIXELS", MAX_CAPTURE_PIXELS)
    return fake


# ══════════════════════════════════════════════════════ geometry


class TestRectEdgesAreExclusive:
    """The single most consequential invariant in this milestone.

    Right and bottom are one past the last valid coordinate. Getting it wrong is
    an off-by-one that silently accepts a position one pixel outside a display -
    and then a capture or a click happens somewhere the caller never named.
    """

    def test_width_is_the_difference_not_the_coordinate(self):
        assert Rect(0, 0, 100, 50).width == 100
        assert Rect(0, 0, 100, 50).height == 50

    def test_origin_is_kept_separately(self):
        """A rectangle at (-1920, 0) is 1920 wide, not -1920."""
        rect = Rect(-1920, 0, 0, 1080)
        assert rect.width == 1920
        assert rect.height == 1080
        assert rect.origin == Point(-1920, 0)
        assert rect.size == Size(1920, 1080)

    def test_zero_zero_is_inside(self):
        assert Rect(0, 0, 100, 100).contains(0, 0)

    def test_the_last_valid_pixel_is_inside(self):
        rect = Rect(0, 0, 100, 100)
        assert rect.contains(99, 99), "the last pixel must be inside"

    def test_the_right_edge_is_outside(self):
        assert not Rect(0, 0, 100, 100).contains(100, 50)

    def test_the_bottom_edge_is_outside(self):
        assert not Rect(0, 0, 100, 100).contains(50, 100)

    def test_both_far_edges_are_outside(self):
        assert not Rect(0, 0, 100, 100).contains(100, 100)

    def test_negative_origins_are_not_special_cased(self):
        left = Rect(-1920, -200, -10, 60)
        assert left.contains(-1920, -200)
        assert left.contains(-11, 59)
        assert not left.contains(-10, 60), "the exclusive edge applies equally"

    def test_adjacent_monitors_share_an_edge_without_overlapping(self):
        """The seam: the right edge of one is the left edge of the next."""
        primary = Rect(0, 0, 1920, 1080)
        secondary = Rect(1920, 0, 3200, 1080)
        assert primary.contains(1919, 500)
        assert not primary.contains(1920, 500)
        assert secondary.contains(1920, 500)
        assert primary.intersection(secondary) is None

    def test_a_gap_between_monitors_contains_no_pixels(self):
        upper = Rect(0, 0, 1920, 1080)
        lower = Rect(0, 1200, 1920, 2280)
        assert upper.intersection(lower) is None
        assert not upper.contains(0, 1100)
        assert not lower.contains(0, 1100)

    def test_overlapping_displays_intersect_to_the_overlap(self):
        a = Rect(0, 0, 2000, 1000)
        b = Rect(1000, 0, 3000, 1000)
        assert a.intersection(b) == Rect(1000, 0, 2000, 1000)

    def test_degenerate_geometry_is_reported_not_normalised(self):
        """Windows can report nonsense after a hot-plug. Do not invent."""
        assert Rect(0, 0, 0, 100).is_empty
        assert Rect(0, 0, 100, 0).is_empty
        assert Rect(50, 0, 10, 100).is_empty, "inverted corners cover nothing"
        assert not Rect(50, 0, 10, 100).is_valid

    def test_from_dict_refuses_rather_than_guessing(self):
        assert Rect.from_dict({"left": 0}) is None
        assert Rect.from_dict("nonsense") is None
        assert Rect.from_dict({"left": 0, "top": 0, "right": 4, "bottom": 4}
                              ).width == 4

    def test_virtual_screen_and_rect_agree_on_the_edges(self):
        screen = VirtualScreen(left=0, top=0, width=100, height=100)
        assert screen.contains(99, 99)
        assert not screen.contains(100, 100)
        assert screen.rect == Rect(0, 0, 100, 100)
        assert VirtualScreen.from_rect(Rect(-1920, 0, 0, 1080)).to_dict() == {
            "left": -1920, "top": 0, "width": 1920, "height": 1080}


# ══════════════════════════════════════════════════════ topology


class TestDisplayTopology:
    def test_the_primary_comes_from_the_flag_not_the_geometry(self):
        """A primary that is not leftmost or not largest is normal."""
        right = _display("RIGHT", (1920, 0, 3840, 1080), primary=True)
        left = _display("LEFT", (0, 0, 1920, 1080), primary=False)
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [left, right]
        assert layer.primary_display().display_id == "RIGHT", (
            "the primary was inferred from ordering rather than the flag")

    def test_no_primary_is_a_structured_failure_not_a_guess(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("A", primary=False)]
        with pytest.raises(DisplayNotFoundError):
            layer.primary_display()

    def test_two_primaries_are_refused_rather_than_picked(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("A", primary=True), _display("B", bounds=(1920, 0, 3840, 1080),
                                                   primary=True)]
        with pytest.raises(WindowsOperationFailedError, match="2 primary"):
            layer.primary_display()

    def test_negative_origins_survive_enumeration(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("LEFT", (-1920, 0, 0, 1080), primary=False),
            _display("PRIMARY", (0, 0, 1920, 1080), primary=True)]
        desktop = layer.virtual_desktop()
        assert desktop.left == -1920
        assert desktop.to_dict()["width"] == 3840

    def test_a_point_in_a_monitor_gap_is_refused(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("TOP", (0, 0, 1920, 1080), primary=True),
            _display("BOTTOM", (0, 1200, 1920, 2280), primary=False)]
        with pytest.raises(WindowsInvalidRequestError, match="not on any"):
            layer.display_at(100, 1100)

    def test_an_ambiguous_point_is_refused(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("A", (0, 0, 2000, 1000), primary=True),
            _display("B", (1000, 0, 3000, 1000), primary=False)]
        with pytest.raises(WindowsInvalidRequestError, match="ambiguous"):
            layer.display_at(1500, 500)

    def test_no_display_reports_a_handle_anywhere(self):
        info = _display()
        payload = json.dumps(info.to_dict())
        for forbidden in ("hmonitor", "hdc", "hbitmap", "0x"):
            assert forbidden not in payload.lower(), (
                f"display payload leaked {forbidden}")

    def test_display_identity_is_the_device_name_not_a_handle(self):
        identity = DisplayIdentity(display_id="\\\\.\\DISPLAY2")
        assert identity.key == "\\\\.\\DISPLAY2"
        assert identity.anchored is True
        assert identity.matches({"display_id": "\\\\.\\DISPLAY2"})
        assert not identity.matches({"display_id": "\\\\.\\DISPLAY1"})


# ══════════════════════════════════════════════════════ DPI honesty


class TestDpiIsReportedTruthfully:
    def test_a_virtualised_dpi_is_never_reported_as_a_measurement(self):
        """The trap this milestone exists to avoid.

        ``GetDpiForMonitor`` answers 96 on a 150% display when the process is
        DPI-unaware. Reporting that 96 would be fabricating a measurement, so the
        value is withheld and the reason is published instead.
        """
        virtualised = _display(virtualized=True, scale=150)
        assert virtualised.dpi_x is None
        assert virtualised.dpi_y is None
        assert virtualised.scale_percent == 150
        assert virtualised.to_dict()["dpi_virtualized"] is True

    def test_no_dpi_value_is_invented_when_it_is_unavailable(self):
        """Unavailability must look like ``None``, never like a plausible number.

        A fabricated 96 is worse than an honest ``None``: it is indistinguishable
        from a real measurement, and every consumer downstream would treat it as
        one.
        """
        info = _display(dpi_x=None, dpi_y=None, scale=None, virtualized=False)
        payload = info.to_dict()
        assert payload["dpi_x"] is None
        assert payload["dpi_y"] is None
        assert payload["scale_percent"] is None
        assert "96" not in json.dumps(payload)

    def test_a_virtualised_host_never_reports_a_dpi_measurement(self):
        info = _display(virtualized=True, scale=150)
        payload = info.to_dict()
        assert payload["dpi_x"] is None and payload["dpi_y"] is None
        assert payload["dpi_virtualized"] is True
        assert payload["scale_percent"] == 150, (
            "the scale factor is truthful in every awareness mode and must "
            "still be reported")

    def test_an_unavailable_scale_is_reported_as_absent(self):
        info = _display(scale=None, virtualized=False)
        assert info.scale_percent is None
        assert info.to_dict()["scale_percent"] is None

    def test_awareness_context_is_published(self):
        layer = DisplayLayer()
        report = layer.dpi_awareness()
        assert report["awareness"] in ("unaware", "system", "per_monitor",
                                       "unknown")
        assert isinstance(report["virtualized"], bool)
        assert report["note"]

    def test_a_physical_measurement_is_passed_through_untouched(self):
        info = _display(dpi_x=144, dpi_y=144, scale=150, virtualized=False)
        assert (info.dpi_x, info.dpi_y) == (144, 144)

    def test_process_awareness_is_reported_not_changed(self):
        """M2.16.4 must not set DPI awareness to make its own numbers nicer.

        Changing it would alter how every other window call in the codebase is
        virtualised, which is a far larger decision than a display foundation
        should take alone.
        """
        import inspect

        import core.windows.displays as module

        source = inspect.getsource(module)
        for setter in ("SetProcessDpiAwareness", "SetProcessDpiAwarenessContext",
                       "SetThreadDpiAwarenessContext", "SetProcessDPIAware"):
            assert setter not in source, (
                f"{setter} appears in the display layer; DPI awareness is "
                "reported here, never changed")


# ══════════════════════════════════════════════════════ provenance


class TestCoordinateProvenanceIsNotLaundered:
    """The M2.16.2 lesson, applied to displays.

    A rectangle that belongs to one display must not be re-expressed against
    another by arithmetic done somewhere convenient.
    """

    def test_translation_is_an_explicit_named_operation(self):
        secondary = Rect(-1920, 0, 0, 1080)
        relative = Rect(10, 20, 40, 60)
        # offset() is raw arithmetic against a named origin.
        assert relative.offset(secondary.left, secondary.top) == Rect(
            -1910, 20, -1880, 60)
        # translated_from() is the sanctioned screen->relative conversion.
        assert relative.translated_from(secondary) == Rect(1930, 20, 1960, 60)
        # A primary at the origin must not move relative coordinates.
        assert relative.offset(0, 0) == relative

    def test_a_rect_cannot_silently_change_coordinate_space(self):
        """Round-tripping through the wrong space must not look identical."""
        secondary = Rect(-1920, 0, 0, 1080)
        relative = Rect(0, 0, 100, 100)
        assert relative.translated_from(secondary) != relative, (
            "a monitor-relative rectangle re-expressed in screen coordinates "
            "must be distinguishable from the original")

    def test_to_screen_point_refuses_a_point_outside_the_display(self):
        layer = DisplayLayer()
        info = _display(bounds=(0, 0, 100, 100))
        assert layer.to_screen_point(info, 99, 99) == (99, 99)
        with pytest.raises(WindowsInvalidRequestError, match="outside"):
            layer.to_screen_point(info, 100, 0)

    def test_to_display_point_refuses_a_point_off_the_display(self):
        layer = DisplayLayer()
        info = _display(bounds=(-1920, 0, 0, 1080))
        assert layer.to_display_point(info, -1919, 0) == (1, 0)
        with pytest.raises(WindowsInvalidRequestError, match="not on"):
            layer.to_display_point(info, 0, 0)

    def test_round_trip_through_screen_coordinates_is_stable(self):
        layer = DisplayLayer()
        info = _display(bounds=(-1920, 200, -10, 1280))
        x, y = layer.to_screen_point(info, 100, 100)
        assert layer.to_display_point(info, x, y) == (100, 100)


# ══════════════════════════════════════════════════════ stale targets


class TestStaleTargets:
    def test_a_disconnected_display_is_refused_by_name(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [_display("A")]
        with pytest.raises(DisplayNotFoundError, match="DISPLAY_B"):
            layer.get_display({"display_id": "\\\\.\\DISPLAY_B"})

    def test_a_missing_display_never_falls_back_to_the_primary(self):
        """The specific failure this guards: silently retargeting."""
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("PRIMARY", (0, 0, 1920, 1080), primary=True)]
        with pytest.raises(DisplayNotFoundError):
            layer.get_display({"display_id": "\\\\.\\DISPLAY_GONE"})

    def test_a_reconfigured_display_is_reported_rather_than_hidden(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("A", (0, 0, 1920, 1080))]
        payload = layer.resolve_target({
            "display_id": "A", "bounds": Rect(0, 0, 1280, 720).to_dict()})
        assert payload["reconfigured"] is True
        assert payload["current_bounds"]["right"] == 1920

    def test_an_unchanged_display_is_not_flagged_as_reconfigured(self):
        layer = DisplayLayer()
        layer.list_displays = lambda cancelled=None: [
            _display("A", (0, 0, 1920, 1080))]
        payload = layer.resolve_target({
            "display_id": "A", "bounds": Rect(0, 0, 1920, 1080).to_dict()})
        assert payload["reconfigured"] is False

    def test_enumeration_refuses_rather_than_returning_an_empty_topology(self):
        import core.windows.displays as module

        with pytest.raises(WindowsOperationFailedError, match="no connected"):
            module._enumerate({"EnumDisplayMonitors": lambda *a: 1,
                               "GetMonitorInfoW": lambda *a: 1})


# ══════════════════════════════════════════════════════ capture target


class TestCaptureRequiresAnExplicitTarget:
    @pytest.mark.parametrize("bad", [
        None, {}, {"x": 10, "y": 10}, {"mode": "all"},
        {"mode": "desktop"}, {"mode": "screen"}, [],
    ])
    def test_an_untargeted_request_is_refused(self, bad):
        with pytest.raises(WindowsInvalidRequestError):
            CaptureTarget.from_dict(bad)

    def test_a_bare_string_is_accepted_but_must_name_a_real_display(self):
        """A string can only mean a device name, so accepting it is safe.

        It still has to resolve against live topology - which is where an
        ambiguous request like ``"everything"`` is refused, rather than being
        guessed at.
        """
        assert CaptureTarget.from_dict("A").mode == "display"
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(DisplayNotFoundError):
            layer.resolve_target("everything")
        with pytest.raises(DisplayNotFoundError):
            layer.resolve_target("the whole screen")

    def test_there_is_no_default_that_means_the_whole_desktop(self):
        with pytest.raises(WindowsInvalidRequestError):
            CaptureTarget.from_dict({})
        # The whole desktop exists, but only when named as such.
        assert CaptureTarget.from_dict({"mode": "virtual_desktop"}).mode == \
            "virtual_desktop"

    def test_display_mode_requires_a_display_id(self):
        with pytest.raises(WindowsInvalidRequestError, match="display_id"):
            CaptureTarget.from_dict({"mode": "display"})

    @pytest.mark.parametrize("rect", [
        {"left": 10, "top": 10, "right": 10, "bottom": 50},     # zero width
        {"left": 10, "top": 10, "right": 50, "bottom": 10},     # zero height
        {"left": 50, "top": 50, "right": 10, "bottom": 10},     # inverted
        {"left": 0, "top": 0, "width": 0, "height": 10},        # zero width
        {"left": 0, "top": 0, "width": -5, "height": 10},       # negative
    ])
    def test_a_rectangle_covering_no_pixels_is_refused(self, rect):
        with pytest.raises(WindowsInvalidRequestError):
            CaptureTarget.from_dict({"mode": "rect", **rect})

    def test_a_width_without_an_origin_is_refused(self):
        """Defaulting the origin to (0,0) would capture somewhere unasked."""
        with pytest.raises(WindowsInvalidRequestError):
            CaptureTarget.from_dict({"mode": "rect", "width": 100, "height": 50})

    def test_a_rect_outside_every_display_is_refused_not_clamped(self):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(WindowsInvalidRequestError, match="does not overlap"):
            layer.resolve_target({"mode": "rect", "left": 5000, "top": 5000,
                                  "right": 5100, "bottom": 5100})

    def test_a_display_target_is_resolved_to_that_display_only(self):
        """Naming one display must not sweep in a second one."""
        layer = CaptureLayer(_FakeDisplays([
            _display("A", (0, 0, 800, 600), primary=True),
            _display("B", (800, 0, 1600, 600), primary=False)]))
        rect, display_id, _info = layer.resolve_target(
            {"mode": "display", "display_id": "B"})
        assert rect == Rect(800, 0, 1600, 600)
        assert display_id == "B"

    def test_an_absurd_size_is_refused_before_allocating(self, gdi):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(WindowsInvalidRequestError, match="pixel limit"):
            layer.capture({"mode": "rect", "left": 0, "top": 0,
                           "right": 60000, "bottom": 60000})
        assert gdi.acquired == [], (
            "the size limit was checked only after native resources were taken")


# ══════════════════════════════════════════════════════ capture mechanics


class TestCaptureMechanics:
    def test_a_capture_returns_exactly_the_requested_pixels(self, gdi):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        capture = layer.capture({"mode": "rect", "left": 5, "top": 6,
                                 "right": 9, "bottom": 9})
        try:
            assert (capture.width, capture.height) == (4, 3)
            assert capture.image.size == (4, 3)
            assert capture.image.getpixel((0, 0)) == (10, 20, 30)
        finally:
            capture.close()

    def test_pixels_are_oriented_top_down(self, gdi):
        """A negative ``biHeight`` requests a top-down DIB.

        Getting it wrong yields an upside-down image that still looks like
        plausible image data, so the orientation is asserted by giving row 0 and
        row 1 different colours.
        """
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        gdi.width, gdi.height = 2, 2

        def _rows(dc, x, y, w, h, src, left, top, rop):
            pixels = bytearray()
            for row in range(h):
                colour = (200, 0, 0) if row == 0 else (0, 0, 200)
                for _ in range(w):
                    pixels += bytes((colour[2], colour[1], colour[0], 0))
            _write_bits(gdi.bits, bytes(pixels))
            return 1

        gdi.BitBlt = _rows
        capture = layer.capture({"mode": "rect", "left": 0, "top": 0,
                                 "right": 2, "bottom": 2})
        try:
            assert capture.image.getpixel((0, 0)) == (200, 0, 0), (
                "the first scanned row is the top of the region")
            assert capture.image.getpixel((0, 1)) == (0, 0, 200)
        finally:
            capture.close()

    def test_the_source_origin_is_the_requested_rectangle(self, gdi):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        layer.capture({"mode": "rect", "left": 11, "top": 13,
                       "right": 15, "bottom": 16}).close()
        assert gdi.bitblt_calls == [(0, 0, 4, 3, 11, 13)]

    def test_a_refused_bitblt_is_an_error_not_a_blank_image(self, gdi):
        gdi.fail_bitblt = True
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(WindowsOperationFailedError, match="BitBlt"):
            layer.capture({"mode": "rect", "left": 0, "top": 0,
                           "right": 4, "bottom": 3})

    def test_a_dimension_mismatch_is_refused(self, gdi, monkeypatch):
        """A capture that came back at the wrong size is not the thing asked for.

        Stubs the low-level capture rather than the fake GDI, because the image
        is sized by the caller: the check under test is the comparison in
        ``CaptureLayer.capture`` between what came back and what was requested.
        """
        import core.windows.capture as module
        from PIL import Image

        monkeypatch.setattr(module, "_capture_rect",
                            lambda *a, **k: Image.new("RGB", (7, 9)))
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(WindowsOperationFailedError, match="came back"):
            layer.capture({"mode": "rect", "left": 0, "top": 0,
                           "right": 4, "bottom": 3})

    def test_a_mismatched_capture_does_not_leak_its_buffer(self, monkeypatch):
        """The image is closed before the error, so the caller has nothing to
        clean up after catching it."""
        import core.windows.capture as module
        from PIL import Image

        made = []

        def _wrong_size(*_a, **_k):
            image = Image.new("RGB", (7, 9))
            made.append(image)
            return image

        monkeypatch.setattr(module, "_capture_rect", _wrong_size)
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(WindowsOperationFailedError):
            layer.capture({"mode": "rect", "left": 0, "top": 0,
                           "right": 4, "bottom": 3})
        assert made and made[0].size == (7, 9), (
            "the stub was never called, so nothing was tested")


# ══════════════════════════════════════════════════════ resource ownership


class TestNativeResourcesAreAlwaysReleased:
    """A leaked device context or DIB section is a kernel resource that never
    comes back, so the cleanup path is the part most worth testing."""

    def _assert_released(self, gdi, action):
        action()
        assert set(gdi.released) == {"screen_dc", "memory_dc", "dib"}, (
            f"native objects were not all released: acquired={gdi.acquired} "
            f"released={gdi.released}")

    def test_released_on_success(self, gdi):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        capture = layer.capture({"mode": "rect", "left": 0, "top": 0,
                                 "right": 4, "bottom": 3})
        self._assert_released(gdi, capture.close)

    def test_released_when_bitblt_fails(self, gdi):
        gdi.fail_bitblt = True
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        self._assert_released(gdi, lambda: _expect(
            lambda: layer.capture({"mode": "rect", "left": 0, "top": 0,
                                   "right": 4, "bottom": 3}),
            WindowsOperationFailedError))

    def test_released_when_cancelled_mid_capture(self, gdi):
        """Cancellation *after* the pixels were copied but before the result.

        This is the interesting moment: the native objects are all live and the
        caller wants to stop. Cleanup still has to run, which is the same
        obligation as the M2.16.3 modifier release.
        """
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))

        def _cancel_once_the_copy_happened():
            # The function itself, not its result: passing the boolean would
            # freeze "not cancelled" at call time and the later check would
            # never see the cancellation.
            return bool(gdi.bitblt_calls)

        self._assert_released(gdi, lambda: _expect(
            lambda: layer.capture({"mode": "rect", "left": 0, "top": 0, "right": 4,
                                   "bottom": 3},
                                  cancelled=_cancel_once_the_copy_happened),
            WindowsCancelledError))
        assert gdi.bitblt_calls, (
            "the capture was cancelled before any copy happened, so the "
            "mid-flight cleanup path was not actually exercised")

    def test_released_when_a_conversion_raises(self, gdi):
        """An exception between allocation and return must still release."""
        import core.windows.capture as module

        original = module._capture_rect
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))

        def _explode_after_release(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated conversion failure")

        module._capture_rect = _explode_after_release
        try:
            self._assert_released(gdi, lambda: _expect(
                lambda: layer.capture({"mode": "rect", "left": 0, "top": 0,
                                       "right": 4, "bottom": 3}),
                RuntimeError))
        finally:
            module._capture_rect = original

    def test_cleanup_runs_even_when_the_caller_cancels_first(self, gdi):
        """Cancellation before dispatch must not skip the preflight check."""
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        _expect(lambda: layer.capture(
            {"mode": "rect", "left": 0, "top": 0, "right": 4, "bottom": 3},
            cancelled=lambda: True), WindowsCancelledError)
        assert gdi.acquired == [], (
            "a cancelled capture acquired native resources at all")


def _expect(action, error):
    with pytest.raises(error):
        action()


# ══════════════════════════════════════════════════════ privacy


class TestCapturePrivacyBoundaries:
    def test_the_payload_never_carries_pixels(self):
        """The audit trail, the cache and the agent's memory all read this.

        If image data ever appeared here, a capture would silently persist
        wherever results are written down.
        """
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        capture = layer.capture({"mode": "rect", "left": 0, "top": 0,
                                 "right": 4, "bottom": 3})
        try:
            payload = capture.metadata()
            json.dumps(payload)                # must be serialisable
            assert "image" not in payload
            assert isinstance(payload["width"], int)
            assert payload["contains_pixels"] is False
        finally:
            capture.close()

    def test_the_repr_cannot_leak_the_image(self):
        capture = ScreenCapture(image=object(), width=4, height=3,
                                bounds=Rect(0, 0, 4, 3))
        assert "object" not in repr(capture), (
            "the image must be excluded from repr, which ends up in logs")

    def test_nothing_is_written_to_disk(self, gdi, tmp_path, monkeypatch):
        """No file is created, whatever the request."""
        import os

        before = set(os.listdir(tmp_path))
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        layer.capture({"mode": "rect", "left": 0, "top": 0,
                       "right": 4, "bottom": 3}).close()
        assert set(os.listdir(tmp_path)) == before

    def test_the_capture_layer_never_opens_a_network_connection(self):
        import core.windows.capture as module

        forbidden = {"requests", "urllib", "urllib3", "socket", "http",
                     "httpx", "ftplib", "smtplib", "boto3", "aiohttp"}
        imported = _imported_names(module)
        assert not (imported & forbidden), (
            f"the capture layer imports {sorted(imported & forbidden)}; a "
            "capture must never leave the machine on its own")

    def test_the_capture_layer_has_no_polling_or_background_loop(self):
        """A timer or a thread is what turns a capability into surveillance."""
        import inspect

        import core.windows.capture as module

        imported = _imported_names(module)
        for forbidden in ("threading", "asyncio", "sched", "timeit"):
            assert forbidden not in imported, (
                f"the capture layer imports {forbidden}; capture must happen "
                "only when a caller asks for it")
        assert "while True" not in inspect.getsource(module), (
            "the capture layer contains an unbounded loop")

    def test_there_is_no_clipboard_or_telemetry_side_channel(self):
        import inspect

        import core.windows.capture as module

        imported = _imported_names(module)
        for forbidden in ("win32clipboard", "subprocess", "sqlite3"):
            assert forbidden not in imported
        assert "SendInput" not in inspect.getsource(module)

    def test_capture_is_classified_mutating_so_it_needs_permission(self):
        from core.windows.capabilities import _MUTATING_OPERATIONS

        assert "capture_screen" in _MUTATING_OPERATIONS

    def test_the_bridge_escalates_capture_and_leaves_reads_alone(self):
        from core.windows.capabilities import WindowsCapabilityLayer
        from integration.contracts import CapabilityRegistry, WindowsCapabilityAdapter
        from tools.capability_bridge import CapabilityBridge
        from config.permissions import PermissionLevel

        registry = CapabilityRegistry()
        registry.register(WindowsCapabilityAdapter(WindowsCapabilityLayer()))
        bridge = CapabilityBridge(registry)
        assert bridge.permission_level(
            {"capability": "windows", "operation": "capture_screen"}) is \
            PermissionLevel.SENSITIVE
        for read in ("list_displays", "primary_display", "virtual_desktop",
                     "get_display", "point_to_display"):
            assert bridge.permission_level(
                {"capability": "windows", "operation": read}) is \
                PermissionLevel.SAFE, f"{read} should be a read"


# ══════════════════════════════════════════════════════ cancellation


class TestCancellation:
    def test_cancelled_before_enumeration_does_no_work(self):
        fake = _FakeDisplays()
        layer = DisplayLayer()
        layer.list_displays = fake.list_displays
        with pytest.raises(WindowsCancelledError):
            layer.list_displays(cancelled=lambda: True)
        assert fake.calls == 0

    def test_cancelled_before_capture_allocates_nothing(self, gdi):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        with pytest.raises(WindowsCancelledError):
            layer.capture({"mode": "rect", "left": 0, "top": 0, "right": 4,
                           "bottom": 3}, cancelled=lambda: True)
        assert gdi.acquired == []

    def test_cancellation_during_resolution_stops_before_dispatch(self, gdi):
        layer = CaptureLayer(_FakeDisplays([_display("A", (0, 0, 800, 600))]))
        calls = {"n": 0}

        def _cancel():
            calls["n"] += 1
            return calls["n"] > 1

        with pytest.raises(WindowsCancelledError):
            layer.capture({"mode": "rect", "left": 0, "top": 0, "right": 4,
                           "bottom": 3}, cancelled=_cancel)
        assert gdi.acquired == [], "dispatch began despite cancellation"


# ══════════════════════════════════════════════════════ verification


def _verify(operation, data):
    from integration.contracts import CapabilityRegistry, WindowsCapabilityAdapter
    from tools.capability_bridge import CapabilityBridge
    from core.windows.capabilities import WindowsCapabilityLayer
    import tools.capability_bridge as module

    registry = CapabilityRegistry()
    registry.register(WindowsCapabilityAdapter(WindowsCapabilityLayer()))
    bridge = CapabilityBridge(registry)
    common = {"capability": "windows", "operation": operation,
              "request_id": "r1"}
    return module._DISPLAY_VERIFIERS[operation](bridge, common, data)


class TestDisplayVerification:
    def test_enumeration_passes_only_with_real_geometry(self):
        result = _verify("list_displays", {
            "displays": [_display("A").to_dict()], "primary_display_id": "A"})
        assert result.state is VerificationState.PASSED

    def test_an_empty_display_list_fails(self):
        """Windows never returns zero displays; that is a failed read."""
        result = _verify("list_displays", {"displays": [],
                                           "primary_display_id": ""})
        assert result.state is VerificationState.FAILED

    def test_a_display_with_no_area_fails(self):
        broken = _display("A").to_dict()
        broken["bounds"]["width"] = 0
        broken["bounds"]["right"] = 0
        result = _verify("list_displays", {"displays": [broken],
                                           "primary_display_id": "A"})
        assert result.state is VerificationState.FAILED

    def test_a_primary_outside_the_listed_set_fails(self):
        result = _verify("list_displays", {
            "displays": [_display("A").to_dict()],
            "primary_display_id": "SOMETHING_ELSE"})
        assert result.state is VerificationState.FAILED

    def test_primary_passes_only_with_the_flag_set(self):
        payload = _display("A").to_dict()
        assert _verify("primary_display",
                       {"display": payload}).state is VerificationState.PASSED
        payload["primary"] = False
        assert _verify("primary_display", {
            "display": payload}).state is VerificationState.FAILED, (
            "a display returned as primary without the flag had its primacy "
            "inferred rather than read")

    def test_no_primary_is_unverified_not_passed(self):
        result = _verify("primary_display", {"display": {}, "display_id": ""})
        assert result.state is VerificationState.UNVERIFIED

    def test_point_mapping_is_recomputed_not_trusted(self):
        info = _display("A", (0, 0, 1920, 1080)).to_dict()
        good = _verify("point_to_display",
                       {"display": info, "point": {"x": 100, "y": 100}})
        assert good.state is VerificationState.PASSED
        bad = _verify("point_to_display",
                      {"display": info, "point": {"x": 5000, "y": 100}})
        assert bad.state is VerificationState.FAILED, (
            "a point outside the reported display was accepted")

    def test_the_exclusive_edge_applies_to_verification_too(self):
        info = _display("A", (0, 0, 1920, 1080)).to_dict()
        inside = _verify("point_to_display",
                         {"display": info, "point": {"x": 1919, "y": 1079}})
        assert inside.state is VerificationState.PASSED
        on_edge = _verify("point_to_display",
                          {"display": info, "point": {"x": 1920, "y": 1079}})
        assert on_edge.state is VerificationState.FAILED

    def test_a_degenerate_virtual_desktop_fails(self):
        result = _verify("virtual_desktop",
                         {"left": 0, "top": 0, "width": 0, "height": 0})
        assert result.state is VerificationState.FAILED

    def test_a_negative_origin_virtual_desktop_passes(self):
        result = _verify("virtual_desktop", {"left": -1920, "top": -200,
                                             "width": 3840, "height": 1480})
        assert result.state is VerificationState.PASSED

    def test_every_display_operation_has_a_verifier(self):
        import tools.capability_bridge as module
        from core.windows.capabilities import WINDOWS_OPERATIONS

        for operation in ("list_displays", "primary_display", "virtual_desktop",
                          "get_display", "point_to_display", "capture_screen"):
            assert operation in WINDOWS_OPERATIONS
            assert operation in module._DISPLAY_VERIFIERS, (
                f"{operation} has no verifier")

    def test_capture_passes_when_the_image_matches_the_request(self):
        result = _verify("capture_screen", {"capture": {
            "width": 100, "height": 50,
            "bounds": Rect(0, 0, 100, 50).to_dict(), "display_id": "A"}})
        assert result.state is VerificationState.PASSED

    def test_capture_fails_when_the_size_does_not_match_the_rectangle(self):
        result = _verify("capture_screen", {"capture": {
            "width": 99, "height": 50,
            "bounds": Rect(0, 0, 100, 50).to_dict(), "display_id": "A"}})
        assert result.state is VerificationState.FAILED

    def test_capture_with_no_metadata_is_unverified(self):
        result = _verify("capture_screen", {})
        assert result.state is VerificationState.UNVERIFIED

    def test_a_passed_capture_does_not_claim_to_know_the_pixels(self):
        result = _verify("capture_screen", {"capture": {
            "width": 100, "height": 50,
            "bounds": Rect(0, 0, 100, 50).to_dict(), "display_id": "A"}})
        assert "nothing about what the pixels contain" in result.detail
        assert result.evidence["has_pixels_in_payload"] is False


# ══════════════════════════════════════════════════════ boundaries


class TestLayerBoundaries:
    def test_no_capture_api_is_reachable_outside_the_capture_layer(self):
        import inspect
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        pattern = re.compile(r"\b(CreateDIBSection|BitBlt|CreateCompatibleDC)\s*\(")
        offenders = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(("tests/", ".venv")):
                continue
            if rel == "core/windows/capture.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(rel)
        assert not offenders, (
            f"capture primitives are called outside core/windows/capture.py: "
            f"{offenders}")

    def test_only_the_capture_layer_touches_capture(self):
        """A second capture path is how unbounded capture creeps back in."""
        import inspect

        import core.windows.capture as module

        layers = [name for name, _ in inspect.getmembers(module, inspect.isclass)
                  if name.endswith("CaptureLayer")]
        assert layers == ["CaptureLayer"], (
            f"core.windows.capture defines {layers}; exactly one layer should "
            "own capture")

    def test_no_other_core_module_defines_a_capture_layer(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "core"
        offenders = [p.relative_to(root).as_posix() for p in root.rglob("*.py")
                     if "class CaptureLayer" in p.read_text(
                         encoding="utf-8", errors="ignore")
                     and p.name != "capture.py"]
        assert not offenders, (
            f"a second CaptureLayer is defined in {offenders}")

    def test_display_operations_do_not_accept_a_raw_handle(self):
        from core.windows.capabilities import WindowsCapabilityLayer
        import inspect

        layer = WindowsCapabilityLayer()
        for operation in ("list_displays", "primary_display", "virtual_desktop",
                          "get_display", "point_to_display", "capture_screen"):
            parameters = set(inspect.signature(
                getattr(layer, operation)).parameters)
            assert "hmonitor" not in parameters
            assert "hdc" not in parameters
            assert "hbitmap" not in parameters

    def test_capture_available_reports_honestly(self):
        assert isinstance(capture_available(), bool)