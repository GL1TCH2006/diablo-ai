from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from core.logging_setup import get_logger
from core.task_store import TaskRecord, TaskStore, serialize_task_summary

log = get_logger("project_task_history")


class ProjectTaskHistory:
    def __init__(self, task_store: TaskStore | None, path_base: Path | str | None = None):
        self._task_store = task_store
        self._path_base = Path(path_base) if path_base else None

    def list(
        self,
        project_name: str,
        project_root: str = "",
        status: str = "",
        limit: Optional[int] = 20,
    ) -> list[dict]:
        if not isinstance(project_name, str) or not isinstance(project_root, str):
            raise ValueError("project name and root must be strings")
        status = status.strip().lower()
        if status and status not in TaskRecord.VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")

        records = [
            record
            for record in self._load_records()
            if self._matches_project(record, project_name, project_root)
            and (not status or record.status == status)
        ]
        records.sort(key=self._sort_key, reverse=True)
        if limit is not None:
            records = records[:limit]
        return [serialize_task_summary(record) for record in records]

    def get(self, project_name: str, project_root: str, task_id: str) -> Optional[dict]:
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        for summary in self.list(project_name, project_root, limit=None):
            if summary["task_id"] == task_id.strip():
                return summary
        return None

    def current(self, project_name: str, project_root: str = "") -> Optional[dict]:
        tasks = self.list(project_name, project_root, status="active", limit=1)
        return tasks[0] if tasks else None

    def most_recent_completed(
        self,
        project_name: str,
        project_root: str = "",
    ) -> Optional[dict]:
        tasks = self.list(project_name, project_root, status="completed", limit=1)
        return tasks[0] if tasks else None

    def failed(
        self,
        project_name: str,
        project_root: str = "",
        limit: Optional[int] = 20,
    ) -> list[dict]:
        return self.list(project_name, project_root, status="failed", limit=limit)

    def find_by_change(
        self,
        project_name: str,
        project_root: str = "",
        file_path: str = "",
        commit_hash: str = "",
    ) -> list[dict]:
        file_path = file_path.strip()
        commit_hash = commit_hash.strip().casefold()
        if not file_path and not commit_hash:
            return []
        records = [
            record
            for record in self._load_records()
            if self._matches_project(record, project_name, project_root)
        ]
        records.sort(key=self._sort_key, reverse=True)
        matches = []
        for record in records:
            if file_path and not self._matches_file(record, file_path):
                continue
            if commit_hash and not self._matches_commit(record, commit_hash):
                continue
            matches.append(serialize_task_summary(record))
        return matches

    def _load_records(self) -> list[TaskRecord]:
        if self._task_store is None:
            return []
        try:
            return self._task_store.list_all()
        except Exception as exc:
            log.warning("task history unavailable: %s", exc)
            return []

    def _matches_project(self, record: TaskRecord, project_name: str, project_root: str) -> bool:
        name_matches = bool(project_name) and record.project_name == project_name.strip()
        if not project_root.strip():
            return name_matches
        if record.project_root:
            return self._normalize_path(record.project_root) == self._normalize_path(project_root)
        return name_matches

    def _normalize_path(self, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() and self._path_base is not None:
            path = self._path_base / path
        try:
            resolved = path.resolve()
        except OSError:
            resolved = Path(os.path.abspath(str(path)))
        return os.path.normcase(str(resolved))

    @staticmethod
    def _sort_key(record: TaskRecord) -> tuple[str, str]:
        activity = record.finished_at or record.started_at or record.created_at or ""
        return activity, record.task_id

    @staticmethod
    def _normalize_file(value: str) -> str:
        return str(value or "").replace("\\", "/").strip().casefold()

    def _matches_file(self, record: TaskRecord, file_path: str) -> bool:
        expected = self._normalize_file(file_path)
        absolute_expected = self._normalize_path(file_path) if Path(file_path).is_absolute() else ""
        for changed in record.git.files_changed or []:
            actual = self._normalize_file(changed)
            if actual == expected or actual.endswith("/" + expected.lstrip("/")):
                return True
            if absolute_expected and record.project_root:
                changed_path = Path(changed)
                if not changed_path.is_absolute():
                    changed_path = Path(record.project_root) / changed_path
                if self._normalize_path(str(changed_path)) == absolute_expected:
                    return True
        return False

    @staticmethod
    def _matches_commit(record: TaskRecord, commit_hash: str) -> bool:
        candidates = [record.git.starting_commit, record.git.ending_commit]
        candidates.extend(commit.hash for commit in record.git.commits_made or [])
        return any(
            candidate
            and (candidate.casefold() == commit_hash or candidate.casefold().startswith(commit_hash))
            for candidate in candidates
        )
