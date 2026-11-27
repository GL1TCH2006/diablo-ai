from __future__ import annotations

"""Additive voice-to-UI status adapter; S2.4 widgets remain unchanged."""

from typing import Any

from ui.models import AgentEvent

from .events import VoiceEvent, VoiceStatus


class VoiceUIAdapter:
    """Expose voice/interruption state to a future or existing UI consumer."""

    def __init__(self, event_sink=None) -> None:
        self._event_sink = event_sink
        self._status = VoiceStatus.IDLE
        self._listening = False
        self._interruption_state = "idle"
        self._last_event: VoiceEvent | None = None
        self._last_agent_event: AgentEvent | None = None

    @property
    def status(self) -> VoiceStatus:
        return self._status

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def interruption_state(self) -> str:
        return self._interruption_state

    @property
    def last_event(self) -> VoiceEvent | None:
        return self._last_event

    @property
    def last_agent_event(self) -> AgentEvent | None:
        return self._last_agent_event

    def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)
        self._status = VoiceStatus.LISTENING if self._listening else VoiceStatus.IDLE

    def handle_event(self, event: VoiceEvent | dict) -> AgentEvent:
        if isinstance(event, dict):
            try:
                event = VoiceEvent.from_dict(event)
            except Exception as exc:
                event = VoiceEvent(
                    type="error",
                    message="Malformed voice event",
                    status="error",
                    metadata={
                        "error": "malformed_voice_event",
                        "reason": str(exc),
                        "raw_type": type(event).__name__,
                    },
                )
        self._last_event = event
        if event.status:
            try:
                self._status = VoiceStatus.from_value(event.status)
            except ValueError:
                self._status = VoiceStatus.ERROR
        if event.type in {"task_cancel_requested", "task_cancelled", "task_cancel_failed"}:
            self._interruption_state = event.type.removeprefix("task_")
        if event.type == "task_cancel_failed":
            agent_status = "error"
        elif event.type == "task_cancelled" or self._status is VoiceStatus.CANCELLED:
            agent_status = "cancelled"
        else:
            agent_status = ""
        agent_event = AgentEvent(
            type=event.type,
            message=event.message,
            status=agent_status,
            task_id=event.task_id,
            timestamp=event.timestamp,
            metadata={"source": "voice", **dict(event.metadata)},
        )
        self._last_agent_event = agent_event
        if self._event_sink is not None:
            self._event_sink(agent_event)
        return agent_event

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status.value,
            "listening": self._listening,
            "interruption_state": self._interruption_state,
            "last_event": self._last_event.to_dict() if self._last_event else None,
        }


__all__ = ["VoiceUIAdapter"]
