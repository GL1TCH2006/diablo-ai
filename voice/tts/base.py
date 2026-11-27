from __future__ import annotations

"""Backend-neutral text-to-speech contracts."""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class TTSError(Exception):
    code = "tts_error"
    default_code = "tts_error"

    def __init__(self, message: str, *, code: str = "", details: dict | None = None) -> None:
        self.message = message
        self.code = code or self.default_code
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class TTSUnavailable(TTSError):
    code = "tts_unavailable"
    default_code = "tts_unavailable"


@dataclass(slots=True)
class TTSResult:
    text: str
    spoken: bool
    backend: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "spoken": self.spoken,
            "backend": self.backend,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


class TTSBackend(ABC):
    name = "base"

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def speak(self, text: str, *, now: bool = False) -> TTSResult:
        ...


class MockTTSBackend(TTSBackend):
    name = "mock"

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.spoken: list[str] = []
        self.calls: list[tuple[str, bool]] = []

    def is_available(self) -> bool:
        return self.available

    def speak(self, text: str, *, now: bool = False) -> TTSResult:
        self.calls.append((text, now))
        if not self.available:
            raise TTSUnavailable("mock TTS backend is unavailable")
        self.spoken.append(text)
        return TTSResult(text=text, spoken=True, backend=self.name, metadata={"mock": True})


__all__ = [
    "TTSError",
    "TTSUnavailable",
    "TTSResult",
    "TTSBackend",
    "MockTTSBackend",
]
