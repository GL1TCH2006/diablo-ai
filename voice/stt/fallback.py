from __future__ import annotations

"""Fallback STT backend selection."""

from .base import SpeechToTextBackend, SpeechToTextUnavailable, UnavailableSpeechToTextBackend
from .local import FasterWhisperBackend, create_faster_whisper_backend


def create_faster_whisper_backend(
    model_name: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    download_root: str | None = None,
) -> FasterWhisperBackend:
    """Return a configured FasterWhisperBackend from settings."""
    from voice.stt.local import FasterWhisperBackend as F
    return F(
        model_name=model_name or "base",
        device=device or "auto",
        compute_type=compute_type or "int8",
        download_root=download_root,
    )


def create_fallback_stt(model_path: str | None = None) -> SpeechToTextBackend:
    """Return an installed local backend when possible, otherwise unavailable."""
    try:
        local = FasterWhisperBackend(model_path)
        if local.is_available():
            return local
    except Exception:
        pass
    return UnavailableSpeechToTextBackend(
        "No configured local speech-to-text backend is available"
    )


__all__ = [
    "SpeechToTextBackend",
    "SpeechToTextUnavailable",
    "FasterWhisperBackend",
    "UnavailableSpeechToTextBackend",
    "create_fallback_stt",
    "create_faster_whisper_backend",
]