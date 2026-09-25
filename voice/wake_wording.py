from __future__ import annotations

"""Wake-word detector contracts with no implicit microphone access."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class WakeWordResult:
    detected: bool
    phrase: str = ""
    confidence: float = 0.0
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "phrase": self.phrase,
            "confidence": self.confidence,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


class WakeWordDetector(ABC):
    name = "base"

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def detect(self, text: str) -> WakeWordResult:
        ...


class MockWakeWordDetector(WakeWordDetector):
    name = "mock"

    def __init__(self, wake_word: str = "diablo", enabled: bool = True) -> None:
        self.wake_word = wake_word.strip().lower()
        self.enabled = enabled
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self.enabled

    def detect(self, text: str) -> WakeWordResult:
        self.calls.append(text)
        if not self.enabled or not isinstance(text, str):
            return WakeWordResult(detected=False, source=self.name, metadata={"enabled": self.enabled})
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        detected = normalized == self.wake_word or normalized.startswith(self.wake_word + " ")
        return WakeWordResult(
            detected=detected,
            phrase=self.wake_word if detected else "",
            confidence=1.0 if detected else 0.0,
            source=self.name,
        )


class DisabledWakeWordDetector(WakeWordDetector):
    name = "disabled"

    def is_available(self) -> bool:
        return False

    def detect(self, text: str) -> WakeWordResult:
        return WakeWordResult(detected=False, source=self.name, metadata={"enabled": False})


__all__ = [
    "WakeWordResult",
    "WakeWordDetector",
    "MockWakeWordDetector",
    "DisabledWakeWordDetector",
]
