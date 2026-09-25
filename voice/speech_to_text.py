from __future__ import annotations

"""Backward-compatible speech-to-text facade."""

from .stt import (
    SpeechToTextBackend,
    SpeechToTextError,
    SpeechToTextUnavailable,
    TranscriptResult,
    create_fallback_stt,
)


class SpeechToText:
    """Existing facade backed by a replaceable local STT implementation."""

    def __init__(self, backend: SpeechToTextBackend | None = None) -> None:
        self.backend = backend or create_fallback_stt()

    def transcribe(self, audio_path: str | None = None, timeout: float = 15.0) -> str:
        return self.transcribe_result(audio_path, timeout).text

    def transcribe_result(self, audio_path: str | None = None, timeout: float = 15.0) -> TranscriptResult:
        return self.backend.transcribe(audio_path=audio_path, timeout=timeout)

    def is_available(self) -> bool:
        return self.backend.is_available()


__all__ = [
    "SpeechToText",
    "SpeechToTextBackend",
    "SpeechToTextError",
    "SpeechToTextUnavailable",
    "TranscriptResult",
]
