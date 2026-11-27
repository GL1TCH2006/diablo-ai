from __future__ import annotations

from dataclasses import dataclass, field

from ai.tokenizer import est_tokens, head_tail
from core.constants import APP_NAME, APP_VERSION
from core.logging_setup import get_logger

log = get_logger("context")

_MISSING = object()


@dataclass
class ContextSection:
    title: str
    body: str = ""
    enabled: bool = True

    def render(self) -> str:
        if not self.enabled or not self.body.strip():
            return ""
        return f"\n===== {self.title} =====\n{self.body.rstrip()}\n"


class ContextBuilder:
    """Token-budgeted prompt assembly for the coding agent.

    Sections: persona (fixed), project, mission, rules, relevant-files,
    conversation tail, tool-result, error, custom. Weights sum <= 1.0.
    """

    DEFAULT_WEIGHTS = {
        "persona": 0.08,
        "project": 0.15,
        "mission": 0.15,
        "rules": 0.08,
        "files": 0.25,
        "conversation": 0.12,
        "tool_result": 0.07,
        "error": 0.10,
        "custom": 0.00,
    }

    def __init__(self, max_tokens: int = 8000, weights: dict[str, float] | None = None):
        self.max_tokens = max_tokens
        self.weights = weights or dict(self.DEFAULT_WEIGHTS)
        self._sections: list[ContextSection] = []
        self._used: dict[str, int] = {}

    def add(self, title: str, body: str, weight: str = "custom", enabled: bool = True) -> "ContextBuilder":
        self._sections.append(ContextSection(title=title, body=body, enabled=enabled))
        self._used.setdefault(weight, 0)
        return self

    def build(self, project_label: str = "") -> str:
        persona = (
            f"You are {APP_NAME}, a personal AI assistant and development agent for a Windows computer, version {APP_VERSION}.\n"
            "PERSONALITY\n- Intelligent, concise, direct, technical when needed. Never verbose.\n"
            "- Never claim something worked unless a tool/verifier confirmed it. Base answers on actual results.\n\n"
            "CODING AGENT\n- Analyse before you edit: inspect the project profile and relevant files first.\n"
            "- Prefer targeted patches over whole-file rewrites.\n"
            "- Run tests after modifying code when a test command is known, then verify real results.\n"
            "- Never report success merely because a command exited 0.\n"
        )
        if project_label:
            persona += f"Current project: {project_label}\n"
        self._sections.insert(0, ContextSection(title="DIABLO", body=persona.rstrip(), enabled=True))

        out = []
        used_tokens = 0
        for section in self._sections:
            rendered = section.render()
            if not rendered:
                continue
            budget = int(self.max_tokens * self.weights.get(section.title.lower().replace(" ", "_"), 0.12))
            if section.title != "DIABLO" and used_tokens + est_tokens(rendered) > self.max_tokens - 200:
                out.append(f"\n===== {section.title} (trimmed for budget) =====\n(body omitted to fit context budget)")
                continue
            out.append(rendered)
            used_tokens += est_tokens(rendered)
            log.debug("context section %s ~%d tokens", section.title, est_tokens(rendered))
        return "\n".join(out)

    @staticmethod
    def trim_tool_result(output: str, max_chars: int = 8000) -> str:
        return head_tail(output or "", max_chars, tail_chars=500)

    @staticmethod
    def format_project(profile_text: str, files_summary: str = "") -> str:
        parts = [profile_text]
        if files_summary:
            parts.append(f"Structure:\n{files_summary}")
        return "\n\n".join(parts)