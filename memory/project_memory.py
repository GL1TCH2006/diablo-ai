from __future__ import annotations

from pathlib import Path

from coding.code_analyzer import ProjectSnapshot, snapshot_project
from core.logging_setup import get_logger

from .storage import load_json, save_json

log = get_logger("memory")


class ProjectMemory:
    """Project awareness: active project plus a registry discovered from the workspace root."""

    DEFAULT_SECTIONS = ("architecture", "technologies", "decisions", "conventions",
                        "known_issues", "important_files", "development_history")

    def __init__(self, memory_dir: Path, workspace_root: Path, task_history=None):
        self._file = memory_dir / "projects.json"
        self._workspace_root = workspace_root
        self._data = load_json(self._file, {"active": "", "projects": {}})
        self._task_history = task_history

    def discover(self) -> list[ProjectSnapshot]:
        if not self._workspace_root.is_dir():
            return []
        snapshots = []
        for entry in sorted(self._workspace_root.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                snap = snapshot_project(entry)
                if snap is not None:
                    snapshots.append(snap)
                    self._data.setdefault("projects", {})[entry.name] = {
                        "path": str(entry.resolve()),
                        "language": snap.language,
                        "markers": snap.markers,
                        "git": snap.git_repo,
                    }
        self._save()
        return snapshots

    def set_active(self, name: str) -> None:
        self._data["active"] = name
        self._save()

    def sections(self, project_name: str) -> dict[str, str]:
        project = self._data.setdefault("projects", {}).setdefault(project_name, {})
        raw = project.get("sections") or {}
        return {name: str(raw.get(name, "")) for name in self.DEFAULT_SECTIONS}

    def get_section(self, project_name: str, name: str) -> str:
        return self.sections(project_name).get(name, "")

    def set_section(self, project_name: str, name: str, text: str) -> None:
        if name not in self.DEFAULT_SECTIONS:
            raise ValueError(f"unknown project section: {name}")
        project = self._data.setdefault("projects", {}).setdefault(project_name, {})
        project.setdefault("sections", {})[name] = _sanitize_text(text, 8000)
        self._save()

    def set_preferred_model(self, project_name: str, model: str) -> None:
        project = self._data.setdefault("projects", {}).setdefault(project_name, {})
        project["preferred_model"] = _sanitize_text(model, 120)
        self._save()

    def get_preferred_model(self, project_name: str) -> str:
        return str(self._data.get("projects", {}).get(project_name, {}).get("preferred_model", ""))

    def set_profile_summary(self, project_name: str, summary: str) -> None:
        project = self._data.setdefault("projects", {}).setdefault(project_name, {})
        project["profile_summary"] = _sanitize_text(summary, 20000)
        self._save()

    def add_history_entry(self, project_name: str, entry: str) -> None:
        project = self._data.setdefault("projects", {}).setdefault(project_name, {})
        history = project.setdefault("sections", {}).setdefault("development_history", "")
        new_entry = f"[{__import__('time').strftime('%Y-%m-%d')}] {_sanitize_text(entry, 400)}"
        history = new_entry if not history else f"{history}\n{new_entry}"
        project["sections"]["development_history"] = history[-12000:]
        self._save()

    def render_context(self, project_name: str, max_chars: int = 1500) -> str:
        profile = self._data.get("projects", {}).get(project_name, {})
        parts = []
        profile_summary = profile.get("profile_summary")
        if profile_summary:
            parts.append(f"Profile: {profile_summary[:max_chars]}")
        for name, text in self.sections(project_name).items():
            if text.strip():
                parts.append(f"[{name}] {text.strip()[:max_chars]}")
        return "\n\n".join(parts)[:max_chars * 3]

    def attach_task_history(self, task_history) -> None:
        self._task_history = task_history

    def get_task_history(self, project_name: str, limit: int = 20) -> list[dict]:
        return self._task_history_call(
            "list",
            project_name,
            self._project_root(project_name),
            limit=limit,
            default=[],
        )

    def get_recent_tasks(self, project_name: str, limit: int = 5) -> list[dict]:
        return self.get_task_history(project_name, limit=limit)

    def get_task_summary(
        self,
        project_name: str,
        task_id: str,
    ) -> dict | None:
        return self._task_history_call(
            "get",
            project_name,
            self._project_root(project_name),
            task_id,
            default=None,
        )

    def get_current_task(self, project_name: str) -> dict | None:
        return self._task_history_call(
            "current",
            project_name,
            self._project_root(project_name),
            default=None,
        )

    def get_most_recent_completed_task(self, project_name: str) -> dict | None:
        return self._task_history_call(
            "most_recent_completed",
            project_name,
            self._project_root(project_name),
            default=None,
        )

    def get_failed_tasks(self, project_name: str, limit: int = 20) -> list[dict]:
        return self._task_history_call(
            "failed",
            project_name,
            self._project_root(project_name),
            limit=limit,
            default=[],
        )

    def find_tasks_by_change(
        self,
        project_name: str,
        file_path: str = "",
        commit_hash: str = "",
    ) -> list[dict]:
        return self._task_history_call(
            "find_by_change",
            project_name,
            self._project_root(project_name),
            file_path=file_path,
            commit_hash=commit_hash,
            default=[],
        )

    def render_task_history_context(
        self,
        project_name: str,
        max_chars: int = 1200,
        limit: int = 5,
    ) -> str:
        if max_chars <= 0:
            return ""
        tasks = self.get_recent_tasks(project_name, limit=limit)
        if not tasks:
            return ""
        lines = ["Recent task history:"]
        for task in tasks:
            title = _one_line(task.get("title") or task.get("goal") or task.get("task_id", ""), 120)
            status = str(task.get("status") or "unknown")
            outcome = _one_line(task.get("outcome") or "", 180)
            timestamp = str(task.get("finished_at") or task.get("started_at") or task.get("created_at") or "")
            commit = str((task.get("git") or {}).get("ending_commit") or "")[:12]
            details = [status]
            if timestamp:
                details.append(timestamp)
            if commit:
                details.append(f"commit {commit}")
            line = f"- {title} ({', '.join(details)})"
            if outcome:
                line += f": {outcome}"
            lines.append(line)
        return "\n".join(lines)[:max_chars]

    def _project_root(self, project_name: str) -> str:
        project = self._data.get("projects", {}).get(project_name, {})
        return str(project.get("path") or "")

    def _task_history_call(self, method: str, *args, default=None, **kwargs):
        if self._task_history is None:
            return default
        callback = getattr(self._task_history, method, None)
        if not callable(callback):
            return default
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            log.warning("project task history unavailable for %s: %s", args[0] if args else "", exc)
            return default

    def get_active(self) -> ProjectSnapshot | None:
        name = self._data.get("active", "")
        if not name:
            return None
        stored = self._data.get("projects", {}).get(name)
        if not stored:
            return None
        return ProjectSnapshot(name=name, path=stored["path"], language=stored.get("language", "unknown"), markers=stored.get("markers", []), git_repo=stored.get("git", False))

    def _save(self) -> None:
        save_json(self._file, self._data)

    @property
    def path(self) -> Path:
        return self._file

    def clear(self) -> None:
        self._data = {"active": "", "projects": {}}
        self._save()


def _sanitize_text(text: str, max_chars: int) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch not in "\x00\x01")
    return cleaned[:max_chars]


def _one_line(value: str, max_chars: int) -> str:
    return " ".join(str(value or "").split())[:max_chars]