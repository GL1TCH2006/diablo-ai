from __future__ import annotations

import re
from pathlib import Path

from core.constants import GREETING

from .provider import AIProvider, AIResponse, ModelInfo, ToolCall


class OfflineProvider(AIProvider):
    """Rule-based fallback used ONLY when no AI backend can be reached.

    It is deliberately limited and always tells the user it is not a real
    model. It supports a few deterministic tool workflows so Diablo stays
    usable for smoke tests and emergencies.
    """

    name = "offline"
    available = True
    model_label = "offline/rule-based (limited mode)"
    reason = "No AI backend available."

    OPEN_RE = re.compile(r"^(?:open|launch|start)\s+(.+)$", re.I)
    HELLO_PY_RE = re.compile(r"(?:create|make).+\bpython\b.+?hello", re.I)
    SYSINFO_RE = re.compile(r"(?:cpu(?: usage)?|memory|ram|system\s*info(?:rmation)?)", re.I)

    def __init__(self, scratch_dir: Path | None = None):
        self._scratch_dir = scratch_dir or Path("workspace")

    def is_available(self) -> bool:
        return True

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name="offline/rule-based", parameter_size="0", context_length=0, capabilities=("tools",))]

    def chat(self, messages: list[dict], tools: list | None = None, model: str | None = None) -> AIResponse:
        last = messages[-1] if messages else {}
        user_text = self._last_user_text(messages)

        if last.get("role") == "tool":
            if last.get("content") == "":
                user_text = ""
            return self._react_to_tool_result(last.get("content", ""))

        text = (user_text or "").strip()
        if re.fullmatch(r"[\s,]*(diablo[,\s]*)?(hello|hi|hey)[\s!.]*", text, re.I):
            return AIResponse(text=f"{GREETING} (offline limited mode: no AI model available.)", model=self.model_label)

        match = self.OPEN_RE.match(text)
        if match:
            app = match.group(1).strip().strip("\"'`.,!?")
            return AIResponse(text="", tool_calls=[ToolCall(name="applications.open", arguments={"app": app})], model=self.model_label)

        if self.HELLO_PY_RE.search(text):
            target = self._scratch_dir / "hello_world.py"
            return AIResponse(
                text="",
                tool_calls=[
                    ToolCall(name="filesystem.write_file", arguments={"path": str(target), "content": 'print("Hello World")\n'}),
                    ToolCall(name="python.run_script", arguments={"script": str(target)}),
                ],
                model=self.model_label,
            )

        if self.SYSINFO_RE.search(text):
            return AIResponse(text="", tool_calls=[ToolCall(name="system.info", arguments={"components": ["cpu", "memory", "os"]})], model=self.model_label)

        return AIResponse(
            text="I am running in limited offline mode because no AI model is reachable. Start Ollama to unlock full conversation and coding tasks.",
            model=self.model_label,
        )

    def _react_to_tool_result(self, content: str) -> AIResponse:
        tool, _, remainder = content.partition("]")
        tool = tool.lstrip("[").strip()

        if tool == "python.run_script":
            if re.search(r"Hello World", remainder, re.I):
                return AIResponse(text="Python script executed and verified. Output: Hello World", model=self.model_label)
            return AIResponse(text=f"Python script ran but output could not be verified: {remainder.strip()[:200]}", model=self.model_label)
        if tool == "applications.open":
            return AIResponse(text=f"Opened {remainder.strip()[:120]}", model=self.model_label)
        if tool == "system.info":
            return AIResponse(text=f"System check: {remainder.strip()[:200]}", model=self.model_label)
        return AIResponse(text=f"Tool {tool} finished: {remainder.strip()[:200]}", model=self.model_label)

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg.get("content", "")
        return ""