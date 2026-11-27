"""M2.18 - the edge companion's speech path must probe before it speaks.

The defect
==========

``ui/edge_companion.py:_maybe_speak`` called ``voice.text_to_speech.speak(text)``
directly, with no availability check. ``ui/terminal.py:_speak`` does probe, once,
and caches the result.

The visible consequence on a host with no SAPI voices: every companion state
change that carries speech (LISTENING, ERROR, RETURNING) spawned a PowerShell
process that failed, and the failure was only logged at debug level. Three wasted
subprocesses per transition cycle, silently.

What is pinned here
===================

* with no TTS backend, **nothing is spoken and no backend module is touched**;
* the probe runs **once** and is cached, including a negative - it must not return
  to the frame path;
* with a working backend, the chosen text still reaches it.

Nothing here creates a window: the method under test is reached on a bare
``object.__new__`` instance with a fake snapshot, so no Tk root is constructed and
no speech is ever produced. ``voice`` is only ever *probed*, never driven.
"""

from __future__ import annotations

import sys
import types

import pytest

from ui.companion_state import CompanionState, CompanionSnapshot


def _bare_app(speak: bool = True):
    """An ``EdgeCompanionApp`` with only the attributes the speech path touches."""
    from ui.edge_companion import EdgeCompanionApp

    app = object.__new__(EdgeCompanionApp)
    app._speak_enabled = bool(speak)
    app._last_spoken_state = None
    app._tts_available_cache = None
    return app


def _snapshot(state: CompanionState) -> CompanionSnapshot:
    return CompanionSnapshot(
        state=state, task_id="t1", task_text="do a thing", status_line="",
        terminal_message="", progress=(), can_cancel=False,
        cancellation_requested=False, events=(), goal_line="", goal_active=False,
    )


@pytest.fixture()
def fake_voice(monkeypatch):
    """A fake ``voice`` package. Records calls; never produces sound.

    Replaces the two module attributes the speech path reaches for:
    ``voice.capabilities.voice_status`` and ``voice.text_to_speech``.
    """
    spoken: list[str] = []
    probes = {"count": 0}
    state = {"available": False}

    def voice_status(*_a, **_k):
        probes["count"] += 1
        return types.SimpleNamespace(tts_available=state["available"], notes=())

    fake_tts = types.SimpleNamespace(speak=lambda text: spoken.append(text))

    voice_module = types.ModuleType("voice")
    voice_module.__path__ = []  # mark as a package so submodule imports resolve
    caps_module = types.ModuleType("voice.capabilities")
    caps_module.voice_status = voice_status
    tts_module = types.ModuleType("voice.text_to_speech")
    tts_module.speak = fake_tts.speak

    monkeypatch.setitem(sys.modules, "voice", voice_module)
    monkeypatch.setitem(sys.modules, "voice.capabilities", caps_module)
    monkeypatch.setitem(sys.modules, "voice.text_to_speech", tts_module)
    return {"spoken": spoken, "probes": probes, "state": state}


def test_no_tts_backend_means_nothing_is_spoken(fake_voice):
    """The regression: an unavailable backend must not reach the speech call."""
    app = _bare_app(speak=True)
    fake_voice["state"]["available"] = False

    app._maybe_speak(_snapshot(CompanionState.LISTENING))

    assert fake_voice["spoken"] == [], (
        f"spoke with no TTS backend available: {fake_voice['spoken']}")
    assert fake_voice["probes"]["count"] == 1, (
        "the availability probe was not consulted exactly once")


def test_available_backend_still_speaks(fake_voice):
    """The probe must not disable speech where speech works."""
    app = _bare_app(speak=True)
    fake_voice["state"]["available"] = True

    app._maybe_speak(_snapshot(CompanionState.LISTENING))

    assert fake_voice["spoken"] == ["What is the task?"], (
        f"a working backend was silenced by the probe: {fake_voice['spoken']}")


def test_the_probe_is_cached_and_never_returns_to_the_frame_path(fake_voice):
    """A probe per frame would put the cost back that the check removed."""
    app = _bare_app(speak=True)
    fake_voice["state"]["available"] = True

    for index in range(25):
        state = (CompanionState.LISTENING if index % 2 == 0
                 else CompanionState.RETURNING)
        app._maybe_speak(_snapshot(state))

    assert fake_voice["probes"]["count"] == 1, (
        f"the probe ran {fake_voice['probes']['count']} times over 25 frames; it "
        f"must be cached like TerminalUI._voice_status")
    assert len(fake_voice["spoken"]) == 25, (
        f"speech was dropped after the first frame: {len(fake_voice['spoken'])}")


def test_a_negative_probe_is_cached_too(fake_voice):
    """No SAPI voice will appear mid-process, so re-probing is pure cost."""
    app = _bare_app(speak=True)
    fake_voice["state"]["available"] = False

    for _ in range(10):
        app._maybe_speak(_snapshot(CompanionState.RETURNING))

    assert fake_voice["probes"]["count"] == 1, (
        f"a missing backend was re-probed {fake_voice['probes']['count']} times")


def test_speech_disabled_never_probes(fake_voice):
    """With --edge-speak off there is nothing to check."""
    app = _bare_app(speak=False)

    app._maybe_speak(_snapshot(CompanionState.LISTENING))

    assert fake_voice["probes"]["count"] == 0, (
        "the voice stack was probed although speaking is disabled")
    assert fake_voice["spoken"] == []


def test_a_broken_voice_stack_is_not_fatal(monkeypatch):
    """A voice subsystem that explodes must not take the companion with it."""
    app = _bare_app(speak=True)

    import voice  # noqa: F401  - installed by the fixture in other tests

    def explode(*_a, **_k):
        raise RuntimeError("voice stack is broken")

    caps = types.ModuleType("voice.capabilities")
    caps.voice_status = explode
    monkeypatch.setitem(sys.modules, "voice.capabilities", caps)

    assert app._tts_available() is False, (
        "a raising voice probe did not report unavailable")
    # And the caller survives it.
    app._maybe_speak(_snapshot(CompanionState.LISTENING))