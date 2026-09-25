from __future__ import annotations

"""Composable local voice service facade."""

from typing import Any, Callable

from .cancellation import CancellationOutcome, InterruptionManager
from .commands import VoiceCommandRouter
from .output import VoiceOutputRouter
from .pipeline import VoicePipeline, VoicePipelineResult
from .stt import SpeechToTextBackend, create_fallback_stt
from .tts import TTSBackend, create_tts_backend
from .ui_adapter import VoiceUIAdapter
from .wake_wording import DisabledWakeWordDetector, WakeWordDetector


class VoiceService:
    """Wire replaceable voice components without starting hardware capture."""

    def __init__(
        self,
        bridge: Any,
        *,
        stt: SpeechToTextBackend | None = None,
        wake_word: WakeWordDetector | None = None,
        tts: TTSBackend | None = None,
        event_sink: Callable[[Any], None] | None = None,
        cancellation_callback: Callable[[str], Any] | None = None,
        on_exit: Callable[[], Any] | None = None,
        require_wake: bool = False,
    ) -> None:
        sinks: list[Callable[[Any], None]] = []
        if event_sink is not None:
            sinks.append(event_sink)
        self.ui = VoiceUIAdapter(event_sink=sinks[0] if sinks else None)
        sinks.append(self.ui.handle_event)
        self.interruptions = InterruptionManager(
            bridge=bridge,
            event_sink=lambda event: [sink(event) for sink in sinks],
            cancel_callback=cancellation_callback,
        )
        self.output = VoiceOutputRouter(tts or create_tts_backend())
        self.output.attach_interruption_manager(self.interruptions)
        self.router = VoiceCommandRouter(
            bridge,
            self.interruptions,
            output=self.output,
            on_exit=on_exit,
        )
        self.pipeline = VoicePipeline(
            stt=stt or create_fallback_stt(),
            wake_word=wake_word or DisabledWakeWordDetector(),
            command_router=self.router,
            event_sink=lambda event: [sink(event) for sink in sinks],
            require_wake=require_wake,
        )

    @property
    def status(self):
        return self.pipeline.status

    @property
    def listening(self) -> bool:
        return self.pipeline.listening

    def start_listening(self) -> bool:
        return self.pipeline.start_listening()

    def stop_listening(self) -> None:
        self.pipeline.stop_listening()

    def process_text(self, text: str) -> VoicePipelineResult:
        return self.pipeline.process_transcript(text)

    def process_audio(self, audio_path: str, timeout: float = 15.0) -> VoicePipelineResult:
        return self.pipeline.process_audio(audio_path, timeout)

    def cancel(self, task_id: str = "", source: str = "voice") -> CancellationOutcome:
        return self.interruptions.request_cancel(task_id, source=source)

    def set_muted(self, muted: bool) -> None:
        self.output.set_muted(muted)

    def toggle_muted(self) -> bool:
        return self.output.toggle_muted()

    def close(self) -> None:
        self.pipeline.close()
        self.output.close()
        self.interruptions.close()


__all__ = ["VoiceService"]
