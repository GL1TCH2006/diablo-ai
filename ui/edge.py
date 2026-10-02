"""M2.16.UI - edge docking geometry, monitor selection and DPI scaling.

All arithmetic lives here so it can be tested without a display, a window, or a
toolkit. ``ui/edge_companion.py`` asks for a rectangle and draws it.

The placement rules
===================

**Anchor to the monitor the user is looking at, not to the primary one.** Windows
reports a primary display, but a user with two monitors generally wants the
companion where their cursor is. :func:`select_monitor` prefers the display
containing a supplied point (normally the cursor), and falls back to primary only
when no point is available.

**Dock by geometry, not by ``geometry()`` strings.** Tk's ``"+x+y"`` is virtual
desktop coordinates on Windows only if the window is not DPI-virtualised, and this
process is not DPI-aware (see ``DisplayLayer.dpi_awareness``). So rectangles are
computed from the M2.16.4 display abstraction and the companion is positioned in
*physical* pixels deliberately, rather than discovering a scale factor afterwards.

**Sleeping is a genuinely small footprint.** The retracted companion is a sliver
at the edge plus a small presence, not a shrunken full panel. Part 5 asks for low
interference; a 420x320 window "retracted" still covers content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "EdgeSide",
    "CompanionGeometry",
    "DockPlacement",
    "select_monitor",
    "place_at_edge",
    "clamp_visible",
]


class EdgeSide(str):
    """One of the four screen edges. A plain ``str`` subclass so it serialises."""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


#: DPI below which the companion would be unusable if scaled down, and above which
#: it would dominate a small monitor. Bounds the scale so a hostile or unexpected
#: DPI value cannot produce an invisible or screen-filling window.
_MIN_SCALE = 1.0
_MAX_SCALE = 2.0


@dataclass(frozen=True)
class CompanionGeometry:
    """The companion's sizes, in unscaled units.

    ``collapsed_*`` is the sleeping sliver plus its presence; ``expanded_*`` is the
    panel. Both are defined here so the view has no size policy of its own.
    """

    expanded_width: int = 420
    expanded_height: int = 340
    collapsed_width: int = 74
    collapsed_height: int = 34
    margin: int = 0

    @property
    def collapsed_visible_width(self) -> int:
        """How much of the collapsed companion sits inside the monitor.

        The rest hangs off the edge, which is what makes it read as *peeking*
        rather than as a small window sitting in a corner.
        """
        return min(self.collapsed_width, max(18, self.collapsed_width - 12))

    @property
    def collapsed_visible_height(self) -> int:
        """The top/bottom equivalent of :attr:`collapsed_visible_width`.

        Separate because the two axes are independent: a companion docked to the
        left shows a vertical sliver, one docked to the top shows a horizontal one.
        Using the width figure for a top dock left nothing reachable, because the
        collapsed height is smaller than the width strip.
        """
        return min(self.collapsed_height, max(14, self.collapsed_height - 10))


@dataclass(frozen=True)
class DockPlacement:
    """Where to put the window, and how it got there."""

    left: int
    top: int
    width: int
    height: int
    edge: str
    display_id: str = ""
    scale: float = 1.0
    #: Fraction of the collapsed companion still on screen. 1.0 while expanded.
    visible_ratio: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": int(self.left),
            "top": int(self.top),
            "width": int(self.width),
            "height": int(self.height),
            "edge": self.edge,
            "display_id": self.display_id,
            "scale": round(float(self.scale), 3),
            "visible_ratio": round(float(self.visible_ratio), 3),
        }

    def geometry_string(self) -> str:
        return f"{self.width}x{self.height}+{self.left}+{self.top}"


def _scale_for(display: Any) -> float:
    """A clamped scale from a ``DisplayInfo``, defaulting to 1.0."""
    scale = getattr(display, "scale_percent", None)
    try:
        percent = float(scale) if scale is not None else 100.0
    except (TypeError, ValueError):
        percent = 100.0
    if percent <= 0:
        percent = 100.0
    return max(_MIN_SCALE, min(_MAX_SCALE, percent / 100.0))


def select_monitor(displays: list[Any], *, point: tuple[int, int] | None = None,
                   fallback: str = "primary") -> Any:
    """Pick the display the companion should dock to.

    ``point`` is a virtual-desktop coordinate - normally the cursor. The display
    containing it wins. Without a point the ``fallback`` strategy applies:
    ``"primary"`` for the display Windows flags, or ``"first"`` for whatever is
    connected. Never returns ``None`` when any display exists.
    """
    usable = [d for d in displays or [] if getattr(d, "bounds", None) is not None]
    if not usable:
        return None
    if point is not None:
        try:
            x, y = int(point[0]), int(point[1])
        except (TypeError, ValueError, IndexError):
            x = y = None  # type: ignore[assignment]
        if x is not None:
            for display in usable:
                try:
                    if display.bounds.contains(x, y):
                        return display
                except Exception:  # noqa: BLE001 - a bad row must not break docking
                    continue
    if str(fallback) == "first":
        return usable[0]
    for display in usable:
        if bool(getattr(display, "primary", False)):
            return display
    return usable[0]


def place_at_edge(
    display: Any,
    edge: str = "right",
    *,
    peek: float = 1.0,
    geometry: CompanionGeometry | None = None,
) -> DockPlacement:
    """Compute the window rectangle for a partially-extended companion.

    ``peek`` runs 0.0 (fully retracted) to 1.0 (fully extended). The window keeps
    its full size at all times and is simply moved off the edge, because resizing
    on every frame is what makes a docked panel look like it is flickering.

    Rectangles are derived from the display's **work area**, not its bounds, so the
    companion never lands on the taskbar or another app's fullscreen edge.
    """
    geom = geometry or CompanionGeometry()
    side = str(edge or "right").strip().lower()
    if side not in ("left", "right", "top", "bottom"):
        side = "right"
    ratio = 0.0 if peek < 0.0 else (1.0 if peek > 1.0 else float(peek))
    scale = _scale_for(display)

    area = getattr(display, "work_area", None) or getattr(display, "bounds", None)
    if area is None:
        raise ValueError("the selected display has no usable rectangle")
    ax, ay = int(area.left), int(area.top)
    aw, ah = int(area.width), int(area.height)

    expanded_w = max(240, int(round(geom.expanded_width * scale)))
    expanded_h = max(160, int(round(geom.expanded_height * scale)))
    collapsed_w = max(24, int(round(geom.collapsed_width * scale)))
    collapsed_h = max(20, int(round(geom.collapsed_height * scale)))
    margin = int(geom.margin)

    # The window keeps a constant size per frame and is *moved* off the edge, rather
    # than resized on every frame - resizing during an animation is what makes a
    # docked panel read as flickering.
    width = collapsed_w + int(round((expanded_w - collapsed_w) * ratio))
    height = collapsed_h + int(round((expanded_h - collapsed_h) * ratio))
    width = min(width, max(1, aw))
    height = min(height, max(1, ah))

    # How much stays on screen when fully retracted. A companion retracted past
    # this point could not be clicked, so it is the floor rather than zero.
    #
    # Separate strips per axis: a companion docked left shows a *vertical* sliver,
    # one docked top shows a *horizontal* one. Reusing the width figure on a top dock
    # exceeds the collapsed height, which made the retraction distance zero and left
    # the companion fully visible while nominally asleep.
    side_edge = side in ("left", "right")
    side_strip = max(8, int(round(geom.collapsed_visible_width * scale)))
    axis_strip = max(8, int(round(geom.collapsed_visible_height * scale)))
    extent = width if side_edge else height
    strip = min(side_strip if side_edge else axis_strip, extent)

    ax, ay = int(area.left), int(area.top)
    aw, ah = int(area.width), int(area.height)
    centre_x = ax + (aw - width) // 2
    centre_y = ay + (ah - height) // 2

    # Retraction distance: how far outside the work area the window slides.
    retraction = max(0, extent - strip) * (1.0 - ratio)

    if side == "right":
        left = ax + aw - width + retraction
        top = centre_y
    elif side == "left":
        left = ax - retraction
        top = centre_y
    elif side == "bottom":
        left = centre_x
        top = ay + ah - height + retraction
    else:  # top
        left = centre_x
        top = ay - retraction

    placement = DockPlacement(
        left=int(left + margin),
        top=int(top + margin),
        width=int(width),
        height=int(height),
        edge=side,
        display_id=str(getattr(display, "display_id", "") or ""),
        scale=scale,
        visible_ratio=round(ratio, 4),
    )
    return clamp_visible(placement, display)


def clamp_visible(placement: DockPlacement, display: Any) -> DockPlacement:
    """Guarantee enough of the companion is always reachable.

    A companion retracted so far that the user cannot click it is unreachable by
    construction. This keeps a minimum strip on screen, which means a "fully
    retracted" companion is still a presence - which is what Part 4 asks for.
    """
    area = getattr(display, "work_area", None) or getattr(display, "bounds", None)
    if area is None:
        return placement
    ax, ay = int(area.left), int(area.top)
    aw, ah = int(area.width), int(area.height)
    minimum = 14
    left, top = int(placement.left), int(placement.top)

    # Visibility is the overlap between the window and the work area, measured from
    # the edge the window is docked to. Measuring "top" against the *bottom* of the
    # work area reported a fully visible window as almost entirely retracted, and
    # then clamped a correct placement into a wrong one.
    if placement.edge == "right":
        visible = (ax + aw) - left
        if visible < minimum:
            left = ax + aw - minimum
    elif placement.edge == "left":
        visible = (left + placement.width) - ax
        if visible < minimum:
            left = ax + minimum - placement.width
    elif placement.edge == "top":
        visible = (top + placement.height) - ay
        if visible < minimum:
            top = ay + minimum - placement.height
    else:  # bottom
        visible = (ay + ah) - top
        if visible < minimum:
            top = ay + ah - minimum

    # Never allow the window to be pushed entirely off the top-left, which would
    # make it unreachable on the opposite axis.
    left = max(left, ax - placement.width + minimum)
    top = max(top, ay - placement.height + minimum)
    return DockPlacement(
        left=left, top=top, width=placement.width, height=placement.height,
        edge=placement.edge, display_id=placement.display_id,
        scale=placement.scale, visible_ratio=placement.visible_ratio,
    )