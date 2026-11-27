from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import urlparse

from config.settings import Settings
from core.logging_setup import get_logger

from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider
from .provider import AIProvider, AIProviderError, ModelInfo

log = get_logger("model_manager")


@dataclass
class BackendResult:
    provider: AIProvider
    model_name: str
    note: str
    models: list[ModelInfo]


def build_backend(settings: Settings, force_offline: bool = False) -> BackendResult:
    provider_name = (settings.ai_provider or "ollama").strip().lower()
    if force_offline or provider_name == "offline":
        from .offline_provider import OfflineProvider
        reason = "forced offline mode" if force_offline else "configured offline provider"
        return BackendResult(
            provider=OfflineProvider(scratch_dir=settings.scratch_dir),
            model_name="offline/rule-based",
            note=f"Running in {reason}.",
            models=[ModelInfo("offline/rule-based", capabilities=("tools",))],
        )
    if provider_name != "ollama":
        if provider_name == "openai":
            return _build_openai_backend(settings)
        return _offline_fallback(settings, f"Unsupported AI provider '{settings.ai_provider}'. No cloud provider is enabled.")
    if not getattr(settings, "allow_remote_ollama", False) and not _is_local_ollama_host(settings.ollama_host):
        return _offline_fallback(settings, "Remote Ollama hosts are disabled. Set allow_remote_ollama=true to opt in.")

    try:
        # Only pass an explicit timeout when one was actually configured, so a
        # substituted provider that accepts just `host` keeps working and the
        # historical call shape is preserved.
        from .ollama_provider import DEFAULT_TOOL_TIMEOUT

        timeout = float(getattr(settings, "ai_request_timeout", DEFAULT_TOOL_TIMEOUT) or 0)
        if timeout <= 0 or timeout == DEFAULT_TOOL_TIMEOUT:
            provider = OllamaProvider(host=settings.ollama_host)
        else:
            provider = OllamaProvider(host=settings.ollama_host, timeout=timeout)
        if not provider.is_available():
            note = f"Ollama is not reachable at {settings.ollama_host}: {provider.reason}"
            return _offline_fallback(settings, note)
        discovered_models = provider.list_models()
        if not discovered_models:
            return _offline_fallback(settings, "Ollama is online but no models are installed (run `ollama pull` first).")
        models = [model for model in discovered_models if not model.capabilities or "tools" in model.capabilities]
        if not models:
            return _offline_fallback(settings, "Ollama has no installed tool-capable model.")
        model_name, resolution_note = _resolve_model(settings, models)
        count = len(models)
        available_note = f"-> Ollama online: {count} model(s) available. {resolution_note}"
        return BackendResult(provider=provider, model_name=model_name, note=available_note, models=models)
    except Exception as exc:
        log.warning("AI backend init failed: %s", exc, exc_info=True)
        return _offline_fallback(settings, f"AI backend unavailable: {exc}")


def _resolve_model(settings: Settings, models: list[ModelInfo]) -> Tuple[str, str]:
    names = [m.name for m in models]

    if settings.ai_model:
        if settings.ai_model in names:
            return settings.ai_model, "Using configured model."
        note = f"Configured model '{settings.ai_model}' is not installed."

    pattern = ".".join(settings.ai_model.split(":")[:1]) if settings.ai_model else None
    if pattern:
        expanded = [n for n in names if n.split(":")[0] == pattern]
        if expanded:
            return expanded[0], note + f" Using '{expanded[0]}' instead."

    for preferred in settings.preferred_models:
        if preferred in names:
            return preferred, (note + " Selected from default preference list." if settings.ai_model else "Selected first preferred model.")
        base = preferred.split(":")[0]
        expanded = [n for n in names if n.split(":")[0] == base]
        if expanded:
            return expanded[0], (note + f" Using '{expanded[0]}' (preferred '{preferred}' not exact).")

    first = names[0]
    return first, note + f" Using first installed model '{first}'."


def _is_local_ollama_host(host: str) -> bool:
    value = str(host or "").strip()
    parsed = urlparse(value if "://" in value else f"http://{value}")
    hostname = (parsed.hostname or "").strip().lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _offline_fallback(settings: Settings, note: str) -> BackendResult:
    from .offline_provider import OfflineProvider
    return BackendResult(
        provider=OfflineProvider(scratch_dir=settings.scratch_dir),
        model_name="offline/rule-based",
        note=note,
        models=[],
    )


def _build_openai_backend(settings: Settings) -> BackendResult:
    """Build an OpenAI-compatible provider backend."""
    base_url = getattr(settings, "openai_base_url", "") or "https://integrate.api.nvidia.com/v1"
    api_key = getattr(settings, "openai_api_key", "") or ""
    model = getattr(settings, "openai_model", "") or ""
    timeout = float(getattr(settings, "ai_request_timeout", 0) or 0)

    from .openai_provider import DEFAULT_TOOL_TIMEOUT

    timeout_val = timeout if timeout > 0 else DEFAULT_TOOL_TIMEOUT
    provider = OpenAIProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout_val,
    )
    if not provider.is_available():
        note = f"OpenAI provider not reachable at {base_url}: {provider.reason}"
        return _offline_fallback(settings, note)

    discovered_models = provider.list_models()
    if model and model in [m.name for m in discovered_models]:
        model_name = model
        note = f"-> OpenAI online: model '{model}' configured."
    elif discovered_models:
        model_name = discovered_models[0].name
        note = f"-> OpenAI online: {len(discovered_models)} model(s) available. Using '{model_name}'."
    else:
        # No models listed but endpoint is reachable; use the configured model name.
        model_name = model or "openai/model"
        note = "-> OpenAI online: no models listed; using configured model."

    available_note = f"{note}"
    return BackendResult(
        provider=provider,
        model_name=model_name,
        note=available_note,
        models=discovered_models,
    )