from __future__ import annotations

import json

from config.settings import Settings
from core.task_store import TaskStore

from .registry import ToolResult


def _json_output(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def task_info(task_id: str, settings: Settings) -> ToolResult:
    if settings is None:
        return ToolResult(False, error="Task inspection requires application settings")
    if not isinstance(task_id, str) or not task_id.strip():
        return ToolResult(False, error="task_id is required")
    normalized_id = task_id.strip()
    try:
        task = TaskStore(settings.sessions_dir).inspect_task(normalized_id)
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(False, error=f"Could not inspect task: {exc}")
    if task is None:
        return ToolResult(False, error=f"Task not found: {normalized_id}")
    return ToolResult(True, output=_json_output(task))


def task_list(
    project: str = "",
    status: str = "",
    limit=20,
    settings: Settings | None = None,
) -> ToolResult:
    if settings is None:
        return ToolResult(False, error="Task inspection requires application settings")
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except ValueError:
            return ToolResult(False, error="limit must be a non-negative integer")
    try:
        tasks = TaskStore(settings.sessions_dir).query_tasks(
            project=project,
            status=status,
            limit=limit,
        )
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(False, error=str(exc))
    return ToolResult(True, output=_json_output({"tasks": tasks, "count": len(tasks)}))
