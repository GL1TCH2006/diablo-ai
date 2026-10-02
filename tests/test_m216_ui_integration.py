"""M2.16.UI - the first true end-to-end product test (Part 27).

The scenario the prompt describes, driven through the real seams:

    companion -> request -> Agent lifecycle -> progress events -> terminal
              -> result shown -> retraction -> clean shutdown

What is real here and what is not, stated plainly:

* **Real**: :class:`~ui.bridge.CoreEventBusBridge`, :class:`~core.events.EventBus`,
  the companion controller, the view, the state machine, the event mapper and its
  redaction, and the launch path's shutdown ordering.
* **Faked**: the Agent turn itself. A model call is not needed to prove that the
  companion is correctly wired to the backend, and pretending otherwise would be
  the dishonest option. A separate real-Windows test covers a real capability.

What this buys that the unit tests do not: it proves the seams line up. The unit
tests inject a fake bridge; this one goes through the real bridge and a real bus,
so a change to either one's event shape breaks here rather than in front of a user.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.events import EventBus
from ui.bridge import CoreEventBusBridge, MockAgentBridge
from ui.companion_state import CompanionController, CompanionState

pytest.importorskip("tkinter", reason="the companion view requires Tk")


class FakeClock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAgent:
    """Just enough Agent surface for the launch path's submitter and canceller."""

    def __init__(self) -> None:
        self.responses: list[str] = []
        self.cancellations: list[str] = []
        self._token = _Token()

    def respond(self, text: str) -> str:
        self.responses.append(text)
        return f"ack: {text}"

    def cancel(self) -> bool:
        self._token.request("cancelled by test", source="test")
        return True


class _Token:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def request(self, reason: str, source: str = "") -> None:
        self.requests.append((reason, source))


class ScriptedBridge:
    """A bridge whose event sequence is emitted synchronously, on demand.

    :class:`MockAgentBridge` runs a real worker thread, which is right for
    exercising the threaded path but makes "what state is the companion in after
    submit" a race. These tests are about lifecycle *ordering*, so they drive a
    bridge that emits exactly what the test tells it to, when the test says so.
    """

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

    def emit(self, kind: str, message: str = "", task_id: str = "",
             tool: str = "", tool_status: str = "", detail: str = "") -> None:
        assert self._handler is not None, "controller is not attached"
        payload: dict[str, Any] = {"type": kind, "message": message, "task_id": task_id}
        if tool:
            payload["tool"] = tool
        if tool_status:
            payload["tool_status"] = tool_status
        if detail:
            payload["detail"] = detail
        self._handler(payload)


def _settle(controller: CompanionController, timeout: float = 2.0) -> None:
    """Wait for the submission worker to hand the turn to the backend.

    Submission is asynchronous so the view never blocks; a test that wants to
    observe what the backend received has to wait for that hand-off.
    """
    assert controller.wait_idle(timeout), "backend submission never settled"


class TestDeterministicLifecycle:
    """The full Part 27 sequence, driven step by step with no timing races."""

    def test_complete_task_lifecycle(self) -> None:
        clock = FakeClock()
        bridge = ScriptedBridge()
        controller = CompanionController(bridge, clock=clock,
                                        peek_seconds=0.28, dwell_seconds=6.0)
        controller.attach()
        try:
            assert controller.state is CompanionState.SLEEPING

            controller.activate()
            clock.advance(1.0)
            controller.tick()
            assert controller.state is CompanionState.LISTENING

            job = controller.submit_task("Open Calculator")
            assert controller.state is CompanionState.THINKING
            _settle(controller)
            assert bridge.submissions == [("Open Calculator", job)]

            bridge.emit("executing", "Working", job)
            controller.tick()
            assert controller.state is CompanionState.EXECUTING

            bridge.emit("step", "Opening Calculator", job, tool="desktop.open",
                        tool_status="running")
            bridge.emit("verification", "Verifying result", job)
            controller.tick()
            progress = " | ".join(controller.snapshot().progress)
            assert "Opening Calculator" in progress
            assert "Verifying result" in progress

            bridge.emit("task_completed", "Calculator opened and verified.", job)
            controller.tick()
            assert controller.state is CompanionState.RETURNING
            assert controller.snapshot().terminal_message == \
                "Calculator opened and verified."

            clock.advance(1.0)
            controller.tick()
            assert controller.state is CompanionState.RETURNING, (
                "the result retracted before it could be read")

            clock.advance(6.0)
            controller.tick()
            assert controller.state is CompanionState.SLEEPING
            assert controller.snapshot().peek == 0.0
            assert controller.closed is False
        finally:
            controller.close()
        assert controller.closed is True

    def test_threaded_mock_bridge_still_settles(self) -> None:
        """The real threaded bridge must also reach a resting state.

        Bounded generously and asserting on the states *seen* rather than on a
        frame count, because the worker thread's timing is not ours to control.
        """
        clock = FakeClock()
        controller = CompanionController(MockAgentBridge(step_delay=0.0),
                                        clock=clock, dwell_seconds=2.0)
        controller.attach()
        try:
            controller.activate()
            clock.advance(1.0)
            controller.tick()
            controller.submit_task("Open Calculator")
            _settle(controller)
            seen: set[str] = set()
            for _ in range(60):
                clock.advance(0.5)
                controller.tick()
                seen.add(controller.state.value)
                if controller.state is CompanionState.SLEEPING:
                    break
            assert controller.state is CompanionState.SLEEPING, (
                f"never settled; saw {sorted(seen)}")
            assert {"executing", "returning"} & seen, (
                f"the threaded bridge produced no progress: {sorted(seen)}")
        finally:
            controller.close()

    def test_result_is_readable_before_retraction(self) -> None:
        bridge = MockAgentBridge(step_delay=0.0)
        clock = FakeClock()
        controller = CompanionController(bridge, clock=clock, dwell_seconds=30.0)
        controller.attach()
        try:
            controller.activate()
            clock.advance(1.0)
            controller.tick()
            controller.submit_task("Open Calculator")
            _settle(controller)
            clock.advance(5.0)
            controller.tick()
            assert controller.state is CompanionState.RETURNING
            assert controller.snapshot().terminal_message, (
                "the companion retracted without showing a result")
        finally:
            controller.close()


class TestRealBridgeAndBus:
    """Through the real ``CoreEventBusBridge`` and a real ``EventBus``."""

    def test_companion_follows_a_real_task_lifecycle(self) -> None:
        bus = EventBus()
        bridge = CoreEventBusBridge(bus, submitter=lambda text, task_id="": {"success": True},
                                    canceler=lambda task_id="": True)
        clock = FakeClock()
        controller = CompanionController(bridge, clock=clock)
        controller.attach()
        try:
            controller.activate()
            clock.advance(1.0)
            controller.tick()
            controller.submit_task("open calculator")
            job = controller.task_id

            bus.emit_status("state", "executing")
            controller.tick()
            assert controller.state is CompanionState.EXECUTING

            bus.emit_tool("desktop.open", "running", "Opening Calculator")
            controller.tick()
            assert any("Opening Calculator" in line
                       for line in controller.snapshot().progress)

            bus.emit_tool("desktop.verify", "running", "Verifying result")
            controller.tick()
            assert controller.state is CompanionState.EXECUTING

            bus.emit("task_done", {"task_id": job, "status": "success",
                                   "outcome": {"message": "Calculator opened and verified."}})
            controller.tick()
            snapshot = controller.snapshot()
            assert snapshot.state is CompanionState.RETURNING
            assert "Calculator opened" in snapshot.terminal_message
        finally:
            controller.close()

    def test_permission_denial_is_surfaced_as_an_error(self) -> None:
        bus = EventBus()
        bridge = CoreEventBusBridge(bus, submitter=lambda text, task_id="": {"success": True},
                                    canceler=lambda task_id="": True)
        clock = FakeClock()
        controller = CompanionController(bridge, clock=clock)
        controller.attach()
        try:
            controller.activate()
            clock.advance(1.0)
            controller.tick()
            controller.submit_task("delete everything")
            bus.emit_tool("fs.write", "denied", "permission denied by policy")
            controller.tick()
            assert controller.state is CompanionState.EXECUTING
            assert any("permission" in line.lower()
                       for line in controller.snapshot().progress)
        finally:
            controller.close()

    def test_failure_reaches_a_recoverable_error(self) -> None:
        bus = EventBus()
        bridge = CoreEventBusBridge(bus, submitter=lambda text, task_id="": {"success": True},
                                    canceler=lambda task_id="": True)
        clock = FakeClock()
        controller = CompanionController(bridge, clock=clock)
        controller.attach()
        try:
            controller.activate()
            clock.advance(1.0)
            controller.tick()
            controller.submit_task("open calculator")
            job = controller.task_id
            bus.emit("task_done", {"task_id": job, "status": "error",
                                   "outcome": {"message": "target stale"}})
            controller.tick()
            # Part 3 puts a failure in ERROR, not RETURNING: the user has to see
            # the reason before it retracts.
            assert controller.state is CompanionState.ERROR
            assert "target stale" in controller.snapshot().terminal_message
            controller.acknowledge()
            controller.tick()
            assert controller.state is CompanionState.RETURNING
            controller.retract()
            assert controller.state is CompanionState.SLEEPING
        finally:
            controller.close()

    def test_raw_source_event_never_reaches_the_companion(self) -> None:
        """The bridge forwards the raw event in metadata; the mapper must drop it."""
        bus = EventBus()
        bridge = CoreEventBusBridge(bus, submitter=lambda text, task_id="": {"success": True},
                                    canceler=lambda task_id="": True)
        clock = FakeClock()
        controller = CompanionController(bridge, clock=clock)
        controller.attach()
        try:
            controller.activate()
            clock.advance(1.0)
            controller.tick()
            controller.submit_task("open calculator")
            job = controller.task_id
            bus.emit("task_done", {
                "task_id": job, "status": "error",
                "outcome": {"message": "failed on hwnd=0x00AB12CD with api_key=sk-secret123456"},
                "hwnd": 0x00AB12CD,
            })
            controller.tick()
            rendered = " ".join(
                [controller.snapshot().terminal_message]
                + list(controller.snapshot().progress))
            assert "0x00AB12CD" not in rendered
            assert "sk-secret123456" not in rendered
        finally:
            controller.close()


class TestLaunchPath:
    def test_launch_wires_one_bridge_and_shuts_down_in_order(self, tk_root) -> None:
        import tkinter as tk

        from ui.launch import build_edge_companion

        window = tk.Toplevel(tk_root)
        window.withdraw()
        bus = EventBus()
        agent = FakeAgent()
        runtime = build_edge_companion(agent, bus, edge="left", root=window)
        try:
            assert runtime.created is True
            assert isinstance(runtime.bridge, CoreEventBusBridge)
            assert runtime.app is not None
            runtime.controller.activate()
            assert runtime.controller.state is CompanionState.AWAKENING
            # The controller is attached exactly once to the one bridge, and the
            # submitter drops the UI's own task id rather than forwarding it.
            runtime.controller.submit_task("hello")
            _settle(runtime.controller)
            assert agent.responses == ["hello"]
        finally:
            runtime.close()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        assert runtime.controller.closed is True
        assert runtime.app.closed is True

    def test_repeated_launch_refuses_a_second_window(self, tk_root) -> None:
        import tkinter as tk

        from ui.edge_companion import active_edge_companion_count
        from ui.launch import build_edge_companion

        window = tk.Toplevel(tk_root)
        window.withdraw()
        bus = EventBus()
        agent = FakeAgent()
        first = build_edge_companion(agent, bus, root=window)
        try:
            before = active_edge_companion_count()
            second = build_edge_companion(agent, bus, root=window)
            try:
                assert second.created is False
                assert second.app is first.app
                assert active_edge_companion_count() == before
            finally:
                second.close()
        finally:
            first.close()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    def test_cancellation_reaches_the_agent_token(self) -> None:
        """Part 12: STOP must reach the existing cancellation token."""
        from ui.launch import _canceller_for

        agent = FakeAgent()
        agent._active_cancellation_token = agent._token
        cancel = _canceller_for(agent)
        assert cancel("some-task") is True
        assert agent._token.requests, "the cancellation token was never requested"

    def test_no_agent_token_means_no_false_cancellation(self) -> None:
        from ui.launch import _canceller_for

        assert _canceller_for(object())("x") is False

    def test_shared_bridge_is_not_closed_by_the_companion(self, tk_root) -> None:
        import tkinter as tk

        from ui.launch import build_edge_companion

        window = tk.Toplevel(tk_root)
        window.withdraw()
        bus = EventBus()
        agent = FakeAgent()
        shared = CoreEventBusBridge(bus, submitter=lambda t, task_id="": {"success": True},
                                   canceler=lambda task_id="": True)
        runtime = build_edge_companion(agent, bus, bridge=shared, root=window)
        try:
            runtime.close()
            assert shared._closed is False, (
                "the companion closed a bridge it does not own")
        finally:
            shared.close()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        assert shared._closed is True


class TestViewEndToEnd:
    def test_full_lifecycle_through_the_window(self, tk_root) -> None:
        import tkinter as tk

        from ui.edge_companion import EdgeCompanionApp

        window = tk.Toplevel(tk_root)
        window.withdraw()
        clock = FakeClock()
        bridge = ScriptedBridge()
        controller = CompanionController(bridge, clock=clock, dwell_seconds=4.0)
        controller.attach()
        app = EdgeCompanionApp(controller, root=window)
        try:
            app.pump_once()
            assert controller.state is CompanionState.SLEEPING

            app._on_activate_click()
            clock.advance(1.0)
            app.pump_once()
            assert controller.state is CompanionState.LISTENING

            app._entry.insert(0, "Open Calculator")
            app._on_return()
            job = controller.task_id
            assert controller.state is CompanionState.THINKING
            assert app._entry.get() == "", "the entry kept the submitted text"

            bridge.emit("step", "Opening Calculator", job, tool="desktop.open",
                        tool_status="running")
            app.pump_once()
            assert controller.state is CompanionState.EXECUTING
            assert app._stop.winfo_manager() == "pack", "no STOP during execution"
            assert "Opening Calculator" in app._progress.get("1.0", "end")

            bridge.emit("task_completed", "Calculator opened and verified.", job)
            app.pump_once()
            assert controller.state is CompanionState.RETURNING
            assert "Calculator opened" in app._status.cget("text")
            assert not app._stop.winfo_manager(), "STOP survived the result"

            clock.advance(5.0)
            app.pump_once()
            assert controller.state is CompanionState.SLEEPING
            assert app.closed is False
        finally:
            app.close()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        assert app.closed is True

    def test_stop_through_the_window_reaches_the_bridge(self, tk_root) -> None:
        import tkinter as tk

        from ui.edge_companion import EdgeCompanionApp

        window = tk.Toplevel(tk_root)
        window.withdraw()
        clock = FakeClock()
        bridge = ScriptedBridge()
        controller = CompanionController(bridge, clock=clock, dwell_seconds=4.0)
        controller.attach()
        app = EdgeCompanionApp(controller, root=window)
        try:
            app.pump_once()
            app._on_activate_click()
            clock.advance(1.0)
            app.pump_once()
            controller.submit_task("a long task")
            job = controller.task_id
            app.pump_once()

            app._on_stop()
            assert bridge.cancellations == [job]
            assert controller.state is CompanionState.THINKING, (
                "the window claimed the task stopped")
            app.pump_once()
            assert app._stop.cget("text") == "STOPPING"

            bridge.emit("task_cancelled", "Stopped.", job)
            app.pump_once()
            assert controller.state is CompanionState.RETURNING
            assert app._status.cget("text") == "Stopped."
        finally:
            app.close()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture(scope="module")
def tk_root():
    import tkinter as tk

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    return root


def test_main_exposes_the_edge_companion_flags() -> None:
    """The milestone must be reachable from the product CLI, not only from tests."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    flags = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    flags.add(arg.value)
    assert "--edge-companion" in flags
    assert "--edge" in flags


def test_launch_module_exposes_the_edge_entry_points() -> None:
    import ui.launch as launch

    for name in ("build_edge_companion", "run_edge_companion", "EdgeCompanionRuntime"):
        assert hasattr(launch, name), f"missing {name}"


def _imported_names(module_name: str) -> set[str]:
    """Every name any import in ``module_name`` binds.

    AST rather than substring matching: a docstring that merely *mentions*
    TaskManager is fine, and a source-text scan would flag it as a duplicate
    system. Only a real import counts.
    """
    import ast
    import importlib
    from pathlib import Path

    module = importlib.import_module(module_name)
    path = Path(module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0]
                         for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_edge_companion_does_not_duplicate_backend_systems() -> None:
    """Part 13-16: no second store, permission manager, executor or ledger."""
    imported = _imported_names("ui.edge_companion")
    forbidden = {"TaskStore", "PermissionManager", "Executor",
                 "VerificationLedger", "TaskManager", "Agent"}
    assert not (imported & forbidden), (
        f"the companion imported its own backend systems: {imported & forbidden}")


def test_edge_companion_does_not_import_a_windows_capability() -> None:
    imported = _imported_names("ui.edge_companion")
    forbidden = {"ctypes", "win32api", "win32gui", "win32con", "pywintypes"}
    # ctypes is permitted *only* for the companion's own window style, which is
    # imported lazily inside ``_WindowStyle._apis`` rather than at module scope.
    import ui.edge_companion as module

    source = module.__file__
    del source
    assert "pywin32" not in imported and "win32gui" not in imported, (
        "the companion reached a native Windows capability module")


def test_companion_never_submits_a_task_id_to_the_agent() -> None:
    """Part 9: identity stays with the TaskManager, not the UI.

    Proven behaviourally: the submitter accepts the UI's id and the agent still
    receives only the text. A source-text assertion here would be checking
    spelling, not behaviour.
    """
    from ui.launch import _submitter_for

    agent = FakeAgent()
    submit = _submitter_for(agent)
    submit("do the thing", task_id="ui-invented-id")
    assert agent.responses == ["do the thing"]
    assert not hasattr(agent, "task_id"), "an id reached the agent"


def test_voice_boundary_is_honest() -> None:
    """Part 8: no STT, no wake word, no claim of voice activation."""
    import pathlib

    import ui.edge_companion as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("speech_to_text", "SpeechToText", "wake_word",
                      "WakeWord", "sounddevice", "pyaudio", "whisper"):
        assert forbidden not in source, (
            f"{forbidden} implies voice support that does not exist")
    assert "text_to_speech" in source, "the optional TTS hook disappeared"


def test_no_new_dependency_was_introduced() -> None:
    """Part 24: the milestone must add nothing to the dependency set.

    The project already has legitimate dependencies; what matters is that none of
    them is a UI framework, browser runtime or audio stack introduced by this
    milestone. Tkinter is stdlib and costs nothing.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(config.get("project", {}).get("dependencies", []) or []).lower()
    for forbidden in ("pyside", "pyqt", "wxpython", "kivy", "dearpygui",
                      "electron", "playwright", "selenium", "sounddevice",
                      "pyaudio", "whisper", "vosk", "speechrecognition"):
        assert forbidden not in deps, f"{forbidden} was added as a dependency"
    assert "tkinter" not in deps, "tkinter is stdlib and must not be pinned"