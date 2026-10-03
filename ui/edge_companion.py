"""M2.16.UI - the edge-docked companion window.

This module is a **renderer**. It reads a :class:`~ui.companion_state.CompanionSnapshot`
and draws it. It decides layout and nothing else: no task state, no timing rules,
no vocabulary, no event filtering. All of that lives in ``ui/companion_state`` and
``ui/companion_events``, which import no Tk and are tested with no display.

The window itself
=================

Borderless, topmost and translucent, docked to one edge of one monitor by the
arithmetic in :mod:`ui.edge`. There is no title bar, because an ``overrideredirect``
toplevel has none and adding one would defeat the entire premise.

Sleeping is deliberately not click-through
==========================================

Part 5 asks for click-through "except on the companion itself", and
``WS_EX_TRANSPARENT`` would deliver exactly that. It would also swallow every click
on the companion, which removes the activation method Part 15 asks for. Those two
requirements are in direct conflict, so the compromise here is:

* the sleeping footprint is genuinely tiny (a ~74x34 sliver), so it cannot
  meaningfully intercept input anywhere else;
* ``WS_EX_NOACTIVATE`` is set while sleeping, so the presence never steals focus
  from the application the user is actually working in, and clicking it activates
  the companion rather than focusing an inert window;
* no desktop-wide hit-testing, no ``WS_EX_LAYERED`` full-screen trick, and
  **no global keyboard or mouse hook of any kind**.

Threading
=========

Tk owns the main thread. The animation is a ``root.after`` timer on that same
thread, so every frame boundary is also a state boundary and the animation is
interruptible at any frame. Nothing here blocks on the backend: TTS goes through
``voice.text_to_speech.speak`` which returns immediately, and every backend call
goes through the controller to the existing bridge.

Native window access
====================

Exactly one native call exists here, and it is the one Rule 8 and Rule 29
explicitly permit "for the companion window itself": reading and writing the
extended window style of *our own* HWND via ``SetWindowLongW``, so the presence can
be ``NOACTIVATE``. It never touches another process's window, never appears in the
model context, and is confined to :meth:`EdgeCompanionApp._apply_window_style`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Callable

from core.logging_setup import get_logger

from .companion_state import CompanionController, CompanionSnapshot, CompanionState
from .edge import CompanionGeometry, DockPlacement, place_at_edge, select_monitor

log = get_logger("ui.edge_companion")

__all__ = [
    "EdgeCompanionApp",
    "EdgeCompanionUnavailable",
    "launch_edge_companion",
    "active_edge_companion_count",
]


class EdgeCompanionUnavailable(RuntimeError):
    """The edge companion window cannot be created on this machine.

    Distinct from a companion *bug*: a caller should fall back to the console UI
    rather than treat this as a crash.
    """

    def __init__(self, message: str, *, reason: str = "unavailable") -> None:
        super().__init__(message)
        self.reason = reason


# ═════════════════════════════════════════════════════════════ palette

# Part 23: dark, minimal, translucent, subtle glow, gold/amber accent. Values are
# original and chosen for contrast on both light and dark desktops.
_PANEL_BG = "#14100b"
_PANEL_EDGE = "#6b4f16"
_ACCENT = "#e0a13a"
_ACCENT_DIM = "#8a6524"
_TEXT = "#e9dfcc"
_MUTED = "#9b8b73"
_OK = "#8fbf6a"
_ERROR = "#c8543f"
_ENTRY_BG = "#1d1710"

#: Window background, made fully transparent via ``-transparentcolor`` so the
#: panel has no visible rectangular box around it. A near-black that is unlikely to
#: be used for anything inside the panel.
_CHROMA = "#010203"

_MIN_EXPANDED_WIDTH = 260   # Part 22: no inaccessible tiny controls while expanded
_MIN_EXPANDED_HEIGHT = 200

#: Win32 extended window style bits used for the companion's own window only.
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080

#: Frame budget. Fast enough to read as a slide, slow enough to be cheap. The
#: controller owns the animation *timing*; this only decides how often the UI asks
#: for the next frame.
_FRAME_MS = 33

#: How often the monitor layout is re-checked. Low frequency on purpose: Part 5
#: forbids constant polling, and a display enumeration is a real Win32 call.
_MONITOR_CHECK_SECONDS = 5.0


class _WindowStyle:
    """Isolated access to *our own* extended window style.

    Kept as a separate object so the native surface is one small, auditable block.
    Every operation is best-effort: on a non-Windows host, or if the handle is not
    yet realised, the companion still runs, it simply keeps focus when clicked.
    """

    def __init__(self) -> None:
        self._available = True

    @staticmethod
    def _apis() -> Any:
        import ctypes

        return ctypes.windll.user32

    def _hwnd(self, root: Any) -> int:
        # Tk's toplevel HWND is the parent of the widget HWND on Windows.
        apis = self._apis()
        widget_hwnd = int(root.winfo_id())
        parent = int(apis.GetParent(widget_hwnd) or 0)
        return parent or widget_hwnd

    def apply(self, root: Any, *, noactivate: bool) -> bool:
        if not self._available:
            return False
        try:
            apis = self._apis()
            apis.SetWindowLongW.restype = ctypes_long()
            hwnd = self._hwnd(root)
            style = int(apis.GetWindowLongW(hwnd, _GWL_EXSTYLE))
            # TOOLWINDOW keeps the companion out of the taskbar and the Alt-Tab
            # list, which is what a desktop presence should be.
            style |= _WS_EX_TOOLWINDOW
            if noactivate:
                style |= _WS_EX_NOACTIVATE
            else:
                style &= ~_WS_EX_NOACTIVATE
            apis.SetWindowLongW(hwnd, _GWL_EXSTYLE, style)
            return True
        except Exception as exc:  # noqa: BLE001 - styling is cosmetic
            self._available = False
            log.debug("companion window style unavailable: %s", exc)
            return False


def ctypes_long() -> Any:
    import ctypes

    return ctypes.c_long


# ═════════════════════════════════════════════════════ single ownership

#: Part 17 requires single-owner lifecycle. A process-wide registry is what makes
#: a repeated launch *refuse cleanly* instead of opening a second window.
_ACTIVE: list["EdgeCompanionApp"] = []


def active_edge_companion_count() -> int:
    """How many edge companion windows this process currently owns."""
    return len([app for app in _ACTIVE if not app.closed])


def _focus_existing() -> "EdgeCompanionApp | None":
    for app in _ACTIVE:
        if not app.closed:
            return app
    return None


def launch_edge_companion(
    controller: CompanionController,
    *,
    display_layer: Any = None,
    edge: str = "right",
    geometry: CompanionGeometry | None = None,
    speak: bool = False,
    root: Any = None,
    goal_provider: Any = None,
    goal_refresh_seconds: float = 1.0,
) -> "tuple[EdgeCompanionApp, bool]":
    """Create the companion window, or focus the one that already exists.

    Returns ``(app, created)``. ``created`` is ``False`` when an existing companion
    was focused instead, which is how a repeated launch is handled without a
    duplicate window (Part 17).
    """
    existing = _focus_existing()
    if existing is not None:
        existing.focus()
        return existing, False
    # Construction registers the window itself; see ``EdgeCompanionApp.__init__``.
    app = EdgeCompanionApp(
        controller=controller,
        display_layer=display_layer,
        edge=edge,
        geometry=geometry,
        speak=speak,
        root=root,
        goal_provider=goal_provider,
        goal_refresh_seconds=goal_refresh_seconds,
    )
    return app, True


# ═════════════════════════════════════════════════════════════ the view


class EdgeCompanionApp:
    """A thin, stateless renderer for one companion."""

    def __init__(
        self,
        controller: CompanionController,
        *,
        display_layer: Any = None,
        edge: str = "right",
        geometry: CompanionGeometry | None = None,
        speak: bool = False,
        root: Any = None,
        poll_ms: int = _FRAME_MS,
        monitor_check_seconds: float = _MONITOR_CHECK_SECONDS,
        goal_provider: Any = None,
        goal_refresh_seconds: float = 1.0,
    ) -> None:
        self.controller = controller
        self._display_layer = display_layer
        self._edge = str(edge or "right").strip().lower()
        self._geometry = geometry or CompanionGeometry()
        self._poll_ms = max(16, int(poll_ms))
        self._monitor_check = max(0.5, float(monitor_check_seconds))
        # A read-only callable returning goal summaries. It is a *projection source*,
        # not an authority: the companion can display what it returns and can do
        # nothing else to a goal. Passing the GoalStore here instead would be the
        # mistake this milestone is built to avoid.
        self._goal_provider = goal_provider if callable(goal_provider) else None
        self._goal_refresh = max(0.25, float(goal_refresh_seconds))
        self._last_goal_refresh = 0.0

        self._closed = False
        self._frame_job: str | None = None
        self._last_render_key: tuple[Any, ...] = ()
        self._last_spoken_state: CompanionState | None = None
        self._last_monitor_check = 0.0
        self._docked_display_id = ""
        self._placement: DockPlacement | None = None
        self._window_style = _WindowStyle()
        self._speak_enabled = bool(speak)
        self._sleeping_style_applied: bool | None = None
        self._display: Any = None

        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except Exception as exc:  # noqa: BLE001 - no Tk is a recoverable condition
            raise EdgeCompanionUnavailable(
                f"tkinter is unavailable: {exc}", reason="import_failed") from exc
        self._tk = tk

        try:
            self.root = root if root is not None else tk.Tk()
        except Exception as exc:  # noqa: BLE001 - no display, TclError
            raise EdgeCompanionUnavailable(
                f"no display available for the companion window: {exc}",
                reason="window_failed") from exc

        try:
            self._build(tk, tkfont)
        except EdgeCompanionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - bad widget construction
            raise EdgeCompanionUnavailable(
                f"companion widgets could not be created: {exc}",
                reason="widget_failed") from exc

        # Registered here rather than only in :func:`launch_edge_companion`, so
        # single ownership holds no matter how the window was constructed. A direct
        # ``EdgeCompanionApp(...)`` is the same product surface and must be just as
        # visible to the duplicate check.
        _ACTIVE.append(self)

    # ── properties ────────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def placement(self) -> DockPlacement | None:
        """The last computed placement. Exposed for tests and for diagnostics."""
        return self._placement

    @property
    def display_id(self) -> str:
        return self._placement.display_id if self._placement else ""

    # ── construction ──────────────────────────────────────────────────

    def _build(self, tk: Any, tkfont: Any) -> None:
        root = self.root
        root.title("Diablo")
        # No title bar, no taskbar entry, always on top. This is what makes the
        # companion read as a presence rather than an application window.
        root.overrideredirect(True)
        for attribute, value in (("-topmost", True), ("-alpha", 0.96)):
            try:
                root.attributes(attribute, value)
            except self._tk.TclError:
                log.debug("companion could not set %s", attribute)
        try:
            root.configure(bg=_CHROMA)
            root.wm_attributes("-transparentcolor", _CHROMA)
        except self._tk.TclError:
            # Not fatal: the companion still works, it just gets a hard rectangular
            # edge instead of a chroma-keyed one.
            log.debug("companion transparency unavailable on this Tk build")

        scale = self._current_display_scale()
        expanded_w = max(_MIN_EXPANDED_WIDTH, int(self._geometry.expanded_width * scale))
        expanded_h = max(_MIN_EXPANDED_HEIGHT, int(self._geometry.expanded_height * scale))
        # One constant window size for the whole session. Resizing every frame is
        # what makes a docked panel look like it is flickering, so the animation
        # moves a fixed-size window instead.
        root.geometry(f"{expanded_w}x{expanded_h}+0+0")

        self._fonts = {
            "presence": tkfont.Font(family="Segoe UI", size=max(9, int(9 * scale)), weight="bold"),
            "status": tkfont.Font(family="Segoe UI", size=max(9, int(10 * scale))),
            "task": tkfont.Font(family="Segoe UI", size=max(9, int(10 * scale)), weight="bold"),
            "body": tkfont.Font(family="Consolas", size=max(8, int(9 * scale))),
            "small": tkfont.Font(family="Segoe UI", size=max(7, int(8 * scale))),
            "button": tkfont.Font(family="Segoe UI", size=max(8, int(9 * scale)), weight="bold"),
        }

        # Panel: inset by 2px so the chroma shows as a soft halo on every side.
        panel = tk.Frame(root, bg=_PANEL_BG, highlightbackground=_PANEL_EDGE,
                         highlightthickness=max(1, int(scale)), bd=0)
        panel.place(x=2, y=2, relwidth=1, relheight=1, width=-4, height=-4)

        header = tk.Frame(panel, bg=_PANEL_BG)
        header.pack(fill="x", padx=max(8, int(10 * scale)), pady=(max(6, int(8 * scale)), 0))

        self._mark = tk.Canvas(header, width=max(14, int(16 * scale)),
                               height=max(14, int(16 * scale)),
                               bg=_PANEL_BG, highlightthickness=0, bd=0)
        self._mark.pack(side="left")
        self._draw_mark()

        self._title = tk.Label(header, text="DIABLO", bg=_PANEL_BG, fg=_ACCENT,
                               font=self._fonts["presence"])
        self._title.pack(side="left", padx=(max(4, int(6 * scale)), 0))

        self._state_label = tk.Label(header, text="", bg=_PANEL_BG, fg=_MUTED,
                                     font=self._fonts["small"], anchor="e")
        self._state_label.pack(side="right")

        rule = tk.Frame(panel, bg=_ACCENT_DIM, height=max(1, int(scale)))
        rule.pack(fill="x", padx=max(8, int(10 * scale)),
                  pady=(max(4, int(6 * scale)), 0))

        self._task_label = tk.Label(panel, text="", bg=_PANEL_BG, fg=_TEXT,
                                    font=self._fonts["task"], anchor="w",
                                    justify="left", wraplength=max(200, expanded_w - 40))
        self._task_label.pack(fill="x", padx=max(8, int(10 * scale)),
                              pady=(max(6, int(8 * scale)), 0))

        self._status = tk.Label(panel, text="", bg=_PANEL_BG, fg=_MUTED,
                                font=self._fonts["status"], anchor="w",
                                justify="left", wraplength=max(200, expanded_w - 40))
        self._status.pack(fill="x", padx=max(8, int(10 * scale)),
                          pady=(max(3, int(4 * scale)), 0))

        # Progress: a scannable list, newest last. Deliberately not a log - the
        # mapper already capped each line and dropped everything unmapped.
        self._progress = tk.Text(panel, height=6, bg=_ENTRY_BG, fg=_MUTED,
                                 font=self._fonts["body"], relief="flat",
                                 highlightthickness=0, wrap="word",
                                 state="disabled")
        self._progress.pack(fill="both", expand=True,
                            padx=max(8, int(10 * scale)),
                            pady=(max(4, int(6 * scale)), 0))
        self._rendered_progress = 0

        footer = tk.Frame(panel, bg=_PANEL_BG)
        footer.pack(fill="x", padx=max(8, int(10 * scale)),
                    pady=(max(6, int(8 * scale)), max(6, int(8 * scale))))

        self._entry = tk.Entry(footer, bg=_ENTRY_BG, fg=_TEXT,
                               insertbackground=_ACCENT, relief="flat",
                               font=self._fonts["body"],
                               highlightthickness=max(1, int(scale)),
                               highlightbackground=_PANEL_EDGE,
                               highlightcolor=_ACCENT_DIM)
        self._entry.pack(side="left", fill="x", expand=True,
                         ipady=max(2, int(4 * scale)))
        self._entry.insert(0, "")
        self._entry.bind("<Return>", self._on_return)
        self._entry.bind("<KP_Enter>", self._on_return)

        self._send = tk.Label(footer, text="SEND", bg=_ACCENT_DIM, fg=_TEXT,
                              font=self._fonts["button"], padx=max(6, int(8 * scale)),
                              pady=max(2, int(4 * scale)), cursor="hand2")
        self._send.pack(side="left", padx=(max(4, int(6 * scale)), 0))
        self._send.bind("<Button-1>", self._on_return)

        # STOP is always constructed and only ever *shown* when the snapshot says
        # cancellation is available, so a late frame cannot show it too early.
        self._stop = tk.Label(footer, text="STOP", bg=_ERROR, fg=_TEXT,
                              font=self._fonts["button"], padx=max(6, int(8 * scale)),
                              pady=max(2, int(4 * scale)), cursor="hand2")
        self._stop.bind("<Button-1>", self._on_stop)

        # Activation and dismissal. Clicking the presence is the deterministic
        # activation method Part 15 asks for; Escape is Part 22's dismissal.
        for widget in (root, panel, self._mark, self._title):
            widget.bind("<Button-1>", self._on_activate_click)
        root.bind("<Escape>", self._on_escape)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Destroy>", self._on_destroy)

        # The window style is deliberately *not* applied here. Tk implements
        # ``-transparentcolor`` by setting ``WS_EX_LAYERED``, and it does so from an
        # idle handler - which rewrites the extended style and drops
        # ``WS_EX_NOACTIVATE``. Applying before that would be silently undone, so
        # :meth:`_sync_window_style` applies it after every geometry commit instead.
        # ``None`` forces the first frame to apply it.
        self._sleeping_style_applied = None

    def _draw_mark(self) -> None:
        """An original geometric presence mark.

        Two nested diamonds with a centre dot. Drawn rather than imported: Part 23
        requires original geometry, and a canvas primitive set needs no asset, no
        download and no licence question.
        """
        canvas = self._mark
        canvas.delete("all")
        size = int(canvas.winfo_reqwidth())
        cx = cy = size // 2
        r = max(3, size // 2 - 1)
        canvas.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy,
                              outline=_ACCENT, fill="", width=1)
        inner = max(1, r // 2)
        canvas.create_polygon(cx, cy - inner, cx + inner, cy, cx, cy + inner,
                              cx - inner, cy, outline=_ACCENT_DIM, fill="", width=1)
        canvas.create_oval(cx - 1, cy - 1, cx + 1, cy + 1, fill=_ACCENT, outline="")

    # ── display handling ──────────────────────────────────────────────

    def _list_displays(self) -> list[Any]:
        layer = self._display_layer
        if layer is None:
            return []
        try:
            return list(layer.list_displays())
        except Exception as exc:  # noqa: BLE001 - display enumeration is best effort
            log.debug("display enumeration failed: %s", exc)
            return []

    def _cursor_point(self) -> tuple[int, int] | None:
        """The pointer position, via Tk's own tracker.

        Tk's ``winfo_pointerxy`` needs no native call of ours and no global hook -
        it is a query, not a listener.
        """
        try:
            x, y = self.root.winfo_pointerxy()
            return int(x), int(y)
        except Exception:  # noqa: BLE001
            return None

    def _refresh_display(self) -> Any:
        """Re-read the display layout and cache the chosen monitor.

        This is the only place that enumerates displays, and it is called on
        construction, on an explicit ``redock()``, and from the rate-limited monitor
        check - never from the frame path.
        """
        displays = self._list_displays()
        if not displays:
            return self._display
        # Part 16: adapt to the monitor the user is looking at, do not assume
        # primary. Falls back to primary only when the pointer is unavailable.
        display = select_monitor(displays, point=self._cursor_point())
        if display is not None:
            self._display = display
            self._docked_display_id = str(getattr(display, "display_id", "") or "")
        return display

    def _current_display(self) -> Any:
        if self._display is None:
            return self._refresh_display()
        return self._display

    def _current_display_scale(self) -> float:
        display = self._current_display()
        if display is None:
            return 1.0
        try:
            percent = float(getattr(display, "scale_percent", None) or 100.0)
        except (TypeError, ValueError):
            return 1.0
        return max(1.0, min(2.0, percent / 100.0 if percent > 0 else 1.0))

    def redock(self) -> DockPlacement | None:
        """Force a fresh monitor lookup and re-place the window.

        The public, explicit version. The per-frame path uses
        :meth:`_apply_placement`, which reuses the cached monitor, so animation
        costs a geometry string and not a Win32 enumeration.
        """
        display = self._refresh_display()
        if display is None:
            return None
        return self._apply_placement()

    def _apply_placement(self, peek: float | None = None) -> DockPlacement | None:
        """Place the window for the cached monitor and the current peek.

        Returns ``None`` when no display information is available - in which case
        the window keeps its last position rather than jumping to an assumed 0,0.
        """
        display = self._current_display()
        if display is None:
            return None
        fraction = self.controller.snapshot().peek if peek is None else float(peek)
        placement = place_at_edge(display, self._edge, peek=fraction,
                                  geometry=self._geometry)
        # Geometry only needs re-applying when the peek actually moved. Skipping the
        # rest keeps an idle companion from issuing a geometry request 30 times a
        # second for no visible reason.
        if self._placement is not None and (
                self._placement.geometry_string() == placement.geometry_string()):
            return self._placement
        self._placement = placement
        try:
            self.root.geometry(placement.geometry_string())
            # An off-screen companion still has to be topmost on its new monitor.
            self.root.attributes("-topmost", True)
            # Tk applies the position immediately but the size only when idle tasks
            # run. Without this the window spends one frame at the new position
            # still wearing the previous size, which during the peek animation
            # means visibly growing out of the edge instead of sliding out of it.
            self.root.update_idletasks()
        except self._tk.TclError as exc:
            log.debug("companion geometry update failed: %s", exc)
        return placement

    def _check_monitor_change(self) -> None:
        """Re-dock if the monitor layout changed since the last check."""
        now = time.monotonic()
        if now - self._last_monitor_check < self._monitor_check:
            return
        self._last_monitor_check = now
        previous = self._docked_display_id
        display = self._refresh_display()
        if display is None:
            return
        if str(getattr(display, "display_id", "") or "") != previous:
            log.info("companion re-docking to display %s", previous or "unknown")
            self._apply_placement()

    # ── rendering ─────────────────────────────────────────────────────

    def _render(self, snapshot: CompanionSnapshot) -> None:
        # Only redraw when something a user can see actually changed. Without this
        # the poll would rewrite every widget 30 times a second, which is exactly
        # the kind of idle CPU burn Part 5 rules out.
        key = (snapshot.state.value, snapshot.task_id, snapshot.task_text,
               snapshot.status_line, snapshot.terminal_message,
               tuple(snapshot.progress), snapshot.can_cancel,
               snapshot.cancellation_requested, len(snapshot.events),
               snapshot.goal_line, snapshot.goal_active)
        if key == self._last_render_key:
            return
        self._last_render_key = key

        state = snapshot.state
        accent = {
            CompanionState.SLEEPING: _ACCENT_DIM,
            CompanionState.AWAKENING: _ACCENT,
            CompanionState.LISTENING: _ACCENT,
            CompanionState.THINKING: _ACCENT,
            CompanionState.EXECUTING: _OK,
            CompanionState.RETURNING: _OK,
            CompanionState.ERROR: _ERROR,
        }.get(state, _MUTED)

        self._title.configure(fg=_ACCENT if state is not CompanionState.SLEEPING
                              else _ACCENT_DIM)
        self._state_label.configure(text=state.value.upper(), fg=accent)

        task_text = snapshot.task_text
        if not task_text and state is CompanionState.SLEEPING:
            task_text = "Diablo"
        if not snapshot.goal_line:
            self._task_label.configure(text=task_text or "", fg=_TEXT)

        status = snapshot.status_line
        if snapshot.cancellation_requested:
            status = "Stopping..."
        elif snapshot.terminal_message and state in (
                CompanionState.RETURNING, CompanionState.ERROR, CompanionState.LISTENING):
            status = snapshot.terminal_message
        self._status.configure(
            text=status, fg=_ERROR if state is CompanionState.ERROR else _MUTED)

        # M2.17: persistent autonomous work, rendered as one bounded line. The
        # companion reads goal state; it never creates or schedules any.
        if snapshot.goal_line:
            self._task_label.configure(
                text=snapshot.goal_line,
                fg=_ACCENT if snapshot.goal_active else _MUTED)
        elif task_text:
            self._task_label.configure(text=task_text, fg=_TEXT)

        self._render_progress(snapshot.progress, accent)

        # STOP appears only when the snapshot says cancellation is genuinely
        # available, and stays visible-but-disabled once requested, so the user can
        # see that the request was registered rather than silently ignored.
        # The STOP affordance tracks *cancellation_requested*, not *can_cancel*.
        # Once the request is registered ``can_cancel`` goes false by design, and
        # keying visibility off it made the button vanish the instant it was
        # pressed - so the user could not tell a registered request from a lost
        # one. It stays on screen, relabelled, until the backend answers.
        if snapshot.can_cancel or snapshot.cancellation_requested:
            if not self._stop.winfo_manager():
                self._stop.pack(side="left", before=self._send,
                                padx=(max(4, int(6)), 0))
        elif self._stop.winfo_manager():
            self._stop.pack_forget()
        if snapshot.cancellation_requested:
            self._stop.configure(text="STOPPING", bg=_ACCENT_DIM, fg=_MUTED,
                                 cursor="arrow")
        else:
            self._stop.configure(text="STOP", bg=_ERROR, fg=_TEXT, cursor="hand2")

        # Sleep styling: while sleeping the panel is visually dormant and the input
        # is not offered, because there is no way to type into a sliver.
        asleep = snapshot.state is CompanionState.SLEEPING
        self._entry.configure(
            state="disabled" if asleep else "normal",
            bg=_PANEL_BG if asleep else _ENTRY_BG)

        self._maybe_speak(snapshot)
        # Geometry first, window style second. The order matters: Tk rewrites the
        # extended style when it realises the transparency attribute, which happens
        # during the geometry commit, so styling before that would be undone.
        self._apply_placement()
        self._sync_window_style(asleep)

    def _sync_window_style(self, asleep: bool) -> None:
        """Keep ``NOACTIVATE``/``TOOLWINDOW`` in step with the sleep state.

        Only writes when the desired state actually changed, so an idle companion
        costs no extra Win32 calls. Written after geometry commits rather than
        before, because Tk's layered-window handling overwrites the extended style.
        """
        if asleep == self._sleeping_style_applied:
            return
        self._window_style.apply(self.root, noactivate=asleep)
        # Recorded even on failure: ``_WindowStyle`` disables itself permanently on
        # the first failure, so retrying every frame would just repeat a call that
        # cannot succeed on this host.
        self._sleeping_style_applied = asleep

    def _render_progress(self, progress: tuple[str, ...], accent: str) -> None:
        if len(progress) == self._rendered_progress:
            return
        # Truncate rather than diff: the list is short, bounded by the controller,
        # and a partial redraw is more error-prone than a cheap full rewrite.
        self._progress.configure(state="normal")
        self._progress.delete("1.0", "end")
        recent = progress[-8:]
        for index, line in enumerate(recent):
            colour = _ACCENT if index == len(recent) - 1 else _MUTED
            self._progress.insert("end", f"{line}\n", colour)
        self._progress.configure(state="disabled")
        self._progress.see("end")
        self._rendered_progress = len(progress)

    def _maybe_speak(self, snapshot: CompanionSnapshot) -> None:
        """Speak at most once per state change.

        Part 8 permits the existing SAPI TTS and forbids claiming voice activation.
        Text input is unaffected either way. Spoken only on transitions into states
        a person needs told about - never per frame, which would spawn a PowerShell
        process per frame.
        """
        if not getattr(self, "_speak_enabled", False):
            return
        state = snapshot.state
        if state is self._last_spoken_state:
            return
        self._last_spoken_state = state
        text = ""
        if state is CompanionState.LISTENING and not snapshot.cancellation_requested:
            text = "What is the task?"
        elif state is CompanionState.ERROR:
            text = "I could not complete that."
        elif state is CompanionState.RETURNING:
            text = "Done." if not snapshot.cancellation_requested else "Stopped."
        if not text:
            return
        try:
            from voice import text_to_speech

            text_to_speech.speak(text)
        except Exception as exc:  # noqa: BLE001 - speech is optional by design
            log.debug("companion TTS unavailable: %s", exc)

    # ── interaction ───────────────────────────────────────────────────

    def _on_activate_click(self, _event: Any = None) -> str:
        """Click-to-activate: the deterministic activation method (Part 15)."""
        if self._closed:
            return "break"
        try:
            self.controller.activate(reason="click")
        except Exception as exc:  # noqa: BLE001 - a click must never crash the UI
            log.debug("companion activation failed: %s", exc)
            return "break"
        self._focus_input()
        return "break"

    def _focus_input(self) -> None:
        if self.controller.snapshot().state is CompanionState.SLEEPING:
            return
        try:
            self.root.deiconify()
            self.root.focus_force()
            self._entry.focus_set()
        except self._tk.TclError as exc:
            log.debug("companion focus failed: %s", exc)

    def _on_return(self, _event: Any = None) -> str:
        if self._closed:
            return "break"
        try:
            text = self._entry.get()
        except self._tk.TclError:
            return "break"
        self._entry.delete(0, "end")
        try:
            self.controller.submit_task(text)
        except Exception as exc:  # noqa: BLE001 - shown as an error, never raised
            log.debug("companion submit failed: %s", exc)
        return "break"

    def _on_stop(self, _event: Any = None) -> str:
        if self._closed:
            return "break"
        # A request only. The snapshot's cancellation_requested flag is what changes
        # now; the terminal state waits for the backend.
        self.controller.cancel(reason="stop button")
        return "break"

    def _on_escape(self, _event: Any = None) -> str:
        """Part 22: Escape cancels an active task, otherwise dismisses."""
        if self._closed:
            return "break"
        snapshot = self.controller.snapshot()
        if snapshot.can_cancel:
            self.controller.cancel(reason="escape")
            return "break"
        if snapshot.state is CompanionState.ERROR:
            self.controller.acknowledge()
            return "break"
        self.controller.retract()
        return "break"

    def focus(self) -> bool:
        """Bring this companion forward. Used when a duplicate launch is refused."""
        if self._closed:
            return False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
        except self._tk.TclError:
            return False
        return self.controller.activate(reason="refocused")

    # ── main loop ─────────────────────────────────────────────────────

    def pump_once(self) -> CompanionSnapshot:
        """Advance and draw exactly one frame, scheduling nothing.

        Separated from :meth:`_tick` so a host - or a test - can drive the
        companion deterministically without a running timer loop. It is also how
        the companion behaves correctly when it is embedded rather than run as its
        own mainloop: the caller owns the clock.

        Never raises. A frame that fails is logged and skipped, because losing one
        frame of animation is always better than losing the companion.
        """
        if self._closed:
            return self.controller.snapshot()
        snapshot = self.controller.snapshot()
        try:
            self._refresh_goal_projection()
            snapshot = self.controller.tick()
            self._check_monitor_change()
            self._render(snapshot)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the UI
            log.warning("companion frame failed: %s", exc)
        return snapshot

    def _tick(self) -> None:
        """Timer-driven frame. Only this schedules the next one."""
        if self._closed:
            return
        self.pump_once()
        self._frame_job = self.root.after(self._poll_ms, self._tick)

    def _refresh_goal_projection(self, *, force: bool = False) -> bool:
        """Pull the newest goal summary into the controller. Throttled, never raises.

        On the existing frame loop rather than a thread of its own: a second timer
        would be a second thing to shut down correctly, and the goal line does not
        need 30 updates a second. A provider that raises, hangs or returns nonsense
        costs one frame's worth of goal display and nothing else.
        """
        if self._closed or self._goal_provider is None:
            return False
        now = time.monotonic()
        if not force and (now - self._last_goal_refresh) < self._goal_refresh:
            return False
        self._last_goal_refresh = now
        try:
            rows = self._goal_provider()
        except Exception as exc:  # noqa: BLE001 - display must not depend on autonomy
            log.warning("goal projection unavailable: %s", exc)
            return False
        summary: Any = None
        if isinstance(rows, Mapping):
            summary = rows
        elif isinstance(rows, (list, tuple)) and rows:
            summary = rows[0]
        try:
            return bool(self.controller.apply_goal_projection(summary))
        except Exception as exc:  # noqa: BLE001
            log.warning("goal projection rejected: %s", exc)
            return False

    def run(self) -> None:
        """Start the frame timer and hand the main thread to Tk."""
        if self._closed:
            return
        self.redock()
        self._frame_job = self.root.after(self._poll_ms, self._tick)
        try:
            self.root.mainloop()
        finally:
            # A mainloop that exits for any reason still owns teardown. This is the
            # single shutdown path Part 17 requires.
            self.close()

    def close(self) -> None:
        """Cancel the timer, close the controller, destroy the window. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._frame_job is not None:
            try:
                self.root.after_cancel(self._frame_job)
            except Exception:  # noqa: BLE001 - the loop may already be gone
                pass
            self._frame_job = None
        try:
            # Controller first: it unsubscribes from the bridge and cancels any
            # in-flight task through the existing TaskManager.
            self.controller.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("companion controller close failed: %s", exc)
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001 - already destroyed
            pass
        if self in _ACTIVE:
            _ACTIVE.remove(self)

    def _on_destroy(self, _event: Any) -> None:
        # Covers the user closing the window through the window manager, which
        # bypasses close() entirely.
        if not self._closed:
            self.close()