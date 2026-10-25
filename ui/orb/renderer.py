"""Headless orb renderer.

A golden, glittering particle orb drawn entirely with numpy and Pillow: bright
white-gold core, a ring of soft particles that is denser toward the outer radius,
a dim hollow band inside the ring, a few stray sparks outside it, a soft glow
behind everything, and faint curved light arcs on the left and right.

**No window, no Tk, no Win32, no I/O at import.** Every frame is computed from a
``(seed, state, t)`` triple, so the same inputs always produce the same image â€”
which is what makes this testable rather than decorative.

Design notes
============

*Additive blending.* Particles, glow and arcs are accumulated into a single float
RGB buffer and clipped at the end. Summing light is physically what a glow does,
and it avoids the sort-per-pixel work an alpha compositor would need.

*Premultiplied-alpha safety.* The background starts at exactly zero and only
colour is ever added, so the accumulated alpha channel equals the luminance of
what was drawn. A pixel nothing touched stays ``(0, 0, 0, 0)``: fully transparent,
which is what a caller compositing over a desktop needs.

*No per-pixel Python loops.* Particles are evaluated with broadcast numpy
expressions over a fixed-size buffer, and every per-state quantity is a scalar or
a small array computed once per frame. The only Python-level loop is over the
particle count, which is bounded and small.

*Determinism.* Particle positions come from a seeded ``numpy.random.Generator``
whose draws depend only on the seed, never on call order across frames. ``t``
enters only through closed-form trigonometry.

Performance target: well under 30 ms per frame at 168 px. The tests assert a
generous 250 ms ceiling so a loaded CI box cannot make this flaky.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

__all__ = ["OrbRenderer", "OrbState", "STATE_PARAMETERS", "PALETTES"]

#: Number of particles in the main ring. Chosen so the outer band reads dense
#: without becoming a solid band.
PARTICLE_COUNT = 170

#: Ring radius as a fraction of the half-width. Large enough that 170 particles
#: sit in an annulus they do not tile - at a smaller radius the stamps overlap
#: into an opaque blob instead of reading as separate glitter.
_RING_RADIUS = 0.46

#: Inner radius of the hollow band, as a fraction of the ring radius. Everything
#: inside this is deliberately empty except the core and the arcs.
_HOLLOW_INNER = 0.42


class OrbState(str):
    """Marker namespace; the real values are the string constants below."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    SLEEPING = "sleeping"
    AWAKENING = "awakening"
    RETURNING = "returning"
    #: A cancellation that has been requested or reached. Distinct from
    #: ``failed``, ``unverified`` and ``completed``: the work was not judged
    #: wrong, it was stopped. ``ui.orb.state_map`` requires a distinct state
    #: here, so it is a real state rather than a fold-into-error shortcut.
    CANCELLING = "cancelling"


ALL_STATES: tuple[str, ...] = (
    OrbState.IDLE,
    OrbState.LISTENING,
    OrbState.THINKING,
    OrbState.EXECUTING,
    OrbState.VERIFYING,
    OrbState.WAITING_APPROVAL,
    OrbState.COMPLETED,
    OrbState.FAILED,
    OrbState.UNVERIFIED,
    OrbState.SLEEPING,
    OrbState.AWAKENING,
    OrbState.RETURNING,
    OrbState.CANCELLING,
)


#: Core/ring/arc colours, linear-ish RGB triples. Warm gold throughout, with the
#: three states that must be distinguishable at a glance given their own hues.
PALETTES: dict[str, tuple[tuple[float, float, float], ...]] = {
    #        core              ring             accent (streaks)
    "gold": ((1.00, 0.94, 0.80), (1.00, 0.78, 0.34), (1.00, 0.68, 0.26)),
    "amber": ((1.00, 0.93, 0.74), (1.00, 0.70, 0.18), (1.00, 0.58, 0.13)),
    "red": ((1.00, 0.84, 0.78), (1.00, 0.34, 0.26), (1.00, 0.22, 0.18)),
    "dull_gold": ((0.90, 0.84, 0.70), (0.80, 0.64, 0.38), (0.68, 0.55, 0.34)),
    "dim_gold": ((0.84, 0.76, 0.62), (0.66, 0.52, 0.30), (0.54, 0.44, 0.26)),
    # Batch 7 Task 3(e): this used to be almost white (1.00, 0.99, 0.94 core /
    # 0.96, 0.90, 0.70 ring), which rendered thinking, verifying and returning as
    # SILVER-GREY rather than gold. Warm gold now, kept distinct from "gold" by
    # being lighter and less saturated rather than by being a different hue.
    #
    # The ring/accent channels were then pulled further toward orange: at
    # (0.98, 0.84, 0.56) the bloom halo read olive-green on the contact sheet,
    # because the bloom tints with this colour and a near-white core over a
    # yellow-green accent lands on yellow-green. More orange, less yellow.
    "pale_gold": ((1.00, 0.95, 0.83), (0.97, 0.78, 0.45), (0.90, 0.68, 0.36)),
}


@dataclass(frozen=True)
class StateParameters:
    """Everything that distinguishes one orb state from another.

    Every field is a scalar or a small tuple, so a frame costs a handful of
    multiplications rather than a branch per particle.
    """

    palette: str = "gold"
    #: Radians per second the ring turns.
    rotation_speed: float = 0.35
    #: Fraction the ring radius breathes, and how fast.
    pulse_amplitude: float = 0.04
    pulse_rate: float = 1.1
    #: Multiplier on the ring radius.
    radius: float = 1.0
    #: Overall multiplier on emitted light.
    brightness: float = 1.0
    #: Random positional jitter per particle, in pixels.
    jitter: float = 1.6
    #: A sweeping highlight, as (0 = off, else sweep angle and width).
    sweep: tuple[float, float] | None = None
    #: An outward burst that decays from t=0, as (strength,) or None.
    burst: float | None = None
    core_gain: float = 1.0
    glow_gain: float = 1.0
    arc_gain: float = 1.0
    #: Batch 7 Task 3(c). The inner bars are gone; streaks now sweep OUTSIDE the
    #: ring. ``swirl`` is their strength, ``swirl_radius`` their distance from the
    #: ring centre as a fraction of the ring radius (1.25x - 1.7x, i.e. outside the
    #: particle band), and ``swirl_span`` their angular length in degrees.
    swirl: float = 0.6
    swirl_radius: float = 1.30
    swirl_span: float = 120.0
    #: Batch 7 Task 3(b). Bloom strength: a half-resolution blur of the bright
    #: parts, added back. Zero disables it.
    bloom: float = 0.55
    #: Batch 6 Task 5(d). What makes two same-coloured states readable at a
    #: glance. Radius, size and core carry most of the signal, so colour is no
    #: longer the only channel that differs between states.
    #: **Task 7: no longer a particle COUNT.** Every state draws every particle;
    #: ``density`` scales particle size and brightness only, so the ring can
    #: never become a C-shaped crescent.
    density: float = 1.0
    core_scale: float = 1.0
    arc_radius: float = 0.62
    #: How far the ring's inner edge is hollowed, as a fraction of the ring
    #: radius. Higher leaves a dimmer band between the core and the ring.
    hollow: float = 0.68


STATE_PARAMETERS: dict[str, StateParameters] = {
    # Batch 7 Task 3. Per state: swirl (outer streak) strength/radius/span,
    # bloom strength, and `density` which is now a SIZE/BRIGHTNESS control rather
    # than a particle count - every state draws every particle, so the ring is
    # closed everywhere.
    # Task 3(e): thinking, verifying and returning moved to the gold family.
    # Task 3(f): idle and completed are separated by core size, radius, streak
    # strength and bloom, plus completed's one-shot ring pulse (burst).
    OrbState.IDLE: StateParameters(
        palette="gold", rotation_speed=0.30, pulse_amplitude=0.04,
        pulse_rate=1.1, brightness=1.35, jitter=1.6,
        radius=1.00, density=1.00, core_scale=0.95, hollow=0.70,
        swirl=0.42, swirl_radius=1.34, swirl_span=124.0,
        bloom=0.55, arc_gain=0.0),
    OrbState.LISTENING: StateParameters(
        palette="gold", rotation_speed=0.70, pulse_amplitude=0.08,
        pulse_rate=1.7, brightness=1.45, jitter=2.2,
        radius=1.10, density=1.00, core_scale=0.80, hollow=0.70,
        swirl=0.70, swirl_radius=1.30, swirl_span=116.0,
        bloom=0.58, arc_gain=0.0),
    OrbState.THINKING: StateParameters(
        palette="gold", rotation_speed=1.30, pulse_amplitude=0.06,
        pulse_rate=2.3, brightness=1.40, jitter=3.0,
        radius=0.95, density=0.92, core_scale=0.90, hollow=0.72,
        swirl=0.52, swirl_radius=1.46, swirl_span=132.0,
        bloom=0.60, arc_gain=0.0),
    OrbState.EXECUTING: StateParameters(
        palette="gold", rotation_speed=2.05, pulse_amplitude=0.10,
        pulse_rate=3.4, brightness=1.62, jitter=3.4,
        radius=0.86, density=0.86, core_scale=1.25, hollow=0.68,
        swirl=0.64, swirl_radius=1.26, swirl_span=104.0,
        bloom=0.66, arc_gain=0.0),
    OrbState.VERIFYING: StateParameters(
        palette="gold", rotation_speed=1.00, pulse_amplitude=0.05,
        pulse_rate=2.0, brightness=1.50, jitter=1.4,
        sweep=(0.55, 0.42), density=0.95, core_scale=1.05, hollow=0.71,
        swirl=0.86, swirl_radius=1.58, swirl_span=140.0,
        bloom=0.72, arc_gain=0.0),
    OrbState.WAITING_APPROVAL: StateParameters(
        palette="amber", rotation_speed=0.20, pulse_amplitude=0.15,
        pulse_rate=1.05, brightness=1.42, jitter=1.0,
        radius=1.22, density=1.00, core_scale=1.45, hollow=0.69,
        swirl=0.95, swirl_radius=1.66, swirl_span=138.0,
        bloom=0.74, arc_gain=0.0),
    OrbState.COMPLETED: StateParameters(
        palette="pale_gold", rotation_speed=0.52, pulse_amplitude=0.09,
        pulse_rate=1.3, brightness=1.85, jitter=1.9, burst=1.0,
        radius=1.06, density=1.00, core_scale=1.45, hollow=0.66,
        swirl=0.80, swirl_radius=1.24, swirl_span=118.0,
        bloom=1.05, arc_gain=0.0),
    OrbState.FAILED: StateParameters(
        palette="red", rotation_speed=0.16, pulse_amplitude=0.03,
        pulse_rate=0.9, brightness=1.15, jitter=0.8,
        radius=0.92, density=0.95, core_scale=0.85, hollow=0.72,
        swirl=0.30, swirl_radius=1.52, swirl_span=110.0,
        bloom=0.50, arc_gain=0.0),
    OrbState.UNVERIFIED: StateParameters(
        palette="dull_gold", rotation_speed=0.28, pulse_amplitude=0.03,
        pulse_rate=0.8, brightness=1.05, jitter=1.0,
        radius=0.99, density=0.90, core_scale=0.92, hollow=0.74,
        swirl=0.26, swirl_radius=1.44, swirl_span=120.0,
        bloom=0.44, arc_gain=0.0),
    OrbState.SLEEPING: StateParameters(
        palette="dim_gold", rotation_speed=0.10, pulse_amplitude=0.02,
        pulse_rate=0.5, brightness=0.80, jitter=0.5, radius=0.80,
        core_gain=0.75, glow_gain=0.80,
        density=0.90, core_scale=0.78, hollow=0.74,
        swirl=0.22, swirl_radius=1.40, swirl_span=118.0,
        bloom=0.38, arc_gain=0.0),
    OrbState.AWAKENING: StateParameters(
        palette="gold", rotation_speed=1.55, pulse_amplitude=0.10,
        pulse_rate=3.0, brightness=1.48, jitter=2.2, burst=0.55,
        core_gain=0.95, glow_gain=0.95,
        radius=0.90, density=0.94, core_scale=1.10, hollow=0.70,
        swirl=0.58, swirl_radius=1.38, swirl_span=128.0,
        bloom=0.70, arc_gain=0.0),
    OrbState.RETURNING: StateParameters(
        palette="pale_gold", rotation_speed=0.42, pulse_amplitude=0.07,
        pulse_rate=1.0, brightness=1.28, jitter=1.2, radius=0.90,
        core_gain=0.85, glow_gain=0.88,
        density=0.94, core_scale=0.88, hollow=0.72,
        swirl=0.48, swirl_radius=1.50, swirl_span=126.0,
        bloom=0.56, arc_gain=0.0),
    OrbState.CANCELLING: StateParameters(
        # Desaturated and slowing: the work is being wound down, not judged.
        palette="dull_gold", rotation_speed=-0.26, pulse_amplitude=0.05,
        pulse_rate=0.6, brightness=0.98, jitter=0.7, radius=0.86,
        core_gain=0.70, glow_gain=0.72,
        density=0.92, core_scale=0.80, hollow=0.73,
        swirl=0.34, swirl_radius=1.42, swirl_span=122.0,
        bloom=0.46, arc_gain=0.0),
}


def parameters_for(state: str) -> StateParameters:
    """Parameters for ``state``, falling back to ``idle`` for an unknown one.

    Falling back rather than raising is deliberate: an unmapped state must still
    draw something readable. A caller that needs strictness should validate
    against :data:`ALL_STATES` itself.
    """
    return STATE_PARAMETERS.get(str(state or "").strip().lower(), STATE_PARAMETERS[OrbState.IDLE])


@dataclass
class OrbRenderer:
    """Renders orb frames. One instance is reusable and stateless between frames."""

    size: int = 168
    seed: int = 20261017
    particle_count: int = PARTICLE_COUNT
    #: Set when an unknown state name is used; the frame is still drawn.
    last_unknown_state: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.size = int(self.size)
        if self.size < 16:
            raise ValueError(f"size must be at least 16, got {self.size}")
        self._rng = np.random.default_rng(int(self.seed))
        self._axis = np.linspace(-1.0, 1.0, self.size, dtype=np.float32)
        self._yy, self._xx = np.meshgrid(self._axis, self._axis, indexing="ij")
        self._radius = np.sqrt(self._xx ** 2 + self._yy ** 2)
        # Batch 6 Task 5: arctan2 was recomputed over the whole frame three times
        # per frame (arcs, sweep, burst). It is a constant of the geometry, so it
        # is built once here instead.
        self._angle = np.arctan2(self._yy, self._xx)
        self._glow = self._build_glow()
        self._sprites = self._build_sprites()
        self._core_mask = None
        self._init_particles()

    # â”€â”€ static geometry, built once â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build_glow(self) -> np.ndarray:
        """One pre-rendered soft glow sprite, reused every frame.

        Additive, so a single sprite scaled by state brightness is all the "soft
        glow behind" the orb needs. Rendered once in ``__post_init__``; nothing
        here is recomputed per frame.
        """
        r = self._radius
        glow = np.exp(-(r / 0.34) ** 2) * 1.0
        halo = np.exp(-(r / 0.62) ** 2) * 0.35
        total = (glow + halo).astype(np.float32)
        # A second, tighter bloom right on the core.
        #
        # Batch 6 Task 5(b): this term used to be exp(-(r/0.13)^2) * 0.9. It reached
        # out to roughly a third of the frame's half-width, which filled the ring's
        # inner hollow with light and made the band between core and ring read as
        # bright as the ring itself (measured: hollow max 0.627 vs ring max 0.773 -
        # nowhere near hollow). Narrowing and dimming it restores the hollow, which
        # is what makes the ring read as a ring rather than as a disc.
        total += np.exp(-(r / 0.085) ** 2).astype(np.float32) * 0.62
        return total

    def _init_particles(self) -> None:
        n = int(self.particle_count)
        rng = self._rng
        # ── Batch 7 Task 3(a). ──
        # Particles are no longer one uniform stamp. Each gets one of three
        # pre-rendered soft sprites (small / medium / large), chosen from a
        # stratified index so the size mix is even rather than clumped, plus its
        # own size jitter. That is what makes the ring read as bokeh - a field of
        # discs of differing diameter - while staying vectorised: the sprites are
        # built once in __post_init__ and the frame just slices and adds them.
        #
        # The particle COUNT is fixed for every state. Batch 6 varied angular
        # coverage with `density`, which produced C-shaped crescents in the
        # low-density states. Task 3(d) forbids that, so `density` now scales
        # particle SIZE and BRIGHTNESS only; every state keeps a closed ring.
        step = 2.0 * math.pi / max(1, n)
        strata = np.arange(n, dtype=np.float64) * step
        self._p_angle0 = (strata + rng.uniform(0.0, step, n)).astype(np.float64)
        self._p_jitter_a = rng.uniform(-1.0, 1.0, n).astype(np.float64)

        # Radial band: stratified, biased toward the rim, leaving a hollow.
        self._p_band = rng.uniform(0.0, 1.0, n).astype(np.float64)

        # Sprite class: 0 small, 1 medium, 2 large. Even thirds.
        self._p_sprite = (np.arange(n, dtype=np.int64) % 3)
        rng.shuffle(self._p_sprite)
        # Per-particle size multiplier within its class, ~0.8x .. 1.25x.
        self._p_size = (0.80 + 0.45 * rng.uniform(0.0, 1.0, n)).astype(np.float64)
        # Per-particle brightness, so the ring glitters instead of pulsing as one.
        self._p_gain = rng.uniform(0.35, 1.0, n).astype(np.float64)
        # Twinkle phase and rate.
        self._p_phase = rng.uniform(0.0, 2.0 * math.pi, n).astype(np.float64)
        self._p_twinkle = rng.uniform(1.4, 4.6, n).astype(np.float64)

        # A handful of stray sparks outside the rim, evenly spread.
        m = max(4, n // 18)
        sstep = 2.0 * math.pi / max(1, m)
        self._s_angle0 = (np.arange(m, dtype=np.float64) * sstep
                          + rng.uniform(0.0, sstep, m)).astype(np.float64)
        self._s_radial = rng.uniform(1.10, 1.48, m).astype(np.float64)
        self._s_gain = rng.uniform(0.25, 0.7, m).astype(np.float64)
        self._s_phase = rng.uniform(0.0, 2.0 * math.pi, m).astype(np.float64)
        self._n_ring = n

    # â”€â”€ frame â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def render(self, state: str, t: float) -> Image.Image:
        """Render one frame.

        :param state: one of :data:`ALL_STATES`; an unknown name falls back to
            ``idle`` and is recorded in :attr:`last_unknown_state`.
        :param t: time in seconds. Only closed-form trigonometry of ``t`` enters,
            so any value is valid and the result is a pure function of ``t``.
        """
        key = str(state or "").strip().lower()
        params = STATE_PARAMETERS.get(key)
        if params is None:
            self.last_unknown_state = key
            params = STATE_PARAMETERS[OrbState.IDLE]
        else:
            self.last_unknown_state = ""

        t = float(t)
        size = self.size
        centre = size / 2.0
        scale = size / 2.0

        # Accumulator: colour only. Alpha is derived from it at the end, which is
        # what keeps this premultiplied-alpha safe - nothing can set alpha without
        # also having put light there.
        acc = np.zeros((size, size, 3), dtype=np.float32)

        pulse = params.pulse_amplitude * math.sin(2.0 * math.pi * params.pulse_rate * t)
        # ``pulse`` is a fraction of the ring radius, so it scales the radius - it
        # is not a pixel offset. An earlier version multiplied it by ``scale`` a
        # second time, which drove the radius negative and flung the ring across
        # the frame as t advanced.
        ring_radius = scale * (_RING_RADIUS * params.radius * (1.0 + pulse))
        spin = params.rotation_speed * t

        self._draw_core(acc, params, t, centre, scale)
        self._draw_glow(acc, params)
        self._draw_ring(acc, params, t, spin, ring_radius, centre)
        self._draw_sparks(acc, params, t, spin, ring_radius, centre)
        self._draw_swirl(acc, params, t, centre, scale, ring_radius)
        if params.sweep is not None:
            self._draw_sweep(acc, params, t, spin, ring_radius, centre, scale)
        if params.burst is not None:
            self._draw_burst(acc, params, t, centre, ring_radius, scale)

        # A clean margin. The wide glow halo reaches past the unit disc, which
        # left a few counts of alpha in the square's corners - enough to show a
        # faint square edge when the orb is composited over a desktop. Fading to
        # exactly zero outside the disc makes the corners provably transparent.
        margin = np.clip((0.995 - self._radius) / 0.06, 0.0, 1.0).astype(np.float32)
        acc *= margin[:, :, None]

        # ── Batch 7 Task 3(b): bloom. ──
        # A separable Gaussian over a half-resolution copy of the accumulator,
        # added back. Half resolution makes it ~4x cheaper than full, and a bloom
        # is low-frequency by definition so the resolution loss is invisible.
        if params.bloom > 0.01:
            acc += self._bloom(acc, float(params.bloom),
                                PALETTES[params.palette][1])

        return self._to_image(acc)

    def _bloom(self, acc: np.ndarray, strength: float, tint) -> np.ndarray:
        """Half-resolution separable Gaussian blur, added back as light.

        Vectorised: two 1-D passes over a half-size array. No Pillow, so the
        package keeps its "pure Pillow + numpy, no I/O" property.
        """
        half = np.clip((acc.shape[0] + 1) // 2, 8, acc.shape[0])
        small = acc[:half * 2:2, :half * 2:2, :]
        sigma = max(1.0, half * 0.035)
        radius = max(1, int(sigma * 2.0))
        k = np.exp(-0.5 * (np.arange(-radius, radius + 1, dtype=np.float32) / sigma) ** 2)
        k /= k.sum()
        tmp = np.empty_like(small)
        out = np.empty_like(small)
        for c in range(small.shape[2]):
            tmp[:, :, c] = np.apply_along_axis(
                lambda m: np.convolve(m, k, mode="same"), 1, small[:, :, c])
            out[:, :, c] = np.apply_along_axis(
                lambda m: np.convolve(m, k, mode="same"), 0, tmp[:, :, c])
        # Upsample with Pillow's bilinear resize rather than a numpy fancy-index.
        # Two earlier versions were worse: stepping by 2 (``glow[::2, ::2]``) is
        # nearest-neighbour and left a visible checkerboard in the core on the 3x
        # sheet; a hand-rolled bilinear gather over three full-size channels was
        # correct but slow enough to push the median frame time to 37 ms. Pillow
        # does the same interpolation in C.
        # Tint the bloom with the palette's ring colour. Bloom is the colour of
        # the thing that blooms; adding it untinted washed the core back towards
        # white and cost the core its warmth (measured R-B fell to 0.020, under
        # the 0.02 threshold in test_the_core_is_not_flat_white).
        tint = np.asarray(tint, dtype=np.float32)
        out = out * (tint / max(1e-6, float(tint.max())))[None, None, :]
        # A bloom haloes a bright thing; it must not fill it. Masking the inner
        # disc keeps the core's own warm gold intact - without this the bloom
        # washed the core neighbourhood toward neutral and the measured core
        # warmth (mean R-B over the core block) collapsed to 0.001.
        hs, ws = out.shape[0], out.shape[1]
        inner = np.clip((self._radius[:hs * 2:2, :ws * 2:2] - 0.17) / 0.10, 0.0, 1.0)
        out = out * inner.astype(np.float32)[:, :, None]
        arr = np.rint(np.clip(out * strength, 0.0, 1.0) * 255.0).astype(np.uint8)
        chans = [Image.fromarray(arr[:, :, c], mode="L")
                 for c in range(min(3, out.shape[2]))]
        while len(chans) < 3:
            chans.append(chans[-1])
        up = Image.merge("RGB", chans[:3])
        up = up.resize((acc.shape[1], acc.shape[0]), Image.BILINEAR)
        return np.asarray(up, dtype=np.float32) / 255.0

    # â”€â”€ layers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _delta(ang: np.ndarray, centre_angle: float) -> np.ndarray:
        """Absolute angular distance from ``centre_angle``, in [0, pi]."""
        d = (ang - centre_angle + math.pi) % (2.0 * math.pi) - math.pi
        return np.abs(d)

    def _build_sprites(self) -> list[np.ndarray]:
        """Three pre-rendered soft bokeh sprites: small, medium, large.

        Batch 7 Task 3(a). Radii are in pixels at the renderer's own size, giving
        a visible diameter spread of roughly 1.6 px to 9 px at 168 px - the
        "bokeh" look. Each is a smooth radial falloff, so a particle placed with
        any one of them still reads as a soft disc rather than a hard dot.

        Built once in ``__post_init__``; a frame only slices and adds.
        """
        out = []
        for radius in (0.8, 2.2, 4.5):
            span = int(math.ceil(radius * 2.2))
            k = np.arange(-span, span + 1, dtype=np.float64)
            d2 = (k[:, None] ** 2) + (k[None, :] ** 2)
            sprite = np.exp(-d2 / (2.0 * radius * radius)).astype(np.float32)
            out.append(sprite)
        return out

    def _stamp(self, acc, xs, ys, colours, sprites, sprite_idx, sizes) -> None:
        """Add soft point sources into ``acc`` without a per-pixel Python loop.

        Batch 7 Task 3(a): each particle now picks one of three pre-rendered
        sprites and scales it, so the loop body is a slice-and-add rather than an
        ``exp`` over a small box - the falloff is precomputed.
        """
        size = acc.shape[0]
        cache: dict = {}
        for i in range(xs.shape[0]):
            cx = xs[i]
            cy = ys[i]
            if not (np.isfinite(cx) and np.isfinite(cy)):
                continue
            cls = int(sprite_idx[i])
            key = cls
            if key not in cache:
                cache[key] = sprites[cls]
            base = cache[key]
            half = base.shape[0] // 2
            grow = int(round(float(sizes[i])))
            if grow > half:
                half = grow
            x0 = int(round(cx)) - half
            y0 = int(round(cy)) - half
            x1, y1 = x0 + 2 * half + 1, y0 + 2 * half + 1
            kx0, kx1 = max(0, -x0), min(base.shape[1], size - x0)
            ky0, ky1 = max(0, -y0), min(base.shape[0], size - y0)
            if kx0 >= kx1 or ky0 >= ky1:
                continue
            patch = base[ky0:ky1, kx0:kx1]
            if half != base.shape[0] // 2:
                # Integer box resize of the sprite; only used when the per-particle
                # scale pushes past the pre-rendered extent.
                scale = (2 * half + 1) / float(base.shape[0])
                ys_i = (np.arange(patch.shape[0]) / scale).astype(np.int64)
                xs_i = (np.arange(patch.shape[1]) / scale).astype(np.int64)
                ys_i = np.clip(ys_i, 0, patch.shape[0] - 1)
                xs_i = np.clip(xs_i, 0, patch.shape[1] - 1)
                patch = patch[np.ix_(ys_i, xs_i)]
            acc[y0 + ky0:y0 + ky1, x0 + kx0:x0 + kx1, :] += (
                patch[:, :, None] * colours[i][None, None, :])

    def _draw_core(self, acc, params, t, centre, scale) -> None:
        """Bright warm-gold core with a soft falloff.

        Task 5(d): ``core_scale`` is how much a state's core differs in size, so
        two states with the same colour still read as different distances.
        """
        rs = self._radius * scale
        r = rs
        core_r = scale * (0.052 + 0.008 * math.sin(2.0 * math.pi * 0.9 * t)) \
            * float(params.core_scale)
        falloff = np.exp(-((r / max(1e-3, core_r)) ** 2)).astype(np.float32)
        gain = 2.6 * params.brightness * params.core_gain
        colour = np.asarray(PALETTES[params.palette][0], dtype=np.float32)
        acc += (falloff * gain)[:, :, None] * colour[None, None, :]
        # A tight white pinpoint so the core reads as the brightest thing present.
        pinpoint = np.exp(-((r / max(1e-3, core_r * 0.32)) ** 2)).astype(np.float32)
        acc += (pinpoint * 3.1 * params.brightness * params.core_gain)[:, :, None] * 0.45

    def _draw_glow(self, acc, params) -> None:
        sprite = self._glow * (0.55 * params.brightness * params.glow_gain)
        colour = np.asarray(PALETTES[params.palette][1], dtype=np.float32)
        acc += sprite[:, :, None] * colour[None, None, :]

    def _particle_geometry(self, params, t, spin, ring_radius, centre, spark=False):
        """Positions, colours and sprite indices for the ring or the sparks."""
        if spark:
            ang = self._s_angle0 + spin * 1.6
            radial = self._s_radial
            gain = self._s_gain
            phase = self._s_phase
            twinkle = self._p_twinkle[: radial.shape[0]] if radial.shape[0] <= self._p_twinkle.shape[0] \
                else np.resize(self._p_twinkle, radial.shape)
            jitter = self._p_jitter_a[: radial.shape[0]] if radial.shape[0] <= self._p_jitter_a.shape[0] \
                else np.resize(self._p_jitter_a, radial.shape)
            base = 1.55
            sprite = np.zeros(radial.shape[0], dtype=np.int64)
            size = np.ones(radial.shape[0], dtype=np.float64)
        else:
            # Every particle is drawn, always - Task 3(d). The state varies the
            # ring through SIZE and BRIGHTNESS, never through angular coverage,
            # so no state can render a C-shaped crescent.
            ang = self._p_angle0 + spin
            inner = float(params.hollow)
            radial = inner + (1.0 - inner) * (self._p_band ** 0.70)
            gain = self._p_gain
            phase = self._p_phase
            twinkle = self._p_twinkle
            jitter = self._p_jitter_a
            sprite = self._p_sprite
            # `density` is now a size/brightness control, not a count control.
            size = self._p_size * (0.72 + 0.28 * float(params.density))
            base = 2.05 * 1.65          # Task 3(a): 1.65x brighter overall
            base *= (1.0 / max(0.55, float(params.density))) ** 0.30

        twinkle = 0.55 + 0.45 * np.sin(2.0 * math.pi * twinkle * t + phase)
        wobble = params.jitter * jitter
        xs = centre + (ring_radius * radial + wobble) * np.cos(ang)
        ys = centre + (ring_radius * radial + wobble) * np.sin(ang)
        strength = gain * twinkle * base * params.brightness
        return xs, ys, strength, sprite, size

    def _draw_ring(self, acc, params, t, spin, ring_radius, centre) -> None:
        xs, ys, strength, sprite, size = self._particle_geometry(
            params, t, spin, ring_radius, centre)
        core_c, ring_c, _ = PALETTES[params.palette]
        ring_c = np.asarray(ring_c, dtype=np.float32)
        # Particles nearer the rim read whiter, which is what gives the ring depth.
        hot = np.clip((self._p_band - 0.45) / 0.55, 0.0, 1.0)[:, None]
        colour = np.asarray(core_c, dtype=np.float32)[None, :] * (0.30 * hot) + ring_c[None, :] * (0.70 + 0.30 * hot)
        colours = colour * strength[:, None]
        self._stamp(acc, xs, ys, colours, self._sprites, sprite, size)

    def _draw_sparks(self, acc, params, t, spin, ring_radius, centre) -> None:
        xs, ys, strength, sprite, size = self._particle_geometry(
            params, t, spin, ring_radius, centre, spark=True)
        ring_c = np.asarray(PALETTES[params.palette][2], dtype=np.float32)
        self._stamp(acc, xs, ys, ring_c[None, :] * strength[:, None],
                    self._sprites, sprite, size)

    def _draw_swirl(self, acc, params, t, centre, scale, ring_radius) -> None:
        """Long, tapered curved streaks that sweep OUTSIDE the ring.

        ── Batch 7 Task 3(c). This REPLACES the two inner bars Batch 6 drew at
        0.50-0.58 of the ring radius, which read as an eye rather than as motion.
        The streaks now live at ``swirl_radius`` of the ring radius - 1.25x to
        1.7x, i.e. clearly outside the particle band - sweep 100-140 degrees on
        each side, taper to nothing at both ends, and rotate slowly.

        Drawn as a full-frame additive layer so it stays a single vectorised
        expression per streak rather than a loop over pixels.
        """
        if params.swirl <= 0.01:
            return
        colour = np.asarray(PALETTES[params.palette][2], dtype=np.float32)
        rs = self._radius * scale
        ang = self._angle
        swirl_r = ring_radius * float(params.swirl_radius)
        drift = params.rotation_speed * t * 0.35
        span = math.radians(float(params.swirl_span))
        for side in (-1.0, 1.0):
            centre_angle = (0.0 if side > 0 else math.pi) + drift * side
            delta = self._delta(ang, centre_angle)
            # Taper to nothing at both ends: sin^1.5 over the span.
            along = np.clip(1.0 - delta / span, 0.0, 1.0)
            window = along ** 1.5
            # The streak curves: its radius grows toward the leading end, which is
            # what makes it read as a swirl rather than as a bar.
            curve = (rs - (swirl_r + 0.16 * scale * (1.0 - along))) / (scale * 0.055)
            band = np.exp(-(curve ** 2))
            # Soft glow around the streak so it has a halo, not a hard edge.
            wide = np.exp(-((rs - (swirl_r + 0.10 * scale)) / (scale * 0.16)) ** 2)
            acc += ((window * band * 1.25 + window * wide * 0.35)
                    * float(params.swirl))[:, :, None] * colour[None, None, :]

    def _draw_sweep(self, acc, params, t, spin, ring_radius, centre, scale) -> None:
        """A bright band sweeping around the ring, for the verifying state."""
        strength, width = params.sweep
        rs = self._radius * scale
        ring_c = np.asarray(PALETTES[params.palette][0], dtype=np.float32)
        ang = self._angle
        sweep_angle = (spin * 0.8) % (2.0 * math.pi)
        delta = self._delta(ang, sweep_angle)
        along = np.clip(1.0 - delta / width, 0.0, 1.0)
        band = np.exp(-((rs - ring_radius) / (scale * 0.045)) ** 2)
        acc += ((along ** 2.2) * band * 1.5 * strength * params.brightness)[:, :, None] * ring_c[None, None, :]

    def _draw_burst(self, acc, params, t, centre, ring_radius, scale) -> None:
        """An outward burst that decays from t=0, for completed / awakening."""
        age = max(0.0, float(t))
        rs = self._radius * scale
        decay = math.exp(-age * 2.6) * params.burst
        if decay < 0.01:
            return
        core_c = np.asarray(PALETTES[params.palette][0], dtype=np.float32)
        front = ring_radius * (1.0 + 0.35 * (1.0 - math.exp(-age * 3.2)))
        band = np.exp(-((rs - front) / (scale * 0.042)) ** 2)
        ang = self._angle
        petals = 0.5 + 0.5 * np.cos(6.0 * ang + age * 2.0)
        acc += (band * (0.35 + 0.65 * petals) * decay * 1.4)[:, :, None] * core_c[None, None, :]

    # â”€â”€ output â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _to_image(self, acc: np.ndarray) -> Image.Image:
        """Tone-map the accumulator to 8-bit and derive alpha from the light.

        ── Batch 6 Task 5(a): the core used to be flat white. ──
        The old line was ``rgb = np.clip(acc, 0.0, 1.0)``. The core accumulates
        well past 1.0 in all three channels at once, so every channel clamped to
        1.0 and the middle of the orb was 255,255,255 - a blown-out white disc
        that lost all of its warmth. The audit could see this on the sheet.

        The fix is a hue-preserving highlight rolloff:

            lum  = max(r, g, b)                  # the strongest channel
            soft = lum / (1 + lum)                # maps [0, inf) into [0, 1), smoothly
            rgb *= soft / lum                     # scales ALL channels equally

        Because every channel is scaled by the same factor, the ratio between them
        - the hue - survives the rolloff. A gold core stays gold: R clips toward 1
        while B, which was lower to begin with, keeps climbing, so the centre reads
        as warm white-gold instead of white. ``lum/(1+lum)`` never reaches 1 for
        any finite input, so nothing is ever hard-clipped.

        A small extra white lift proportional to ``soft**4`` keeps the very centre
        reading as "hot" without returning to a flat 255,255,255.

        Alpha is the luminance, so it is 0 exactly where nothing was drawn. The
        result is premultiplied-alpha safe: dividing by alpha is never needed to
        place it over another surface.
        """
        acc = np.maximum(acc, 0.0)
        lum = acc.max(axis=2)
        safe = lum > 1e-8
        soft = np.zeros_like(lum)
        np.divide(lum, 1.0 + lum, out=soft, where=safe)
        gain = np.ones_like(lum)
        np.divide(soft, lum, out=gain, where=safe)
        rgb = acc * gain[:, :, None]
        # Gentle highlight lift: the top of the core trends to white without ever
        # becoming a flat white disc.
        lift = (soft ** 4)[:, :, None] * 0.55
        rgb = rgb + lift * (1.0 - soft)[:, :, None]
        rgb = np.clip(rgb, 0.0, 1.0)
        alpha = soft
        out = np.empty((acc.shape[0], acc.shape[1], 4), dtype=np.uint8)
        out[:, :, :3] = np.rint(rgb * 255.0).astype(np.uint8)
        out[:, :, 3] = np.rint(alpha * 255.0).astype(np.uint8)
        return Image.fromarray(out, mode="RGBA")