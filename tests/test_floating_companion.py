"""Headless-first tests for the S2.4 floating companion UI."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from core.events import EventBus
from ui import (
    AgentBridgeError,
    AgentEvent,
    CoreEventBusBridge,
    MockAgentBridge,
    UIController,
    UIStatus,
)


class FailingBridge:
    def __init__(self):
        self.events = []
        self.closed = False

    def submit_message(self, text, task_id=""):
        raise RuntimeError("bridge offline")

    def cancel_task(self, task_id=""):
        return False

    def subscribe_to_events(self, callback):
        self.events.append(callback)
        return lambda: None

    def current_status(self):
        return UIStatus.ERROR

    def close(self):
        self.closed = True


def controller_with_bridge(**kwargs):
    bridge = MockAgentBridge(**kwargs)
    return UIController(bridge), bridge


def test_ui_controller_initialization_and_initial_state():
    controller, _ = controller_with_bridge()
    assert controller.state is UIStatus.READY
    assert controller.status_text == "Ready"
    assert controller.is_busy is False
    assert controller.last_error is None
    assert controller.activities[0].role == "system"
    controller.close()


def test_ui_state_enum_aliases_and_serialization():
    assert UIStatus.from_value("done") is UIStatus.SUCCESS
    assert UIStatus.from_value("failed") is UIStatus.ERROR
    assert UIStatus.from_value("working") is UIStatus.EXECUTING
    assert json.dumps(UIStatus.THINKING.value)
    with pytest.raises(ValueError):
        UIStatus.from_value("not-a-state")


def test_agent_event_structured_serialization():
    event = AgentEvent(
        type="status",
        message="Working",
        status="executing",
        task_id="task-1",
        metadata={"source": "test"},
    )
    payload = event.to_dict()
    assert payload["type"] == "status"
    assert payload["metadata"] == {"source": "test"}
    assert json.loads(json.dumps(payload)) == payload
    assert AgentEvent.from_dict(payload).to_dict() == payload


def test_send_message_enters_thinking_state():
    controller, _ = controller_with_bridge()
    result = controller.send_message("Build a scene")
    assert result.success is True
    assert result.job_id
    assert controller.state is UIStatus.THINKING
    assert controller.active_job_ids == (result.job_id,)
    controller.close()


def test_empty_message_is_rejected_without_activity():
    controller, _ = controller_with_bridge()
    before = len(controller.activities)
    result = controller.send_message("   ")
    assert result.success is False
    assert result.error["error"] == "empty_message"
    assert controller.state is UIStatus.READY
    assert len(controller.activities) == before + 1
    controller.close()


def test_mock_bridge_event_sequence_reaches_success():
    controller, _ = controller_with_bridge()
    controller.send_message("hello")
    assert controller.wait_for_idle(1.0)
    assert controller.state is UIStatus.SUCCESS
    texts = [activity.text for activity in controller.activities]
    assert "Message received" in texts
    assert "Executing request" in texts
    assert "Verifying result" in texts
    assert "Task completed" in texts
    assert "Mock response: hello" in texts
    assert controller.last_error is None
    controller.close()


def test_mock_bridge_error_state_and_last_error():
    controller, _ = controller_with_bridge(fail_on="fail")
    controller.send_message("please fail")
    assert controller.wait_for_idle(1.0)
    assert controller.state is UIStatus.ERROR
    assert controller.last_error["error"] == "agent_error"
    assert "Mock bridge failure" in controller.last_error["message"]
    controller.close()


def test_bridge_exception_is_normalized():
    bridge = FailingBridge()
    controller = UIController(bridge)
    result = controller.send_message("hello")
    assert result.success is True
    assert controller.wait_for_idle(1.0)
    assert controller.state is UIStatus.ERROR
    assert controller.last_error["error"] == "bridge_failure"
    assert "bridge offline" in controller.last_error["message"]
    assert bridge.closed is False
    controller.close()


def test_event_queue_preserves_order():
    controller, bridge = controller_with_bridge()
    events = [
        AgentEvent(type="status", message="one", status="thinking"),
        AgentEvent(type="status", message="two", status="executing"),
        AgentEvent(type="message", message="three", metadata={"role": "diablo"}),
        AgentEvent(type="status", message="four", status="success"),
    ]
    for event in events:
        bridge._emit(event)
    assert controller.event_queue_size == 4
    assert controller.pump_events() == 4
    assert [activity.text for activity in controller.activities[1:]] == [
        "one",
        "two",
        "three",
        "four",
    ]
    assert controller.state is UIStatus.SUCCESS
    controller.close()


def test_malformed_event_is_reported_without_crashing():
    controller, bridge = controller_with_bridge()
    bridge._emit({"message": "missing type"})
    assert controller.pump_events() == 1
    assert controller.last_error["error"] == "malformed_event"
    assert controller.state is UIStatus.ERROR
    assert any(activity.role == "error" for activity in controller.activities)
    controller.close()


def test_worker_runs_off_ui_caller_thread():
    controller, _ = controller_with_bridge(step_delay=0.01)
    caller_thread = threading.get_ident()
    bridge_threads = []
    original_submit = controller.bridge.submit_message

    def capture_submit(text, task_id=""):
        bridge_threads.append(threading.get_ident())
        return original_submit(text, task_id=task_id)

    controller.bridge.submit_message = capture_submit
    controller.send_message("background")
    assert controller.wait_for_idle(1.0)
    assert bridge_threads
    assert bridge_threads[0] != caller_thread
    controller.close()


def test_controller_pump_is_non_blocking_while_worker_runs():
    controller, _ = controller_with_bridge(step_delay=0.05)
    controller.send_message("slow")
    started = time.perf_counter()
    controller.pump_events()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.02
    assert controller.is_busy is True
    assert controller.wait_for_idle(1.0)
    controller.close()


def test_repeated_submissions_are_queued_with_distinct_ids():
    controller, _ = controller_with_bridge(step_delay=0.02)
    first = controller.send_message("first")
    second = controller.send_message("second")
    assert first.job_id != second.job_id
    assert len(controller.active_job_ids) == 2
    assert controller.wait_for_idle(1.0)
    texts = [activity.text for activity in controller.activities]
    assert "first" in texts
    assert "second" in texts
    assert texts.count("Mock response: first") == 1
    assert texts.count("Mock response: second") == 1
    controller.close()


def test_cancel_task_uses_bridge_boundary():
    controller, bridge = controller_with_bridge(step_delay=0.2)
    result = controller.send_message("long task")
    assert result.success is True
    cancel = controller.cancel_task(result.job_id)
    assert cancel.success is True
    assert controller.wait_for_idle(1.0)
    assert controller.state is UIStatus.READY
    assert any("cancelled" in activity.text.lower() for activity in controller.activities)
    assert bridge.current_status() is UIStatus.READY
    controller.close()


def test_close_during_active_task_stops_worker_cleanly():
    controller, _ = controller_with_bridge(step_delay=0.2)
    controller.send_message("active task")
    controller.close(timeout=1.0)
    assert controller.state is UIStatus.CLOSED
    assert controller.is_busy is False
    assert controller.send_message("after close").success is False


def test_compact_mode_is_controller_state():
    controller, _ = controller_with_bridge()
    assert controller.compact is False
    assert controller.toggle_compact() is True
    assert controller.compact is True
    controller.set_compact(False)
    assert controller.compact is False
    controller.close()


def test_core_event_bus_bridge_adapts_existing_events():
    bus = EventBus()
    submitted = []
    bridge = CoreEventBusBridge(bus, submitter=lambda text, task_id="": submitted.append((text, task_id)))
    received = []
    bridge.subscribe_to_events(received.append)

    bus.emit_status("state", "thinking")
    bus.emit("task", {"lines": ["step one", "step two"]})
    bus.emit_tool("blender", "running", "render")
    bus.emit("task_done", {"steps": ["done"]})

    assert [event.status for event in received] == [
        UIStatus.THINKING.value,
        UIStatus.EXECUTING.value,
        UIStatus.EXECUTING.value,
        UIStatus.ERROR.value,
    ]
    assert received[1].message == "step one\nstep two"
    assert received[2].message == "blender: running"
    assert bridge.submit_message("hello", task_id="task-2") is None
    assert submitted == [("hello", "task-2")]
    bridge.close()


def test_core_event_bus_bridge_preserves_task_done_outcomes_and_isolates_listeners():
    bus = EventBus()
    bridge = CoreEventBusBridge(bus)
    received = []
    bridge.subscribe_to_events(lambda event: (_ for _ in ()).throw(RuntimeError("listener failure")))
    bridge.subscribe_to_events(received.append)
    bus.emit("task_done", {"status": "success", "outcome": "finished", "task_id": "task-success"})
    bus.emit("task_done", {"status": "failed", "outcome": "broken", "task_id": "task-failed"})
    bus.emit("task_done", {"status": "cancelled", "task_id": "task-cancelled"})
    bus.emit("task_done", {"outcome": "missing status", "task_id": "task-missing"})
    bus.emit("task_done", {"status": "unknown", "outcome": "bad status", "task_id": "task-unknown"})
    assert [event.status for event in received] == [
        UIStatus.SUCCESS.value,
        UIStatus.ERROR.value,
        UIStatus.CANCELLED.value,
        UIStatus.ERROR.value,
        UIStatus.ERROR.value,
    ]
    assert received[0].message == "finished"
    assert received[1].message == "broken"
    assert received[2].message == "Task cancelled"
    assert received[3].message == "missing status"
    assert received[0].task_id == "task-success"
    bridge.close()


def test_core_event_bus_bridge_without_submitter_fails_cleanly():
    bridge = CoreEventBusBridge(EventBus())
    with pytest.raises(AgentBridgeError):
        bridge.submit_message("hello")


def test_mock_bridge_rejects_empty_and_closed_messages():
    bridge = MockAgentBridge()
    with pytest.raises(AgentBridgeError):
        bridge.submit_message(" ")
    bridge.close()
    with pytest.raises(AgentBridgeError):
        bridge.submit_message("hello")


def test_mock_bridge_is_deterministic():
    first = MockAgentBridge()
    second = MockAgentBridge()
    first_events = []
    second_events = []
    first.subscribe_to_events(first_events.append)
    second.subscribe_to_events(second_events.append)
    first.submit_message("same", task_id="a")
    second.submit_message("same", task_id="a")
    assert [event.message for event in first_events] == [event.message for event in second_events]
    assert [event.status for event in first_events] == [event.status for event in second_events]
    first.close()
    second.close()


def test_standalone_ui_has_no_execution_backend_dependency():
    controller = UIController(MockAgentBridge())
    assert controller.bridge.current_status() is UIStatus.READY
    assert not hasattr(controller, "blender")
    assert not hasattr(controller, "vision")
    assert not hasattr(controller, "voice")
    controller.close()


def test_tkinter_companion_smoke_with_withdrawn_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")

    controller = UIController(MockAgentBridge())
    app = None
    try:
        from ui.companion import FloatingCompanionApp

        app = FloatingCompanionApp(controller=controller, root=root, always_on_top=False)
        app._input.insert("1.0", "hello from tk")
        handled = app._on_return(SimpleNamespace(state=0))
        assert handled == "break"
        root.update()
        assert controller.wait_for_idle(1.0)
        app._render()
        assert "Mock response: hello from tk" in app._activity.get("1.0", "end")
        assert app.set_compact(True) is True
        assert app.toggle_compact() is False
        assert app.always_on_top is False
        app.minimize()
        app.focus()
        app.close()
    finally:
        if app is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        else:
            root.destroy()
