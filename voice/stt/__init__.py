from .base import (
    MockSpeechToTextBackend,
    SpeechToTextBackend,
    SpeechToTextError,
    SpeechToTextUnavailable,
    TranscriptResult,
    UnavailableSpeechToTextBackend,
)
from .fallback import create_fallback_stt, create_faster_whisper_backend
from .local import FasterWhisperBackend, create_faster_whisper_backend

# Backward compatibility alias
LocalSpeechToTextBackend = FasterWhisperBackend

__all__ = [
    "SpeechToTextBackend",
    "SpeechToTextError",
    "SpeechToTextUnavailable",
    "TranscriptResult",
    "MockSpeechToTextBackend",
    "UnavailableSpeechToTextBackend",
    "FasterWhisperBackend",
    "LocalSpeechToTextBackend",
    "create_fallback_stt",
    "create_faster_whisper_backend",
]