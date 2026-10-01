"""M2.14.1 - honest voice capability reporting.

The voice subsystem shipped at S2.5 with a contract for TTS, a contract for STT,
a wake-word listener, and cancellation. What it did not ship is a working STT, a
real audio wake-word detector, or audio barge-in. ``voice_enabled`` defaults to
``False``, the production path is three lines in :mod:`ui.terminal`, and no
capability adapter is registered for voice at all - so nothing in the model-facing
surface ever claimed any of it existed.

That is the right shape for unfinished work, but it left the roadmap claiming a
voice milestone it had not fully delivered, and left a reader unable to tell
which half worked. This module states the truth in one place, in the same
structured form the rest of the codebase uses for capability discovery, and
keeps it testable without a sound card.

What is genuinely available
---------------------------
TTS
    ``voice/text_to_speech.py`` drives Windows SAPI through PowerShell and works
    on a machine with SAPI voices installed. Validated live on the development
    machine (2 voices present). The neural backend is a **stub that raises
    unconditionally** even when fully provisioned - see ``voice/tts/neural.py``.

STT
    **Not implemented.** All three backends raise
    :class:`~voice.stt.base.SpeechToTextUnavailable`; none decodes audio. The
    local backend only detects whether ``faster_whisper`` is installed.

Wake word
    **Not implemented as audio.** ``voice/wake_wording.py`` matches a prefix
    against text that has already been transcribed, i.e. something that would
    require working STT first. There is no microphone capture, no hotword model.

Barge-in
    **Not implemented.** ``voice/text_to_speech.py`` discards its speaking
    thread and drives SAPI through ``subprocess.run``, so audio already playing
    cannot be stopped, and with no microphone there is nothing to detect the user
    starting to talk.

What *is* real
---------------
Task-level cooperative cancellation: ``voice/cancellation.py`` provides a
``CancellationToken`` and an ``InterruptionManager`` that emit structured events
and request cancellation of the running task. That is genuinely working and is
compatible with the Agent's own cancellation protocol, which is why it is worth
keeping while the audio half is deferred.

Reporting, not pretending
-------------------------
:func:`voice_status` returns what each subsystem actually is, with a stable
vocabulary. It is what the companion and console UIs can display, and what a
future ``voice.*`` capability adapter should gate on. Nothing here enables a
feature that does not work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["VoiceCapability", "VoiceComponentStatus", "voice_status", "render_voice_status"]


class VoiceCapability(str, Enum):
    """What a voice component actually is. No value implies "partly works"."""

    IMPLEMENTED = "implemented"
    """Works against a real dependency, validated."""

    STUB = "stub"
    """Code exists and raises or does nothing. Never reported as working."""

    DEFERRED = "deferred"
    """Deliberately not built yet. Honest about the absence."""

    UNCONFIGURED = "unconfigured"
    """Real implementation, but nothing is wired to it in production."""


class VoiceComponentStatus(str, Enum):
    """Coarse availability of a voice component, for a UI badge."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"

    @classmethod
    def from_value(cls, value: str) -> "VoiceComponentStatus":
        try:
            return cls(str(value or "").strip().lower())
        except ValueError:
            return cls.UNAVAILABLE


@dataclass(frozen=True)
class VoiceStatus:
    """Structured, truthful voice status.

    Deliberately has no ``enabled`` field that implies audio works. ``tts`` being
    available says nothing about ``stt``.
    """

    tts_capability: VoiceCapability = VoiceCapability.STUB
    tts_backend: str = ""
    tts_available: bool = False
    stt_capability: VoiceCapability = VoiceCapability.DEFERRED
    stt_backend: str = ""
    stt_available: bool = False
    wake_capability: VoiceCapability = VoiceCapability.DEFERRED
    wake_available: bool = False
    barge_in_capability: VoiceCapability = VoiceCapability.DEFERRED
    barge_in_available: bool = False
    cancellation_available: bool = True
    notes: tuple[str, ...] = field(default=())

    @property
    def any_working(self) -> bool:
        return self.tts_available or self.stt_available

    def to_dict(self) -> dict:
        return {
            "tts": {
                "capability": self.tts_capability.value,
                "available": self.tts_available,
                "backend": self.tts_backend,
            },
            "stt": {
                "capability": self.stt_capability.value,
                "available": self.stt_available,
                "backend": self.stt_backend,
                "reason": "no installed backend decodes audio; all backends raise",
            },
            "wake_word": {
                "capability": self.wake_capability.value,
                "available": self.wake_available,
                "reason": "text prefix match over an existing transcript, not audio detection",
            },
            "barge_in": {
                "capability": self.barge_in_capability.value,
                "available": self.barge_in_available,
                "reason": "no microphone capture and no way to stop audio already playing",
            },
            "cancellation": {
                "available": self.cancellation_available,
                "note": "task-level cooperative cancellation; compatible with Agent cancellation",
            },
            "any_working": self.any_working,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """One line, for a status bar."""
        if not self.any_working:
            return "voice: nothing available"
        parts = [f"tts={self.tts_backend or 'none'}"]
        if self.stt_available:
            parts.append(f"stt={self.stt_backend}")
        else:
            parts.append("stt=unimplemented")
        return "voice: " + ", ".join(parts)


def voice_status(settings=None) -> VoiceStatus:
    """Report what voice can actually do on this machine, right now.

    Probes the real factories rather than reading configuration, so the answer is
    about the installed system and not about what someone hoped was installed.
    Never raises: a broken voice subsystem must not stop Diablo from starting.
    """
    notes: list[str] = []
    enabled = bool(getattr(settings, "voice_enabled", False))

    tts_capability = VoiceCapability.STUB
    tts_backend = ""
    tts_available = False
    try:
        from voice.tts import create_tts_backend
        backend = create_tts_backend()
        tts_backend = type(backend).__name__
        tts_available = bool(getattr(backend, "is_available", lambda: False)())
        if tts_available:
            tts_capability = VoiceCapability.IMPLEMENTED
        else:
            notes.append("no TTS backend is available on this machine")
    except Exception as exc:  # noqa: BLE001 - status must never raise
        notes.append(f"TTS probe failed: {exc}")

    # STT is reported as DEFERRED unconditionally. That is a statement about the
    # code, not about this machine: no shipped backend implements transcription,
    # so no amount of configuration can make one available. A future real backend
    # changes this line and nothing else.
    stt_capability = VoiceCapability.DEFERRED
    stt_backend = ""
    stt_available = False
    try:
        from voice.stt import create_fallback_stt
        stt = create_fallback_stt()
        stt_backend = type(stt).__name__
        # is_available() is True only for the mock, which returns canned strings
        # and never opens a microphone. It is not transcription.
        if type(stt).__name__ == "MockSpeechToTextBackend":
            stt_backend = ""
    except Exception as exc:  # noqa: BLE001
        notes.append(f"STT probe failed: {exc}")

    cancellation_available = True
    try:
        from voice.cancellation import CancellationToken, InterruptionManager  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        cancellation_available = False
        notes.append(f"voice cancellation unavailable: {exc}")

    if not enabled:
        notes.append("voice_enabled is false; TTS is not invoked by the UI")

    return VoiceStatus(
        tts_capability=tts_capability,
        tts_backend=tts_backend,
        tts_available=tts_available,
        stt_capability=stt_capability,
        stt_backend=stt_backend,
        stt_available=stt_available,
        wake_capability=VoiceCapability.DEFERRED,
        wake_available=False,
        barge_in_capability=VoiceCapability.DEFERRED,
        barge_in_available=False,
        cancellation_available=cancellation_available,
        notes=tuple(notes),
    )


def render_voice_status(status: VoiceStatus | None = None, settings=None) -> str:
    """Human-readable status block, for the console UI and the companion."""
    status = status or voice_status(settings)
    lines = ["Voice capability status (M2.14.1 - measured, not assumed):"]
    lines.append(f"  TTS        : {status.tts_capability.value}"
                 f"{f' via {status.tts_backend}' if status.tts_backend else ''}"
                 f"{' (available)' if status.tts_available else ''}")
    lines.append(f"  STT        : {status.stt_capability.value} - no installed backend decodes audio")
    lines.append(f"  Wake word  : {status.wake_capability.value} - text match, not audio detection")
    lines.append(f"  Barge-in   : {status.barge_in_capability.value} - no microphone capture")
    lines.append(f"  Cancellation: {'available' if status.cancellation_available else 'unavailable'}"
                 " (task-level, real)")
    for note in status.notes:
        lines.append(f"  - {note}")
    return "\n".join(lines)
