from __future__ import annotations

from pathlib import Path

from .storage import load_json, save_json


class PreferencesStore:
    def __init__(self, memory_dir: Path):
        self._file = memory_dir / "preferences.json"
        self._data = load_json(self._file, {})

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self._save()

    def set_permission(self, tool: str, rule: str) -> None:
        perms = self._data.setdefault("permissions", {})
        perms[tool] = rule
        self._save()

    def get_permission(self, tool: str) -> str | None:
        return self._data.get("permissions", {}).get(tool)

    def clear_permission(self, tool: str) -> None:
        self._data.get("permissions", {}).pop(tool, None)
        self._save()

    def all(self) -> dict:
        return self._data

    def clear(self) -> None:
        self._data = {}
        self._save()

    def _save(self) -> None:
        save_json(self._file, self._data)

    @property
    def path(self) -> Path:
        return self._file