from __future__ import annotations

"""Microphone audio capture using sounddevice."""

import logging
import queue
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger("voice.capture")


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "int16"
    block_size: int = 1024


@dataclass
class RecordingConfig:
    max_duration: float = 30.0
    silence_timeout: float = 2.0
    energy_threshold: float = 0.01
    min_recording_duration: float = 0.5


@dataclass
class RecordingResult:
    file_path: str
    duration: float
    sample_rate: int
    channels: int
    success: bool
    error: str | None = None


class MicrophoneCapture:
    """Capture audio from microphone using sounddevice."""

    def __init__(
        self,
        audio_config: AudioConfig | None = None,
        recording_config: RecordingConfig | None = None,
        device: int | str | None = None,
    ) -> None:
        self.audio_config = audio_config or AudioConfig()
        self.recording_config = recording_config or RecordingConfig()
        self._device = device
        self._stream: sd.InputStream | None = None
        self._recording = False
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._frames: list[np.ndarray] = []
        self._start_time: float = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def list_devices(self) -> list[dict[str, Any]]:
        """List available audio input devices."""
        devices = []
        for i, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] > 0:
                devices.append({
                    "index": i,
                    "name": device["name"],
                    "channels": device["max_input_channels"],
                    "default_samplerate": device["default_samplerate"],
                })
        return devices

    def is_available(self) -> bool:
        """Check if microphone capture is available."""
        try:
            devices = self.list_devices()
            return len(devices) > 0
        except Exception as exc:
            log.warning(f"Microphone capture unavailable: {exc}")
            return False

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: sd.CallbackFlags) -> None:
        """Callback for sounddevice input stream."""
        if status:
            log.warning(f"Audio callback status: {status}")
        if self._recording:
            self._audio_queue.put(indata.copy())

    def start_recording(self, output_path: str | Path) -> bool:
        """Start recording audio to a file."""
        with self._lock:
            if self._recording:
                log.warning("Already recording")
                return False

            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            self._frames = []
            self._stop_event.clear()
            self._recording = True
            self._start_time = time.time()

            try:
                self._stream = sd.InputStream(
                    samplerate=self.audio_config.sample_rate,
                    channels=self.audio_config.channels,
                    dtype=self.audio_config.dtype,
                    blocksize=self.audio_config.block_size,
                    device=self._device,
                    callback=self._audio_callback,
                )
                self._stream.start()
            except Exception as exc:
                self._recording = False
                log.error(f"Failed to start audio stream: {exc}")
                return False

            self._thread = threading.Thread(
                target=self._write_loop,
                args=(output_path,),
                daemon=True,
            )
            self._thread.start()
            return True

    def _write_loop(self, output_path: Path) -> None:
        """Write audio frames to WAV file."""
        try:
            with wave.open(str(output_path), "wb") as wf:
                wf.setnchannels(self.audio_config.channels)
                wf.setsampwidth(2)  # int16 = 2 bytes
                wf.setframerate(self.audio_config.sample_rate)

                silence_start: float | None = None
                last_audio_time = time.time()

                while self._recording and not self._stop_event.is_set():
                    try:
                        chunk = self._audio_queue.get(timeout=0.1)
                        wf.writeframes(chunk.tobytes())

                        # Energy-based VAD
                        audio_data = chunk.astype(np.float32) / 32768.0
                        energy = np.mean(np.abs(audio_data))
                        now = time.time()

                        if energy > 0.01:  # Configurable threshold
                            last_audio_time = now
                            silence_start = None
                        else:
                            if silence_start is None:
                                silence_start = now
                            elif now - silence_start >= self.recording_config.silence_timeout:
                                if now - self._start_time >= self.recording_config.min_recording_duration:
                                    log.info("Silence timeout reached, stopping recording")
                                    break

                        # Max duration check
                        if now - self._start_time >= self.recording_config.max_duration:
                            log.info("Max recording duration reached")
                            break

                    except queue.Empty:
                        continue
        finally:
            with self._lock:
                self._recording = False

    def stop_recording(self) -> RecordingResult:
        """Stop recording and return result."""
        with self._lock:
            if not self._recording:
                return RecordingResult(
                    file_path="",
                    duration=0.0,
                    sample_rate=0,
                    channels=0,
                    success=False,
                    error="Not recording",
                )

            self._stop_event.set()
            self._recording = False

        if self._thread:
            self._thread.join(timeout=5.0)

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass

        duration = time.time() - self._start_time
        return RecordingResult(
            file_path="",  # Will be set by caller
            duration=duration,
            sample_rate=self.audio_config.sample_rate,
            channels=self.audio_config.channels,
            success=True,
        )

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def close(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._recording:
                self._recording = False
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


class AudioCaptureError(Exception):
    """Error during audio capture."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)


def list_microphones() -> list[dict[str, Any]]:
    """List available microphone input devices."""
    devices = []
    for i, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            devices.append({
                "index": i,
                "name": device["name"],
                "channels": device["max_input_channels"],
                "default_samplerate": device["default_samplerate"],
            })
    return devices


def is_microphone_available() -> bool:
    """Check if any microphone is available."""
    try:
        return len(list_microphones()) > 0
    except Exception:
        return False


__all__ = [
    "AudioConfig",
    "RecordingConfig",
    "RecordingResult",
    "MicrophoneCapture",
    "AudioCaptureError",
    "list_microphones",
    "is_microphone_available",
]