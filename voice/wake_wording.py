from __future__ import annotations

"""Wake-word detector contracts with no implicit microphone access."""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("voice.wake_word")


@dataclass(slots=True)
class WakeWordResult:
    detected: bool
    phrase: str = ""
    confidence: float = 0.0
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "phrase": self.phrase,
            "confidence": self.confidence,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


class WakeWordDetector(ABC):
    name = "base"

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def detect(self, text: str) -> WakeWordResult:
        ...


class MockWakeWordDetector(WakeWordDetector):
    name = "mock"

    def __init__(self, wake_word: str = "diablo", enabled: bool = True) -> None:
        self.wake_word = wake_word.strip().lower()
        self.enabled = enabled
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self.enabled

    def detect(self, text: str) -> WakeWordResult:
        self.calls.append(text)
        if not self.enabled or not isinstance(text, str):
            return WakeWordResult(detected=False, source=self.name, metadata={"enabled": self.enabled})
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        detected = normalized == self.wake_word or normalized.startswith(self.wake_word + " ")
        return WakeWordResult(
            detected=detected,
            phrase=self.wake_word if detected else "",
            confidence=1.0 if detected else 0.0,
            source=self.name,
        )


class DisabledWakeWordDetector(WakeWordDetector):
    name = "disabled"

    def is_available(self) -> bool:
        return False

    def detect(self, text: str) -> WakeWordResult:
        return WakeWordResult(detected=False, source=self.name, metadata={"enabled": False})


class PorcupineWakeWordDetector(WakeWordDetector):
    """Wake word detector using Picovoice Porcupine (requires access key)."""

    name = "porcupine"

    def __init__(
        self,
        access_key: str,
        keyword_paths: list[str] | None = None,
        keyword_names: list[str] | None = None,
        sensitivities: list[float] | None = None,
    ) -> None:
        self._access_key = access_key
        self._keyword_paths = [Path(p) for p in (keyword_paths or [])]
        self._keyword_names = keyword_names or ["diablo"]
        self._sensitivities = sensitivities or [0.5] * len(self._keyword_names)
        self._porcupine = None
        self._initialized = False
        self._init_error: str | None = None

    def _initialize(self) -> bool:
        if self._initialized:
            return self._porcupine is not None
        self._initialized = True
        try:
            import pvporcupine
            self._porcupine = pvporcupine.create(
                access_key=self._access_key,
                keyword_paths=[str(p) for p in self._keyword_paths] if self._keyword_paths else None,
                keywords=self._keyword_names if not self._keyword_paths else None,
                sensitivities=self._sensitivities,
            )
            log.info(f"Porcupine wake word detector initialized with keywords: {self._keyword_names}")
            return True
        except Exception as exc:
            self._init_error = f"Failed to initialize Porcupine: {exc}"
            log.error(self._init_error)
            return False

    def is_available(self) -> bool:
        if not self._initialized:
            return self._initialize()
        return self._porcupine is not None

    def detect_audio(self, audio_frame: list[int]) -> tuple[bool, str]:
        """Detect wake word from audio frame (16-bit PCM, single channel)."""
        if not self.is_available():
            return False, ""
        try:
            keyword_index = self._porcupine.process(audio_frame)
            if keyword_index >= 0:
                keyword = self._keyword_names[keyword_index] if keyword_index < len(self._keyword_names) else "unknown"
                return True, keyword
        except Exception as exc:
            log.error(f"Porcupine detection error: {exc}")
        return False, ""

    def detect(self, text: str) -> "WakeWordResult":
        # Text-based fallback for compatibility
        if not self.is_available():
            return WakeWordResult(detected=False, source=self.name, metadata={"error": self._init_error or "unavailable"})
        # Text-based detection as fallback
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        detected = normalized.startswith(self._keyword_names[0] + " ") or normalized == self._keyword_names[0]
        return WakeWordResult(
            detected=detected,
            phrase=self._keyword_names[0] if detected else "",
            confidence=1.0 if detected else 0.0,
            source=self.name,
        )

    def close(self) -> None:
        if self._porcupine:
            self._porcupine.delete()
            self._porcupine = None


class OpenWakeWordDetector(WakeWordDetector):
    """Wake word detector using openWakeWord (open-source, no access key needed)."""

    name = "openwakeword"

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float = 0.5,
    ) -> None:
        self._model_path = model_path
        self._threshold = threshold
        self._model = None
        self._initialized = False
        self._init_error: str | None = None

    def _initialize(self) -> bool:
        if self._initialized:
            return self._model is not None
        self._initialized = True
        try:
            from openwakeword.model import Model
            self._model = Model(wakeword_models=[self._model_path] if self._model_path else None)
            log.info("openWakeWord model loaded")
            return True
        except Exception as exc:
            self._init_error = f"Failed to load openWakeWord: {exc}"
            log.error(self._init_error)
            return False

    def is_available(self) -> bool:
        if not self._initialized:
            return self._initialize()
        return self._model is not None

    def detect_audio(self, audio_frame: list[float]) -> tuple[bool, str]:
        if not self.is_available():
            return False, ""
        try:
            import numpy as np
            prediction = self._model.predict(np.array(audio_frame, dtype=np.float32))
            for keyword, score in prediction.items():
                if score >= self._threshold:
                    return True, keyword
        except Exception as exc:
            log.error(f"openWakeWord detection error: {exc}")
        return False, ""

    def detect(self, text: str) -> "WakeWordResult":
        # Text-based fallback
        if not self.is_available():
            return WakeWordResult(detected=False, source=self.name, metadata={"error": self._init_error or "unavailable"})
        # Not applicable for text-based detection
        return WakeWordResult(detected=False, source=self.name, metadata={"note": "audio-only detector"})

    def close(self) -> None:
        if self._model:
            # openWakeWord model cleanup if needed
            self._model = None


class WakeWordListener:
    """Text-fed wake-word listener; it never opens a microphone by itself."""

    def __init__(
        self,
        wake_word: str = "diablo",
        language: str = "en-US",
        detector: "WakeWordDetector | None" = None,
    ) -> None:
        self.wake_word = wake_word.lower()
        self.language = language
        self.detector = detector or MockWakeWordDetector(self.wake_word)
        self._on_wake: Any | None = None
        self._listening = False

    def start(self, on_wake: Callable[[Any], None]) -> None:
        self._on_wake = on_wake
        self._listening = True

    def stop(self) -> None:
        self._listening = False

    def process_text(self, text: str) -> "WakeWordResult":
        if not self._listening:
            from .wake_wording import WakeWordResult
            return WakeWordResult(detected=False, source="listener", metadata={"listening": False})
        result = self.detector.detect(text)
        if result.detected and callable(self._on_wake):
            self._on_wake(result)
        return result

    @property
    def listening(self) -> bool:
        return self._listening


class WakeWordError(Exception):
    pass


def create_wake_word_detector(
    detector_type: str = "mock",
    wake_word: str = "diablo",
    **kwargs: Any,
) -> "WakeWordDetector":
    """Factory function to create a wake word detector."""
    if detector_type == "porcupine":
        return PorcupineWakeWordDetector(**kwargs)
    elif detector_type == "openwakeword":
        return OpenWakeWordDetector(**kwargs)
    elif detector_type == "disabled":
        return DisabledWakeWordDetector()
    else:
        return MockWakeWordDetector(wake_word=wake_word, **kwargs)


class DisabledWakeWordDetector(WakeWordDetector):
    name = "disabled"

    def is_available(self) -> bool:
        return False

    def detect(self, text: str) -> "WakeWordResult":
        return WakeWordResult(detected=False, source=self.name, metadata={"enabled": False})


class MockWakeWordDetector(WakeWordDetector):
    name = "mock"

    def __init__(self, wake_word: str = "diablo", enabled: bool = True) -> None:
        self.wake_word = wake_word.strip().lower()
        self.enabled = enabled
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self.enabled

    def detect(self, text: str) -> "WakeWordResult":
        self.calls.append(text)
        if not self.enabled or not isinstance(text, str):
            return WakeWordResult(detected=False, source=self.name, metadata={"enabled": self.enabled})
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        detected = normalized == self.wake_word or normalized.startswith(self.wake_word + " ")
        return WakeWordResult(
            detected=detected,
            phrase=self.wake_word if detected else "",
            confidence=1.0 if detected else 0.0,
            source=self.name,
        )


class PorcupineWakeWordDetector(WakeWordDetector):
    """Wake word detector using Picovoice Porcupine (requires access key)."""

    name = "porcupine"

    def __init__(
        self,
        access_key: str,
        keyword_paths: list[str] | None = None,
        keyword_names: list[str] | None = None,
        sensitivities: list[float] | None = None,
    ) -> None:
        self._access_key = access_key
        self._keyword_paths = [Path(p) for p in (keyword_paths or [])]
        self._keyword_names = keyword_names or ["diablo"]
        self._sensitivities = sensitivities or [0.5] * len(self._keyword_names)
        self._porcupine = None
        self._initialized = False
        self._init_error: str | None = None

    def _initialize(self) -> bool:
        if self._initialized:
            return self._porcupine is not None
        self._initialized = True
        try:
            import pvporcupine
            self._porcupine = pvporcupine.create(
                access_key=self._access_key,
                keyword_paths=[str(p) for p in self._keyword_paths] if self._keyword_paths else None,
                keywords=self._keyword_names if not self._keyword_paths else None,
                sensitivities=self._sensitivities,
            )
            log.info(f"Porcupine wake word detector initialized with keywords: {self._keyword_names}")
            return True
        except Exception as exc:
            self._init_error = f"Failed to initialize Porcupine: {exc}"
            log.error(self._init_error)
            return False

    def is_available(self) -> bool:
        if not self._initialized:
            return self._initialize()
        return self._porcupine is not None

    def detect_audio(self, audio_frame: list[int]) -> tuple[bool, str]:
        """Detect wake word from audio frame (16-bit PCM, single channel)."""
        if not self.is_available():
            return False, ""
        try:
            keyword_index = self._porcupine.process(audio_frame)
            if keyword_index >= 0:
                keyword = self._keyword_names[keyword_index] if keyword_index < len(self._keyword_names) else "unknown"
                return True, keyword
        except Exception as exc:
            log.error(f"Porcupine detection error: {exc}")
        return False, ""

    def detect(self, text: str) -> "WakeWordResult":
        # Text-based fallback for compatibility
        if not self.is_available():
            return WakeWordResult(detected=False, source=self.name, metadata={"error": self._init_error or "unavailable"})
        # Text-based detection as fallback
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        detected = normalized.startswith(self._keyword_names[0] + " ") or normalized == self._keyword_names[0]
        return WakeWordResult(
            detected=detected,
            phrase=self._keyword_names[0] if detected else "",
            confidence=1.0 if detected else 0.0,
            source=self.name,
        )

    def close(self) -> None:
        if self._porcupine:
            self._porcupine.delete()
            self._porcupine = None


class OpenWakeWordDetector(WakeWordDetector):
    """Wake word detector using openWakeWord (open-source, no access key needed)."""

    name = "openwakeword"

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float = 0.5,
    ) -> None:
        self._model_path = model_path
        self._threshold = threshold
        self._model = None
        self._initialized = False
        self._init_error: str | None = None

    def _initialize(self) -> bool:
        if self._initialized:
            return self._model is not None
        self._initialized = True
        try:
            from openwakeword.model import Model
            self._model = Model(wakeword_models=[self._model_path] if self._model_path else None)
            log.info("openWakeWord model loaded")
            return True
        except Exception as exc:
            self._init_error = f"Failed to load openWakeWord: {exc}"
            log.error(self._init_error)
            return False

    def is_available(self) -> bool:
        if not self._initialized:
            return self._initialize()
        return self._model is not None

    def detect_audio(self, audio_frame: list[float]) -> tuple[bool, str]:
        if not self.is_available():
            return False, ""
        try:
            import numpy as np
            prediction = self._model.predict(np.array(audio_frame, dtype=np.float32))
            for keyword, score in prediction.items():
                if score >= self._threshold:
                    return True, keyword
        except Exception as exc:
            log.error(f"openWakeWord detection error: {exc}")
        return False, ""

    def detect(self, text: str) -> "WakeWordResult":
        # Text-based fallback
        if not self.is_available():
            return WakeWordResult(detected=False, source=self.name, metadata={"error": self._init_error or "unavailable"})
        # Not applicable for text-based detection
        return WakeWordResult(detected=False, source=self.name, metadata={"note": "audio-only detector"})

    def close(self) -> None:
        if self._model:
            # openWakeWord model cleanup if needed
            self._model = None


class DisabledWakeWordDetector(WakeWordDetector):
    name = "disabled"

    def is_available(self) -> bool:
        return False

    def detect(self, text: str) -> "WakeWordResult":
        return WakeWordResult(detected=False, source=self.name, metadata={"enabled": False})


class MockWakeWordDetector(WakeWordDetector):
    name = "mock"

    def __init__(self, wake_word: str = "diablo", enabled: bool = True) -> None:
        self.wake_word = wake_word.strip().lower()
        self.enabled = enabled
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self.enabled

    def detect(self, text: str) -> "WakeWordResult":
        self.calls.append(text)
        if not self.enabled or not isinstance(text, str):
            return WakeWordResult(detected=False, source=self.name, metadata={"enabled": self.enabled})
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        detected = normalized == self.wake_word or normalized.startswith(self.wake_word + " ")
        return WakeWordResult(
            detected=detected,
            phrase=self.wake_word if detected else "",
            confidence=1.0 if detected else 0.0,
            source=self.name,
        )


class WakeWordListener:
    """Text-fed wake-word listener; it never opens a microphone by itself."""

    def __init__(
        self,
        wake_word: str = "diablo",
        language: str = "en-US",
        detector: "WakeWordDetector | None" = None,
    ) -> None:
        self.wake_word = wake_word.lower()
        self.language = language
        self.detector = detector or MockWakeWordDetector(self.wake_word)
        self._on_wake: Any | None = None
        self._listening = False

    def start(self, on_wake: Callable[[Any], None]) -> None:
        self._on_wake = on_wake
        self._listening = True

    def stop(self) -> None:
        self._listening = False

    def process_text(self, text: str) -> "WakeWordResult":
        if not self._listening:
            from .wake_wording import WakeWordResult
            return WakeWordResult(detected=False, source="listener", metadata={"listening": False})
        result = self.detector.detect(text)
        if result.detected and callable(self._on_wake):
            self._on_wake(result)
        return result

    @property
    def listening(self) -> bool:
        return self._listening


class WakeWordError(Exception):
    pass


def create_wake_word_detector(
    detector_type: str = "mock",
    wake_word: str = "diablo",
    **kwargs: Any,
) -> "WakeWordDetector":
    """Factory function to create a wake word detector."""
    if detector_type == "porcupine":
        return PorcupineWakeWordDetector(**kwargs)
    elif detector_type == "openwakeword":
        return OpenWakeWordDetector(**kwargs)
    elif detector_type == "disabled":
        return DisabledWakeWordDetector()
    else:
        return MockWakeWordDetector(wake_word=wake_word, **kwargs)


__all__ = [
    "WakeWordResult",
    "WakeWordDetector",
    "WakeWordListener",
    "WakeWordError",
    "MockWakeWordDetector",
    "DisabledWakeWordDetector",
    "PorcupineWakeWordDetector",
    "OpenWakeWordDetector",
    "create_wake_word_detector",
]