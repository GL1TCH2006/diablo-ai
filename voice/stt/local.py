from __future__ import annotations

"""Optional local speech-to-text backend detection without downloads."""

import importlib.util
from pathlib import Path

from .base import SpeechToTextBackend, SpeechToTextUnavailable, TranscriptResult


class LocalSpeechToTextBackend(SpeechToTextBackend):
    """Detect an already-installed local STT dependency.

    S2.5 intentionally does not download or configure a model. A future local
    backend can be added by implementing the same contract and supplying a
    model path explicitly.
    """

    name = "local"

    def __init__(self, model_path: str | Path | None = None) -> None:
        self.model_path = Path(model_path) if model_path else None

    def dependency_status(self) -> dict[str, bool]:
        return {
            "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
            "speech_recognition": importlib.util.find_spec("speech_recognition") is not None,
        }

    def is_available(self) -> bool:
        status = self.dependency_status()
        return bool(status["faster_whisper"] and self.model_path and self.model_path.exists())

    def transcribe(self, audio_path: str | None = None, timeout: float = 15.0) -> TranscriptResult:
        if not self.is_available():
            raise SpeechToTextUnavailable(
                "local STT requires an already-installed faster-whisper package and explicit model path",
                details=self.dependency_status(),
            )
        raise SpeechToTextUnavailable(
            "local STT adapter is reserved for a future explicitly configured backend",
            details={"model_path": str(self.model_path) if self.model_path else ""},
        )


__all__ = ["LocalSpeechToTextBackend"]
