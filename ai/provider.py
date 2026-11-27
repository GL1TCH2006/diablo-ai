from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class FailureCategory:
    """Why a provider call failed, in terms routing can act on.

    Plain string constants rather than an enum so they stay trivially
    serialisable into events, logs and JSON state.
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP = "http"
    MALFORMED = "malformed"
    NO_MODEL = "no_model"
    UNKNOWN = "unknown"

    ALL = (TIMEOUT, CONNECTION, HTTP, MALFORMED, NO_MODEL, UNKNOWN)


#: Categories where trying again - on the same model after a pause, or on a
#: different one - can plausibly succeed. A malformed response is excluded: the
#: same model given the same request will malform the same way, and retrying it
#: only hides a capability gap that routing should see.
RETRYABLE = frozenset({FailureCategory.TIMEOUT, FailureCategory.CONNECTION, FailureCategory.HTTP})


class AIProviderError(Exception):
    """A provider failure that carries enough structure to be routed around.

    M2.13.0. Previously this was a bare ``Exception`` subclass whose only
    payload was a message string, so the agent could not distinguish a timeout
    (try something smaller) from a malformed response (try a different model)
    from a missing model (configuration is wrong). The message is still the
    first positional argument and ``str(error)`` is unchanged, so existing
    handling and tests keep working.
    """

    def __init__(
        self,
        message: str = "",
        *,
        category: str = FailureCategory.UNKNOWN,
        model: str = "",
        latency: float = 0.0,
        retryable: bool | None = None,
        status: int = 0,
    ):
        super().__init__(message)
        self.message = str(message or "")
        self.category = str(category or FailureCategory.UNKNOWN)
        self.model = str(model or "")
        self.latency = float(latency or 0.0)
        self.status = int(status or 0)
        self.retryable = (
            self._default_retryable() if retryable is None else bool(retryable)
        )

    def _default_retryable(self) -> bool:
        """Infer retryability from the failure itself.

        For HTTP the status decides: 5xx is transient, 4xx means this request
        will never be accepted. Honouring the status here rather than only at
        the raise site means any code path that constructs an error without
        spelling out ``retryable`` still gets the right answer.
        """
        if self.category == FailureCategory.HTTP and self.status:
            return 500 <= self.status < 600
        return self.category in RETRYABLE

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "model": self.model,
            "latency": round(self.latency, 3),
            "retryable": self.retryable,
            "status": self.status,
            "message": self.message[:300],
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AIProviderError(category={self.category!r}, model={self.model!r}, "
            f"latency={self.latency:.2f}, retryable={self.retryable}): {self.message}"
        )


@dataclass
class ModelInfo:
    name: str
    size_bytes: int = 0
    parameter_size: str = ""
    context_length: int = 0
    capabilities: tuple[str, ...] = ()
    family: str = ""
    digest: str = ""

    @property
    def human_size(self) -> str:
        if self.size_bytes <= 0:
            return "?"
        return f"{self.size_bytes / 1024 ** 3:.1f}GB"


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)
    id: str = ""


@dataclass
class AIResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""


class AIProvider(ABC):
    """Abstraction over any AI backend.

    Phases 1+ only implement Ollama and an honest offline fallback.
    New providers implement chat() and plug in without touching the agent.
    """

    name = "abstract"
    model_label = "unknown"
    available = False
    reason = ""

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    def list_models(self) -> list[ModelInfo]:
        return []

    @abstractmethod
    def chat(self, messages: list[dict], tools: list | None = None, model: str | None = None) -> AIResponse:
        raise NotImplementedError