"""Explicit, bounded screen capture.

M2.16.4. This module can read a rectangle of screen pixels and nothing else.

Why it is shaped the way it is
------------------------------
Screen capture is the first capability in M2.16 that can expose something the
user did not intend to show. Everything about this module's design follows from
that, and none of it is incidental:

* **An explicit target is required.** There is no ``capture()`` with no argument,
  and no default that means "the whole desktop". A caller names a display, or
  names a rectangle, or names the virtual desktop *as the virtual desktop*.
  There is no way to ask for a capture by not specifying one, because
  unspecified is exactly how an unbounded capture happens.
* **Nothing is persisted.** The pixels live in the returned object and nowhere
  else: no file, no log, no memory store, no upload. :meth:`ScreenCapture.to_dict`
  deliberately omits the image so that a payload which gets logged, cached or
  written to an audit trail carries dimensions and metadata only.
* **Nothing runs by itself.** There is no timer, thread, poller or hook. A capture
  happens when a caller asks for one, which is the only property that makes this
  a capability rather than surveillance.
* **Native resources are always released.** Every GDI object acquired here is
  released in a ``finally``, including on cancellation, on an invalid target, and
  when a conversion raises partway through. A leaked device context or DIB section
  is a kernel resource that never comes back, so the cleanup path is the part
  most worth testing.
* **Coordinates are verified, not assumed.** The image is checked against the
  rectangle that was requested. On a DPI-unaware process the desktop is
  virtualised, and a capture that silently returned the wrong pixels would be
  worse than one that refused.

Where the pixels come from
--------------------------
``BitBlt`` from the screen device context, into a top-down 32-bit DIB section.
That is the simplest mechanism that is correct on every supported Windows version
and needs no driver support: Desktop Duplication is faster and is what a
high-framerate capture wants, but it needs DXGI, a device, and a whole lifecycle -
which is a video-pipeline decision, not a foundation one. Windows Graphics
Capture is the modern answer but is per-window, asynchronous, and needs a
package; it is the right choice for M2.16.5 when a *window's* contents are what
is wanted. ``PrintWindow`` is avoided entirely: for many windows it returns a
blank or stale image while reporting success, which is precisely the fake success
this codebase refuses to produce.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.logging_setup import get_logger

from .coordinates import Rect, VirtualScreen
from .displays import DisplayInfo, DisplayLayer
from .errors import (
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
)

log = get_logger("windows.capture")

__all__ = ["CaptureTarget", "ScreenCapture", "CaptureLayer", "capture_available"]

#: A capture larger than this is refused rather than attempted. Four bytes per
#: pixel, so this is roughly a 14000x9600 desktop - far beyond any real display,
#: and far below the point where a single request could exhaust memory.
MAX_CAPTURE_PIXELS = 120_000_000


# ══════════════════════════════════════════════════════ target


@dataclass(frozen=True)
class CaptureTarget:
    """What to capture. Always explicit.

    ``mode`` is one of:

    ``display``
        One display, named by ``display_id``. The capture covers exactly that
        display's bounds and nothing else, so a secondary monitor cannot be swept
        in by accident.
    ``rect``
        An explicit rectangle in virtual-desktop coordinates. Checked against
        live topology; a rectangle that leaves every display is refused rather
        than clamped.
    ``virtual_desktop``
        The whole desktop across every display. Available, but only when the
        caller asks for it *by name*. There is no implicit way to reach it, and no
        default value that resolves to it.
    """

    mode: str
    display_id: str = ""
    rect: Rect | None = None

    MODES = ("display", "rect", "virtual_desktop")

    @classmethod
    def from_dict(cls, data: Any) -> "CaptureTarget":
        if isinstance(data, CaptureTarget):
            return data
        if isinstance(data, str):
            # A bare string is accepted as a display_id, because that is the only
            # thing it could plausibly be. It still resolves against live
            # topology and still refuses when absent.
            return cls(mode="display", display_id=data)
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError(
                "a capture requires an explicit target: "
                "{mode: 'display', display_id: ...}, "
                "{mode: 'rect', left, top, right, bottom}, or "
                "{mode: 'virtual_desktop'}.")
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in cls.MODES:
            raise WindowsInvalidRequestError(
                f"capture mode must be one of {list(cls.MODES)}, got "
                f"{mode!r}. A capture with no explicit target is refused.",
                diagnostics={"mode": mode})
        if mode == "display":
            display_id = str(data.get("display_id") or "").strip()
            if not display_id:
                raise WindowsInvalidRequestError(
                    "mode 'display' requires a display_id.")
            return cls(mode=mode, display_id=display_id)
        if mode == "rect":
            rect = _rect_from_any(data)
            if rect is None:
                raise WindowsInvalidRequestError(
                    "mode 'rect' requires integer left, top, right and bottom.")
            # Checked here as well as in resolve, so a rectangle covering no
            # pixels is refused at the parsing boundary rather than surviving
            # into the capture path to be caught later.
            if rect.is_empty or not rect.is_valid:
                raise WindowsInvalidRequestError(
                    f"cannot capture {rect.to_dict()}: the rectangle covers no "
                    "pixels. right and bottom are exclusive, so right must exceed "
                    "left and bottom must exceed top.",
                    diagnostics={"rect": rect.to_dict()})
            return cls(mode=mode, rect=rect)
        return cls(mode=mode)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "display_id": self.display_id,
                "rect": self.rect.to_dict() if self.rect else None}


def _rect_from_any(data: dict) -> Rect | None:
    """Build a Rect from either explicit edges or an explicit width/height.

    Width and height are accepted only alongside a top-left corner. A width with
    no origin is not a rectangle, and defaulting the origin to (0, 0) would put
    the capture somewhere the caller never asked for.
    """
    if all(k in data for k in ("left", "top", "right", "bottom")):
        try:
            return Rect(left=int(data["left"]), top=int(data["top"]),
                        right=int(data["right"]), bottom=int(data["bottom"]))
        except (TypeError, ValueError):
            return None
    if all(k in data for k in ("left", "top", "width", "height")):
        try:
            left = int(data["left"])
            top = int(data["top"])
            width = int(data["width"])
            height = int(data["height"])
        except (TypeError, ValueError):
            return None
        return Rect(left=left, top=top, right=left + width,
                    bottom=top + height)
    return None


# ══════════════════════════════════════════════════════ result


@dataclass(frozen=True)
class ScreenCapture:
    """A captured image and the facts about where it came from.

    ``image`` is a Pillow image held in memory only. It is excluded from
    ``to_dict`` and from ``repr`` so that logging a capture cannot leak what was
    on the user's screen - the metadata is what an audit trail needs, and the
    pixels are not.
    """

    image: Any = field(repr=False)
    width: int
    height: int
    bounds: Rect
    coordinate_space: str = "virtual_desktop"
    display_id: str = ""
    target_mode: str = ""
    dpi_virtualized: bool = False
    captured_at: str = ""

    @property
    def size(self) -> tuple[int, int]:
        return int(self.width), int(self.height)

    def matches(self, expected: Rect) -> bool:
        """Whether the returned pixels cover exactly the requested rectangle."""
        return self.bounds == expected and (self.width, self.height) == (
            expected.width, expected.height)

    def metadata(self) -> dict[str, Any]:
        """Everything except the pixels. Safe to log, cache and audit."""
        return {
            "width": int(self.width),
            "height": int(self.height),
            "bounds": self.bounds.to_dict(),
            "coordinate_space": self.coordinate_space,
            "display_id": self.display_id,
            "target_mode": self.target_mode,
            "dpi_virtualized": self.dpi_virtualized,
            "captured_at": self.captured_at,
            "image_format": getattr(self.image, "mode", "") or "",
            "contains_pixels": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.metadata()

    def close(self) -> None:
        """Release the pixel buffer.

        Explicit because ownership is explicit: the caller asked for an image and
        therefore owns it until it says otherwise. Idempotent, and safe to call
        during cleanup.
        """
        image = getattr(self, "image", None)
        if image is not None:
            try:
                image.close()
            except Exception:  # noqa: BLE001 - best effort
                pass


# ══════════════════════════════════════════════════════ native capture


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_DIB_RGB_COLORS = 0
_BI_RGB = 0
_PNG_SAFE_FORMAT = "BGRX"


def _load_gdi_apis() -> dict[str, Any]:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    return {
        "user32": user32,
        "gdi32": gdi32,
        "GetDC": user32.GetDC,
        "ReleaseDC": user32.ReleaseDC,
        "CreateCompatibleDC": gdi32.CreateCompatibleDC,
        "DeleteDC": gdi32.DeleteDC,
        "CreateDIBSection": gdi32.CreateDIBSection,
        "SelectObject": gdi32.SelectObject,
        "DeleteObject": gdi32.DeleteObject,
        "BitBlt": gdi32.BitBlt,
    }


def capture_available() -> bool:
    """Whether screen capture can run on this host."""
    try:
        apis = _load_gdi_apis()
    except Exception:  # noqa: BLE001
        return False
    return all(apis.get(name) for name in
               ("GetDC", "ReleaseDC", "CreateCompatibleDC", "DeleteDC",
                "CreateDIBSection", "SelectObject", "DeleteObject", "BitBlt"))


def _capture_rect(apis: dict[str, Any], region: Rect, *,
                  cancelled=None) -> Any:
    """Copy ``region`` of the screen into a Pillow image.

    Every native object is acquired on one line and released in the ``finally``.
    The order matters: the DIB has to be deselected from the memory DC before it
    is deleted, and the memory DC before the screen DC is released. Getting that
    order wrong leaks a GDI object per capture rather than failing loudly.
    """
    from PIL import Image

    width, height = region.width, region.height
    if width <= 0 or height <= 0:
        raise WindowsInvalidRequestError(
            f"cannot capture {region.to_dict()}: the rectangle covers no pixels. "
            "right and bottom are exclusive, so right must exceed left and "
            "bottom must exceed top.",
            diagnostics={"rect": region.to_dict()})
    if width * height > MAX_CAPTURE_PIXELS:
        raise WindowsInvalidRequestError(
            f"cannot capture {width}x{height} pixels: the request exceeds the "
            f"{MAX_CAPTURE_PIXELS} pixel limit for a single capture.",
            diagnostics={"rect": region.to_dict(),
                         "pixels": width * height,
                         "limit": MAX_CAPTURE_PIXELS})

    _check_cancelled(cancelled)

    header = _BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = width
    # Negative height requests a top-down DIB, so row 0 is the top of the region
    # and the image needs no vertical flip. A positive value would silently
    # produce an upside-down capture that still looks like plausible image data.
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = _BI_RGB
    header.biSizeImage = width * height * 4
    info = _BITMAPINFO()
    info.bmiHeader = header

    screen_dc = None
    memory_dc = None
    bitmap = None
    previous = None
    try:
        screen_dc = apis["GetDC"](None)
        if not screen_dc:
            raise WindowsOperationFailedError(
                "GetDC(NULL) returned no device context, so there is nothing to "
                "capture from.")
        memory_dc = apis["CreateCompatibleDC"](screen_dc)
        if not memory_dc:
            raise WindowsOperationFailedError(
                "CreateCompatibleDC failed; no memory device context to capture "
                "into.")

        raw_bits = ctypes.c_void_p()
        # Set the bitfields to 32bpp BI_RGB so the DIB is readable as bytes.
        info.bmiHeader.biSizeImage = width * height * 4
        bitmap = apis["CreateDIBSection"](
            screen_dc, ctypes.byref(info), _DIB_RGB_COLORS,
            ctypes.byref(raw_bits), None, 0)
        if not bitmap or not raw_bits:
            raise WindowsOperationFailedError(
                "CreateDIBSection failed; no bitmap to capture into.")

        previous = apis["SelectObject"](memory_dc, bitmap)
        _check_cancelled(cancelled)

        ok = apis["BitBlt"](memory_dc, 0, 0, width, height,
                            screen_dc, int(region.left), int(region.top),
                            _SRCCOPY | _CAPTUREBLT)
        if not ok:
            raise WindowsOperationFailedError(
                f"BitBlt refused the capture of {region.to_dict()}. The region "
                "may be outside the virtual desktop, or the session may not "
                "permit screen capture.",
                diagnostics={"rect": region.to_dict()})
        _check_cancelled(cancelled)

        buffer = ctypes.string_at(raw_bits, width * height * 4)
        image = Image.frombuffer("RGB", (width, height), buffer, "raw",
                                 _PNG_SAFE_FORMAT, 0, 1)
        # The DIB is about to be deleted, and the image shares its memory, so the
        # pixels are copied out before the handle goes.
        return image.copy()
    finally:
        # Reverse acquisition order. Each step is independent: a failure to
        # release one object must not prevent releasing the rest.
        if memory_dc is not None:
            if previous:
                try:
                    apis["SelectObject"](memory_dc, previous)
                except Exception:  # noqa: BLE001
                    log.warning("could not restore the memory DC stock object")
            try:
                apis["DeleteDC"](memory_dc)
            except Exception:  # noqa: BLE001
                log.warning("could not delete the memory device context")
        if bitmap:
            try:
                apis["DeleteObject"](bitmap)
            except Exception:  # noqa: BLE001
                log.warning("could not delete the DIB section")
        if screen_dc is not None:
            try:
                apis["ReleaseDC"](None, screen_dc)
            except Exception:  # noqa: BLE001
                log.warning("could not release the screen device context")


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and callable(cancelled) and cancelled():
        from .errors import WindowsCancelledError

        raise WindowsCancelledError(
            "cancelled before the capture completed.",
            diagnostics={"cancellation_terminated": False})


# ══════════════════════════════════════════════════════ the layer


class CaptureLayer:
    """Bounded, explicit, in-memory screen capture."""

    def __init__(self, display_layer: DisplayLayer | None = None) -> None:
        self._displays = display_layer or DisplayLayer()
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _guard(self) -> None:
        if self._closed:
            raise WindowsOperationFailedError("the capture layer is closed.")

    def resolve_target(self, target: Any, *, cancelled=None) -> tuple[Rect, str,
                                                                    DisplayInfo | None]:
        """Turn a capture target into a concrete rectangle.

        Returns the region, the display it belongs to (if any), and the display
        whose identity the caller named. Refuses anything not actually on a
        display: a rectangle that leaves every screen is an error, not something
        to clamp to the nearest edge.
        """
        self._guard()
        wanted = CaptureTarget.from_dict(target)
        _check_cancelled(cancelled)
        displays = self._displays.list_displays(cancelled=cancelled)

        if wanted.mode == "display":
            info = self._displays.get_display({"display_id": wanted.display_id},
                                              cancelled=cancelled)
            _check_cancelled(cancelled)
            return info.bounds, wanted.display_id, info

        if wanted.mode == "virtual_desktop":
            desktop = VirtualScreen.from_rect(
                _union(displays)) if displays else None
            rect = desktop.rect if desktop is not None else Rect(0, 0, 0, 0)
            _check_cancelled(cancelled)
            return rect, "", None

        rect = wanted.rect
        assert rect is not None            # narrowed by CaptureTarget.from_dict
        if rect.is_empty or not rect.is_valid:
            raise WindowsInvalidRequestError(
                f"cannot capture {rect.to_dict()}: the rectangle covers no "
                "pixels. right and bottom are exclusive, so right must exceed "
                "left and bottom must exceed top.",
                diagnostics={"rect": rect.to_dict()})
        if not rect.is_valid:
            raise WindowsInvalidRequestError(
                f"cannot capture {rect.to_dict()}: the rectangle's corners are "
                "inverted.",
                diagnostics={"rect": rect.to_dict()})
        covering = [d for d in displays if d.bounds.intersection(rect) is not None]
        if not covering:
            desktop = VirtualScreen.from_rect(_union(displays))
            raise WindowsInvalidRequestError(
                f"cannot capture {rect.to_dict()}: it does not overlap any "
                f"connected display {desktop.to_dict() if desktop else {}}.",
                diagnostics={"rect": rect.to_dict(),
                             "display_ids": [d.display_id for d in displays]})
        display_id = covering[0].display_id if len(covering) == 1 else ""
        _check_cancelled(cancelled)
        return rect, display_id, covering[0] if len(covering) == 1 else None

    def capture(self, target: Any, *, cancelled=None) -> ScreenCapture:
        """Capture one explicit region.

        The returned object's dimensions are verified against the requested
        rectangle before it is handed back. On a DPI-virtualised host a capture
        can come back at a different size than requested, and returning it anyway
        would leave the caller holding an image of something other than what it
        asked for.
        """
        self._guard()
        rect, display_id, info = self.resolve_target(target, cancelled=cancelled)
        _check_cancelled(cancelled)

        image = _capture_rect(_load_gdi_apis(), rect, cancelled=cancelled)
        try:
            observed_width, observed_height = image.size
            if (observed_width, observed_height) != (rect.width, rect.height):
                # Close the image before raising: a caller that catches this gets
                # an exception, not an object it has to know to clean up.
                try:
                    image.close()
                except Exception:  # noqa: BLE001
                    pass
                raise WindowsOperationFailedError(
                    f"the capture came back {observed_width}x{observed_height} "
                    f"but {rect.width}x{rect.height} was requested; the "
                    "coordinate space the desktop presented does not match the "
                    "rectangle that was asked for.",
                    diagnostics={"requested": rect.to_dict(),
                                 "observed": [observed_width, observed_height]})
            return ScreenCapture(
                image=image, width=observed_width, height=observed_height,
                bounds=rect, coordinate_space="virtual_desktop",
                display_id=display_id, target_mode=CaptureTarget.from_dict(
                    target).mode,
                dpi_virtualized=bool(info and info.dpi_virtualized),
                captured_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            # Any failure between allocating the image and returning it must not
            # leave the buffer alive. The caller's ``finally`` only knows about
            # objects it was handed.
            try:
                image.close()
            except Exception:  # noqa: BLE001
                pass
            raise


def _union(displays: list[DisplayInfo]) -> Rect:
    """The smallest rectangle containing every display."""
    if not displays:
        return Rect(0, 0, 0, 0)
    return Rect(left=min(d.bounds.left for d in displays),
                top=min(d.bounds.top for d in displays),
                right=max(d.bounds.right for d in displays),
                bottom=max(d.bounds.bottom for d in displays))