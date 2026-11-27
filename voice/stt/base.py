from __future__ import annotations

"""Backend-neutral speech-to-text contracts and deterministic test backend."""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class SpeechToTextError(Exception):
    code = "speech_to_text_error"
    default_code = "speech_to_text_error"

    def __init__(self, message: str, *, code: str = "", details: dict | None = None) -> None:
        self.message = message
        self.code = code or self.default_code
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class SpeechToTextUnavailable(SpeechToTextError):
    code = "speech_to_text_unavailable"
    default_code = "speech_to_text_unavailable"


@dataclass(slots=True)
class TranscriptResult:
    text: str
    confidence: float | None = None
    language: str = ""
    source: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "language": self.language,
            "source": self.source,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


class SpeechToTextBackend(ABC):
    name = "base"

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def transcribe(self, audio_path: str | None = None, timeout: float = 15.0) -> TranscriptResult:
        ...


class MockSpeechToTextBackend(SpeechToTextBackend):
    """Deterministic STT backend that never opens a microphone."""

    name = "mock"

    def __init__(self, responses: list[str] | None = None, default_text: str = "") -> None:
        self.responses = list(responses or [])
        self.default_text = default_text
        self.calls: list[tuple[str | None, float]] = []

    def queue(self, text: str) -> None:
        self.responses.append(text)

    def is_available(self) -> bool:
        return True

    def transcribe(self, audio_path: str | None = None, timeout: float = 15.0) -> TranscriptResult:
        self.calls.append((audio_path, timeout))
        text = self.responses.pop(0) if self.responses else self.default_text
        return TranscriptResult(text=text, source=self.name, metadata={"mock": True})


class UnavailableSpeechToTextBackend(SpeechToTextBackend):
    name = "unavailable"

    def __init__(self, reason: str = "No local speech-to-text backend is configured") -> None:
        self.reason = reason

    def is_available(self) -> bool:
        return False

    def transcribe(self, audio_path: str | None = None, timeout: float = 15.0) -> TranscriptResult:
        raise SpeechToTextUnavailable(self.reason)


__all__ = [
    "SpeechToTextError",
    "SpeechToTextUnavailable",
    "TranscriptResult",
    "SpeechToTextBackend",
    "MockSpeechToTextBackend",
    "UnavailableSpeechToTextBackend",
]
