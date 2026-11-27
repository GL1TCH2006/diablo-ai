"""S2.6 validation and hardening tests."""

from __future__ import annotations

import threading
import time

import pytest

from blender import BlenderCapabilityLayer, BlenderRunner
from integration import BlenderVisionIntegration
from ui import AgentBridge, UIController, UIStatus
from ui.bridge import MockAgentBridge
from voice import (
    CancellationError,
    InterruptionManager,
    MockSpeechToTextBackend,
    MockTTSBackend,
    MockWakeWordDetector,
    SapiTTSBackend,
    VoiceCommand,
    VoiceCommandRouter,
    VoiceEvent,
    VoiceOutputRouter,
    VoiceService,
    VoiceUIAdapter,
)
from voice.stt import SpeechToTextUnavailable, UnavailableSpeechToTextBackend
from voice.tts import TTSBackend
from voice.wake_wording import DisabledWakeWordDetector


class BridgeFake:
    def __init__(self, cancel_result=True):
        self.cancel_result = cancel_result
        self.cancel_calls = []
        self.submitted = []

    def submit_message(self, text, task_id=""):
        self.submitted.append((text, task_id))
        return {"success": True}

    def cancel_task(self, task_id=""):
        self.cancel_calls.append(task_id)
        return self.cancel_result

    def current_status(self):
        return UIStatus.READY


class ExplodingTTS(TTSBackend):
    name = "exploding"

    def is_available(self):
        return True

    def speak(self, text, *, now=False):
        raise RuntimeError("unexpected tts failure")


class LongOperation:
    def __init__(self, token):
        self.token = token
        self.started = threading.Event()
        self.release = threading.Event()
        self.cleanup = []
        self.result = None

    def run(self):
        self.started.set()
        try:
            while not self.release.is_set():
                self.token.raise_if_cancelled()
                time.sleep(0.002)
            self.token.raise_if_cancelled()
            self.result = "completed"
        except CancellationError:
            self.result = "cancelled"
        finally:
            self.cleanup.append("released")
        return self.result


class RaceBridge:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.finished = False
        self.lock = threading.Lock()

    def submit_message(self, text, task_id=""):
        self.started.set()
        self.release.wait(1.0)
        with self.lock:
            self.finished = True
        return {"success": True}

    def cancel_task(self, task_id=""):
        self.cancelled.set()
        with self.lock:
            return not self.finished


def test_closed_ui_callback_is_not_reported_as_bridge_accepted():
    bridge = MockAgentBridge()
    controller = UIController(bridge)
    manager = InterruptionManager(cancel_callback=controller.cancel_task)
    controller.close()
    outcome = manager.request_cancel("closed-task")
    assert outcome.accepted is True
    assert outcome.bridge_accepted is False
    assert "closed" in outcome.error.lower()


def test_unexpected_tts_failure_is_isolated():
    output = VoiceOutputRouter(ExplodingTTS())
    result = output.handle_event(VoiceEvent(type="error", message="failure"))
    assert result.handled is True
    assert result.spoken is False
    assert result.error["error"] == "tts_backend_failure"
    assert "unexpected tts failure" in result.error["message"]


def test_long_running_operation_observes_cancellation_and_cleans_up():
    manager = InterruptionManager()
    token = manager.token_for("long-op")
    operation = LongOperation(token)
    events = []
    manager.subscribe(events.append)
    thread = threading.Thread(target=operation.run, daemon=True)
    thread.start()
    assert operation.started.wait(1.0)
    outcome = manager.request_cancel("long-op", source="voice", reason="user_interruption")
    thread.join(1.0)
    assert outcome.accepted is True
    assert operation.result == "cancelled"
    assert operation.cleanup == ["released"]
    assert [event.type for event in events] == ["task_cancel_requested", "task_cancelled"]


def test_finish_cancel_race_has_valid_outcome_and_cleanup():
    for _ in range(20):
        bridge = RaceBridge()
        manager = InterruptionManager(bridge)
        token = manager.token_for("race")
        operation = LongOperation(token)
        thread = threading.Thread(target=operation.run, daemon=True)
        thread.start()
        assert operation.started.wait(1.0)
        barrier = threading.Barrier(2)
        results = []

        def cancel():
            barrier.wait()
            results.append(manager.request_cancel("race"))

        def finish():
            barrier.wait()
            operation.release.set()

        cancel_thread = threading.Thread(target=cancel)
        finish_thread = threading.Thread(target=finish)
        cancel_thread.start()
        finish_thread.start()
        cancel_thread.join(1.0)
        finish_thread.join(1.0)
        thread.join(1.0)
        assert operation.cleanup == ["released"]
        assert operation.result in {"completed", "cancelled"}
        assert results[0].accepted is True
        assert results[0].request.task_id == "race"


def test_three_stop_commands_emit_one_request_and_one_completion():
    bridge = BridgeFake()
    manager = InterruptionManager(bridge)
    events = []
    manager.subscribe(events.append)
    first = manager.request_cancel("task")
    second = manager.request_cancel("task")
    third = manager.request_cancel("task")
    assert first is second is third
    assert len(events) == 2
    assert bridge.cancel_calls == ["task"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  dIaBlO, StOp  ", VoiceCommand.STOP),
        ("CANCEL THAT", VoiceCommand.STOP),
        ("  Repeat That ", VoiceCommand.REPEAT),
        ("STATUS", VoiceCommand.STATUS),
        ("Mute", VoiceCommand.MUTE),
        ("Unmute", VoiceCommand.UNMUTE),
        ("Close", VoiceCommand.EXIT),
        ("  Build a scene ", VoiceCommand.REQUEST),
    ],
)
def test_command_variants_are_normalized(text, expected):
    bridge = BridgeFake()
    router = VoiceCommandRouter(bridge, InterruptionManager(bridge))
    assert router.classify(text) is expected


def test_malformed_commands_return_unknown_without_crash():
    bridge = BridgeFake()
    router = VoiceCommandRouter(bridge, InterruptionManager(bridge))
    for value in (None, "", "   ", "Diablo"):
        result = router.route(value)
        assert result.command is VoiceCommand.UNKNOWN
        assert result.supported is False


def test_unavailable_stt_does_not_break_text_commands_or_tts():
    bridge = BridgeFake()
    service = VoiceService(
        bridge,
        stt=UnavailableSpeechToTextBackend("STT unavailable"),
        wake_word=DisabledWakeWordDetector(),
        tts=MockTTSBackend(available=False),
    )
    assert service.start_listening() is False
    result = service.process_text("Build a scene")
    assert result.command.success is True
    output_result = service.output.handle_event(VoiceEvent(type="error", message="failure"))
    assert output_result.error["error"] == "tts_unavailable"
    service.close()


def test_bridge_unavailable_does_not_break_cooperative_token():
    manager = InterruptionManager(bridge=None)
    outcome = manager.request_cancel("offline-task")
    assert outcome.accepted is True
    assert outcome.bridge_accepted is False
    assert manager.is_cancelled("offline-task") is True


def test_malformed_output_event_is_filtered_without_crash():
    output = VoiceOutputRouter(MockTTSBackend())
    result = output.handle_event({"message": "missing type"})
    assert result.handled is False
    assert output.tts.spoken == []


def test_public_s2_api_surface_remains_importable():
    assert AgentBridge is not None
    assert BlenderCapabilityLayer is not None
    assert BlenderRunner is not None
    assert BlenderVisionIntegration is not None
    assert hasattr(UIController, "cancel_task")
    assert hasattr(SapiTTSBackend, "speak")
    assert hasattr(VoiceService, "process_text")
    assert hasattr(InterruptionManager, "request_cancel")


def test_voice_modules_do_not_depend_on_s1_agent_architecture():
    from pathlib import Path

    voice_root = Path(__file__).resolve().parent.parent / "voice"
    source = "\n".join(path.read_text(encoding="utf-8") for path in voice_root.rglob("*.py"))
    assert "core.agent" not in source
    assert "coding." not in source
    assert "tools." not in source
