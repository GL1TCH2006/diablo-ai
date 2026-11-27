"""Headless orb rendering for the companion UI.

Pure Pillow + numpy. **No Tk, no Win32, no I/O at import.** Importing this package
must never open a window or touch the filesystem, so it can be used from a test,
a headless build agent, or a preview script without a desktop session.

    >>> from ui.orb import OrbRenderer
    >>> renderer = OrbRenderer(size=168, seed=7)
    >>> frame = renderer.render("thinking", t=1.25)   # a 168x168 RGBA image

See :mod:`ui.orb.renderer` for the rendering design, and
:mod:`ui.orb.state_map` for the projection-to-visual-state mapping.
"""

from __future__ import annotations

from .renderer import (
    ALL_STATES,
    PALETTES,
    PARTICLE_COUNT,
    OrbRenderer,
    OrbState,
    StateParameters,
    STATE_PARAMETERS,
    parameters_for,
)

__all__ = [
    "ALL_STATES",
    "OrbRenderer",
    "OrbState",
    "PALETTES",
    "PARTICLE_COUNT",
    "STATE_PARAMETERS",
    "StateParameters",
    "parameters_for",
]