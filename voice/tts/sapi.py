from __future__ import annotations

"""Windows SAPI TTS adapter using the existing guaranteed fallback."""

from collections.abc import Callable

from voice.text_to_speech import is_sapi_available, speak

from .base import TTSBackend, TTSResult, TTSUnavailable


class SapiTTSBackend(TTSBackend):
    name = "sapi"

    def __init__(
        self,
        availability_check: Callable[[], bool] = is_sapi_available,
        speaker: Callable[..., bool] = speak,
    ) -> None:
        self._availability_check = availability_check
        self._speaker = speaker

    def is_available(self) -> bool:
        try:
            return bool(self._availability_check())
        except Exception:
            return False

    def speak(self, text: str, *, now: bool = False) -> TTSResult:
        if not text.strip():
            raise TTSUnavailable("TTS text is empty")
        if not self.is_available():
            raise TTSUnavailable("Windows SAPI TTS is unavailable")
        try:
            spoken = bool(self._speaker(text, now=now))
        except Exception as exc:
            raise TTSUnavailable(f"SAPI TTS failed: {exc}") from exc
        if not spoken:
            raise TTSUnavailable("SAPI TTS did not accept the utterance")
        return TTSResult(text=text, spoken=True, backend=self.name, metadata={"now": now})


__all__ = ["SapiTTSBackend"]
