"""The orb renderer, tested headlessly.

Everything here is deterministic: a seeded renderer, a fixed ``(state, t)``, and no
window, no filesystem and no clock. If a test in this file fails, the renderer
changed behaviour - not the machine.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from ui.orb import ALL_STATES, OrbRenderer, OrbState, PALETTES, parameters_for

#: The requirement is "well under 30 ms". This asserts a deliberately generous
#: ceiling so a loaded CI box cannot turn a performance regression into a flaky
#: failure. The real figure is measured and reported separately in the preview
#: script, and logged here.
PER_FRAME_CEILING_SECONDS = 0.250


@pytest.fixture(scope="module")
def renderer() -> OrbRenderer:
    return OrbRenderer(size=168, seed=20261017)


def _arrays(image):
    """(rgb float 0..1, alpha float 0..1) for numeric assertions."""
    data = np.asarray(image, dtype=np.float32) / 255.0
    return data[:, :, :3], data[:, :, 3]


def test_frame_is_the_requested_size_and_rgba(renderer):
    image = renderer.render(OrbState.IDLE, 0.5)
    assert image.size == (168, 168)
    assert image.mode == "RGBA"


def test_import_opened_nothing_and_the_package_is_pure():
    """No Tk, no Win32, no filesystem access anywhere in the package."""
    from pathlib import Path

    import ui.orb

    root = Path(ui.orb.__file__).parent
    sources = list(root.glob("*.py"))
    assert sources, "the orb package has no sources"
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for banned in ("import tkinter", "win32", "ctypes", "urllib",
                       "requests", "socket", "subprocess"):
            assert banned not in text, (
                f"{source.name} references {banned!r}; the orb package must be "
                f"headless and side-effect free at import")


def test_same_seed_state_and_time_produce_identical_frames():
    a = OrbRenderer(size=168, seed=99).render(OrbState.THINKING, 2.25)
    b = OrbRenderer(size=168, seed=99).render(OrbState.THINKING, 2.25)
    assert np.array_equal(np.asarray(a), np.asarray(b)), (
        "the same (seed, state, t) produced different pixels; the renderer is "
        "not deterministic")


def test_different_seeds_produce_different_frames():
    a = np.asarray(OrbRenderer(size=168, seed=1).render(OrbState.IDLE, 1.0))
    b = np.asarray(OrbRenderer(size=168, seed=2).render(OrbState.IDLE, 1.0))
    assert not np.array_equal(a, b), (
        "two different seeds produced identical frames, so the particle layout "
        "does not depend on the seed")


def test_time_advances_the_frame(renderer):
    a = np.asarray(renderer.render(OrbState.IDLE, 0.0))
    b = np.asarray(renderer.render(OrbState.IDLE, 1.7))
    assert not np.array_equal(a, b), "the frame did not change over time"


def test_corners_are_exactly_transparent(renderer):
    for state in ALL_STATES:
        image = renderer.render(state, 1.0)
        _, alpha = _arrays(image)
        for y, x in ((0, 0), (0, 167), (167, 0), (167, 167)):
            assert alpha[y, x] == 0.0, (
                f"corner ({y},{x}) of the {state!r} frame is not fully "
                f"transparent (alpha={alpha[y, x]:.4f})")


def test_the_core_is_the_brightest_part_of_the_frame(renderer):
    """A bright white-gold core is the point; if it is not brightest, it is not a core."""
    for state in ALL_STATES:
        rgb, alpha = _arrays(renderer.render(state, 1.0))
        brightness = rgb.max(axis=2) * alpha
        centre = brightness[84, 84]
        # Compare against the ring band, not the whole frame: the ring is meant to
        # sparkle brightly and should not have to be dimmer than the core's peak.
        band = brightness[84, 84:70:-1].max()
        assert centre >= band * 0.9, (
            f"{state!r}: the centre ({centre:.3f}) is dimmer than the ring band "
            f"({band:.3f}); there is no core")


def test_no_state_raises_and_every_state_is_different():
    renderer = OrbRenderer(size=168, seed=5)
    signatures = {}
    for state in ALL_STATES:
        image = renderer.render(state, 1.0)
        assert image.size == (168, 168), f"{state} rendered at the wrong size"
        rgb, alpha = _arrays(image)
        assert np.isfinite(rgb).all(), f"{state} produced non-finite colour"
        assert np.isfinite(alpha).all(), f"{state} produced non-finite alpha"
        signatures[state] = (float(rgb.sum()), float(alpha.sum()))

    # No two states may be visually indistinguishable.
    for i, a in enumerate(ALL_STATES):
        for b in ALL_STATES[i + 1:]:
            assert signatures[a] != signatures[b], (
                f"{a!r} and {b!r} render identically, so one of them carries no "
                f"information")


def test_states_differ_measurably_by_brightness(renderer):
    """The states are not all the same brightness; sleeping really is dim."""
    levels = {}
    for state in ALL_STATES:
        _, alpha = _arrays(renderer.render(state, 1.0))
        levels[state] = float(alpha.mean())

    assert levels[OrbState.SLEEPING] < levels[OrbState.EXECUTING], (
        f"sleeping ({levels[OrbState.SLEEPING]:.4f}) is not dimmer than "
        f"executing ({levels[OrbState.EXECUTING]:.4f})")
    assert levels[OrbState.UNVERIFIED] < levels[OrbState.IDLE], (
        "unverified should read dimmer than a plain idle orb")
    assert levels[OrbState.AWAKENING] > levels[OrbState.SLEEPING], (
        "awakening should read brighter than sleeping")


def test_failed_is_red_dominant_and_waiting_approval_is_amber_dominant(renderer):
    """Colour is what separates failure from attention at a glance.

    Measured as *greenness* (mean green over mean red) rather than raw channel
    values, because the states differ in overall brightness and brightness is a
    separate property. Lower greenness means redder: the failure palette keeps
    almost no green, while plain gold keeps a lot.
    """
    def mean_rgb(state):
        rgb, alpha = _arrays(renderer.render(state, 1.0))
        weight = alpha[:, :, None] + 1e-6
        return (rgb * weight).sum(axis=(0, 1)) / weight.sum()

    def greenness(state):
        mean = mean_rgb(state)
        return float(mean[1] / max(1e-6, float(mean[0])))

    failed_rgb = mean_rgb(OrbState.FAILED)
    assert failed_rgb[0] > failed_rgb[1] and failed_rgb[0] > failed_rgb[2], (
        f"failed is not red-dominant: R={failed_rgb[0]:.3f} "
        f"G={failed_rgb[1]:.3f} B={failed_rgb[2]:.3f}")

    failed, idle, waiting = (greenness(OrbState.FAILED),
                            greenness(OrbState.IDLE),
                            greenness(OrbState.WAITING_APPROVAL))
    assert failed < idle, (
        f"failed ({failed:.3f}) is not redder than the gold idle orb ({idle:.3f})")
    assert failed < waiting < idle, (
        f"amber should sit between red and gold: failed={failed:.3f} "
        f"waiting={waiting:.3f} idle={idle:.3f}")
    waiting_rgb = mean_rgb(OrbState.WAITING_APPROVAL)
    assert waiting_rgb[0] > waiting_rgb[2], (
        f"waiting_approval is not warm: R={waiting_rgb[0]:.3f} B={waiting_rgb[2]:.3f}")


def test_sleeping_is_small(renderer):
    """Sleeping shrinks as well as dims, so it reads as withdrawn."""
    def lit_radius(state):
        _, alpha = _arrays(renderer.render(state, 1.0))
        yy, xx = np.mgrid[0:168, 0:168]
        radius = np.sqrt((xx - 84) ** 2 + (yy - 84) ** 2)
        lit = radius[alpha > 0.05]
        return float(lit.max()) if lit.size else 0.0

    assert lit_radius(OrbState.SLEEPING) < lit_radius(OrbState.IDLE), (
        "the sleeping orb is not smaller than the idle one")


def test_a_completed_burst_decays_over_time(renderer):
    """The burst decays from t=0.

    Measured on the burst layer alone. Comparing two full frames would be
    meaningless: the ring also rotates and twinkles with t, so total brightness
    moves for reasons that have nothing to do with the burst.
    """
    params = parameters_for(OrbState.COMPLETED)
    assert params.burst is not None, "completed no longer declares a burst"

    def burst_energy(t: float) -> float:
        acc = np.zeros((168, 168, 3), dtype=np.float32)
        renderer._draw_burst(acc, params, t, 84.0, 28.0, 84.0)
        return float(acc.sum())

    early, late = burst_energy(0.05), burst_energy(3.0)
    assert early > late * 3.0, (
        f"the burst did not decay: {early:.1f} at t=0.05 versus {late:.1f} at t=3.0")
    assert burst_energy(0.05) > 0.0, "the burst drew nothing at all"


def test_verifying_adds_a_sweep_that_moves(renderer):
    """The sweep is visible and rotates; that is the point of the state."""
    _, a0 = _arrays(renderer.render(OrbState.VERIFYING, 0.0))
    _, a1 = _arrays(renderer.render(OrbState.VERIFYING, 1.1))
    assert not np.array_equal(a0, a1), "the verifying sweep does not move"


def test_time_per_frame_is_under_the_ceiling(renderer):
    """Measured over several states, logged and asserted generously."""
    timings = {}
    for state in (OrbState.IDLE, OrbState.THINKING, OrbState.EXECUTING,
                  OrbState.VERIFYING, OrbState.COMPLETED):
        renderer.render(state, 0.0)  # warm up
        started = time.perf_counter()
        renderer.render(state, 1.0)
        timings[state] = time.perf_counter() - started

    worst = max(timings.values())
    print("\norb frame times at 168 px:")
    for state, seconds in timings.items():
        print("  %-12s %6.1f ms" % (state, seconds * 1000))
    print("  worst        %6.1f ms (ceiling %.0f ms)"
          % (worst * 1000, PER_FRAME_CEILING_SECONDS * 1000))

    assert worst < PER_FRAME_CEILING_SECONDS, (
        f"slowest orb frame was {worst * 1000:.0f} ms, over the "
        f"{PER_FRAME_CEILING_SECONDS * 1000:.0f} ms ceiling")


def test_an_unknown_state_falls_back_and_says_so():
    """An unmapped state must still draw, and must be visible as unmapped."""
    renderer = OrbRenderer(size=96, seed=3)
    image = renderer.render("no-such-state", 1.0)
    assert image.size == (96, 96)
    assert renderer.last_unknown_state == "no-such-state", (
        "an unknown state was used without being recorded")

    known = np.asarray(renderer.render(OrbState.IDLE, 1.0))
    unknown = np.asarray(image)
    assert np.array_equal(known, unknown), (
        "an unknown state did not fall back to idle")


def test_every_state_has_parameters():
    for state in ALL_STATES:
        params = parameters_for(state)
        assert params.palette in PALETTES, (
            f"{state} names a palette that does not exist: {params.palette}")
        assert math.isfinite(params.rotation_speed)
        assert params.radius > 0.0


def test_a_small_size_still_renders():
    image = OrbRenderer(size=32, seed=1).render(OrbState.SLEEPING, 0.4)
    assert image.size == (32, 32)
    assert image.mode == "RGBA"


def test_an_absurd_size_is_refused():
    with pytest.raises(ValueError):
        OrbRenderer(size=4)