from __future__ import annotations

import json

from config.settings import Settings
from core.task_store import TaskStore

from .registry import ToolResult


def _json_output(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _plan_public(record, include_args: bool) -> dict:
    """Read-only plan view.

    Raw step arguments are withheld by default: a plan is model-authored and can
    embed file contents, credentials or long payloads. ``arg_count`` is exposed
    instead so a caller can tell an empty step from a large one.
    """
    from core.planner import ExecutionPlanRecord

    plan = getattr(record, "plan", None)
    if not isinstance(plan, ExecutionPlanRecord):
        return {"present": False}
    view = plan.to_public_dict(include_args=include_args)
    view["present"] = True
    return view


def task_info(
    task_id: str,
    settings: Settings,
    include_plan_args: bool = False,
) -> ToolResult:
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
    # inspect_task returns the raw serialised record; swap in the safe plan view.
    record = TaskStore(settings.sessions_dir).get(normalized_id)
    task["plan"] = _plan_public(record, bool(include_plan_args))
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
