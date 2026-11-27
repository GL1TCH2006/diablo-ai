"""Native display topology: what is actually plugged in, and where.

M2.16.4. This module answers "what displays exist, where are they, and what
coordinate space are we in" from native Windows state. It exposes no
``HMONITOR``, no ``HDC`` and no other native handle: an ``HMONITOR`` is an
address Windows recycles, exactly like the ``HWND`` that M2.16.1 refused to treat
as an identity, so it is read inside the enumeration callback and never stored.

What this module deliberately does not do
-----------------------------------------
It does not capture anything (see :mod:`core.windows.capture`), it does not
target anything (:mod:`core.windows.input` does that, and requires a *window*),
and it does not decide whether a capture is permitted. It reports topology.

DPI, and the honesty problem in it
----------------------------------
``GetDpiForMonitor`` does not always answer the question it appears to. In a
DPI-*unaware* process Windows virtualises the desktop and reports an effective
DPI of 96 - even on a display running at 150%. On this machine that call returns
exactly 96 while ``GetScaleFactorForMonitor`` returns 150, and the monitor
reports 1707x1067 where the panel is 2560x1600. All three are consistent: the
process is seeing a virtualised desktop.

Reporting that 96 as a measured DPI would be fabricating a number. So the scale
factor, which Windows reports truthfully in every awareness mode, is reported as
the measurement; the effective DPI is reported only when the process is actually
per-monitor aware, and is otherwise ``None`` with ``dpi_virtualized`` set to say
why. Every bounds value in this module is likewise in the *virtualised* space on
such a host, which is why ``coordinate_space`` is reported alongside it and why
capture verifies its own dimensions rather than assuming physical pixels.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.logging_setup import get_logger

from .coordinates import Rect, Size, VirtualScreen
from .errors import (
    WindowsInvalidRequestError,
    WindowsOperationFailedError,
)

log = get_logger("windows.displays")

__all__ = [
    "DisplayIdentity", "DisplayInfo", "DisplayLayer", "DisplayNotFoundError",
    "DPI_AWARENESS_LABELS", "read_virtual_desktop", "displays_available",
]


# ══════════════════════════════════════════════════════ DPI awareness

#: Process DPI awareness, as reported by ``GetProcessDpiAwareness``.
#:
#: The names are Windows' own. They matter because they decide whether the DPI
#: and bounds below describe physical pixels or a virtualised view of them.
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM = 1
DPI_AWARENESS_PER_MONITOR = 2

DPI_AWARENESS_LABELS: dict[int, str] = {
    DPI_AWARENESS_UNAWARE: "unaware",
    DPI_AWARENESS_SYSTEM: "system",
    DPI_AWARENESS_PER_MONITOR: "per_monitor",
}


# ══════════════════════════════════════════════════════ models


class DisplayNotFoundError(WindowsOperationFailedError):
    """A named display is not present in the current topology.

    Distinct from :class:`~core.windows.errors.WindowsInvalidRequestError` so a
    *stale target* is not flattened into "bad request": the request was
    well-formed and named something real once, and the answer is that it is gone.
    That distinction is what lets a caller retry against a fresh enumeration
    instead of reporting the request as a typo.
    """

    code = "display_not_found"


@dataclass(frozen=True)
class DisplayIdentity:
    """A display's stable identity.

    The identity is the Win32 *device name* (``\\\\.\\DISPLAY1``) - a string, not
    a handle, so it can cross the boundary and be stored in a payload. It is
    stable for as long as that display is connected.

    There is no second anchor of the kind M2.16.1 windows carry. A window can be
    pinned to a process creation time; a monitor belongs to no process, so the
    device name is the whole identity. That is why :meth:`matches` compares only
    the name, and why :meth:`DisplayLayer.get_display` re-reads the live topology
    instead of trusting remembered bounds - see :class:`DisplayTarget`.
    """

    display_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.display_id, str) or not self.display_id.strip():
            raise WindowsInvalidRequestError(
                "a display identity requires a non-empty device name.")
        object.__setattr__(self, "display_id", self.display_id.strip())

    @property
    def key(self) -> str:
        return self.display_id

    @property
    def anchored(self) -> bool:
        """Always true. A device name is a real anchor, unlike a bare handle."""
        return True

    def matches(self, other: Any) -> bool:
        if isinstance(other, DisplayIdentity):
            return self.display_id == other.display_id
        if isinstance(other, dict):
            return self.display_id == str(other.get("display_id") or "")
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"display_id": self.display_id, "window_key": self.display_id}

    @classmethod
    def from_dict(cls, data: Any) -> "DisplayIdentity":
        if isinstance(data, DisplayIdentity):
            return data
        if isinstance(data, str):
            return cls(display_id=data)
        if isinstance(data, dict):
            value = data.get("display_id") or data.get("window_key")
            if value is not None:
                return cls(display_id=str(value))
        raise WindowsInvalidRequestError(
            "a display identity requires a display_id.")


@dataclass(frozen=True)
class DisplayInfo:
    """One display, as Windows currently reports it.

    Immutable, and free of native handles: everything here is a value that was
    copied out of native structures inside the enumeration callback.
    """

    identity: DisplayIdentity
    bounds: Rect
    work_area: Rect
    primary: bool = False
    dpi_x: int | None = None
    dpi_y: int | None = None
    scale_percent: int | None = None
    orientation: str | None = None
    dpi_virtualized: bool = False
    device_name: str = ""

    @property
    def display_id(self) -> str:
        return self.identity.display_id

    @property
    def size(self) -> Size:
        return self.bounds.size

    @property
    def width(self) -> int:
        return self.bounds.width

    @property
    def height(self) -> int:
        return self.bounds.height

    @property
    def key(self) -> str:
        return self.identity.key

    def contains(self, x: int, y: int) -> bool:
        return self.bounds.contains(x, y)

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_id": self.display_id,
            "device_name": self.device_name or self.display_id,
            "bounds": self.bounds.to_dict(),
            "work_area": self.work_area.to_dict(),
            "primary": self.primary,
            "width": self.width,
            "height": self.height,
            "size": self.size.to_dict(),
            "dpi_x": self.dpi_x,
            "dpi_y": self.dpi_y,
            "scale_percent": self.scale_percent,
            "orientation": self.orientation,
            # Present and True means "the numbers above describe a virtualised
            # desktop, not physical pixels". A caller that needs physical pixels
            # has to run per-monitor aware; silently rescaling is not this
            # layer's decision to make.
            "dpi_virtualized": self.dpi_virtualized,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DisplayInfo":
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError(
                "display information must be an object.")
        try:
            bounds = Rect.from_dict(data.get("bounds"))
            work = Rect.from_dict(data.get("work_area"))
            if bounds is None or work is None:
                raise ValueError("bounds and work_area are required")
            info = cls(
                identity=DisplayIdentity.from_dict(data),
                bounds=bounds,
                work_area=work,
                primary=bool(data.get("primary", False)),
                dpi_x=data.get("dpi_x"),
                dpi_y=data.get("dpi_y"),
                scale_percent=data.get("scale_percent"),
                orientation=data.get("orientation"),
                dpi_virtualized=bool(data.get("dpi_virtualized", False)),
                device_name=str(data.get("device_name") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WindowsInvalidRequestError(
                f"display information is malformed: {exc}") from exc
        if not isinstance(info.dpi_x, int):
            object.__setattr__(info, "dpi_x", None)
        if not isinstance(info.dpi_y, int):
            object.__setattr__(info, "dpi_y", None)
        return info


@dataclass(frozen=True)
class DisplayTarget:
    """A request to act on one specific display.

    Carries the display identity *and* the bounds the caller believed, so a
    re-resolution can say whether the display is still where it was. Reconfigured
    is not the same failure as missing, and conflating them would make a caller
    re-plan a layout change it could simply re-measure.
    """

    identity: DisplayIdentity
    bounds: Rect | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "DisplayTarget":
        if isinstance(data, DisplayTarget):
            return data
        if isinstance(data, str):
            # A bare device name is the only thing a string could plausibly be,
            # and accepting it keeps "the display you just listed" expressible
            # without wrapping it. It still resolves against live topology and
            # still refuses when the display is gone.
            return cls(identity=DisplayIdentity(display_id=data))
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError(
                "a display target must be an object with a display_id, or a "
                "display_id string.")
        identity = DisplayIdentity.from_dict(data)
        bounds = Rect.from_dict(data.get("bounds")) if data.get("bounds") else None
        return cls(identity=identity, bounds=bounds)

    def to_dict(self) -> dict[str, Any]:
        return {"display_id": self.identity.display_id,
                "bounds": self.bounds.to_dict() if self.bounds else None}

    def matches(self, info: DisplayInfo) -> bool:
        return self.identity.matches(info.identity)


# ══════════════════════════════════════════════════════ native


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


#: ``EnumDisplayMonitors`` callback signature: ``(HMONITOR, HDC, LPRECT, LPARAM)``.
#: Distinct from the struct type - declaring the callback as a pointer to
#: ``MONITORINFOEXW`` compiles but never receives the arguments, and the monitor
#: list comes back empty rather than raising.
_ENUM_DISPLAY_MONITORS_PROTO = ctypes.WINFUNCTYPE(
    ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


def _load_display_apis() -> dict[str, Any]:
    """Bind the Win32 entry points display topology needs.

    Every one is resolved from an already-loaded system library, so a host
    missing any of them gets a typed "unavailable" rather than an ``AttributeError``
    from deep inside a callback.
    """
    try:
        user32 = ctypes.windll.user32
    except OSError as exc:  # pragma: no cover - Windows only
        raise WindowsOperationFailedError(
            "user32 is unavailable on this host.") from exc

    shcore = getattr(ctypes.windll, "shcore", None)
    return {
        "user32": user32,
        "EnumDisplayMonitors": getattr(user32, "EnumDisplayMonitors", None),
        "GetMonitorInfoW": getattr(user32, "GetMonitorInfoW", None),
        "GetDpiForMonitor": getattr(shcore, "GetDpiForMonitor", None),
        "GetScaleFactorForMonitor": getattr(
            shcore, "GetScaleFactorForMonitor", None),
        "GetProcessDpiAwareness": getattr(
            shcore, "GetProcessDpiAwareness", None),
        "EnumDisplaySettingsW": getattr(user32, "EnumDisplaySettingsW", None),
    }


def displays_available() -> bool:
    """Whether this host can report display topology at all."""
    try:
        apis = _load_display_apis()
    except Exception:  # noqa: BLE001
        return False
    return bool(apis["EnumDisplayMonitors"] and apis["GetMonitorInfoW"])


def _dpi_awareness() -> int:
    """This process's DPI awareness, read from the OS.

    Reported rather than configured. Changing it would alter how every other
    window call in this codebase is virtualised, which is a much larger decision
    than a display foundation should make on its own.
    """
    try:
        apis = _load_display_apis()
        getter = apis.get("GetProcessDpiAwareness")
        if getter is None:
            return DPI_AWARENESS_UNAWARE
        value = ctypes.c_int(DPI_AWARENESS_UNAWARE)
        if getter(None, ctypes.byref(value)) != 0:
            # 0 == S_OK. Anything else means we could not find out, and claiming
            # "unaware" would be a guess in the one direction that fabricates DPI.
            return DPI_AWARENESS_UNAWARE
        return int(value.value)
    except Exception:  # noqa: BLE001
        return DPI_AWARENESS_UNAWARE


def _monitor_dpi(apis: dict[str, Any], hmonitor: int,
                 aware: bool) -> tuple[int | None, int | None, int | None,
                                       bool]:
    """``(dpi_x, dpi_y, scale_percent, virtualized)`` for one monitor.

    The scale factor is asked for first and is the number that can be trusted
    across awareness modes. The effective DPI is returned only when the process
    is per-monitor aware, because otherwise Windows answers with the virtualised
    96 regardless of the panel.
    """
    scale: int | None = None
    scale_getter = apis.get("GetScaleFactorForMonitor")
    if scale_getter is not None:
        value = ctypes.c_int(0)
        try:
            if scale_getter(wintypes.HMONITOR(hmonitor),
                            ctypes.byref(value)) == 0 and value.value > 0:
                scale = int(value.value)
        except Exception:  # noqa: BLE001 - optional capability
            scale = None

    if not aware:
        return None, None, scale, True

    dpi_getter = apis.get("GetDpiForMonitor")
    if dpi_getter is None:
        return None, None, scale, False
    x = ctypes.c_uint(0)
    y = ctypes.c_uint(0)
    try:
        # MDT_EFFECTIVE_DPI == 0
        if dpi_getter(wintypes.HMONITOR(hmonitor), ctypes.c_uint(0),
                      ctypes.byref(x), ctypes.byref(y)) != 0:
            return None, None, scale, False
    except Exception:  # noqa: BLE001 - optional capability
        return None, None, scale, False
    if x.value == 0 or y.value == 0:
        return None, None, scale, False
    return int(x.value), int(y.value), scale, False


#: ``dmDisplayOrientation`` sits at this offset in ``DEVMODEW``. Reading a fixed
#: offset rather than declaring the whole structure is deliberate: ``DEVMODEW``
#: has grown fields across Windows releases, and a wrong offset is a wrong answer
#: rather than a crash. If the buffer cannot be trusted the orientation is reported
#: as unavailable instead.
_DEVMODE_DISPLAY_ORIENTATION_OFFSET = 84
_ORIENTATIONS = {0: "landscape", 1: "portrait", 2: "landscape_flipped",
                 3: "portrait_flipped"}


def _monitor_orientation(apis: dict[str, Any], device: str) -> str | None:
    """Display orientation, when Windows reports it for this device.

    ``None`` when unavailable. A rotation guess from the aspect ratio would be a
    fabricated measurement, so it is not offered.
    """
    setter = apis.get("EnumDisplaySettingsW")
    if setter is None or not device:
        return None
    try:
        user32 = apis["user32"]
        ctypes.windll.user32.EnumDisplaySettingsW.restype = ctypes.c_bool
    except Exception:  # noqa: BLE001
        return None
    try:
        buffer = ctypes.create_string_buffer(220)
        ok = setter(ctypes.c_wchar_p(device), ctypes.c_uint(-1), buffer)
    except Exception:  # noqa: BLE001
        return None
    del user32
    if not ok:
        return None
    raw = buffer.raw
    if len(raw) <= _DEVMODE_DISPLAY_ORIENTATION_OFFSET + 3:
        return None
    value = int.from_bytes(
        raw[_DEVMODE_DISPLAY_ORIENTATION_OFFSET:
            _DEVMODE_DISPLAY_ORIENTATION_OFFSET + 4], "little")
    return _ORIENTATIONS.get(value)


def _enumerate(apis: dict[str, Any] | None = None) -> list[DisplayInfo]:
    """Every connected display, read entirely inside the enumeration callback.

    The ``HMONITOR`` exists only for the duration of the callback call and is
    never placed on a returned object. That is the whole reason this function
    builds its own list of values instead of returning handles for a later pass.
    """
    apis = apis or _load_display_apis()
    enum = apis.get("EnumDisplayMonitors")
    info_getter = apis.get("GetMonitorInfoW")
    if enum is None or info_getter is None:
        raise WindowsOperationFailedError(
            "this host does not expose display enumeration (EnumDisplayMonitors "
            "/ GetMonitorInfoW).",
            diagnostics={"enum_display_monitors": enum is not None,
                         "get_monitor_info": info_getter is not None})

    aware = _dpi_awareness() == DPI_AWARENESS_PER_MONITOR
    collected: list[DisplayInfo] = []
    failure: list[str] = []

    def _visit(hmonitor, _hdc, _rect, _data) -> bool:
        try:
            monitor = _MONITORINFOEXW()
            monitor.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if not info_getter(wintypes.HMONITOR(hmonitor),
                               ctypes.byref(monitor)):
                failure.append(f"GetMonitorInfoW refused "
                               f"0x{int(hmonitor):X}")
                return True
            device = str(monitor.szDevice or "")
            bounds = Rect(left=int(monitor.rcMonitor.left),
                          top=int(monitor.rcMonitor.top),
                          right=int(monitor.rcMonitor.right),
                          bottom=int(monitor.rcMonitor.bottom))
            work = Rect(left=int(monitor.rcWork.left),
                        top=int(monitor.rcWork.top),
                        right=int(monitor.rcWork.right),
                        bottom=int(monitor.rcWork.bottom))
            dpi_x, dpi_y, scale, virtualized = _monitor_dpi(
                apis, int(hmonitor), aware)
            collected.append(DisplayInfo(
                identity=DisplayIdentity(display_id=device),
                bounds=bounds, work_area=work,
                primary=bool(int(monitor.dwFlags) & 1),
                dpi_x=dpi_x, dpi_y=dpi_y, scale_percent=scale,
                orientation=_monitor_orientation(apis, device),
                dpi_virtualized=virtualized,
                device_name=device,
            ))
        except Exception as exc:  # noqa: BLE001
            # A ctypes callback swallows exceptions, so the failure is recorded
            # and re-raised by the caller. Letting it vanish here would silently
            # shorten the display list, and a short list looks like a valid
            # answer about the machine's topology.
            failure.append(f"{type(exc).__name__}: {exc}")
        return True

    try:
        callback = _ENUM_DISPLAY_MONITORS_PROTO(_visit)
        if not enum(None, None, callback, 0):
            raise WindowsOperationFailedError(
                "EnumDisplayMonitors reported failure while reading display "
                "topology.",
                diagnostics={"failures": list(failure)})
    finally:
        del callback

    if not collected:
        raise WindowsOperationFailedError(
            "Windows reported no connected displays, so there is no topology to "
            "report.",
            diagnostics={"failures": list(failure)})
    return collected


def read_virtual_desktop(displays: list[DisplayInfo] | None = None) -> VirtualScreen:
    """The bounding rectangle of every connected display.

    **This is not the same rectangle as ``SM_CXVIRTUALSCREEN``, and the
    difference is real.** Windows reports the system metric as the span of the
    desktop including any *gap* between two monitors, so a machine with a display
    above-and-left of another and nothing between them gets a metric rectangle
    covering pixels that no display occupies. The union of the display rectangles
    here covers only pixels that exist.

    Both rectangles are correct answers to different questions:

    * this one answers "where are the pixels I can capture", and
    * the system metric answers "where can the pointer be", because Windows
      clamps the pointer to the desktop rather than to any one display.

    So :mod:`core.windows.input` deliberately keeps using the system metric, and
    this function is used for topology and capture. Unifying them would have
    meant either letting the pointer be validated against a smaller rectangle than
    Windows will actually let it reach, or letting a capture cover a gap that has
    no pixels in it. :func:`assert_consistent` exists to keep the two honest
    about each other.

    Negative coordinates are preserved exactly. A second monitor to the left of
    the primary is not an error to be normalised away; it is the normal case
    Win32 callers have to handle.
    """
    if displays is None:
        displays = _enumerate()
    if not displays:
        raise WindowsOperationFailedError(
            "no displays were available to derive the virtual desktop from.")
    left = min(d.bounds.left for d in displays)
    top = min(d.bounds.top for d in displays)
    right = max(d.bounds.right for d in displays)
    bottom = max(d.bounds.bottom for d in displays)
    return VirtualScreen(left=left, top=top, width=right - left,
                         height=bottom - top)


def assert_consistent(displays: list[DisplayInfo]) -> None:
    """Log if the display union and the pointer's metric rectangle disagree.

    They legitimately differ when there is a gap between monitors, so this is a
    log line rather than an error. It exists because a *difference with no gap to
    explain it* would mean one of the two readings is wrong, and the only way to
    notice that is to compare them while they are both in hand.
    """
    try:
        import win32api
        import win32con
    except ImportError:  # pragma: no cover - Windows only
        return
    try:
        metric = VirtualScreen(
            left=int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)),
            top=int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)),
            width=int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)),
            height=int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)),
        )
    except Exception:  # noqa: BLE001
        return
    union = read_virtual_desktop(displays)
    if metric == union:
        return
    # Overlapping or inset unions can still enclose the same desktop; only a
    # genuine mismatch is worth a line.
    if (union.contains(metric.left, metric.top)
            and union.contains(metric.right - 1, metric.bottom - 1)
            and metric.contains(union.left, union.top)
            and metric.contains(union.right - 1, union.bottom - 1)):
        return
    log.warning(
        "display union %s and the pointer's virtual-screen metric %s disagree "
        "with no gap to explain it; capture will use the union and pointer "
        "validation uses the metric.",
        union.to_dict(), metric.to_dict())


# ══════════════════════════════════════════════════════ the layer


class DisplayLayer:
    """Read-only display topology, plus the conversions that depend on it."""

    def __init__(self, *, default_timeout: float = 5.0) -> None:
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _guard(self) -> None:
        if self._closed:
            raise WindowsOperationFailedError("the display layer is closed.")

    # ------------------------------------------------------------- topology

    def dpi_awareness(self) -> dict[str, Any]:
        """How this process is configured to see DPI, and what follows from it."""
        self._guard()
        awareness = _dpi_awareness()
        return {
            "awareness": DPI_AWARENESS_LABELS.get(awareness, "unknown"),
            "virtualized": awareness != DPI_AWARENESS_PER_MONITOR,
            "note": (
                "This process is not per-monitor DPI aware, so Windows "
                "virtualises the desktop: bounds and the effective DPI describe "
                "the virtualised desktop rather than physical pixels. The scale "
                "factor is still reported truthfully."
                if awareness != DPI_AWARENESS_PER_MONITOR else
                "This process is per-monitor DPI aware, so bounds and DPI "
                "describe physical pixels."),
        }

    def list_displays(self, *, cancelled=None) -> list[DisplayInfo]:
        """Every connected display, in whatever order Windows reports them.

        Order carries no meaning and nothing downstream may depend on it: a
        caller that wants the primary asks for the primary.
        """
        self._guard()
        _check_cancelled(cancelled)
        displays = _enumerate()
        assert_consistent(displays)
        _check_cancelled(cancelled)
        return displays

    def primary_display(self, *, cancelled=None) -> DisplayInfo:
        """The display Windows flags as primary.

        Taken from ``MONITORINFOF_PRIMARY``, never from position, ordering or
        size. A primary that is not leftmost or not largest is completely normal,
        and inferring it from geometry is how a machine with the primary on the
        right gets the wrong answer.
        """
        displays = self.list_displays(cancelled=cancelled)
        primaries = [d for d in displays if d.primary]
        if len(primaries) == 1:
            return primaries[0]
        if not primaries:
            # Structured failure rather than a guess. "First display" would be a
            # fabricated answer, and the caller would go on to target it.
            raise DisplayNotFoundError(
                "Windows reported no primary display among "
                f"{len(displays)} connected display(s).",
                diagnostics={"display_ids": [d.display_id for d in displays]})
        raise WindowsOperationFailedError(
            f"Windows reported {len(primaries)} primary displays, which cannot "
            "be resolved to one.",
            diagnostics={"primary_display_ids":
                         [d.display_id for d in primaries]})

    def virtual_desktop(self, *, cancelled=None) -> VirtualScreen:
        """The bounding rectangle of every connected display.

        Goes through :meth:`list_displays` rather than enumerating again, so
        there is one read path: one cancellation contract, one awareness check,
        and one topology for a caller that asks two questions in a row.
        """
        self._guard()
        _check_cancelled(cancelled)
        screen = read_virtual_desktop(self.list_displays(cancelled=cancelled))
        _check_cancelled(cancelled)
        return screen

    # ------------------------------------------------------------- targeting

    def get_display(self, target: Any, *, cancelled=None) -> DisplayInfo:
        """Resolve a display target against live topology.

        Re-reads rather than trusting remembered bounds, and refuses to invent a
        substitute. Falling back to the primary display when a named one is
        missing would send the caller's work to a screen nobody asked about.
        """
        self._guard()
        wanted = DisplayTarget.from_dict(target)
        _check_cancelled(cancelled)
        displays = self.list_displays(cancelled=cancelled)
        for info in displays:
            if wanted.matches(info):
                _check_cancelled(cancelled)
                return info
        raise DisplayNotFoundError(
            f"no connected display is named {wanted.identity.display_id!r}; it "
            f"may have been disconnected.",
            diagnostics={
                "requested": wanted.identity.display_id,
                "connected_display_ids": [d.display_id for d in displays],
            })

    def resolve_target(self, target: Any, *, cancelled=None) -> dict[str, Any]:
        """Resolve a target and report whether the display moved since it was named.

        The reconfigured flag is the useful part. Silently returning the new
        bounds would be right by accident; flagging it lets a caller whose
        layout assumptions just broke know that, instead of discovering it later
        as a mis-aimed click.
        """
        self._guard()
        wanted = DisplayTarget.from_dict(target)
        info = self.get_display(wanted, cancelled=cancelled)
        payload = {"display": info.to_dict(), "display_id": info.display_id,
                   "resolved": True, "reconfigured": False}
        if wanted.bounds is not None and wanted.bounds != info.bounds:
            payload["reconfigured"] = True
            payload["requested_bounds"] = wanted.bounds.to_dict()
            payload["current_bounds"] = info.bounds.to_dict()
        return payload

    def display_at(self, x: int, y: int, *, cancelled=None) -> DisplayInfo:
        """The display containing a virtual-desktop point.

        Raises when the point is in a gap or outside every display. That is a real
        configuration (``B`` above ``A`` with nothing between them) and answering
        "the nearest one" would send a caller to a screen their point is not on.
        """
        self._guard()
        point = (int(x), int(y))
        _check_cancelled(cancelled)
        displays = self.list_displays(cancelled=cancelled)
        matches = [d for d in displays if d.contains(*point)]
        if len(matches) == 1:
            _check_cancelled(cancelled)
            return matches[0]
        desktop = read_virtual_desktop(displays)
        if not matches:
            raise WindowsInvalidRequestError(
                f"({x}, {y}) is not on any connected display; it is outside the "
                f"virtual desktop {desktop.to_dict()} or inside a gap between "
                f"displays.",
                diagnostics={"x": int(x), "y": int(y),
                             "virtual_desktop": desktop.to_dict(),
                             "display_ids": [d.display_id for d in displays]})
        # Overlapping displays are not something a sane configuration produces,
        # but Windows can report them after a hot-plug. Say so rather than
        # picking one and presenting it as unambiguous.
        raise WindowsInvalidRequestError(
            f"({x}, {y}) falls inside {len(matches)} connected displays; the "
            "point is ambiguous.",
            diagnostics={"x": int(x), "y": int(y),
                         "display_ids": [d.display_id for d in matches]})

    # ------------------------------------------------------------ conversion

    def to_screen_point(self, display: Any, relative_x: int,
                        relative_y: int, *, cancelled=None) -> tuple[int, int]:
        """Convert a display-relative point to virtual-desktop coordinates.

        Refuses when the result lands outside the display. Silently clamping
        would return a point the caller never asked for - and then a click or a
        capture would quietly happen somewhere else.
        """
        self._guard()
        info = (display if isinstance(display, DisplayInfo)
                else self.get_display(display, cancelled=cancelled))
        _check_cancelled(cancelled)
        x = int(info.bounds.left) + int(relative_x)
        y = int(info.bounds.top) + int(relative_y)
        if not info.bounds.contains(x, y):
            raise WindowsInvalidRequestError(
                f"relative ({relative_x}, {relative_y}) resolves to ({x}, {y}), "
                f"which is outside display {info.display_id!r} "
                f"{info.bounds.to_dict()}.",
                diagnostics={"display_id": info.display_id,
                             "bounds": info.bounds.to_dict(),
                             "relative": [int(relative_x), int(relative_y)],
                             "resolved": [x, y]})
        _check_cancelled(cancelled)
        return x, y

    def to_display_point(self, display: Any, x: int,
                         y: int, *, cancelled=None) -> tuple[int, int]:
        """Convert a virtual-desktop point to display-relative coordinates."""
        self._guard()
        info = (display if isinstance(display, DisplayInfo)
                else self.get_display(display, cancelled=cancelled))
        _check_cancelled(cancelled)
        if not info.bounds.contains(int(x), int(y)):
            raise WindowsInvalidRequestError(
                f"({x}, {y}) is not on display {info.display_id!r} "
                f"{info.bounds.to_dict()}.",
                diagnostics={"display_id": info.display_id,
                             "bounds": info.bounds.to_dict(),
                             "point": [int(x), int(y)]})
        _check_cancelled(cancelled)
        return int(x) - int(info.bounds.left), int(y) - int(info.bounds.top)

    def describe(self, *, cancelled=None) -> dict[str, Any]:
        """The whole topology in one payload, for a caller that needs it at once."""
        self._guard()
        displays = self.list_displays(cancelled=cancelled)
        desktop = read_virtual_desktop(displays)
        try:
            primary = next(d for d in displays if d.primary)
            primary_id = primary.display_id
        except StopIteration:
            primary_id = ""
        return {
            "displays": [d.to_dict() for d in displays],
            "display_ids": [d.display_id for d in displays],
            "primary_display_id": primary_id,
            "virtual_desktop": desktop.to_dict(),
            "virtual_screen": desktop.to_dict(),
            "count": len(displays),
            "dpi_awareness": self.dpi_awareness(),
            "coordinate_space": "virtual_desktop",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }


def _check_cancelled(cancelled) -> None:
    """Cancellation check that mirrors the window layer's contract.

    A callable returning true means "stop". Deliberately not wired to a
    ``CancellationToken`` instance here: the layer takes any checker, and the
    capability layer is what binds the request's token to it.
    """
    if cancelled is not None and callable(cancelled) and cancelled():
        from .errors import WindowsCancelledError

        raise WindowsCancelledError(
            "cancelled before the display operation completed.",
            diagnostics={"cancellation_terminated": False})


def _coerce_displays(value: Any) -> list[DisplayInfo]:
    """Accept ``DisplayInfo`` objects or their dict form, uniformly."""
    if isinstance(value, DisplayInfo):
        return [value]
    if isinstance(value, dict):
        return [DisplayInfo.from_dict(value)]
    if isinstance(value, (list, tuple)):
        return [v if isinstance(v, DisplayInfo) else DisplayInfo.from_dict(v)
                for v in value]
    raise WindowsInvalidRequestError(
        f"expected display information, got {type(value).__name__}.")