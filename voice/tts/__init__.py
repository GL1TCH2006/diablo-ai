from .base import MockTTSBackend, TTSBackend, TTSError, TTSResult, TTSUnavailable
from .neural import OptionalNeuralTTSBackend
from .sapi import SapiTTSBackend


def create_tts_backend(model_path: str | None = None, package_name: str = "kokoro") -> TTSBackend:
    """Select an explicitly configured neural backend, then SAPI, then unavailable-safe mock."""
    neural = OptionalNeuralTTSBackend(model_path=model_path, package_name=package_name)
    if neural.is_available():
        return neural
    sapi = SapiTTSBackend()
    if sapi.is_available():
        return sapi
    return MockTTSBackend(available=False)


__all__ = [
    "TTSBackend",
    "TTSError",
    "TTSUnavailable",
    "TTSResult",
    "MockTTSBackend",
    "SapiTTSBackend",
    "OptionalNeuralTTSBackend",
    "create_tts_backend",
]
