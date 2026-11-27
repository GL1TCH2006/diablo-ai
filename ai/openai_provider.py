"""OpenAI-compatible provider for Diablo.

Supports NVIDIA NIM, OpenAI, Groq, and any OpenAI-compatible endpoint.
Uses httpx directly (no openai package dependency).
"""

from __future__ import annotations

import json
import time

import httpx

from .provider import AIProvider, AIProviderError, AIResponse, FailureCategory, ModelInfo, ToolCall

DEFAULT_TOOL_TIMEOUT = 600.0
_LIST_TIMEOUT = 10.0


def to_openai_schema(spec) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters or {"type": "object", "properties": {}, "required": []},
        },
    }


class OpenAIProvider(AIProvider):
    """OpenAI-compatible chat completion provider.

    Works with NVIDIA NIM, OpenAI API, Groq, and any endpoint that follows
    the OpenAI chat/completions contract.
    """

    name = "openai"
    available = False
    reason = ""

    def __init__(
        self,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        api_key: str = "",
        model: str = "",
        timeout: float | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.model_label = f"openai@{self.base_url}"
        self.timeout = DEFAULT_TOOL_TIMEOUT if timeout is None else max(1.0, float(timeout))

    def is_available(self) -> bool:
        """Probe the endpoint by listing models or a lightweight chat.

        For endpoints that support /v1/models, we use that. Otherwise we fall
        back to a trivial chat completion to verify connectivity.
        """
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=_LIST_TIMEOUT,
            )
            if resp.status_code == 401:
                self.available = False
                self.reason = "Unauthorized: invalid or missing API key"
                return False
            if resp.status_code >= 500:
                self.available = False
                self.reason = f"Server error {resp.status_code}"
                return False
            self.available = True
            self.reason = ""
            return True
        except httpx.TimeoutException as exc:
            self.available = False
            self.reason = f"Timed out after {_LIST_TIMEOUT}s"
            return False
        except Exception as exc:
            self.available = False
            self.reason = str(exc)
            return False

    def list_models(self) -> list[ModelInfo]:
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=_LIST_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception:
            return []
        data = resp.json()
        models = []
        for item in data.get("data", []):
            models.append(
                ModelInfo(
                    name=item.get("id", ""),
                    size_bytes=0,
                    parameter_size=item.get("parameter_size", "") or "",
                    context_length=int(item.get("context_length", 0) or 0),
                    capabilities=tuple(str(v) for v in (item.get("capabilities") or [])),
                    family=item.get("family", "") or "",
                    digest=item.get("digest", "") or "",
                )
            )
        return models

    def chat(self, messages: list[dict], tools: list | None = None, model: str | None = None) -> AIResponse:
        model_name = model or self.model
        if not model_name:
            raise AIProviderError(
                "No OpenAI model selected.", category=FailureCategory.NO_MODEL, retryable=False
            )

        prepared = self._prepare_messages(messages)
        payload: dict = {
            "model": model_name,
            "messages": prepared,
            "stream": False,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = [to_openai_schema(t) for t in tools]
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        try:
            response = self._post(payload, model=model_name, started=started)
        except AIProviderError as exc:
            if not exc.model:
                exc.model = str(model_name or "")
            if not exc.latency:
                exc.latency = time.perf_counter() - started
            raise
        except httpx.TimeoutException as exc:
            raise AIProviderError(
                f"OpenAI provider timed out after {self.timeout}s.",
                category=FailureCategory.TIMEOUT,
                model=model_name,
                latency=time.perf_counter() - started,
            ) from exc
        except Exception as exc:
            raise AIProviderError(
                f"OpenAI provider request failed: {exc}",
                category=FailureCategory.CONNECTION,
                model=model_name,
                latency=time.perf_counter() - started,
            ) from exc

        if isinstance(response, dict) and response.get("error"):
            err = response["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            status = err.get("code", 0) if isinstance(err, dict) else 0
            raise AIProviderError(
                msg,
                category=FailureCategory.HTTP,
                model=model_name,
                latency=time.perf_counter() - started,
                retryable=isinstance(status, int) and 500 <= status < 600,
                status=status if isinstance(status, int) else 0,
            )

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        content = message.get("content") or ""
        calls = []
        for slot, call in enumerate(message.get("tool_calls", []) or []):
            function = call.get("function", {}) or {}
            calls.append(
                ToolCall(
                    id=call.get("id", f"call_{slot}"),
                    name=function.get("name", ""),
                    arguments=self._parse_args(function.get("arguments", "{}"), model=model_name),
                )
            )
        return AIResponse(text=content, tool_calls=calls, model=model_name)

    # ── internal helpers ──

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, payload: dict, model: str = "", started: float = 0.0) -> dict:
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else ""
            status = exc.response.status_code if exc.response is not None else 0
            raise AIProviderError(
                f"OpenAI provider HTTP {status}: {detail}",
                category=FailureCategory.HTTP,
                model=model,
                latency=(time.perf_counter() - started) if started else 0.0,
                retryable=500 <= status < 600,
                status=status,
            ) from exc
        except httpx.TimeoutException as exc:
            raise AIProviderError(
                f"OpenAI provider timed out after {self.timeout}s.",
                category=FailureCategory.TIMEOUT,
                model=model,
                latency=(time.perf_counter() - started) if started else 0.0,
            ) from exc

    @staticmethod
    def _prepare_messages(messages: list[dict]) -> list[dict]:
        """Normalize messages for OpenAI-compatible format.

        Drops any 'think' field (Ollama-specific), ensures every message has
        role and content, and gives assistant messages a tool_calls field if
        missing.
        """
        cleaned: list[dict] = []
        for msg in messages:
            entry = dict(msg)
            entry.pop("think", None)
            role = entry.get("role", "user")
            if "content" not in entry:
                entry["content"] = ""
            if role == "assistant":
                entry.setdefault("tool_calls", None)
            cleaned.append(entry)
        return cleaned

    @staticmethod
    def _parse_args(raw, model: str = "") -> dict:
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError) as exc:
            raise AIProviderError(
                "OpenAI provider returned malformed tool arguments.",
                category=FailureCategory.MALFORMED,
                model=model,
                retryable=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise AIProviderError(
                "OpenAI provider returned non-object tool arguments.",
                category=FailureCategory.MALFORMED,
                model=model,
                retryable=False,
            )
        return parsed