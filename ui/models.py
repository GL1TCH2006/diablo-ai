from __future__ import annotations

"""Structured state and activity models for the Diablo companion UI."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UIStatus(str, Enum):
    """Lifecycle states exposed by the UI controller."""

    IDLE = "idle"
    READY = "ready"
    THINKING = "thinking"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCESS = "success"
    CANCELLED = "cancelled"
    ERROR = "error"
    CLOSED = "closed"

    @classmethod
    def from_value(cls, value: "UIStatus | str") -> "UIStatus":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "done": "success",
            "complete": "success",
            "completed": "success",
            "failed": "error",
            "failure": "error",
            "canceled": "cancelled",
            "busy": "executing",
            "working": "executing",
            "verify": "verifying",
        }
        normalized = aliases.get(normalized, normalized)
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"unknown UI status: {value!r}") from exc

    @property
    def label(self) -> str:
        return self.value.capitalize()


@dataclass(slots=True)
class AgentEvent:
    """Structured event delivered by an agent bridge."""

    type: str
    message: str
    status: str = ""
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("event type is required")
        if not isinstance(self.message, str):
            raise ValueError("event message must be a string")
        if not isinstance(self.metadata, dict):
            raise ValueError("event metadata must be an object")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentEvent":
        if not isinstance(data, dict):
            raise ValueError("event must be an object")
        if "type" not in data:
            raise ValueError("event is missing type")
        timestamp = data.get("timestamp", time.time())
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise ValueError("event timestamp must be numeric") from exc
        return cls(
            type=str(data["type"]),
            message=str(data.get("message", "")),
            status=str(data.get("status", "")),
            task_id=str(data.get("task_id", "")),
            timestamp=timestamp,
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "status": self.status,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class UIActivity:
    """One displayable conversation or system activity entry."""

    role: str
    text: str
    timestamp: float = field(default_factory=time.time)
    status: str = ""
    task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "timestamp": self.timestamp,
            "status": self.status,
            "task_id": self.task_id,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class UIActionResult:
    """Result of a UI action such as sending or cancelling a message."""

    success: bool
    job_id: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "job_id": self.job_id,
            "error": self.error,
        }


class UIControllerError(Exception):
    """Structured controller failure for callers that prefer exceptions."""

    code = "ui_controller_error"
    default_code = "ui_controller_error"

    def __init__(self, message: str, *, code: str = "", details: dict | None = None) -> None:
        self.message = message
        self.code = code or self.default_code
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


__all__ = [
    "UIStatus",
    "AgentEvent",
    "UIActivity",
    "UIActionResult",
    "UIControllerError",
]
