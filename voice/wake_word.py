from __future__ import annotations

"""Backward-compatible wake-word interface with explicit, text-only operation."""

from .wake_wording import (
    DisabledWakeWordDetector,
    MockWakeWordDetector,
    WakeWordDetector,
    WakeWordResult,
)


class WakeWordError(Exception):
    pass


class WakeWordListener:
    """Text-fed wake-word listener; it never opens a microphone by itself."""

    def __init__(self, wake_word: str = "diablo", language: str = "en-US", detector: WakeWordDetector | None = None):
        self.wake_word = wake_word.lower()
        self.language = language
        self.detector = detector or MockWakeWordDetector(self.wake_word)
        self._on_wake = None
        self._listening = False

    def start(self, on_wake) -> None:
        self._on_wake = on_wake
        self._listening = True

    def stop(self) -> None:
        self._listening = False

    def process_text(self, text: str) -> WakeWordResult:
        if not self._listening:
            return WakeWordResult(detected=False, source="listener", metadata={"listening": False})
        result = self.detector.detect(text)
        if result.detected and callable(self._on_wake):
            self._on_wake(result)
        return result

    @property
    def listening(self) -> bool:
        return self._listening


__all__ = [
    "WakeWordError",
    "WakeWordListener",
    "WakeWordResult",
    "WakeWordDetector",
    "MockWakeWordDetector",
    "DisabledWakeWordDetector",
]
