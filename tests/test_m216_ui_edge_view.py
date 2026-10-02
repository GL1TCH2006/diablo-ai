"""M2.16.UI - tests for the edge companion view.

The view is the thinnest layer in the milestone, so these tests are mostly about
the properties a renderer can get wrong: leaking timers, keeping a window alive
after close, stealing focus while asleep, offering STOP when cancellation is not
available, and opening a second window on a repeated launch.

Frames are driven with :meth:`EdgeCompanionApp.pump_once` rather than by running
``mainloop``, so every test is deterministic and no timer survives the test. Tk is
optional in exactly the way the existing floating-companion tests treat it: a
missing Tk or a missing display skips rather than fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from ui.companion_state import CompanionController, CompanionState
from ui.edge import CompanionGeometry

pytest.importorskip("tkinter", reason="the companion view requires Tk")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBridge:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str]] = []
        self.cancellations: list[str] = []
        self.subscribe_calls = 0
        self.unsubscribes = 0
        self._handler: Any = None

    def subscribe_to_events(self, handler: Any) -> Any:
        self.subscribe_calls += 1
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


class FakeDisplay:
    def __init__(self, display_id: str, left: int, top: int,
                 right: int, bottom: int, *, primary: bool = True,
                 scale_percent: int | None = 100) -> None:
        from core.windows.coordinates import Rect

        self.display_id = display_id
        self.bounds = Rect.from_dict({"left": left, "top": top,
                                      "right": right, "bottom": bottom})
        self.work_area = self.bounds
        self.primary = primary
        self.scale_percent = scale_percent


class FakeDisplayLayer:
    def __init__(self, displays: list[FakeDisplay]) -> None:
        self._displays = displays
        self.calls = 0

    def list_displays(self) -> list[FakeDisplay]:
        self.calls += 1
        return list(self._displays)


PRIMARY = FakeDisplay(r"\\.\DISPLAY1", 0, 0, 1920, 1080)
SECOND = FakeDisplay(r"\\.\DISPLAY2", -1280, 0, 0, 1024, primary=False, scale_percent=125)


@pytest.fixture(scope="module")
def tk_root():
    """One ``Tk`` root for the whole module.

    Tk permits exactly one root per interpreter, and creating a second one after
    the first is destroyed fails with "can't find a usable init.tcl". Per-test roots
    therefore mean every test after the first silently skips. This root is created
    once and never destroyed by the tests; each test gets its own ``Toplevel``
    instead, which behaves as a real toplevel window - ``overrideredirect``,
    ``geometry`` and ``attributes`` all apply - and whose ``destroy()`` does not
    take the interpreter with it.
    """
    import tkinter as tk

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - no display
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    return root


@pytest.fixture()
def harness(tk_root):
    """A constructed view with manual frame control, torn down deterministically."""
    import tkinter as tk

    from ui.edge_companion import EdgeCompanionApp

    window = tk.Toplevel(tk_root)
    window.withdraw()

    clock = FakeClock()
    bridge = FakeBridge()
    controller = CompanionController(bridge, clock=clock, peek_seconds=0.28,
                                     dwell_seconds=6.0)
    controller.attach()
    app = EdgeCompanionApp(
        controller,
        display_layer=FakeDisplayLayer([PRIMARY]),
        geometry=CompanionGeometry(),
        root=window,
    )
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


class TestWindowShape:
    def test_window_is_borderless_and_topmost(self, harness) -> None:
        app, _, _, _ = harness
        # Tk's getters return 1/0 rather than True/False.
        assert bool(app.root.overrideredirect()) is True, "the companion has a title bar"
        assert bool(app.root.attributes("-topmost")) is True

    def test_window_is_not_in_the_taskbar(self, harness) -> None:
        """WS_EX_TOOLWINDOW keeps the presence out of the taskbar and Alt-Tab.

        A frame is pumped first on purpose: the style is applied *after* the
        geometry commit, because Tk's layered-window handling for
        ``-transparentcolor`` overwrites the extended style during idle
        processing. Asserting before the first frame would be asserting the wrong
        thing.
        """
        app, _, _, _ = harness
        app.pump_once()
        import ctypes

        try:
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetParent(int(app.root.winfo_id())) or app.root.winfo_id())
            style = int(user32.GetWindowLongW(hwnd, -20))
        except Exception:  # noqa: BLE001 - non-Windows or unmapped
            pytest.skip("extended window style unavailable")
        assert style & 0x00000080, "companion is missing WS_EX_TOOLWINDOW"

    def test_expanded_size_respects_the_accessibility_minimum(self, harness) -> None:
        app, controller, _, clock = harness
        app.pump_once()
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        assert app.placement is not None
        assert app.placement.width >= 260 and app.placement.height >= 200

    def test_sleeping_geometry_is_much_smaller_than_expanded(self, harness) -> None:
        app, controller, _, clock = harness
        app.pump_once()
        sleeping = app.placement
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        awake = app.placement
        assert sleeping is not None and awake is not None
        assert sleeping.width * sleeping.height < (awake.width * awake.height) / 4

    def test_sleeping_companion_is_docked_to_the_work_area_edge(self, harness) -> None:
        app, _, _, _ = harness
        app.pump_once()
        assert app.placement is not None
        assert app.placement.edge == "right"

    def test_no_display_information_keeps_the_last_position(self, harness) -> None:
        """A display failure must not teleport the companion to 0,0."""
        app, _, _, _ = harness
        app.pump_once()
        good = app.placement.geometry_string()

        # Invalidate the cache and make enumeration genuinely fail.
        app._display_layer = FakeDisplayLayer([])
        app._display = None

        assert app._apply_placement() is None, "placed a window with no display data"
        assert app.placement.geometry_string() == good, (
            "a display lookup failure moved the companion")


class TestInteraction:
    def test_click_activates(self, harness) -> None:
        app, controller, _, _ = harness
        app.pump_once()
        assert app._on_activate_click() == "break"
        assert controller.state is CompanionState.AWAKENING

    def test_enter_submits_and_clears_the_entry(self, harness) -> None:
        app, controller, bridge, clock = harness
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        app._entry.insert(0, "Open Calculator")
        assert app._on_return() == "break"
        # Submission is handed to the worker, so the bridge records it a moment
        # later. The view itself returned immediately, which is the point.
        assert controller.wait_idle(5.0)
        assert bridge.submissions and bridge.submissions[0][0] == "Open Calculator"
        assert app._entry.get() == ""
        assert controller.state is CompanionState.THINKING

    def test_stop_is_hidden_until_cancellation_is_real(self, harness) -> None:
        """``winfo_manager``, not ``winfo_ismapped``: the test root is withdrawn, so
        no child is ever mapped, which would make the assertion vacuous."""
        app, controller, _, clock = harness
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        assert not app._stop.winfo_manager(), "STOP offered while idle"

        controller.submit_task("long task")
        app.pump_once()
        assert app._stop.winfo_manager() == "pack", "STOP missing during execution"

        app._on_stop()
        app.pump_once()
        assert app._stop.winfo_manager() == "pack", (
            "STOP vanished once requested; the user must see the request registered")
        assert app._stop.cget("text") == "STOPPING"

        controller.retract()
        app.pump_once()
        assert not app._stop.winfo_manager(), "STOP survived retraction"

    def test_stop_requests_but_does_not_claim_cancellation(self, harness) -> None:
        app, controller, bridge, clock = harness
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        controller.submit_task("long task")
        job = controller.task_id
        app.pump_once()
        assert app._on_stop() == "break"
        assert bridge.cancellations == [job]
        assert controller.state is CompanionState.THINKING, (
            "the UI claimed the task stopped on button press")
        app.pump_once()
        assert app._stop.cget("text") == "STOPPING", (
            "the UI did not show that the cancellation request was registered")

    def test_escape_cancels_an_active_task(self, harness) -> None:
        app, controller, bridge, clock = harness
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        controller.submit_task("long task")
        job = controller.task_id
        assert app._on_escape() == "break"
        assert bridge.cancellations == [job]
        assert controller.state is CompanionState.THINKING

    def test_escape_dismisses_when_idle(self, harness) -> None:
        app, controller, _, clock = harness
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        assert app._on_escape() == "break"
        assert controller.state is CompanionState.SLEEPING

    def test_entry_is_disabled_while_sleeping(self, harness) -> None:
        app, controller, _, clock = harness
        app.pump_once()
        assert str(app._entry.cget("state")) == "disabled", (
            "a sliver offers a text field")
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        assert str(app._entry.cget("state")) == "normal"

    def test_render_is_skipped_when_nothing_changed(self, harness) -> None:
        """Idle frames must not rewrite every widget."""
        app, _, _, _ = harness
        app.pump_once()
        first = app._last_render_key
        app.pump_once()
        app.pump_once()
        assert app._last_render_key == first

    def test_progress_is_rendered(self, harness) -> None:
        app, controller, bridge, clock = harness
        controller.activate()
        clock.advance(1.0)
        app.pump_once()
        controller.submit_task("open calculator")
        job = controller.task_id
        bridge.emit("step_started", "Opening Calculator", task_id=job)
        app.pump_once()
        body = app._progress.get("1.0", "end")
        assert "Opening Calculator" in body


class TestLifecycle:
    def test_close_is_idempotent_and_unsubscribes_once(self, harness) -> None:
        app, controller, bridge, _ = harness
        app.close()
        app.close()
        assert app.closed is True
        assert controller.closed is True
        assert bridge.unsubscribes == 1

    def test_no_timer_survives_close(self, harness) -> None:
        app, _, _, _ = harness
        app._frame_job = app.root.after(10000, lambda: None)
        job = app._frame_job
        app.close()
        assert app._frame_job is None
        # after_info must not still hold the cancelled id.
        assert job not in app.root.after_info()

    def test_pump_after_close_is_a_no_op(self, harness) -> None:
        app, _, _, _ = harness
        app.close()
        assert app.pump_once() is not None

    def test_repeated_launch_focuses_instead_of_duplicating(self, harness) -> None:
        from ui.edge_companion import active_edge_companion_count, launch_edge_companion

        app, controller, _, _ = harness
        before = active_edge_companion_count()
        other_controller = CompanionController(FakeBridge(), clock=FakeClock())
        other_controller.attach()
        again, created = launch_edge_companion(other_controller)
        try:
            assert created is False
            assert again is app
            assert active_edge_companion_count() == before
        finally:
            other_controller.close()

    def test_close_removes_the_window_from_the_registry(self, harness) -> None:
        from ui.edge_companion import active_edge_companion_count

        app, _, _, _ = harness
        assert active_edge_companion_count() >= 1
        app.close()
        assert active_edge_companion_count() == 0


class TestMultiMonitor:
    def test_follows_the_cursor_to_another_monitor(self, harness) -> None:
        app, _, _, _ = harness
        app._display_layer = FakeDisplayLayer([PRIMARY, SECOND])
        app.root.winfo_pointerxy = lambda: (-400, 200)  # type: ignore[method-assign]
        app.redock()
        assert app.placement is not None
        assert app.placement.display_id == SECOND.display_id
        assert app.placement.scale == pytest.approx(1.25)

    def test_monitor_change_triggers_a_redock(self, harness) -> None:
        app, _, _, _ = harness
        app.pump_once()
        assert app.display_id == PRIMARY.display_id
        layer = FakeDisplayLayer([SECOND])
        app._display_layer = layer
        # The change check is rate limited; clear the limiter to force the branch.
        app._last_monitor_check = 0.0
        app._check_monitor_change()
        assert app.display_id == SECOND.display_id
        assert layer.calls >= 1

    def test_monitor_polling_is_rate_limited(self, harness) -> None:
        """Part 5 forbids constant polling; enumeration must not run every frame."""
        app, _, _, _ = harness
        layer = FakeDisplayLayer([PRIMARY])
        app._display_layer = layer
        app._last_monitor_check = 1e18  # pretend we just checked
        layer.calls = 0
        for _ in range(50):
            app.pump_once()
        assert layer.calls == 0, "display enumeration ran on every frame"


class TestSecurity:
    def test_module_registers_no_global_input_hooks(self) -> None:
        """Part 5: no global keyboard hook, no global mouse hook, ever."""
        import pathlib

        import ui.edge_companion as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("SetWindowsHookEx", "RegisterHotKey", "WH_KEYBOARD_LL",
                          "WH_MOUSE_LL", "GetAsyncKeyState", "GetKeyState"):
            assert forbidden not in source, f"{forbidden} would be a global hook"

    def test_view_does_not_import_a_windows_capability(self) -> None:
        """The UI is a client. It must not reach a capability or the Agent."""
        import pathlib

        import ui.edge_companion as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("SendInput", "SetCursorPos", "mouse_event", "keybd_event",
                          "CapabilityRegistry", "from core.agent", "import agent"):
            assert forbidden not in source, f"the view reached {forbidden}"

    def test_no_vlm_or_browser_dependency_was_added(self) -> None:
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        deps = set(config.get("project", {}).get("dependencies", []) or [])
        names = {d.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
                 for d in deps}
        for forbidden in ("PySide6", "PyQt6", "electron", "selenium", "playwright"):
            assert not any(forbidden.lower() in n for n in names), (
                f"{forbidden} was added as a dependency")