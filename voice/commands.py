from __future__ import annotations

"""Deterministic voice command classification and routing."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from .cancellation import InterruptionManager


class VoiceCommand(str, Enum):
    STOP = "stop"
    REQUEST = "request"
    REPEAT = "repeat"
    STATUS = "status"
    MUTE = "mute"
    UNMUTE = "unmute"
    EXIT = "exit"
    UNKNOWN = "unknown"


class VoiceCommandBridge(Protocol):
    def submit_message(self, text: str, task_id: str = "") -> Any:
        ...

    def cancel_task(self, task_id: str = "") -> bool:
        ...

    def current_status(self):
        ...


@dataclass(slots=True)
class VoiceCommandResult:
    command: VoiceCommand
    text: str
    success: bool
    supported: bool = True
    action: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.value,
            "text": self.text,
            "success": self.success,
            "supported": self.supported,
            "action": self.action,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


class VoiceCommandRouter:
    """Route a small deterministic command vocabulary through existing bridges."""

    def __init__(
        self,
        bridge: VoiceCommandBridge,
        interruptions: InterruptionManager,
        *,
        output=None,
        on_exit: Callable[[], Any] | None = None,
    ) -> None:
        self._bridge = bridge
        self._interruptions = interruptions
        self._output = output
        self._on_exit = on_exit
        self._last_request = ""
        self._muted = False

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def last_request(self) -> str:
        return self._last_request

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())

    def classify(self, text: str) -> VoiceCommand:
        if not isinstance(text, str) or not text.strip():
            return VoiceCommand.UNKNOWN
        normalized = self._normalize(text)
        if normalized == "diablo" or not normalized:
            return VoiceCommand.UNKNOWN
        if normalized.startswith("diablo "):
            normalized = normalized[7:].strip()
        stop_phrases = {"stop", "stop that", "cancel", "cancel that", "stop everything", "cancel everything"}
        repeat_phrases = {"repeat", "repeat that", "say again"}
        status_phrases = {"status", "status please", "what is your status"}
        mute_phrases = {"mute", "mute voice"}
        unmute_phrases = {"unmute", "unmute voice"}
        exit_phrases = {"exit", "close", "quit"}
        if normalized in stop_phrases:
            return VoiceCommand.STOP
        if normalized in repeat_phrases:
            return VoiceCommand.REPEAT
        if normalized in status_phrases:
            return VoiceCommand.STATUS
        if normalized in mute_phrases:
            return VoiceCommand.MUTE
        if normalized in unmute_phrases:
            return VoiceCommand.UNMUTE
        if normalized in exit_phrases:
            return VoiceCommand.EXIT
        return VoiceCommand.REQUEST

    def _error_result(self, command: VoiceCommand, text: str, message: str, supported: bool = True) -> VoiceCommandResult:
        return VoiceCommandResult(
            command=command,
            text=text,
            success=False,
            supported=supported,
            message=message,
        )

    def _current_task_id(self) -> str:
        active_ids = getattr(self._bridge, "active_job_ids", ())
        if isinstance(active_ids, (list, tuple, set)) and len(active_ids) == 1:
            return str(next(iter(active_ids)))
        return ""

    @staticmethod
    def _submission_result(response: Any) -> tuple[bool, str]:
        if response is None:
            return True, ""
        success = getattr(response, "success", None)
        if success is not None:
            if not isinstance(success, bool):
                return False, "bridge returned a malformed success flag"
            if not success:
                error = getattr(response, "error", None)
                if isinstance(error, Mapping):
                    return False, str(error.get("message") or error.get("error") or "bridge rejected request")
                return False, str(error or "bridge rejected request")
            return True, ""
        if isinstance(response, Mapping):
            if "success" not in response:
                return False, "bridge returned a malformed result"
            if not isinstance(response["success"], bool):
                return False, "bridge returned a malformed success flag"
            if response["success"]:
                return True, ""
            error = response.get("error")
            if isinstance(error, Mapping):
                return False, str(error.get("message") or error.get("error") or "bridge rejected request")
            return False, str(error or "bridge rejected request")
        return False, "bridge returned a malformed result"

    def route(self, text: str) -> VoiceCommandResult:
        command = self.classify(text)
        if command is VoiceCommand.UNKNOWN:
            return self._error_result(command, text, "Voice command is empty or unsupported", supported=False)
        if command is VoiceCommand.STOP:
            outcome = self._interruptions.request_cancel(
                self._current_task_id(),
                source="voice",
                reason="user_interruption",
                metadata={"transcript": text},
            )
            success = outcome.accepted and not outcome.error
            return VoiceCommandResult(
                command=command,
                text=text,
                success=success,
                action="cancel",
                message="Cancellation propagated" if success else (outcome.error or "Cancellation was rejected"),
                metadata=outcome.to_dict(),
            )
        if command is VoiceCommand.REQUEST:
            self._last_request = text
            try:
                response = self._bridge.submit_message(text)
            except Exception as exc:
                return self._error_result(command, text, str(exc) or type(exc).__name__)
            success, error = self._submission_result(response)
            if not success:
                return self._error_result(command, text, error)
            return VoiceCommandResult(command=command, text=text, success=True, action="submit", metadata={"response": response} if isinstance(response, dict) else {})
        if command is VoiceCommand.REPEAT:
            if not self._last_request:
                return self._error_result(command, text, "There is no previous request to repeat")
            try:
                response = self._bridge.submit_message(self._last_request)
            except Exception as exc:
                return self._error_result(command, text, str(exc) or type(exc).__name__)
            success, error = self._submission_result(response)
            if not success:
                return self._error_result(command, text, error)
            return VoiceCommandResult(command=command, text=text, success=True, action="repeat", metadata={"response": response} if isinstance(response, dict) else {})
        if command is VoiceCommand.STATUS:
            try:
                status = self._bridge.current_status()
            except Exception as exc:
                return self._error_result(command, text, str(exc) or type(exc).__name__)
            return VoiceCommandResult(command=command, text=text, success=True, action="status", message=str(status), metadata={"status": str(status)})
        if command in {VoiceCommand.MUTE, VoiceCommand.UNMUTE}:
            self._muted = command is VoiceCommand.MUTE
            if self._output is not None and hasattr(self._output, "set_muted"):
                self._output.set_muted(self._muted)
            return VoiceCommandResult(command=command, text=text, success=True, action="mute" if self._muted else "unmute")
        if command is VoiceCommand.EXIT:
            if self._on_exit is None:
                return self._error_result(command, text, "No exit handler is configured", supported=False)
            try:
                self._on_exit()
            except Exception as exc:
                return self._error_result(command, text, str(exc) or type(exc).__name__)
            return VoiceCommandResult(command=command, text=text, success=True, action="exit")
        return self._error_result(command, text, "Voice command is unsupported", supported=False)


__all__ = ["VoiceCommand", "VoiceCommandResult", "VoiceCommandBridge", "VoiceCommandRouter"]
