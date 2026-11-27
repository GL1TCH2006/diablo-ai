from __future__ import annotations

"""Windows SAPI TTS adapter with interruption support."""

from collections.abc import Callable

from voice.text_to_speech import is_sapi_available, speak, stop_speaking, is_speaking

from .base import TTSBackend, TTSResult, TTSUnavailable


class SapiTTSBackend(TTSBackend):
    name = "sapi"

    def __init__(
        self,
        availability_check: Callable[[], bool] = is_sapi_available,
        speaker: Callable[..., tuple[bool, bool] | bool] = speak,
        stopper: Callable[[], bool] = stop_speaking,
        speaking_check: Callable[[], bool] = is_speaking,
    ) -> None:
        self._availability_check = availability_check
        self._speaker = speaker
        self._stopper = stopper
        self._speaking_check = speaking_check

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
            # Handle both old (bool) and new (tuple[bool, bool]) speaker signatures
            result = self._speaker(text, now=now)
            if isinstance(result, tuple):
                spoken, cancelled = result
            else:
                spoken = bool(result)
                cancelled = False
        except Exception as exc:
            raise TTSUnavailable(f"SAPI TTS failed: {exc}") from exc
        if not spoken:
            raise TTSUnavailable("SAPI TTS did not accept the utterance")
        metadata = {"now": now}
        if cancelled:
            metadata["cancelled"] = True
        return TTSResult(text=text, spoken=True, backend=self.name, metadata=metadata)

    def stop(self) -> bool:
        """Stop any ongoing speech. Returns True if speech was stopped."""
        return self._stopper()

    def is_speaking(self) -> bool:
        """Check if currently speaking."""
        return self._speaking_check()


__all__ = ["SapiTTSBackend"]