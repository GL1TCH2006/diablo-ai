from __future__ import annotations

import json

import httpx

from .provider import AIProvider, AIProviderError, AIResponse, ModelInfo, ToolCall

_TOOL_TIMEOUT = 600.0
_LIST_TIMEOUT = 5.0


def to_ollama_schema(spec) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters or {"type": "object", "properties": {}, "required": []},
        },
    }


class OllamaProvider(AIProvider):
    name = "ollama"
    available = False
    reason = ""

    def __init__(self, host: str = "http://localhost:11434"):
        self.host = host.rstrip("/")
        self.model_label = f"ollama@{self.host}"

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.host}/api/tags", timeout=_LIST_TIMEOUT)
            resp.raise_for_status()
            self.available = True
            self.reason = ""
            return True
        except Exception as exc:
            self.available = False
            self.reason = str(exc)
            return False

    def list_models(self) -> list[ModelInfo]:
        resp = httpx.get(f"{self.host}/api/tags", timeout=_LIST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        models = []
        for item in data.get("models", []):
            details = item.get("details", {}) or {}
            models.append(
                ModelInfo(
                    name=item.get("name", ""),
                    size_bytes=int(item.get("size", 0) or 0),
                    parameter_size=details.get("parameter_size", ""),
                    context_length=int(details.get("context_length", item.get("context_length", 0)) or 0),
                    capabilities=tuple(str(value) for value in item.get("capabilities", []) or []),
                    family=details.get("family", ""),
                    digest=item.get("digest", ""),
                )
            )
        return models

    def chat(self, messages: list[dict], tools: list | None = None, model: str | None = None) -> AIResponse:
        if not model:
            raise AIProviderError("No Ollama model selected.")
        prepared = _drop_tool_calls_holes(messages)
        for message in prepared:
            if message.get("role") in ("system", "user"):
                message["think"] = False
        payload = {
            "model": model,
            "messages": prepared,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        if tools:
            payload["tools"] = [to_ollama_schema(t) for t in tools]

        try:
            response = self._post(payload)
        except AIProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise AIProviderError(f"Ollama timed out after {_TOOL_TIMEOUT}s.") from exc
        except Exception as exc:
            raise AIProviderError(f"Ollama request failed: {exc}") from exc

        data = response
        if isinstance(data, dict) and data.get("error"):
            raise AIProviderError(str(data["error"]))

        message = (data or {}).get("message", {}) or {}
        content = message.get("content") or ""
        calls = []
        for slot, call in enumerate(message.get("tool_calls", []) or []):
            function = call.get("function", {}) or {}
            calls.append(
                ToolCall(
                    id=f"call_{slot}",
                    name=function.get("name", ""),
                    arguments=_parse_args(function.get("arguments", "{}")),
                )
            )
        return AIResponse(text=content, tool_calls=calls, model=model)

    def _post(self, payload: dict) -> dict:
        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=_TOOL_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else ""
            raise AIProviderError(f"Ollama HTTP {exc.response.status_code}: {detail}") from exc


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise AIProviderError("Ollama returned malformed tool arguments.") from exc
    if not isinstance(parsed, dict):
        raise AIProviderError("Ollama returned non-object tool arguments.")
    return parsed


def _drop_tool_calls_holes(messages: list[dict]) -> list[dict]:
    """Normalise assistant tool-call messages for the Ollama API."""
    cleaned = []
    for msg in messages:
        entry = dict(msg)
        if entry.get("role") == "assistant":
            entry.setdefault("tool_calls", None)
        cleaned.append(entry)
    return cleaned