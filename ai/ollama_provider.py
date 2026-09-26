from __future__ import annotations

import json
import time

import httpx

from .provider import AIProvider, AIProviderError, AIResponse, FailureCategory, ModelInfo, ToolCall

#: Generous by design. A cold 30B model on a loaded machine can legitimately
#: think for minutes, and cutting a slow model off early destroys work it was
#: about to finish. It is configurable per provider instance; the default is
#: unchanged.
DEFAULT_TOOL_TIMEOUT = 600.0
_TOOL_TIMEOUT = DEFAULT_TOOL_TIMEOUT
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

    def __init__(self, host: str = "http://localhost:11434", timeout: float | None = None):
        self.host = host.rstrip("/")
        self.model_label = f"ollama@{self.host}"
        #: ``None`` means "use the module default", so an unset setting cannot
        #: accidentally mean "no timeout".
        self.timeout = DEFAULT_TOOL_TIMEOUT if timeout is None else max(1.0, float(timeout))

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
            raise AIProviderError(
                "No Ollama model selected.", category=FailureCategory.NO_MODEL, retryable=False
            )
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

        started = time.perf_counter()
        try:
            response = self._post(payload, model=model, started=started)
        except AIProviderError as exc:
            # Fill in the model and latency if the inner layer did not.
            if not exc.model:
                exc.model = str(model or "")
            if not exc.latency:
                exc.latency = time.perf_counter() - started
            raise
        except httpx.TimeoutException as exc:
            raise AIProviderError(
                f"Ollama timed out after {self.timeout}s.",
                category=FailureCategory.TIMEOUT,
                model=model,
                latency=time.perf_counter() - started,
            ) from exc
        except Exception as exc:
            raise AIProviderError(
                f"Ollama request failed: {exc}",
                category=FailureCategory.CONNECTION,
                model=model,
                latency=time.perf_counter() - started,
            ) from exc

        data = response
        if isinstance(data, dict) and data.get("error"):
            raise AIProviderError(
                str(data["error"]),
                category=FailureCategory.HTTP,
                model=model,
                latency=time.perf_counter() - started,
            )

        message = (data or {}).get("message", {}) or {}
        content = message.get("content") or ""
        calls = []
        for slot, call in enumerate(message.get("tool_calls", []) or []):
            function = call.get("function", {}) or {}
            calls.append(
                ToolCall(
                    id=f"call_{slot}",
                    name=function.get("name", ""),
                    arguments=_parse_args(function.get("arguments", "{}"), model=model),
                )
            )
        return AIResponse(text=content, tool_calls=calls, model=model)

    def _post(self, payload: dict, model: str = "", started: float = 0.0) -> dict:
        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else ""
            status = exc.response.status_code if exc.response is not None else 0
            raise AIProviderError(
                f"Ollama HTTP {status}: {detail}",
                category=FailureCategory.HTTP,
                model=model,
                latency=(time.perf_counter() - started) if started else 0.0,
                # 5xx is transient; 4xx means this request will never be accepted.
                retryable=500 <= status < 600,
                status=status,
            ) from exc
        except httpx.TimeoutException as exc:
            raise AIProviderError(
                f"Ollama timed out after {self.timeout}s.",
                category=FailureCategory.TIMEOUT,
                model=model,
                latency=(time.perf_counter() - started) if started else 0.0,
            ) from exc


def _parse_args(raw, model: str = "") -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise AIProviderError(
            "Ollama returned malformed tool arguments.",
            category=FailureCategory.MALFORMED,
            model=model,
            # Deliberately not retryable: the same model will malform the same
            # way, so this is a capability signal, not a transient fault.
            retryable=False,
        ) from exc
    if not isinstance(parsed, dict):
        raise AIProviderError(
            "Ollama returned non-object tool arguments.",
            category=FailureCategory.MALFORMED,
            model=model,
            retryable=False,
        )
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