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

__all__ = ["ScreenPoint", "WindowPoint", "ResolvedPoint", "VirtualScreen"]


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