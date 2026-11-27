"""M2.13.0 - routing signals and a resource guard that actually guards.

Two defects are covered here that no existing test could have caught, because
both made the code return "no opinion" rather than raise:

* ``_memory_status`` declared ``MEMORYSTATUSEX`` without its two leading
  ``DWORD`` fields, so every value was read at the wrong offset and the
  resource guard silently did nothing.
* That guard compared a model's size against *available* memory, a snapshot of
  what happens to be free right now, when the question is whether the model
  fits at all.
"""

from __future__ import annotations

import dataclasses

import pytest

from ai.model_router import (
    ModelChoice,
    ModelRouter,
    _available_ram,
    _memory_status,
    _total_ram,
    estimate_complexity,
)
from ai.provider import ModelInfo

GIB = 1024 ** 3


def _catalog():
    return [
        ModelInfo(name="qwen3:8b", size_bytes=5 * GIB, parameter_size="8B",
                  capabilities=("tools",), family="qwen3"),
        ModelInfo(name="qwen3:14b", size_bytes=9 * GIB, parameter_size="14B",
                  capabilities=("tools",), family="qwen3"),
    ]


def _settings(tmp_path, **kwargs):
    from config.settings import Settings

    data = tmp_path / "data"
    return Settings(
        base_dir=tmp_path, ai_provider="ollama", ollama_host="http://x", ai_model="",
        workspace_root=tmp_path / "p", data_dir=data, memory_dir=data / "m",
        logs_dir=data / "l", backups_dir=data / "b", scratch_dir=tmp_path / "w",
        sessions_dir=data / "s", voice_enabled=False, log_level="INFO",
        allowed_roots=[tmp_path], raw={}, **kwargs,
    )


# --------------------------------------------------------------------------
# memory reporting
# --------------------------------------------------------------------------


def test_memory_status_reports_real_values():
    total, available = _memory_status()
    # The misaligned struct returned 0 here, which silently disabled the guard.
    assert total > 0, "total physical memory must be readable"
    assert 0 < available <= total


def test_total_and_available_are_distinct_quantities():
    total, available = _total_ram(), _available_ram()
    assert total > 0
    assert 0 <= available <= total


def test_unknown_memory_degrades_to_zero_rather_than_raising(monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (0, 0))
    assert _total_ram() == 0
    assert _available_ram() == 0


# --------------------------------------------------------------------------
# the resource guard
# --------------------------------------------------------------------------


def test_a_model_that_fits_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (16 * GIB, 1 * GIB))
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=True), catalog=_catalog())
    # 5GiB of weights in 16GiB total, with only 1GiB free right now: it fits.
    assert router._guard_resource(ModelChoice("qwen3:8b")).name == "qwen3:8b"


def test_a_model_bigger_than_total_memory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (16 * GIB, 14 * GIB))
    catalog = _catalog() + [
        ModelInfo(name="nemotron:30b", size_bytes=24 * GIB, parameter_size="30B",
                  capabilities=("tools",), family="nemotron"),
    ]
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=True), catalog=catalog)
    choice = router._guard_resource(ModelChoice("nemotron:30b"))
    assert choice.name != "nemotron:30b"
    assert "resource-fallback" in choice.source
    assert "total" in choice.reason
    # A decision made against a hard limit is not a confident one.
    assert choice.confidence < 0.5


def test_the_guard_uses_total_not_available(tmp_path, monkeypatch):
    # 9GiB of weights needs ~14GiB with runtime overhead, so it fits in 16GiB
    # total even though only 2GiB happens to be free right now. The old
    # available-RAM comparison would have refused it.
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (16 * GIB, 2 * GIB))
    catalog = [
        ModelInfo(name="big:12b", size_bytes=9 * GIB, parameter_size="12B",
                  capabilities=("tools",), family="big"),
        ModelInfo(name="qwen3:8b", size_bytes=5 * GIB, parameter_size="8B",
                  capabilities=("tools",), family="qwen3"),
    ]
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=True), catalog=catalog)
    assert router._guard_resource(ModelChoice("big:12b")).name == "big:12b"


def test_no_smaller_model_leaves_a_warning_not_a_silent_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (8 * GIB, 7 * GIB))
    catalog = [
        ModelInfo(name="huge:70b", size_bytes=40 * GIB, parameter_size="70B",
                  capabilities=("tools",), family="huge"),
    ]
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=True), catalog=catalog)
    choice = router._guard_resource(ModelChoice("huge:70b"))
    # With nowhere to fall back to, say so instead of pretending all is well.
    assert choice.name == "huge:70b"
    assert "resource-warn" in choice.source
    assert choice.confidence < 0.5


def test_guard_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (1 * GIB, 1 * GIB))
    catalog = _catalog() + [
        ModelInfo(name="nemotron:30b", size_bytes=24 * GIB, parameter_size="30B",
                  capabilities=("tools",)),
    ]
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=False), catalog=catalog)
    assert router._guard_resource(ModelChoice("nemotron:30b")).name == "nemotron:30b"


def test_unknown_memory_never_blocks_a_model(tmp_path, monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (0, 0))
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=True), catalog=_catalog())
    # Not knowing the hardware must not stop the agent from working.
    assert router._guard_resource(ModelChoice("qwen3:8b")).name == "qwen3:8b"


def test_unknown_model_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("ai.model_router._memory_status", lambda: (1 * GIB, 1 * GIB))
    router = ModelRouter(_settings(tmp_path, resource_check_enabled=True), catalog=_catalog())
    assert router._guard_resource(ModelChoice("not-installed")).name == "not-installed"


# --------------------------------------------------------------------------
# choice signals
# --------------------------------------------------------------------------


def test_choice_defaults_are_backwards_compatible():
    choice = ModelChoice("m")
    assert choice.confidence == 1.0
    assert choice.signals == ()
    assert choice.to_state_line() == "MODEL: m  MODE: AUTO  SOURCE: default"


def test_choice_serialises_its_evidence():
    choice = ModelChoice("m", "auto", "auto-complexity", "because", confidence=0.75,
                         signals=("complexity=0.80",))
    payload = choice.to_dict()
    assert payload["confidence"] == 0.75
    assert payload["signals"] == ["complexity=0.80"]
    assert payload["reason"] == "because"


def test_explicit_override_stays_most_confident(tmp_path):
    router = ModelRouter(_settings(tmp_path), catalog=_catalog())
    choice = router.choose(task_override="qwen3:14b")
    assert choice.name == "qwen3:14b"
    assert choice.source == "task"
    assert choice.confidence == 1.0


def test_routing_itself_is_unchanged(tmp_path):
    # The M2.12 precedence must not shift:
    # task override > escalation after a failed verification > complexity > fast.
    settings = _settings(tmp_path, models_complexity_threshold=0.5)
    router = ModelRouter(settings, catalog=_catalog())
    assert router.choose(task_override="qwen3:14b").source == "task"
    assert router.choose(complexity=0.9).source == "auto-complexity"
    assert router.choose(complexity=0.0).source == "default"

    escalated = router.choose(complexity=0.0, last_verify_failed=True, escalations_used=0)
    assert escalated.escalated is True
    assert escalated.source == "escalation"
    # Bounded: once the budget is spent, it stops escalating.
    spent = router.choose(complexity=0.0, last_verify_failed=True, escalations_used=1)
    assert spent.escalated is False


def test_complexity_heuristic_is_unchanged():
    assert estimate_complexity("hello") < 0.3
    assert estimate_complexity("refactor the architecture and fix the deadlock") > 0.3
    assert estimate_complexity("do this and then that and then the other thing") > \
        estimate_complexity("hello")
    assert 0.0 <= estimate_complexity("") <= 1.0


def test_settings_carry_the_new_knobs(tmp_path):
    settings = _settings(tmp_path)
    assert settings.ai_request_timeout == 600.0
    assert settings.tools_full_context is False
    assert dataclasses.replace(settings, tools_full_context=True).tools_full_context is True
