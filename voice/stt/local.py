from __future__ import annotations

"""Local speech-to-text backend using faster-whisper."""

import importlib.util
import logging
import threading
from pathlib import Path
from typing import Any

from .base import SpeechToTextBackend, SpeechToTextUnavailable, TranscriptResult

log = logging.getLogger("voice.stt")

# Monkey-patch av.open to handle metadata_errors parameter (removed in PyAV 12.0+)
# faster-whisper uses av.open(metadata_errors="ignore") which was removed in PyAV 12.0+
try:
    import av
    _original_av_open = av.open
    def _patched_av_open(*args, **kwargs):
        # Remove metadata_errors if present (removed in PyAV 12.0+)
        kwargs.pop("metadata_errors", None)
        return _original_av_open(*args, **kwargs)
    av.open = _patched_av_open
except Exception:
    pass


class FasterWhisperBackend(SpeechToTextBackend):
    """Local speech-to-text backend using faster-whisper."""

    name = "faster-whisper"

    def __init__(
        self,
        model_name: str = "base",
        device: str = "auto",
        compute_type: str = "int8",
        download_root: str | Path | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._download_root = Path(download_root) if download_root else None
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._initialized = False
        self._init_error: str | None = None

    def _resolve_device(self) -> str:
        """Resolve the device string to a concrete value."""
        if self._device != "auto":
            return self._device
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def _initialize_model(self) -> bool:
        """Initialize the faster-whisper model. Returns True if successful."""
        with self._model_lock:
            if self._initialized:
                return self._model is not None
            self._initialized = True
            try:
                from faster_whisper import WhisperModel
                device = self._resolve_device()
                log.info(f"Loading faster-whisper model '{self._model_name}' on {device} ({self._compute_type})")
                self._model = WhisperModel(
                    self._model_name,
                    device=device,
                    compute_type=self._compute_type,
                    download_root=str(self._download_root) if self._download_root else None,
                )
                log.info(f"faster-whisper model '{self._model_name}' loaded successfully")
                return True
            except Exception as exc:
                self._init_error = f"Failed to load faster-whisper model: {exc}"
                log.error(self._init_error)
                return False

    def is_available(self) -> bool:
        # Only available if explicitly configured with a model path
        if not self._initialized:
            # Only initialize if explicitly configured with a model path
            if self._download_root and Path(self._download_root).exists():
                return self._initialize_model()
            return False
        return self._model is not None

    def dependency_status(self) -> dict[str, bool]:
        """Return the status of optional dependencies."""
        return {
            "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
            "speech_recognition": importlib.util.find_spec("speech_recognition") is not None,
        }

    def transcribe(self, audio_path: str | None = None, timeout: float = 30.0) -> TranscriptResult:
        if not self.is_available():
            raise SpeechToTextUnavailable(
                "faster-whisper backend is not available",
                details={"init_error": self._init_error or "Model not initialized"},
            )

        if audio_path is None:
            raise SpeechToTextUnavailable("No audio file path provided")

        audio_file = Path(audio_path)
        if not audio_file.exists():
            raise SpeechToTextUnavailable(f"Audio file not found: {audio_path}")

        try:
            log.debug(f"Transcribing {audio_file} with faster-whisper")
            segments, info = self._model.transcribe(
                str(audio_file),
                beam_size=5,
                language=None,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            text_parts = []
            for segment in segments:
                text_parts.append(segment.text)
            full_text = " ".join(text_parts).strip()

            return TranscriptResult(
                text=full_text,
                confidence=None,
                language=info.language or "",
                source=self.name,
                metadata={
                    "model": self._model_name,
                    "duration": info.duration,
                    "all_language_probs": getattr(info, "language_probability", None),
                },
            )
        except Exception as exc:
            raise SpeechToTextUnavailable(f"Transcription failed: {exc}")


def create_faster_whisper_backend(
    model_name: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    download_root: str | Path | None = None,
) -> FasterWhisperBackend:
    """Factory to create a configured FasterWhisperBackend from settings."""
    return FasterWhisperBackend(
        model_name=model_name or "base",
        device=device or "auto",
        compute_type=compute_type or "int8",
        download_root=download_root,
    )


__all__ = ["FasterWhisperBackend", "create_faster_whisper_backend"]