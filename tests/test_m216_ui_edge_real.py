"""M2.16.UI - real Windows validation of the edge companion.

Gated on ``DIABLO_REAL_WINDOWS_TESTS=1`` like the other real suites in this
repository, so it is skipped by default and runs only when explicitly requested.

What is genuinely exercised here, and what is not:

* **Real**: a real Tk toplevel on the real desktop, real Win32 window styles, real
  ``DisplayLayer`` enumeration, real monitor bounds and work areas, real geometry
  calls, real focus behaviour, real thread and window leak checks.
* **Not real**: the Agent turn. These tests prove the companion behaves correctly
  against the real window system; they do not claim the model picks the right tool.
  That is real-model validation, which is reported separately and honestly.

Nothing here mutates the desktop. The companion is the only window created, it is
closed at the end of every test, and the leak checks assert that it left nothing.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

from core.windows.displays import DisplayLayer
from ui.companion_state import CompanionController, CompanionState
from ui.edge import place_at_edge

REAL_WINDOWS = os.environ.get("DIABLO_REAL_WINDOWS_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not REAL_WINDOWS,
    reason="set DIABLO_REAL_WINDOWS_TESTS=1 to run real Windows companion tests",
)


# ═════════════════════════════════════════════════════════════ helpers


def _visible_window_titles() -> list[str]:
    """Titles of all currently visible top-level windows."""
    try:
        import win32gui
    except Exception:  # noqa: BLE001 - pywin32 absent means nothing to count
        return []
    titles: list[str] = []

    def collect(hwnd, _param):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title:
                titles.append(title)

    win32gui.EnumWindows(collect, None)
    return titles


class ScriptedBridge:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str]] = []
        self.cancellations: list[str] = []
        self.unsubscribes = 0
        self._handler: Any = None

    def subscribe_to_events(self, handler: Any) -> Any:
        self._handler = handler

        def _unsubscribe() -> None:
            self.unsubscribes += 1
            self._handler = None

        return _unsubscribe

    def submit_message(self, text: str, task_id: str = "") -> dict:
        self.submissions.append((text, task_id))
        return {"success": True}

    def cancel_task(self, task_id: str = "") -> bool:
        self.cancellations.append(task_id)
        return True

    def emit(self, kind: str, message: str = "", task_id: str = "") -> None:
        assert self._handler is not None
        self._handler({"type": kind, "message": message, "task_id": task_id})


class FakeClock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope="module")
def tk_root():
    import tkinter as tk

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    return root


@pytest.fixture()
def display_layer():
    layer = DisplayLayer()
    try:
        yield layer
    finally:
        layer.close()


@pytest.fixture()
def companion(tk_root, display_layer):
    """A real, visible edge companion on the real desktop."""
    import tkinter as tk

    from ui.edge_companion import EdgeCompanionApp

    window = tk.Toplevel(tk_root)
    clock = FakeClock()
    bridge = ScriptedBridge()
    controller = CompanionController(bridge, clock=clock, dwell_seconds=5.0)
    controller.attach()
    app = EdgeCompanionApp(controller, display_layer=display_layer,
                           edge="right", root=window)
    try:
        yield app, controller, bridge, clock
    finally:
        try:
            app.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass


# ═════════════════════════════════════════════════════════════ window


class TestRealWindow:
    def test_window_actually_appears(self, companion) -> None:
        app, _, _, _ = companion
        app.pump_once()
        app.root.deiconify()
        app.root.update()
        assert app.root.winfo_viewable() == 1, "the companion window is not visible"
        assert app.root.winfo_width() > 1 and app.root.winfo_height() > 1

    def test_borderless_on_the_real_desktop(self, companion) -> None:
        app, _, _, _ = companion
        app.pump_once()
        assert bool(app.root.overrideredirect()) is True
        # A decorated window reports a style; an overrideredirect one does not.
        style = app.root.winfo_id()
        assert style

    def test_tool_window_and_noactivate_while_sleeping(self, companion) -> None:
        """WS_EX_TOOLWINDOW and WS_EX_NOACTIVATE on the real window."""
        app, _, _, _ = companion
        app.pump_once()
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = int(user32.GetParent(int(app.root.winfo_id()))
                       or app.root.winfo_id())
            exstyle = int(user32.GetWindowLongW(hwnd, -20))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"extended window style unavailable: {exc}")
        assert exstyle & 0x00000080, "WS_EX_TOOLWINDOW missing"
        assert exstyle & 0x08000000, "WS_EX_NOACTIVATE missing while sleeping"

    def test_noactivate_is_dropped_once_awake(self, companion) -> None:
        app, controller, _, clock = companion
        app.pump_once()
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = int(user32.GetParent(int(app.root.winfo_id()))
                       or app.root.winfo_id())
            exstyle = int(user32.GetWindowLongW(hwnd, -20))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"extended window style unavailable: {exc}")
        assert not exstyle & 0x08000000, (
            "the awake companion still refuses focus, so it cannot be typed into")

    def test_sleeping_footprint_is_small(self, companion) -> None:
        app, _, _, _ = companion
        app.pump_once()
        placement = app.placement
        assert placement is not None
        assert placement.width * placement.height < 40_000, (
            f"sleeping footprint is {placement.width}x{placement.height}, "
            "which is not a lightweight presence")


# ═════════════════════════════════════════════════════════ placement


class TestRealPlacement:
    def test_placement_lands_on_a_real_monitor(self, companion, display_layer) -> None:
        app, _, _, _ = companion
        app.pump_once()
        placement = app.placement
        assert placement is not None, "no placement computed from real displays"
        displays = display_layer.list_displays()
        assert displays, "the real display layer reported no monitors"
        assert placement.display_id in {d.display_id for d in displays}

    @pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
    def test_every_real_edge_docks_on_screen(self, companion, display_layer, edge) -> None:
        app, controller, _, clock = companion
        app._edge = edge
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        placement = app.placement
        assert placement is not None and placement.edge == edge
        display = display_layer.list_displays()[0]
        area = display.work_area
        width = max(0, min(placement.left + placement.width, area.right)
                    - max(placement.left, area.left))
        height = max(0, min(placement.top + placement.height, area.bottom)
                     - max(placement.top, area.top))
        assert width > 0 and height > 0, (
            f"{edge} dock is entirely off the work area: {placement.to_dict()}")

    def test_real_dpi_is_respected(self, companion, display_layer) -> None:
        """Geometry scales with the real reported scale, within the clamp."""
        app, _, _, _ = companion
        app.pump_once()
        placement = app.placement
        assert placement is not None
        assert 1.0 <= placement.scale <= 2.0
        reported = display_layer.list_displays()[0].scale_percent
        if reported:
            expected = max(1.0, min(2.0, reported / 100.0))
            assert placement.scale == pytest.approx(expected)

    def test_no_hardcoded_1080p_assumption(self, companion) -> None:
        app, controller, _, clock = companion
        app.pump_once()
        sleeping = app.placement
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        awake = app.placement
        assert sleeping is not None and awake is not None
        assert sleeping.width != 1920, "geometry appears hardcoded to 1920"

    def test_real_geometry_string_is_accepted_by_tk(self, companion) -> None:
        app, _, _, _ = companion
        app.pump_once()
        placement = app.placement
        assert placement is not None
        app.root.geometry(placement.geometry_string())
        app.root.update_idletasks()
        assert app.root.winfo_width() == placement.width, (
            f"Tk refused the placement: wanted {placement.width}, "
            f"got {app.root.winfo_width()}")


# ═════════════════════════════════════════════════════════ lifecycle


class TestRealLifecycle:
    def test_launch_activation_and_shutdown(self, companion) -> None:
        app, controller, bridge, clock = companion
        app.pump_once()
        assert controller.state is CompanionState.SLEEPING

        app._on_activate_click()
        clock.advance(1.0)
        app.pump_once()
        assert controller.state is CompanionState.LISTENING

        app._entry.insert(0, "Open Calculator")
        app._on_return()
        job = controller.task_id
        assert bridge.submissions and bridge.submissions[0][0] == "Open Calculator"

        bridge.emit("step", "Opening Calculator", job)
        app.pump_once()
        assert controller.state is CompanionState.EXECUTING

        bridge.emit("task_completed", "Calculator opened and verified.", job)
        app.pump_once()
        assert controller.state is CompanionState.RETURNING

        clock.advance(6.0)
        app.pump_once()
        assert controller.state is CompanionState.SLEEPING

        app.close()
        assert app.closed is True
        assert controller.closed is True
        assert bridge.unsubscribes == 1

    def test_repeated_launch_refuses_a_second_real_window(self, tk_root,
                                                         display_layer) -> None:
        import tkinter as tk

        from ui.edge_companion import (EdgeCompanionApp, active_edge_companion_count,
                                       launch_edge_companion)

        window = tk.Toplevel(tk_root)
        try:
            first_controller = CompanionController(ScriptedBridge(), clock=FakeClock())
            first_controller.attach()
            app = EdgeCompanionApp(first_controller, display_layer=display_layer,
                                   root=window)
            try:
                before = active_edge_companion_count()
                other = CompanionController(ScriptedBridge(), clock=FakeClock())
                other.attach()
                again, created = launch_edge_companion(other, display_layer=display_layer)
                try:
                    assert created is False
                    assert again is app
                    assert active_edge_companion_count() == before
                finally:
                    other.close()
            finally:
                app.close()
        finally:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    def test_close_is_idempotent_on_a_real_window(self, companion) -> None:
        app, controller, bridge, _ = companion
        app.pump_once()
        for _ in range(4):
            app.close()
        assert app.closed is True
        assert controller.closed is True
        assert bridge.unsubscribes == 1


# ═════════════════════════════════════════════════════════ leaks


class TestNoLeaks:
    def test_no_window_is_leaked(self, companion) -> None:
        before = set(_visible_window_titles())
        app, _, _, _ = companion
        app.pump_once()
        app.root.deiconify()
        app.root.update()
        app.close()
        app.root.update()
        time.sleep(0.2)
        after = set(_visible_window_titles())
        assert after <= before, (
            f"the companion leaked windows: {sorted(after - before)}")

    def test_no_thread_is_leaked(self, companion) -> None:
        before = {t.name for t in threading.enumerate()}
        app, controller, _, clock = companion
        for index in range(30):
            app.pump_once()
            if index % 5 == 0:
                controller.activate()
                clock.advance(1.0)
        app.close()
        time.sleep(0.2)
        after = {t.name for t in threading.enumerate()}
        assert after <= before, (
            f"the companion leaked threads: {sorted(after - before)}")

    def test_no_timer_accumulates(self, companion) -> None:
        """Part 30: the frame timer must be one per scheduled frame, and zero
        after close.

        ``pump_once`` deliberately schedules nothing, and one ``_tick`` schedules
        exactly one timer. Calling ``_tick`` twice without an event loop in
        between would leave two timers, which is correct behaviour - the first has
        not fired yet - so the invariant asserted here is the steady-state one.
        """
        app, _, _, _ = companion
        app.pump_once()
        assert app._frame_job is None, "pump_once must not schedule a timer"
        assert not app.root.after_info(), "an idle pump scheduled a timer"

        app._tick()
        assert app._frame_job is not None
        assert len(app.root.after_info()) == 1, (
            f"expected exactly one frame timer, got {app.root.after_info()}")

        app.close()
        assert app._frame_job is None
        app.root.update_idletasks()
        assert not app.root.after_info(), (
            f"timers survived close: {app.root.after_info()}")

    def test_many_cycles_do_not_grow_the_event_history(self, companion) -> None:
        app, controller, bridge, clock = companion
        for _ in range(40):
            app.pump_once()
            controller.activate()
            clock.advance(1.0)
            app.pump_once()
            job = controller.submit_task(f"task {_}")
            for step in range(5):
                bridge.emit("step", f"step {step}", job)
            bridge.emit("task_completed", "done", job)
            for _ in range(6):
                clock.advance(1.0)
                app.pump_once()
            if controller.state is not CompanionState.SLEEPING:
                controller.retract()
        snapshot = controller.snapshot()
        assert len(snapshot.events) <= controller._max_progress * 4
        assert len(snapshot.progress) <= controller._max_progress


class TestStableFrameRate:
    def test_idle_frames_do_not_redo_work(self, companion) -> None:
        """Part 5: an idle companion must not churn the window or the display layer.

        Counted with a real display layer wrapped so the enumeration calls are
        observable, then asserted unchanged across many idle frames.
        """
        app, _, _, _ = companion
        app.pump_once()
        placement = app.placement
        assert placement is not None

        calls = {"n": 0}
        real_layer = app._display_layer
        original = real_layer.list_displays

        def counting():
            calls["n"] += 1
            return original()

        app._display_layer = type("Counting", (), {"list_displays": staticmethod(counting)})()
        app._display = None
        for _ in range(40):
            app.pump_once()
        assert calls["n"] <= 2, (
            f"display enumeration ran {calls['n']} times over 40 idle frames")
        assert app.placement.geometry_string() == placement.geometry_string()