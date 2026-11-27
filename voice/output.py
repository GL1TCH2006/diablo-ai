from __future__ import annotations

"""Structured event-to-speech routing with a deliberately small response policy."""

from dataclasses import dataclass, field
from typing import Any

from .tts import TTSBackend, TTSError, TTSResult


@dataclass(slots=True)
class VoiceResponsePolicy:
    speak_types: frozenset[str] = frozenset(
        {"message", "success", "task_completed", "task_failed", "task_cancelled", "task_cancel_failed", "error", "permission_required"}
    )

    def should_speak(self, event: Any) -> bool:
        event_type = str(getattr(event, "type", ""))
        return event_type in self.speak_types

    def message_for(self, event: Any) -> str:
        event_type = str(getattr(event, "type", ""))
        if event_type in {"task_cancelled", "cancelled"}:
            return "Task cancelled."
        if event_type in {"task_cancel_failed", "error", "task_failed"}:
            return str(getattr(event, "message", "Diablo encountered an error."))
        if event_type in {"success", "task_completed"}:
            return "Task completed."
        if event_type == "permission_required":
            return "Permission is required."
        return str(getattr(event, "message", ""))


@dataclass(slots=True)
class VoiceOutputResult:
    handled: bool
    spoken: bool = False
    text: str = ""
    result: TTSResult | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "spoken": self.spoken,
            "text": self.text,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class VoiceOutputRouter:
    """Speak selected structured events through a replaceable TTS backend."""

    def __init__(self, tts: TTSBackend, policy: VoiceResponsePolicy | None = None, interruptible: bool = True) -> None:
        self.tts = tts
        self.policy = policy or VoiceResponsePolicy()
        self._muted = False
        self._subscription = None
        self._interruptible = bool(interruptible)

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def toggle_muted(self) -> bool:
        self._muted = not self._muted
        return self._muted

    @property
    def interruptible(self) -> bool:
        return self._interruptible

    def set_interruptible(self, interruptible: bool) -> None:
        self._interruptible = bool(interruptible)

    def attach_interruption_manager(self, manager) -> None:
        self._subscription = manager.subscribe(self.handle_event)

    def handle_event(self, event: Any) -> VoiceOutputResult:
        # Handle cancellation events by stopping any ongoing speech if interruptible
        event_type = str(getattr(event, "type", ""))
        if event_type in {"task_cancel_requested", "task_cancelled"} and self._interruptible:
            self.stop_speaking()

        if not self.policy.should_speak(event):
            return VoiceOutputResult(handled=False, metadata={"reason": "filtered_event"})
        text = self.policy.message_for(event)
        if self._muted:
            return VoiceOutputResult(handled=True, text=text, metadata={"muted": True})
        try:
            result = self.tts.speak(text, now=False)
        except TTSError as exc:
            return VoiceOutputResult(handled=True, text=text, error=exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - isolate an optional TTS backend
            return VoiceOutputResult(
                handled=True,
                text=text,
                error={"error": "tts_backend_failure", "message": str(exc) or type(exc).__name__},
            )
        return VoiceOutputResult(handled=True, spoken=True, text=text, result=result)

    def stop_speaking(self) -> bool:
        """Stop any ongoing speech. Returns True if speech was stopped."""
        if hasattr(self.tts, 'stop'):
            return self.tts.stop()
        return False

    def close(self) -> None:
        if self._subscription is not None:
            self._subscription()
            self._subscription = None


__all__ = ["VoiceResponsePolicy", "VoiceOutputResult", "VoiceOutputRouter"]
