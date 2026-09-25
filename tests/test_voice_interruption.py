"""Deterministic S2.5 voice and interruption tests."""

from __future__ import annotations

import threading

import pytest

from ui.bridge import MockAgentBridge
from ui.controller import UIController
from ui.models import UIStatus
from voice import (
    CancellationError,
    CancellationToken,
    InterruptionManager,
    MockSpeechToTextBackend,
    MockTTSBackend,
    MockWakeWordDetector,
    SapiTTSBackend,
    SpeechToText,
    VoiceCommand,
    VoiceCommandRouter,
    VoiceEvent,
    VoiceOutputRouter,
    VoicePipeline,
    VoiceService,
    VoiceStatus,
    VoiceUIAdapter,
    WakeWordListener,
)
from voice.stt import LocalSpeechToTextBackend, SpeechToTextUnavailable, UnavailableSpeechToTextBackend
from voice.tts import OptionalNeuralTTSBackend, TTSUnavailable
from voice.wake_wording import DisabledWakeWordDetector


class FakeBridge:
    def __init__(self, *, cancel_result=True, cancel_error=None, submit_error=None):
        self.cancel_result = cancel_result
        self.cancel_error = cancel_error
        self.submit_error = submit_error
        self.cancel_calls = []
        self.submitted = []
        self.closed = False

    def submit_message(self, text, task_id=""):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append((text, task_id))
        return {"success": True}

    def cancel_task(self, task_id=""):
        self.cancel_calls.append(task_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_result

    def current_status(self):
        return "ready"

    def close(self):
        self.closed = True


def test_stt_mock_is_available_without_microphone():
    stt = MockSpeechToTextBackend(["Diablo, stop"])
    assert stt.is_available() is True
    result = stt.transcribe("ignored.wav")
    assert result.text == "Diablo, stop"
    assert result.source == "mock"
    assert stt.calls == [("ignored.wav", 15.0)]


def test_stt_unavailable_backend_is_structured():
    stt = UnavailableSpeechToTextBackend("no backend")
    assert stt.is_available() is False
    with pytest.raises(SpeechToTextUnavailable) as exc_info:
        stt.transcribe()
    assert exc_info.value.to_dict()["error"] == "speech_to_text_unavailable"


def test_existing_speech_to_text_facade_remains_compatible():
    facade = SpeechToText(MockSpeechToTextBackend(["hello"]))
    assert facade.is_available() is True
    assert facade.transcribe() == "hello"
    default_facade = SpeechToText()
    assert default_facade.is_available() is False


def test_local_stt_does_not_download_or_claim_unconfigured_backend():
    backend = LocalSpeechToTextBackend()
    assert backend.is_available() is False
    assert set(backend.dependency_status()) == {"faster_whisper", "speech_recognition"}
    with pytest.raises(SpeechToTextUnavailable):
        backend.transcribe()


def test_tts_mock_interface():
    tts = MockTTSBackend()
    result = tts.speak("Diablo ready", now=True)
    assert result.spoken is True
    assert tts.spoken == ["Diablo ready"]
    assert tts.calls == [("Diablo ready", True)]


def test_tts_mock_unavailable_state():
    tts = MockTTSBackend(available=False)
    assert tts.is_available() is False
    with pytest.raises(TTSUnavailable):
        tts.speak("no")


def test_sapi_tts_delegates_to_existing_fallback():
    calls = []

    def speaker(text, now=False):
        calls.append((text, now))
        return True

    backend = SapiTTSBackend(availability_check=lambda: True, speaker=speaker)
    assert backend.is_available() is True
    result = backend.speak("Diablo online", now=True)
    assert result.backend == "sapi"
    assert calls == [("Diablo online", True)]


def test_sapi_tts_unavailable_does_not_speak():
    backend = SapiTTSBackend(availability_check=lambda: False, speaker=lambda text, now=False: True)
    with pytest.raises(TTSUnavailable):
        backend.speak("hello")


def test_optional_neural_tts_requires_explicit_local_model():
    backend = OptionalNeuralTTSBackend()
    assert backend.is_available() is False
    with pytest.raises(TTSUnavailable):
        backend.speak("hello")


def test_wake_word_mock_detection_and_disable_switch():
    detector = MockWakeWordDetector()
    assert detector.detect("Diablo, stop").detected is True
    assert detector.detect("stop").detected is False
    detector.enabled = False
    assert detector.detect("Diablo, stop").detected is False


def test_disabled_wake_word_detector():
    detector = DisabledWakeWordDetector()
    assert detector.is_available() is False
    assert detector.detect("Diablo").detected is False


def test_wake_word_listener_is_text_fed_and_explicit():
    calls = []
    listener = WakeWordListener(detector=MockWakeWordDetector())
    listener.start(calls.append)
    assert listener.process_text("Diablo hello").detected is True
    assert calls
    listener.stop()
    assert listener.process_text("Diablo hello").detected is False


def test_cancellation_token_request_fields_and_idempotence():
    token = CancellationToken("task-1")
    request = token.request("user stop", source="voice", metadata={"phrase": "stop"})
    assert token.is_cancelled() is True
    assert token.task_id == "task-1"
    assert token.reason == "user stop"
    assert token.source == "voice"
    assert token.timestamp == request.timestamp
    assert token.request("again") == request
    assert token.wait(0) is True
    with pytest.raises(CancellationError):
        token.raise_if_cancelled()
    assert token.reset() is True
    assert token.is_cancelled() is False


def test_cancellation_manager_propagates_to_bridge_and_emits_events():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    events = []
    manager.subscribe(events.append)
    outcome = manager.request_cancel("task-1")
    assert outcome.accepted is True
    assert outcome.bridge_accepted is True
    assert bridge.cancel_calls == ["task-1"]
    assert [event.type for event in events] == ["task_cancel_requested", "task_cancelled"]
    assert events[0].source == "voice"
    assert events[0].metadata["reason"] == "user_interruption"


def test_cancellation_manager_repeated_request_is_idempotent():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    events = []
    manager.subscribe(events.append)
    first = manager.request_cancel("task-1")
    second = manager.request_cancel("task-1")
    assert first is second
    assert bridge.cancel_calls == ["task-1"]
    assert len(events) == 2


def test_wildcard_cancellation_is_one_shot_and_does_not_cancel_future_tasks():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    first = manager.request_cancel()
    second = manager.request_cancel()
    later = manager.token_for("later-task")
    assert first is not second
    assert bridge.cancel_calls == ["", ""]
    assert later.is_cancelled() is False


def test_cancellation_manager_marks_queued_task_before_bridge_work():
    manager = InterruptionManager(FakeBridge())
    token = manager.token_for("queued")
    outcome = manager.request_cancel("queued")
    assert outcome.accepted is True
    assert token.is_cancelled() is True
    with pytest.raises(CancellationError):
        token.raise_if_cancelled()


def test_cancellation_manager_bridge_failure_is_structured():
    bridge = FakeBridge(cancel_error=RuntimeError("cancel denied"))
    manager = InterruptionManager(bridge)
    events = []
    manager.subscribe(events.append)
    outcome = manager.request_cancel("task-1")
    assert outcome.error == "cancel denied"
    assert [event.type for event in events] == ["task_cancel_requested", "task_cancel_failed"]
    assert manager.is_cancelled("task-1") is True


def test_cancellation_manager_is_thread_safe():
    manager = InterruptionManager(FakeBridge())
    outcomes = []
    lock = threading.Lock()

    def request():
        outcome = manager.request_cancel("shared")
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=request) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(outcome) for outcome in outcomes}) == 1


def test_voice_command_router_stop_reaches_interruption_and_bridge():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    router = VoiceCommandRouter(bridge, manager)
    result = router.route("Diablo, stop")
    assert result.command is VoiceCommand.STOP
    assert result.success is True
    assert result.action == "cancel"
    assert bridge.cancel_calls == [""]


def test_voice_command_router_preserves_cancel_rejection():
    bridge = FakeBridge(cancel_result=False)
    router = VoiceCommandRouter(bridge, InterruptionManager(bridge))
    result = router.route("Diablo, stop")
    assert result.success is False
    assert "rejected" in result.message.lower()


def test_cancellation_manager_rejects_malformed_bridge_result():
    class MalformedBridge(FakeBridge):
        def cancel_task(self, task_id=""):
            return "not-a-result"

    bridge = MalformedBridge()
    manager = InterruptionManager(bridge)
    outcome = manager.request_cancel("task-malformed")
    assert outcome.error == "bridge returned a malformed cancellation result"
    assert outcome.bridge_accepted is False


def test_voice_command_router_preserves_submission_outcomes():
    class ResultBridge(FakeBridge):
        def __init__(self, result):
            super().__init__()
            self.result = result

        def submit_message(self, text, task_id=""):
            return self.result

    rejected = ResultBridge({"success": False, "error": {"message": "request rejected"}})
    router = VoiceCommandRouter(rejected, InterruptionManager(rejected))
    result = router.route("Build a scene")
    assert result.success is False
    assert "rejected" in result.message

    malformed = ResultBridge("not-a-result")
    router = VoiceCommandRouter(malformed, InterruptionManager(malformed))
    result = router.route("Build a scene")
    assert result.success is False
    assert "malformed" in result.message

    class ThrowingBridge(FakeBridge):
        def submit_message(self, text, task_id=""):
            raise RuntimeError("bridge offline")

    throwing = ThrowingBridge()
    router = VoiceCommandRouter(throwing, InterruptionManager(throwing))
    result = router.route("Build a scene")
    assert result.success is False
    assert "offline" in result.message


def test_voice_command_router_normal_request_repeat_and_status():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    router = VoiceCommandRouter(bridge, manager)
    request = router.route("Build a Blender scene")
    assert request.command is VoiceCommand.REQUEST
    repeat = router.route("Repeat that")
    status = router.route("Status")
    assert request.success and repeat.success and status.success
    assert bridge.submitted[0][0] == "Build a Blender scene"
    assert bridge.submitted[1][0] == "Build a Blender scene"
    assert status.action == "status"


def test_voice_command_router_mute_exit_and_unknown():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    output = VoiceOutputRouter(MockTTSBackend())
    exits = []
    router = VoiceCommandRouter(bridge, manager, output=output, on_exit=lambda: exits.append(True))
    assert router.route("Mute").command is VoiceCommand.MUTE
    assert router.route("Unmute").command is VoiceCommand.UNMUTE
    assert router.route("Close").command is VoiceCommand.EXIT
    assert exits == [True]
    unknown = router.route("")
    assert unknown.command is VoiceCommand.UNKNOWN
    assert unknown.supported is False


def test_voice_command_permission_failure_is_not_bypassed():
    bridge = FakeBridge(submit_error=PermissionError("confirmation required"))
    router = VoiceCommandRouter(bridge, InterruptionManager(bridge))
    result = router.route("Delete the project")
    assert result.success is False
    assert "confirmation required" in result.message


def test_voice_pipeline_routes_mock_audio_without_microphone():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    router = VoiceCommandRouter(bridge, manager)
    pipeline = VoicePipeline(
        MockSpeechToTextBackend(["Diablo, stop"]),
        MockWakeWordDetector(),
        router,
    )
    result = pipeline.process_audio("virtual.wav")
    assert result.transcript.text == "Diablo, stop"
    assert result.command.command is VoiceCommand.STOP
    assert bridge.cancel_calls == [""]
    assert pipeline.listening is False


def test_voice_pipeline_wake_requirement_allows_explicit_stop():
    bridge = FakeBridge()
    router = VoiceCommandRouter(bridge, InterruptionManager(bridge))
    pipeline = VoicePipeline(
        MockSpeechToTextBackend(["stop"]),
        MockWakeWordDetector(),
        router,
        require_wake=True,
    )
    result = pipeline.process_transcript("stop")
    assert result.command.command is VoiceCommand.STOP
    ignored = pipeline.process_transcript("build something")
    assert ignored.command is None


def test_voice_output_policy_filters_internal_events():
    tts = MockTTSBackend()
    output = VoiceOutputRouter(tts)
    thinking = VoiceEvent(type="status", message="Thinking", status="thinking")
    assert output.handle_event(thinking).handled is False
    cancelled = VoiceEvent(type="task_cancelled", message="Task cancelled", status="cancelled")
    result = output.handle_event(cancelled)
    assert result.handled is True
    assert result.spoken is True
    assert tts.spoken == ["Task cancelled."]


def test_voice_output_attaches_to_interruption_events_and_mutes():
    bridge = FakeBridge()
    manager = InterruptionManager(bridge)
    tts = MockTTSBackend()
    output = VoiceOutputRouter(tts)
    output.attach_interruption_manager(manager)
    manager.request_cancel("task-1")
    assert "Task cancelled." in tts.spoken
    output.set_muted(True)
    output.handle_event(VoiceEvent(type="error", message="failure"))
    assert tts.spoken == ["Task cancelled."]


def test_voice_ui_adapter_tracks_interruption_state_and_malformed_events():
    adapter = VoiceUIAdapter()
    requested = adapter.handle_event(
        VoiceEvent(type="task_cancel_requested", message="Cancellation requested", status="cancelling", task_id="task-1")
    )
    assert requested.metadata["source"] == "voice"
    assert adapter.status is VoiceStatus.CANCELLING
    event = adapter.handle_event(
        VoiceEvent(type="task_cancelled", message="Task cancelled", status="cancelled", task_id="task-1")
    )
    assert event.metadata["source"] == "voice"
    assert event.status == "cancelled"
    assert adapter.status is VoiceStatus.CANCELLED
    assert adapter.interruption_state == "cancelled"
    malformed = adapter.handle_event({"message": "missing type"})
    assert malformed.type == "error"
    assert adapter.status is VoiceStatus.ERROR


def test_voice_service_wires_interruption_to_existing_ui_controller():
    bridge = MockAgentBridge(step_delay=0.2)
    controller = UIController(bridge)
    service = VoiceService(
        bridge,
        stt=MockSpeechToTextBackend(["Diablo, stop"]),
        wake_word=MockWakeWordDetector(),
        tts=MockTTSBackend(),
        cancellation_callback=lambda task_id="": controller.cancel_task(task_id).success,
    )
    controller.send_message("long running task")
    result = service.process_text("Diablo, stop")
    assert result.command.success is True
    assert controller.wait_for_idle(1.0)
    assert controller.state is UIStatus.READY
    assert any("cancelled" in activity.text.lower() for activity in controller.activities)
    service.close()
    controller.close()


def test_voice_service_does_not_start_hardware_capture():
    bridge = FakeBridge()
    service = VoiceService(
        bridge,
        stt=MockSpeechToTextBackend(["hello"]),
        wake_word=MockWakeWordDetector(),
        tts=MockTTSBackend(),
    )
    assert service.listening is False
    assert service.start_listening() is True
    assert service.listening is True
    service.stop_listening()
    assert service.listening is False
    service.close()
