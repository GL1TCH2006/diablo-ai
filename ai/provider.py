from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AIProviderError(Exception):
    pass


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