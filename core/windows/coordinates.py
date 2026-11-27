"""Target-aware coordinates: a coordinate alone is not a target.

M2.16.2. ``click(x, y)`` is not a safe interface, because ``(x, y)`` names a
point on a screen and not a *thing*. By the time input reaches Windows, the
foreground window may have changed, the window may have moved, and the point may
now lie over a different application entirely. A caller that supplied bare
coordinates has no way to notice.

So every coordinate here carries the target it was meant for:

* :class:`ScreenPoint` - absolute screen coordinates, explicitly *unanchored*.
  Accepted, but the caller is recorded as having claimed no target, and the
  result says so.
* :class:`WindowPoint` - window-relative coordinates plus a
  :class:`~core.windows.window_models.WindowIdentity`. Resolution re-reads the
  window and re-verifies the identity immediately before use, so a stale rect
  cannot be applied to a recycled handle.

Resolution is deliberately strict. Bounds are read fresh, the identity is checked,
the resulting screen point must fall inside the window it was resolved against,
and a minimized window is refused outright - clicking coordinates belonging to a
minimized window would land wherever its rectangle happens to sit, which is
somewhere on the desktop that is not the target.

The resolved point records *both* the target it was computed for and the window
state it was computed from, so a later stage can re-check that the click still
belongs to the window the caller named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import (
    WindowsInvalidRequestError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from .window_models import WindowIdentity

__all__ = [
    "ScreenPoint", "WindowPoint", "ResolvedPoint", "VirtualScreen",
    "Rect", "Size", "Point", "require_anchored",
]


# ══════════════════════════════════════════════════════════ geometry


@dataclass(frozen=True)
class Size:
    """A width and a height, in pixels of one named coordinate space.

    Carries no origin. A size on its own says nothing about *where* it is, which
    is the same reason :class:`Rect` keeps an explicit origin rather than storing
    a size and a position separately.
    """

    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"width": int(self.width), "height": int(self.height)}


@dataclass(frozen=True)
class Point:
    """An ``(x, y)`` in one named coordinate space."""

    x: int
    y: int

    def to_dict(self) -> dict[str, int]:
        return {"x": int(self.x), "y": int(self.y)}


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle with **exclusive** right and bottom edges.

    This is the Win32 convention (``GetWindowRect``, ``GetMonitorInfo``,
    ``SM_CXVIRTUALSCREEN``) and getting it wrong is an off-by-one that silently
    admits a coordinate one pixel outside the region. So it is stated once, here,
    and every containment test in this codebase agrees:

        width  = right - left
        height = bottom - top
        valid: left <= x < right  and  top <= y < bottom

    A 100x100 rectangle therefore contains ``(99, 99)`` and **not** ``(100, 100)``.

    ``is_empty`` is kept separate from ``is_valid`` because Windows can report
    geometry that is genuinely impossible - a zero or negative extent on a
    display, or a rectangle whose corners arrive inverted. Those are reported
    rather than normalised into something plausible: silently "fixing" native
    state would make a broken configuration look like a working one.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return int(self.right) - int(self.left)

    @property
    def height(self) -> int:
        return int(self.bottom) - int(self.top)

    @property
    def size(self) -> Size:
        return Size(width=self.width, height=self.height)

    @property
    def origin(self) -> Point:
        return Point(x=int(self.left), y=int(self.top))

    @property
    def is_empty(self) -> bool:
        """A rectangle covering no pixels at all."""
        return self.width <= 0 or self.height <= 0

    @property
    def is_valid(self) -> bool:
        """A rectangle covering at least one pixel, with ordered corners."""
        return (int(self.right) >= int(self.left)
                and int(self.bottom) >= int(self.top))

    def contains(self, x: int, y: int) -> bool:
        """Whether a point in the same space lies inside. Exclusive far edges."""
        return (int(self.left) <= int(x) < int(self.right)
                and int(self.top) <= int(y) < int(self.bottom))

    def contains_point(self, point: Point) -> bool:
        return self.contains(point.x, point.y)

    def intersection(self, other: "Rect") -> "Rect | None":
        """The overlapping region, or ``None`` when they do not overlap.

        Returns ``None`` rather than a zero-size rectangle so that "these do not
        overlap" cannot be mistaken for "they overlap at exactly one pixel".
        """
        left = max(int(self.left), int(other.left))
        top = max(int(self.top), int(other.top))
        right = min(int(self.right), int(other.right))
        bottom = min(int(self.bottom), int(other.bottom))
        if right <= left or bottom <= top:
            return None
        return Rect(left=left, top=top, right=right, bottom=bottom)

    def offset(self, dx: int, dy: int) -> "Rect":
        """The same rectangle translated within its own coordinate space.

        Named rather than done with a helper elsewhere so that translating a
        *monitor-relative* rectangle into *screen* coordinates is a visible,
        greppable conversion and not an arithmetic trick performed inline.
        """
        return Rect(left=int(self.left) + int(dx), top=int(self.top) + int(dy),
                    right=int(self.right) + int(dx),
                    bottom=int(self.bottom) + int(dy))

    def translated_from(self, origin: "Rect | Point | tuple[int, int]") -> "Rect":
        """This rectangle re-expressed relative to another rectangle's origin.

        The inverse of :meth:`offset`, and the only sanctioned way to turn
        monitor-relative geometry into screen geometry. Going through one named
        function is what stops a relative coordinate being *reused* as an absolute
        one - the provenance laundering M2.16.2 exists to prevent.
        """
        if isinstance(origin, Rect):
            dx, dy = int(origin.left), int(origin.top)
        elif isinstance(origin, Point):
            dx, dy = int(origin.x), int(origin.y)
        else:
            dx, dy = int(origin[0]), int(origin[1])
        return self.offset(-dx, -dy)

    def to_dict(self) -> dict[str, int]:
        return {"left": int(self.left), "top": int(self.top),
                "right": int(self.right), "bottom": int(self.bottom),
                "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: Any) -> "Rect | None":
        """Rebuild from the dict form, or ``None`` when it is unusable.

        ``None`` rather than a defaulted rectangle: guessing geometry would put a
        fabricated region into a capture request.
        """
        if not isinstance(data, dict):
            return None
        try:
            return cls(left=int(data["left"]), top=int(data["top"]),
                       right=int(data["right"]), bottom=int(data["bottom"]))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class VirtualScreen:
    """The union of every display, in virtual-desktop coordinates.

    This is deliberately *not* the primary monitor. A machine with a second
    display has a virtual screen larger than - and offset from - the primary, and
    point validation against the primary alone would reject valid positions on the
    second display and accept invalid ones above it.
    """

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        # ``right``/``bottom`` are one past the last valid coordinate, not a valid
        # coordinate themselves. ``SM_CXVIRTUALSCREEN`` is a *width*, so the last
        # addressable pixel is ``left + width - 1``; accepting ``left + width``
        # would admit a position Windows clamps back to the edge.
        return (self.left <= x < self.right) and (self.top <= y < self.bottom)

    @property
    def rect(self) -> Rect:
        """The same region as a :class:`Rect`, with the same exclusive edges."""
        return Rect(left=int(self.left), top=int(self.top),
                    right=int(self.left) + int(self.width),
                    bottom=int(self.top) + int(self.height))

    @classmethod
    def from_rect(cls, rect: Rect) -> "VirtualScreen":
        return cls(left=int(rect.left), top=int(rect.top),
                   width=int(rect.width), height=int(rect.height))

    def to_dict(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: Any) -> "VirtualScreen | None":
        """Rebuild from the dict form, or ``None`` when it is unusable.

        Returns ``None`` rather than a defaulted rectangle: guessing monitor
        geometry would validate coordinates against a screen that does not exist.
        """
        if not isinstance(data, dict):
            return None
        try:
            return cls(int(data["left"]), int(data["top"]),
                       int(data["width"]), int(data["height"]))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class ScreenPoint:
    """Absolute screen coordinates with no target attached.

    Kept because some operations genuinely are absolute - "move the pointer to
    the middle of the screen" needs no window - but always labelled
    ``anchored=False`` so a caller can tell the difference between "the user asked
    for this point" and "this point is inside a verified target".
    """

    x: int
    y: int

    anchored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"mode": "screen", "x": int(self.x), "y": int(self.y),
                "anchored": False}

    @classmethod
    def from_dict(cls, data: Any) -> "ScreenPoint":
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError("a screen point must be an object.")
        try:
            return cls(x=int(data["x"]), y=int(data["y"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise WindowsInvalidRequestError(
                "a screen point requires integer x and y."
            ) from exc


@dataclass(frozen=True)
class WindowPoint:
    """Coordinates relative to a named window's top-left corner.

    Relative rather than absolute so the request survives the window moving: the
    point is resolved against freshly-read bounds immediately before use.
    """

    identity: WindowIdentity | dict
    relative_x: int
    relative_y: int

    def to_dict(self) -> dict[str, Any]:
        identity = self.identity
        payload = identity.to_dict() if isinstance(identity, WindowIdentity) \
            else dict(identity or {})
        return {
            "mode": "window",
            "window": payload,
            "relative_x": int(self.relative_x),
            "relative_y": int(self.relative_y),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WindowPoint":
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError("a window point must be an object.")
        window = data.get("window")
        if not isinstance(window, (dict, WindowIdentity)):
            raise WindowsInvalidRequestError(
                "a window point requires a window identity.")
        if isinstance(window, WindowIdentity):
            identity = window
        else:
            identity = WindowIdentity.from_dict(window)
        try:
            return cls(identity=identity,
                       relative_x=int(data["relative_x"]),
                       relative_y=int(data["relative_y"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise WindowsInvalidRequestError(
                "a window point requires integer relative_x and relative_y."
            ) from exc


@dataclass(frozen=True)
class ResolvedPoint:
    """A screen point resolved against a verified, freshly-read target.

    Carries the provenance so a later stage can confirm the input still belongs to
    the window the caller named rather than to whatever is on top by then, and the
    identity itself so foreground can be re-checked without the caller having to
    send it twice.
    """

    x: int
    y: int
    anchored: bool = False
    window_key: str = ""
    window_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    verified: bool = False
    identity: WindowIdentity | None = None

    @property
    def verified_target(self) -> bool:
        """Whether this point was resolved against a confirmed window."""
        return self.anchored and self.verified and self.identity is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": int(self.x),
            "y": int(self.y),
            "anchored": self.anchored,
            "verified_target": self.verified_target,
            "window_key": self.window_key,
            "window_rect": list(self.window_rect),
        }


def coerce_point(raw: Any) -> ScreenPoint | WindowPoint:
    """Parse a caller-supplied point in either supported mode.

    The ``mode`` field is authoritative. Inferring it from which keys happen to be
    present would let a caller smuggle a window identity in and have it silently
    treated as absolute coordinates - losing the anchoring exactly when it was
    most wanted.
    """
    if isinstance(raw, (ScreenPoint, WindowPoint)):
        return raw
    if not isinstance(raw, dict):
        raise WindowsInvalidRequestError(
            "a target must be a screen point {mode: 'screen', x, y} or a window "
            "point {mode: 'window', window: {...}, relative_x, relative_y}.")
    mode = str(raw.get("mode") or "").strip().lower()
    if mode == "screen":
        return ScreenPoint.from_dict(raw)
    if mode == "window":
        return WindowPoint.from_dict(raw)
    raise WindowsInvalidRequestError(
        "a target must declare mode 'screen' or mode 'window' explicitly; it is "
        "not inferred from the keys present, because inferring it would discard "
        "the window anchoring."
    )


def resolve_point(raw: Any, *, reader, virtual_screen: VirtualScreen | None,
                  require_foreground: bool = False,
                  observed_foreground_key: str = "",
                  ) -> ResolvedPoint:
    """Turn a caller point into a verified screen coordinate.

    ``reader`` must be ``WindowLayer.get_window``: the identity check and the
    bounds read happen inside it, immediately before this returns, so the rect used
    is the window's current geometry rather than a cached one.

    Ordering is the safety property. Verify identity, read bounds, then compute -
    never the reverse - because a window can move or be replaced between any two
    of those steps and the caller has no way to detect that.
    """
    point = coerce_point(raw)

    if isinstance(point, ScreenPoint):
        x, y = int(point.x), int(point.y)
        if virtual_screen is not None and not virtual_screen.contains(x, y):
            raise WindowsInvalidRequestError(
                f"({x}, {y}) is outside the virtual screen "
                f"{virtual_screen.to_dict()}.",
                diagnostics={"x": x, "y": y,
                             "virtual_screen": virtual_screen.to_dict()})
        return ResolvedPoint(x=x, y=y, anchored=False, verified=False)

    # Window-relative: the identity is verified and the bounds read fresh.
    wanted = point.identity if isinstance(point.identity, WindowIdentity) \
        else WindowIdentity.from_dict(point.identity)
    if not wanted.anchored:
        # Defence in depth. The window layer refuses these too, but this
        # function is reachable from more than one caller and an unanchored
        # window has nothing to verify a re-read against.
        raise WindowsWindowIdentityMismatchError(
            "the target window identity is not anchored to a process creation "
            "time, so it cannot be verified before input is dispatched.",
            diagnostics={"requested": wanted.to_dict()},
        )
    current = reader(wanted)          # raises NOT_FOUND / IDENTITY_MISMATCH
    if not current.identity.anchored:
        # Defence in depth on the *observed* side too. ``matches`` would already
        # return False for an unanchored window, but returning an unresolved
        # point and marking it unverified is weaker than refusing: a point with no
        # verifiable target should never exist.
        raise WindowsWindowIdentityMismatchError(
            "the re-read window is not anchored to a process creation time, so "
            "the requested coordinates cannot be resolved against it.",
            diagnostics={"requested": wanted.to_dict(),
                         "observed": current.identity.to_dict()},
        )

    if current.minimized:
        raise WindowsInvalidRequestError(
            "the target window is minimized, so its coordinates cannot be "
            "targeted; restore it first.",
            diagnostics={"window_key": wanted.key,
                         "minimized": True})

    left, top, right, bottom = (int(v) for v in current.rect)
    if right <= left or bottom <= top:
        raise WindowsInvalidRequestError(
            "the target window reports empty bounds, so its coordinates cannot "
            "be targeted.",
            diagnostics={"window_key": wanted.key, "rect": list(current.rect)})

    x = left + int(point.relative_x)
    y = top + int(point.relative_y)

    # Inside the window that was actually read, not the one the caller believed.
    # Exclusive at the far edge: ``GetWindowRect`` reports a right/bottom one past
    # the last covered pixel, so ``left <= x <= right`` would accept a relative
    # coordinate of ``width``, which is outside the window.
    if not (left <= x < right and top <= y < bottom):
        raise WindowsInvalidRequestError(
            f"relative ({point.relative_x}, {point.relative_y}) resolves to "
            f"({x}, {y}), which is outside the target window's current bounds "
            f"{[left, top, right, bottom]}.",
            diagnostics={"relative": [point.relative_x, point.relative_y],
                         "resolved": [x, y],
                         "rect": [left, top, right, bottom],
                         "window_key": wanted.key})

    if require_foreground and observed_foreground_key != current.identity.key:
        raise WindowsInvalidRequestError(
            "the target window is not the foreground window, so input would not "
            "reach it.",
            diagnostics={"window_key": current.identity.key,
                         "foreground_window_key": observed_foreground_key})

    return ResolvedPoint(
        x=x, y=y,
        anchored=True,
        window_key=current.identity.key,
        window_rect=(left, top, right, bottom),
        verified=current.identity.matches(wanted),
        identity=current.identity,
    )


def require_anchored(point: ResolvedPoint | None) -> str:
    """The window key of a resolved point, or a refusal.

    Input that changes the machine must name a verified target. An unanchored
    point is refused rather than dispatched, because "the user asked to click at
    (400, 300)" is not enough information to have meant any particular window.

    ``None`` is refused the same way. A keyboard verb takes a target purely to
    prove where the keystroke is going, so a caller passing nothing there has not
    named a target either, and letting it reach an attribute lookup would report a
    ``TypeError`` rather than the reason the request was turned down.
    """
    if point is None or not getattr(point, "verified_target", False):
        raise WindowsInvalidRequestError(
            "input must name a target window; absolute screen coordinates alone "
            "cannot be verified against anything before dispatch.",
            diagnostics=(point.to_dict() if isinstance(point, ResolvedPoint)
                         else {"target": None}),
        )
    return point.window_key


_ = (WindowsWindowNotFoundError, WindowsWindowIdentityMismatchError)