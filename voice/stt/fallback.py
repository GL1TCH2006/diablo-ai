from __future__ import annotations

"""Fallback STT backend selection."""

from .base import SpeechToTextBackend, SpeechToTextUnavailable, UnavailableSpeechToTextBackend
from .local import LocalSpeechToTextBackend


def create_fallback_stt(model_path: str | None = None) -> SpeechToTextBackend:
    """Return an installed local backend when possible, otherwise unavailable."""
    local = LocalSpeechToTextBackend(model_path)
    return local if local.is_available() else UnavailableSpeechToTextBackend(
        "No configured local speech-to-text backend is available"
    )


__all__ = [
    "SpeechToTextBackend",
    "SpeechToTextUnavailable",
    "LocalSpeechToTextBackend",
    "UnavailableSpeechToTextBackend",
    "create_fallback_stt",
]
