from __future__ import annotations

"""Structured voice-layer events and status values."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VoiceStatus(str, Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    ROUTING = "routing"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    MUTED = "muted"
    ERROR = "error"

    @classmethod
    def from_value(cls, value: "VoiceStatus | str") -> "VoiceStatus":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        normalized = {"canceled": "cancelled"}.get(normalized, normalized)
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"unknown voice status: {value!r}") from exc


@dataclass(slots=True)
class VoiceEvent:
    """JSON-safe event emitted by voice input, interruption, or output routing."""

    type: str
    message: str
    status: str = ""
    source: str = "voice"
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("voice event type is required")
        if not isinstance(self.message, str):
            raise ValueError("voice event message must be a string")
        if not isinstance(self.metadata, dict):
            raise ValueError("voice event metadata must be an object")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceEvent":
        if not isinstance(data, dict):
            raise ValueError("voice event must be an object")
        if "type" not in data:
            raise ValueError("voice event is missing type")
        try:
            timestamp = float(data.get("timestamp", time.time()))
        except (TypeError, ValueError) as exc:
            raise ValueError("voice event timestamp must be numeric") from exc
        return cls(
            type=str(data["type"]),
            message=str(data.get("message", "")),
            status=str(data.get("status", "")),
            source=str(data.get("source", "voice")),
            task_id=str(data.get("task_id", "")),
            timestamp=timestamp,
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "status": self.status,
            "source": self.source,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


__all__ = ["VoiceStatus", "VoiceEvent"]
