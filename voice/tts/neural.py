from __future__ import annotations

"""Optional explicitly configured neural TTS detection."""

import importlib.util
from pathlib import Path

from .base import TTSBackend, TTSResult, TTSUnavailable


class OptionalNeuralTTSBackend(TTSBackend):
    name = "neural_local"

    def __init__(self, model_path: str | Path | None = None, package_name: str = "kokoro") -> None:
        self.model_path = Path(model_path) if model_path else None
        self.package_name = package_name

    def is_available(self) -> bool:
        return bool(
            importlib.util.find_spec(self.package_name)
            and self.model_path
            and self.model_path.exists()
        )

    def speak(self, text: str, *, now: bool = False) -> TTSResult:
        if not self.is_available():
            raise TTSUnavailable(
                "optional neural TTS requires an already-installed package and explicit model path",
                details={"package": self.package_name, "model_path": str(self.model_path or "")},
            )
        raise TTSUnavailable("optional neural TTS adapter is reserved for a future local implementation")


__all__ = ["OptionalNeuralTTSBackend"]
