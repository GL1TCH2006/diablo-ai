from __future__ import annotations

"""Composable local voice service facade."""

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .cancellation import CancellationOutcome, InterruptionManager
from .capture import AudioConfig, MicrophoneCapture, RecordingConfig, RecordingResult
from .commands import VoiceCommandRouter
from .output import VoiceOutputRouter
from .pipeline import VoicePipeline, VoicePipelineResult
from .stt import SpeechToTextBackend, create_fallback_stt, create_faster_whisper_backend
from .tts import TTSBackend, create_tts_backend
from .ui_adapter import VoiceUIAdapter
from .vad import VAD, EnergyVAD, create_vad
from .wake_wording import DisabledWakeWordDetector, WakeWordDetector, create_wake_word_detector

log = logging.getLogger("voice.service")


class VoiceService:
    """Wire replaceable voice components without starting hardware capture by default.

    The service wires together:
    - STT backend (faster-whisper, mock, or unavailable)
    - Wake word detector (mock, porcupine, openwakeword, or disabled)
    - TTS backend (SAPI, neural, or mock)
    - Voice Activity Detection (energy-based or webrtcvad)
    - Microphone capture (sounddevice)
    - Voice pipeline for processing
    - Interruption manager for cancellation
    """

    def __init__(
        self,
        bridge: Any,
        *,
        stt: SpeechToTextBackend | None = None,
        wake_word: WakeWordDetector | None = None,
        tts: Any | None = None,
        event_sink: Callable[[Any], None] | None = None,
        cancellation_callback: Callable[[str], Any] | None = None,
        on_exit: Callable[[], Any] | None = None,
        require_wake: bool = False,
        # Configuration
        stt_model: str = "base",
        stt_device: str = "auto",
        stt_compute_type: str = "int8",
        stt_model_path: str = "",
        wake_word_type: str = "mock",
        wake_word_phrase: str = "diablo",
        vad_method: str = "energy",
        vad_threshold: float = 0.01,
        max_recording_duration: float = 30.0,
        silence_timeout: float = 2.0,
        microphone_device: int | str | None = None,
        sample_rate: int = 16000,
        output_dir: str | Path = "voice_recordings",
        voice_tts_interruptible: bool = True,
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

        # TTS backend
        tts_backend = tts or create_tts_backend()
        self.output = VoiceOutputRouter(tts_backend, interruptible=voice_tts_interruptible)
        self.output.attach_interruption_manager(self.interruptions)

        # STT backend - use faster-whisper if model path is provided, otherwise fallback
        if stt is None:
            if stt_model_path:
                stt_backend = create_faster_whisper_backend(
                    model_name=stt_model,
                    device=stt_device,
                    compute_type=stt_compute_type,
                    download_root=stt_model_path,
                )
            else:
                stt_backend = create_fallback_stt()
        else:
            stt_backend = stt

        # Wake word detector
        wake_word_detector = wake_word or DisabledWakeWordDetector()

        # VAD
        vad = create_vad(vad_method, sample_rate=16000, threshold=0.01)

        # Command router
        self.router = VoiceCommandRouter(
            bridge,
            self.interruptions,
            output=self.output,
            on_exit=on_exit,
        )

        # Voice pipeline
        self.pipeline = VoicePipeline(
            stt=stt_backend,
            wake_word=wake_word_detector,
            command_router=self.router,
            event_sink=lambda event: [sink(event) for sink in sinks],
            require_wake=False,  # Default to False, can be overridden
        )

        # Voice components
        self._stt_backend = stt_backend
        self._wake_word_detector = wake_word_detector
        self._vad = vad
        self._tts_backend = tts_backend
        self._microphone = MicrophoneCapture(
            audio_config=AudioConfig(sample_rate=sample_rate),
            recording_config=RecordingConfig(
                max_duration=max_recording_duration,
                silence_timeout=silence_timeout,
                energy_threshold=vad_threshold,
            ),
            device=microphone_device,
        )
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._recording_in_progress = False
        self._current_recording_path: Path | None = None

        # Configuration
        self._config = {
            "stt_model": stt_model,
            "stt_device": stt_device,
            "stt_compute_type": stt_compute_type,
            "stt_model_path": stt_model_path,
            "wake_word_type": wake_word_type,
            "wake_word_phrase": wake_word_phrase,
            "vad_method": vad_method,
            "vad_threshold": vad_threshold,
            "max_recording_duration": max_recording_duration,
            "silence_timeout": silence_timeout,
            "microphone_device": microphone_device,
            "sample_rate": sample_rate,
            "output_dir": str(output_dir),
        }

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

    def cancel(self, task_id: str = "", source: str = "voice") -> Any:
        return self.interruptions.request_cancel(task_id, source=source)

    def set_muted(self, muted: bool) -> None:
        self.output.set_muted(muted)

    def toggle_muted(self) -> bool:
        return self.output.toggle_muted()

    # New voice capture methods

    def start_voice_recording(self, filename: str | None = None) -> bool:
        """Start recording audio from microphone."""
        if self._recording_in_progress:
            log.warning("Already recording")
            return False

        if not self._microphone.is_available():
            log.error("No microphone available")
            return False

        if filename is None:
            import time
            filename = f"voice_{int(time.time())}.wav"

        output_path = self._output_dir / filename
        self._current_recording_path = output_path

        success = self._microphone.start_recording(output_path)
        if success:
            self._recording_in_progress = True
            log.info(f"Started voice recording to {output_path}")
        else:
            self._current_recording_path = None

        return success

    def stop_voice_recording(self) -> RecordingResult:
        """Stop recording and return the result with file path."""
        if not self._recording_in_progress:
            return RecordingResult(
                file_path="",
                duration=0.0,
                sample_rate=0,
                channels=0,
                success=False,
                error="Not recording",
            )

        result = self._microphone.stop_recording()
        self._recording_in_progress = False

        if self._current_recording_path:
            result.file_path = str(self._current_recording_path)
            self._current_recording_path = None

        return result

    def record_and_transcribe(
        self,
        filename: str | None = None,
        max_duration: float | None = None,
        silence_timeout: float | None = None,
    ) -> tuple[RecordingResult, str | None]:
        """Record audio and transcribe it in one call.

        Returns:
            Tuple of (RecordingResult, transcribed_text or None)
        """
        if max_duration is not None:
            self._microphone.recording_config.max_duration = max_duration
        if silence_timeout is not None:
            self._microphone.recording_config.silence_timeout = silence_timeout

        if not self.start_voice_recording(filename):
            return RecordingResult(
                file_path="",
                duration=0.0,
                sample_rate=0,
                channels=0,
                success=False,
                error="Failed to start recording",
            ), None

        result = self.stop_voice_recording()

        if not result.success or not result.file_path:
            return result, None

        try:
            transcript = self._stt_backend.transcribe(result.file_path)
            return result, transcript.text
        except Exception as exc:
            log.error(f"Transcription failed: {exc}")
            return result, None

    def is_microphone_available(self) -> bool:
        return self._microphone.is_available()

    def list_microphones(self) -> list[dict[str, Any]]:
        return self._microphone.list_devices()

    def is_stt_available(self) -> bool:
        return self._stt_backend.is_available()

    def is_wake_word_available(self) -> bool:
        return self._wake_word_detector.is_available()

    def is_tts_available(self) -> bool:
        return self._tts_backend.is_available()

    def set_muted(self, muted: bool) -> None:
        self.output.set_muted(muted)

    def toggle_muted(self) -> bool:
        return self.output.toggle_muted()

    def cancel(self, task_id: str = "", source: str = "voice") -> Any:
        return self.interruptions.request_cancel(task_id, source=source)

    def close(self) -> None:
        self.pipeline.close()
        self.output.close()
        self.interruptions.close()
        self._microphone.close()


__all__ = ["VoiceService"]