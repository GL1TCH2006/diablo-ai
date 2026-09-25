from __future__ import annotations

import time
from pathlib import Path

from core.logging_setup import get_logger

from .storage import load_json, save_json

log = get_logger("memory")

_MAX_TURNS_DEFAULT = 20


class ConversationStore:
    def __init__(self, memory_dir: Path, max_turns: int = _MAX_TURNS_DEFAULT):
        self._file = memory_dir / "conversation.json"
        self._max_turns = max_turns

    def _load(self) -> list[dict]:
        data = load_json(self._file, [])
        return data if isinstance(data, list) else []

    def append_user(self, text: str) -> None:
        self._append("user", text)

    def append_assistant(self, text: str) -> None:
        self._append("assistant", text)

    def _append(self, role: str, text: str) -> None:
        entries = self._load()
        if len(entries) > 0 and entries[-1].get("role") == role and role == "user":
            entries[-1]["content"] = text
        else:
            entries.append({"role": role, "content": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if len(entries) > self._max_turns * 2:
            entries = entries[-self._max_turns * 2:]
        self._save(entries)

    def recent(self, n: int) -> list[dict]:
        entries = self._load()
        if len(entries) <= n * 2:
            return entries
        return entries[-n * 2:]

    def clear(self) -> None:
        self._save([])

    def _save(self, entries: list[dict]) -> None:
        save_json(self._file, entries)

    @property
    def path(self) -> Path:
        return self._file