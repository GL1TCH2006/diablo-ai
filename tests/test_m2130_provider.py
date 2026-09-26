"""M2.13.0 - structured provider failure and observable degradation.

The point of these tests is that a degraded answer can no longer be
indistinguishable from a real one. Previously a provider failure was caught
with a bare ``except Exception`` and discarded, so an offline reply looked
exactly like a model's own.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from ai.ollama_provider import OllamaProvider, _parse_args
from ai.provider import (
    RETRYABLE,
    AIProviderError,
    FailureCategory,
    ModelInfo,
    ToolCall,
)
from config.permissions import PermissionManager
from core.agent import Agent
from core.events import EventBus
from memory.preferences import PreferencesStore
from tools import build_registry


# --------------------------------------------------------------------------
# the error type
# --------------------------------------------------------------------------


def test_error_is_still_backwards_compatible():
    # Existing call sites raise with a bare message; str() must be unchanged.
    err = AIProviderError("something broke")
    assert str(err) == "something broke"
    assert err.category == FailureCategory.UNKNOWN
    assert err.retryable is False
    assert isinstance(err, Exception)


def test_categories_separate_slow_from_incapable():
    # A timeout means "too slow for this request"; malformed means "cannot do
    # this task". Conflating them is what made the old fallback chain blind.
    slow = AIProviderError("t", category=FailureCategory.TIMEOUT)
    broken = AIProviderError("m", category=FailureCategory.MALFORMED)
    assert slow.retryable is True
    assert broken.retryable is False
    assert slow.category != broken.category


def test_http_retryability_follows_the_status():
    assert AIProviderError("x", category=FailureCategory.HTTP, status=503).retryable is True
    assert AIProviderError("x", category=FailureCategory.HTTP, status=404).retryable is False
    # No status to judge by: fall back to the category.
    assert AIProviderError("x", category=FailureCategory.HTTP).retryable is True


def test_retryable_can_be_overridden():
    err = AIProviderError("x", category=FailureCategory.HTTP, status=404, retryable=True)
    assert err.retryable is True


def test_error_carries_model_and_latency():
    err = AIProviderError("x", category=FailureCategory.TIMEOUT, model="qwen3:8b", latency=1.23456)
    assert err.model == "qwen3:8b"
    assert err.latency == pytest.approx(1.23456)
    assert err.to_dict()["latency"] == 1.235
    assert err.to_dict()["model"] == "qwen3:8b"


def test_error_is_serialisable():
    payload = AIProviderError("x", category=FailureCategory.TIMEOUT, model="m").to_dict()
    assert set(payload) == {"category", "model", "latency", "retryable", "status", "message"}
    assert all(isinstance(value, (str, int, float, bool)) for value in payload.values())


def test_every_category_is_declared_and_non_overlapping():
    assert set(FailureCategory.ALL) == set(RetryABLE := RETRYABLE) | {
        FailureCategory.MALFORMED, FailureCategory.NO_MODEL, FailureCategory.UNKNOWN
    }
    assert FailureCategory.MALFORMED not in RETRYABLE


def test_long_messages_are_truncated_for_telemetry():
    detail = AIProviderError("x" * 5000, category=FailureCategory.HTTP)
    assert len(detail.to_dict()["message"]) <= 300
    # The full text is still available on the exception itself.
    assert len(detail.message) == 5000


# --------------------------------------------------------------------------
# the provider
# --------------------------------------------------------------------------


def test_timeout_defaults_to_the_historical_value():
    # An unset timeout must not silently mean "no timeout" or "zero".
    assert OllamaProvider("http://x").timeout == 600.0
    assert OllamaProvider("http://x", timeout=None).timeout == 600.0


def test_timeout_is_configurable_and_clamped():
    assert OllamaProvider("http://x", timeout=45).timeout == 45.0
    # A nonsensical value floors at 1s rather than failing or hanging.
    assert OllamaProvider("http://x", timeout=0).timeout == 1.0
    assert OllamaProvider("http://x", timeout=-10).timeout == 1.0


def test_malformed_arguments_are_categorised_and_not_retryable():
    with pytest.raises(AIProviderError) as info:
        _parse_args("{not json", model="qwen3:8b")
    assert info.value.category == FailureCategory.MALFORMED
    assert info.value.retryable is False
    assert info.value.model == "qwen3:8b"


def test_non_object_arguments_are_malformed():
    with pytest.raises(AIProviderError) as info:
        _parse_args("[1, 2, 3]")
    assert info.value.category == FailureCategory.MALFORMED
    assert info.value.retryable is False


def test_dict_arguments_pass_through():
    assert _parse_args({"a": 1}) == {"a": 1}
    assert _parse_args("") == {}


def test_missing_model_is_a_configuration_error_not_a_transient_one():
    provider = OllamaProvider("http://x")
    with pytest.raises(AIProviderError) as info:
        provider.chat([{"role": "user", "content": "hi"}], model="")
    assert info.value.category == FailureCategory.NO_MODEL
    assert info.value.retryable is False


def test_server_error_is_retryable_and_records_the_model(monkeypatch):
    provider = OllamaProvider("http://x")
    response = httpx.Response(503, text="overloaded", request=httpx.Request("POST", "http://x"))
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
            httpx.HTTPStatusError("boom", request=response.request, response=response)
        )
    )
    with pytest.raises(AIProviderError) as info:
        provider.chat([{"role": "user", "content": "hi"}], model="qwen3:8b")
    assert info.value.category == FailureCategory.HTTP
    assert info.value.retryable is True
    assert info.value.status == 503
    assert info.value.model == "qwen3:8b"
    assert info.value.latency > 0


def test_client_error_is_not_retryable(monkeypatch):
    provider = OllamaProvider("http://x")
    response = httpx.Response(404, text="no such model", request=httpx.Request("POST", "http://x"))
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
            httpx.HTTPStatusError("boom", request=response.request, response=response)
        )
    )
    with pytest.raises(AIProviderError) as info:
        provider.chat([{"role": "user", "content": "hi"}], model="ghost:1b")
    assert info.value.retryable is False
    assert info.value.status == 404


def test_timeout_is_categorised_as_a_timeout(monkeypatch):
    provider = OllamaProvider("http://x", timeout=30)
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
    )
    with pytest.raises(AIProviderError) as info:
        provider.chat([{"role": "user", "content": "hi"}], model="qwen3:8b")
    assert info.value.category == FailureCategory.TIMEOUT
    assert info.value.retryable is True
    assert info.value.model == "qwen3:8b"
    assert "30" in str(info.value)


class FakeResponse:
    """Minimal stand-in for the httpx response the provider consumes."""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_injected_tool_timeout_is_used_for_the_request(monkeypatch):
    provider = OllamaProvider("http://x", timeout=30)
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse({"message": {"content": "hi", "tool_calls": []}})

    monkeypatch.setattr(httpx, "post", fake_post)
    provider.chat([{"role": "user", "content": "hi"}], model="qwen3:8b")
    assert seen["timeout"] == 30.0


def test_successful_response_is_unchanged(monkeypatch):
    provider = OllamaProvider("http://x")
    payload = {
        "message": {
            "content": "done",
            "tool_calls": [
                {"function": {"name": "filesystem.read_file", "arguments": '{"path": "a"}'}},
            ],
        },
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))
    response = provider.chat([{"role": "user", "content": "hi"}], model="qwen3:8b")
    assert response.text == "done"
    assert response.model == "qwen3:8b"
    assert response.tool_calls == [ToolCall(id="call_0", name="filesystem.read_file", arguments={"path": "a"})]


# --------------------------------------------------------------------------
# degradation is observable
# --------------------------------------------------------------------------


class BrokenProvider:
    """Fails every call with a structured error."""

    model_label = "broken"
    name = "broken"

    def __init__(self, exc=None):
        self.exc = exc or AIProviderError("too slow", category=FailureCategory.TIMEOUT, model="primary")

    def is_available(self):
        return True

    def chat(self, messages, tools=None, model=None):
        raise self.exc


class AlternatingProvider:
    """Fails on the primary model, succeeds on the fallback."""

    model_label = "alt"
    name = "alt"

    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, model=None):
        self.calls.append(model)
        if len(self.calls) == 1:
            raise AIProviderError("too slow", category=FailureCategory.TIMEOUT, model=model)
        return SimpleNamespace(text="recovered", tool_calls=[])


def _agent(tmp_settings, provider, catalog=None):
    prefs = PreferencesStore(tmp_settings.memory_dir)
    bus = EventBus()
    seen = []
    for kind in ("model_fallback", "model_offline_fallback"):
        bus.subscribe(kind, lambda data: seen.append(data))
    agent = Agent(
        provider=provider,
        settings=tmp_settings,
        registry=build_registry(tmp_settings),
        permissions=PermissionManager(tmp_settings, prefs),
        events=bus,
        confirmer=None,
        project=None,
        memory_preferences=prefs,
        model_name="primary",
    )
    if catalog is not None:
        # The router is the only thing that decides which model is tried next,
        # so a fallback test has to give it something to fall back *to*.
        from ai.model_router import ModelRouter

        agent._router = ModelRouter(tmp_settings, catalog=catalog)
    return agent, seen


def _catalog():
    return [
        ModelInfo(name="qwen3:8b", size_bytes=5 * 1024 ** 3, parameter_size="8B",
                  capabilities=("tools",), family="qwen3"),
        ModelInfo(name="qwen3:14b", size_bytes=9 * 1024 ** 3, parameter_size="14B",
                  capabilities=("tools",), family="qwen3"),
    ]


def test_failure_is_recorded_on_the_request_context(tmp_settings):
    agent, _ = _agent(tmp_settings, BrokenProvider())
    agent.respond("add a greeting function to the project")
    failures = agent.provider_failures
    assert failures, "a swallowed provider failure must be recorded"
    assert failures[0]["category"] == FailureCategory.TIMEOUT
    assert failures[0]["model"] == "primary"
    assert failures[0]["next_model"]


def test_offline_degradation_is_announced(tmp_settings):
    agent, seen = _agent(tmp_settings, BrokenProvider())
    agent.respond("add a greeting function to the project")
    offline = [d for d in seen if "next_model" not in d or d.get("next_model") == "offline"]
    assert agent._offline_fallback_used is True
    assert any(d.get("reason") for d in seen), "the reason for going offline must be stated"


def test_provider_fallback_is_announced_with_its_cause(tmp_settings):
    agent, seen = _agent(tmp_settings, AlternatingProvider(), catalog=_catalog())
    agent.respond("add a greeting function to the project")
    fallbacks = [d for d in seen if d.get("next_model") and d["next_model"] != "offline"]
    assert fallbacks, "a model change must be observable"
    assert fallbacks[0]["category"] == FailureCategory.TIMEOUT
    assert agent._offline_fallback_used is False


def test_both_failures_are_recorded_in_order(tmp_settings):
    class AlwaysFails:
        model_label = "bad"
        name = "bad"

        def is_available(self):
            return True

        def chat(self, messages, tools=None, model=None):
            raise AIProviderError("nope", category=FailureCategory.TIMEOUT, model=model)

    agent, _ = _agent(tmp_settings, AlwaysFails(), catalog=_catalog())
    agent.respond("add a greeting function to the project")
    failures = agent.provider_failures
    assert len(failures) == 2
    # Primary first, then the model it fell back to.
    assert failures[0]["next_model"] == failures[1]["model"]
    assert failures[1]["next_model"] == "offline"


def test_unknown_failures_are_still_categorised(tmp_settings):
    agent, _ = _agent(tmp_settings, BrokenProvider(exc=RuntimeError("kaboom")))
    agent.respond("add a greeting function to the project")
    failures = agent.provider_failures
    assert failures[0]["category"] == FailureCategory.UNKNOWN
    assert failures[0]["exception"] == "RuntimeError"
    assert "kaboom" in failures[0]["message"]


def test_no_failures_recorded_when_the_model_works(tmp_settings):
    class Ok:
        model_label = "ok"
        name = "ok"

        def is_available(self):
            return True

        def chat(self, messages, tools=None, model=None):
            return SimpleNamespace(text="fine", tool_calls=[])

    agent, seen = _agent(tmp_settings, Ok())
    agent.respond("add a greeting function to the project")
    assert agent.provider_failures == []
    assert seen == []


def test_model_info_shape_is_unchanged():
    # The richer ModelInfo from M2.12 must not regress.
    info = ModelInfo(name="m", size_bytes=2 * 1024 ** 3)
    assert info.human_size == "2.0GB"
    assert info.capabilities == ()
