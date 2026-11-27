from __future__ import annotations

"""Voice Activity Detection (VAD) implementations."""

import logging
import numpy as np

log = logging.getLogger("voice.vad")


class VAD:
    """Base Voice Activity Detection interface."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate

    def is_speech(self, audio_frame: np.ndarray) -> bool:
        """Return True if the frame contains speech."""
        raise NotImplementedError

    def reset(self) -> None:
        """Reset internal state."""
        pass


class EnergyVAD(VAD):
    """Simple energy-based Voice Activity Detection.

    Uses short-term energy to detect speech presence.
    No external dependencies required.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.01,
        frame_duration_ms: int = 30,
        hangover_frames: int = 10,
    ) -> None:
        super().__init__(sample_rate)
        self.threshold = threshold
        self.frame_size = int(sample_rate * frame_duration_ms / 1000)
        self.hangover_frames = hangover_frames
        self._hangover_counter = 0
        self._in_speech = False

    def is_speech(self, audio_frame: np.ndarray) -> bool:
        """Detect speech using short-term energy."""
        if len(audio_frame) == 0:
            return False

        # Normalize to float [-1, 1]
        if audio_frame.dtype == np.int16:
            audio_float = audio_frame.astype(np.float32) / 32768.0
        elif audio_frame.dtype == np.int32:
            audio_float = audio_frame.astype(np.float32) / 2147483648.0
        else:
            audio_float = audio_frame.astype(np.float32)

        # Compute short-term energy (RMS)
        energy = np.sqrt(np.mean(audio_float ** 2))

        is_speech = energy > self.threshold

        # Hangover logic: keep speech state for a few frames after energy drops
        if is_speech:
            self._in_speech = True
            self._hangover_counter = self.hangover_frames
            return True
        elif self._in_speech and self._hangover_counter > 0:
            self._hangover_counter -= 1
            return True
        else:
            self._in_speech = False
            return False

    def reset(self) -> None:
        self._in_speech = False
        self._hangover_counter = 0


class WebRTCVAD(VAD):
    """WebRTC-based Voice Activity Detection (optional, requires webrtcvad package)."""

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 2,
    ) -> None:
        super().__init__(sample_rate)
        self.aggressiveness = aggressiveness
        self._vad = None
        self._init_vad()

    def _init_vad(self) -> None:
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(self.aggressiveness)
        except ImportError:
            log.warning("webrtcvad not available, falling back to EnergyVAD")
            self._vad = None

    def is_speech(self, audio_frame: np.ndarray) -> bool:
        if self._vad is None:
            # Fallback to energy-based
            if not hasattr(self, "_energy_vad"):
                self._energy_vad = EnergyVAD(self.sample_rate)
            return self._energy_vad.is_speech(audio_frame)

        if len(audio_frame) == 0:
            return False

        # WebRTC VAD expects 10, 20, or 30ms frames at 8/16/32/48kHz
        if audio_frame.dtype == np.int16:
            frame_bytes = audio_frame.tobytes()
        elif audio_frame.dtype == np.float32:
            frame_bytes = (audio_frame * 32767).astype(np.int16).tobytes()
        else:
            frame_bytes = audio_frame.astype(np.int16).tobytes()

        try:
            return self._vad.is_speech(frame_bytes, self.sample_rate)
        except Exception:
            return False

    def reset(self) -> None:
        pass


def create_vad(
    method: str = "energy",
    sample_rate: int = 16000,
    **kwargs: Any,
) -> VAD:
    """Factory function to create a VAD instance."""
    if method == "webrtc":
        return WebRTCVAD(sample_rate=sample_rate, **kwargs)
    else:
        return EnergyVAD(sample_rate=sample_rate, **kwargs)


__all__ = [
    "VAD",
    "EnergyVAD",
    "WebRTCVAD",
    "create_vad",
]