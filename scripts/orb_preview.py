"""Render every orb state into contact sheets, headlessly.

Writes two files inside the gitignored ``data/orb_preview/`` tree:

* ``sheet.png``       - all 13 states x 4 time samples at 168 px, the sweep sheet
* ``sheet_large.png`` - idle / executing / completed / failed at 3x scale, for
  looking at the core, the bokeh particles and the swirl streaks closely

    .venv\\Scripts\\python.exe scripts\\orb_preview.py

No window is opened: Pillow + numpy only. Use it to *look* at the orb without
starting the companion.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw  # noqa: E402

from ui.orb import ALL_STATES, OrbRenderer  # noqa: E402

#: Time samples per state. Several, so motion and decay are visible rather than
#: judged from one frame.
SAMPLES = (0.0, 0.8, 1.6, 2.4)

CELL = 168
LABEL_H = 18
GAP = 8
BACKGROUND = (14, 14, 18)

#: Batch 7 Task 3: the close-up sheet. Three states that must be told apart at a
#: glance, at three times the base size so the bokeh and the streaks are legible.
LARGE_STATES = ("idle", "executing", "completed", "failed")
LARGE_SCALE = 3
LARGE_SAMPLES = (0.0, 1.2, 2.4)

#: Repeat renders per state when timing. This host's scheduler noise is large
#: enough that a single frame measures whichever process happened to be running,
#: so the reported figure is the median of several.
TIMING_REPEATS = 7


def build_sheet(size: int = CELL, seed: int = 20261017,
                states=None, samples=SAMPLES) -> tuple[Image.Image, dict]:
    renderer = OrbRenderer(size=size, seed=seed)
    states = tuple(states or ALL_STATES)
    columns = len(samples)
    rows = len(states)
    cell_h = CELL + LABEL_H
    width = columns * CELL + (columns + 1) * GAP
    height = rows * cell_h + (rows + 1) * GAP + LABEL_H

    sheet = Image.new("RGBA", (width, height), BACKGROUND + (255,))
    draw = ImageDraw.Draw(sheet)
    timings: dict[str, float] = {}

    for row, state in enumerate(states):
        y0 = GAP + row * (cell_h + GAP)
        draw.text((GAP, y0 - 2), state, fill=(220, 220, 230, 255))
        for column, t in enumerate(samples):
            x0 = GAP + column * (CELL + GAP)
            started = time.perf_counter()
            frame = renderer.render(state, t)
            elapsed = time.perf_counter() - started
            timings.setdefault(state, 0.0)
            timings[state] += elapsed / len(samples)
            # Composite over the sheet background so transparency is visible as
            # "the dark background shows through", which is how it will look on a
            # real desktop.
            sheet.alpha_composite(frame, (x0, y0 + LABEL_H))
            draw.text((x0 + 2, y0 + LABEL_H - 1), f"t={t:g}",
                      fill=(150, 150, 165, 255))
    return sheet, timings


def build_large_sheet(seed: int = 20261017) -> tuple[Image.Image, dict]:
    """The close-up sheet: four states at 3x, one renderer sized for it."""
    size = CELL * LARGE_SCALE
    states = tuple(s for s in LARGE_STATES)
    renderer = OrbRenderer(size=size, seed=seed)
    cell = size
    cell_h = cell + LABEL_H
    columns = len(LARGE_SAMPLES)
    width = columns * cell + (columns + 1) * GAP
    height = len(states) * cell_h + (len(states) + 1) * GAP + LABEL_H

    sheet = Image.new("RGBA", (width, height), BACKGROUND + (255,))
    draw = ImageDraw.Draw(sheet)
    timings: dict[str, float] = {}

    for row, state in enumerate(states):
        y0 = GAP + row * (cell_h + GAP)
        draw.text((GAP, y0 - 2), f"{state}  ({size}px, {LARGE_SCALE}x)",
                  fill=(220, 220, 230, 255))
        for column, t in enumerate(LARGE_SAMPLES):
            x0 = GAP + column * (cell + GAP)
            started = time.perf_counter()
            frame = renderer.render(state, t)
            timings[state] = timings.get(state, 0.0) + \
                (time.perf_counter() - started) / len(LARGE_SAMPLES)
            sheet.alpha_composite(frame, (x0, y0 + LABEL_H))
            draw.text((x0 + 2, y0 + LABEL_H - 1), f"t={t:g}",
                      fill=(150, 150, 165, 255))
    return sheet, timings


def measure(seed: int = 20261018) -> dict:
    """min / median / max per state at 168 px, the figure Task 3 asks for."""
    renderer = OrbRenderer(size=CELL, seed=seed)
    for state in ALL_STATES:          # warm up every state first
        renderer.render(state, 2.0)
    out = {}
    for state in ALL_STATES:
        samples = []
        for _ in range(TIMING_REPEATS):
            started = time.perf_counter()
            renderer.render(state, 2.0)
            samples.append((time.perf_counter() - started) * 1000.0)
        out[state] = (min(samples), statistics.median(samples), max(samples))
    return out


def main() -> int:
    out_dir = REPO / "data" / "orb_preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    sheet, timings = build_sheet()
    target = out_dir / "sheet.png"
    sheet.save(target)
    print(f"wrote {target}")
    print(f"  size   : {sheet.size[0]}x{sheet.size[1]}")
    print(f"  states : {len(ALL_STATES)}   samples: {len(SAMPLES)}")

    large, large_timings = build_large_sheet()
    large_target = out_dir / "sheet_large.png"
    large.save(large_target)
    print(f"wrote {large_target}")
    print(f"  size   : {large.size[0]}x{large.size[1]}")
    print(f"  states : {', '.join(LARGE_STATES)}   scale: {LARGE_SCALE}x "
          f"({CELL * LARGE_SCALE}px)   samples: {len(LARGE_SAMPLES)}")

    print()
    print("per-state frame time at 168 px (ms):")
    print(f"  {'state':<20}{'min':>8}{'median':>9}{'max':>8}")
    measured = measure()
    for state in ALL_STATES:
        lo, mid, hi = measured[state]
        print(f"  {state:<20}{lo:8.1f}{mid:9.1f}{hi:8.1f}")
    worst_median = max(v[1] for v in measured.values())
    print(f"  worst MEDIAN: {worst_median:.1f} ms  (ceiling 30 ms)")
    if worst_median >= 30.0:
        print("  WARNING: the median frame time is over the 30 ms ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
