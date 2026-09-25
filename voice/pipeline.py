from __future__ import annotations

"""One-shot local voice pipeline: STT, wake policy, command routing, and output."""

from dataclasses import dataclass, field
from typing import Any, Callable

from .commands import VoiceCommand, VoiceCommandResult, VoiceCommandRouter
from .events import VoiceEvent, VoiceStatus
from .stt import SpeechToTextBackend, TranscriptResult
from .wake_wording import WakeWordDetector


@dataclass(slots=True)
class VoicePipelineResult:
    transcript: TranscriptResult | None = None
    command: VoiceCommandResult | None = None
    status: VoiceStatus = VoiceStatus.IDLE
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "command": self.command.to_dict() if self.command else None,
            "status": self.status.value,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class VoicePipeline:
    """Run one explicit audio/text operation without opening a microphone."""

    def __init__(
        self,
        stt: SpeechToTextBackend,
        wake_word: WakeWordDetector,
        command_router: VoiceCommandRouter,
        *,
        event_sink: Callable[[VoiceEvent], None] | None = None,
        require_wake: bool = False,
    ) -> None:
        self.stt = stt
        self.wake_word = wake_word
        self.command_router = command_router
        self.event_sink = event_sink
        self.require_wake = require_wake
        self._status = VoiceStatus.IDLE
        self._listening = False

    @property
    def status(self) -> VoiceStatus:
        return self._status

    @property
    def listening(self) -> bool:
        return self._listening

    def _set_status(self, status: VoiceStatus) -> None:
        self._status = status
        if self.event_sink is not None:
            try:
                self.event_sink(
                    VoiceEvent(
                        type="voice_status",
                        message=status.value.replace("_", " ").title(),
                        status=status.value,
                        metadata={"listening": self._listening},
                    )
                )
            except Exception:
                pass

    def start_listening(self) -> bool:
        if not self.stt.is_available() or not self.wake_word.is_available():
            self._set_status(VoiceStatus.ERROR)
            return False
        self._listening = True
        self._set_status(VoiceStatus.LISTENING)
        return True

    def stop_listening(self) -> None:
        self._listening = False
        self._set_status(VoiceStatus.IDLE)

    def process_audio(self, audio_path: str, timeout: float = 15.0) -> VoicePipelineResult:
        self._set_status(VoiceStatus.TRANSCRIBING)
        try:
            transcript = self.stt.transcribe(audio_path=audio_path, timeout=timeout)
        except Exception as exc:
            self._set_status(VoiceStatus.ERROR)
            to_dict = getattr(exc, "to_dict", None)
            error = to_dict() if callable(to_dict) else {"error": type(exc).__name__, "message": str(exc)}
            return VoicePipelineResult(status=VoiceStatus.ERROR, error=error)
        return self.process_transcript(transcript)

    def process_transcript(self, transcript: TranscriptResult | str) -> VoicePipelineResult:
        if isinstance(transcript, str):
            transcript = TranscriptResult(text=transcript, source="text")
        self._set_status(VoiceStatus.ROUTING)
        wake_result = self.wake_word.detect(transcript.text)
        command = self.command_router.classify(transcript.text)
        if self.require_wake and not wake_result.detected and command is not VoiceCommand.STOP:
            self._set_status(VoiceStatus.IDLE)
            return VoicePipelineResult(
                transcript=transcript,
                status=VoiceStatus.IDLE,
                metadata={"wake_detected": False},
            )
        command_result = self.command_router.route(transcript.text)
        self._set_status(VoiceStatus.IDLE)
        return VoicePipelineResult(
            transcript=transcript,
            command=command_result,
            status=VoiceStatus.IDLE,
            metadata={"wake_detected": wake_result.detected},
        )

    def close(self) -> None:
        self._listening = False
        self._status = VoiceStatus.IDLE


__all__ = ["VoicePipelineResult", "VoicePipeline"]
