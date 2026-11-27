from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .registry import ToolResult

# Ensure the DDLE package is importable
DDLE_PATH = Path("C:/PROJECTS/DDLE")
if str(DDLE_PATH) not in sys.path:
    sys.path.insert(0, str(DDLE_PATH))


def _controller():
    """Lazily create a DDLEController."""
    from controller.ddle_controller import DDLEController
    from controller.permissions import DDLEPermissions

    return DDLEController(permissions=DDLEPermissions())


def start_task(
    description: str,
    exit_conditions: list[str] | None = None,
    max_iterations: int = 10,
) -> ToolResult:
    """Create and run a DDLE task.

    Runs the four-part DDLE development loop (context → execution → critic → exit
    check) until a terminal state is reached or max_iterations is exceeded.

    Args:
        description: Human-readable description of the task.
        exit_conditions: List of strings that must appear in execution output for
            the critic to consider the task complete.
        max_iterations: Maximum number of loop iterations before failing.

    Returns:
        ToolResult with the final task state summary.
    """
    if not description or not description.strip():
        return ToolResult(False, error="description is required")
    if exit_conditions is None:
        exit_conditions = [description]

    try:
        controller = _controller()
        task = controller.create_task(
            description=description.strip(),
            exit_conditions=list(exit_conditions),
            max_iterations=max_iterations,
        )
        final = controller.run_task(task)
        return ToolResult(
            True,
            output=(
                f"DDLE task {final.task_id} finished: state={final.state.value} "
                f"iteration={final.iteration}/{final.max_iterations}\n"
                f"Description: {final.description}\n"
                f"Errors: {', '.join(final.errors) if final.errors else 'none'}\n"
                f"Evidence: {'; '.join(final.evidence) if final.evidence else 'none'}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - report errors as ToolResult, don't raise
        return ToolResult(False, error=f"DDLE task failed: {exc}")


def task_status(task_id: str) -> ToolResult:
    """Load a DDLE task from disk and return its current status.

    Args:
        task_id: The DDLE task ID (8-char hex).

    Returns:
        ToolResult with the task's state, iteration, errors, and evidence.
    """
    if not task_id or not task_id.strip():
        return ToolResult(False, error="task_id is required")

    try:
        controller = _controller()
        status = controller.get_status(task_id.strip())
        return ToolResult(
            True,
            output=(
                f"DDLE task {status['task_id']}\n"
                f"State: {status['state']}\n"
                f"Iteration: {status['iteration']}/{status['max_iterations']}\n"
                f"Description: {status['description']}\n"
                f"Errors: {', '.join(status['errors']) if status['errors'] else 'none'}\n"
                f"Evidence: {'; '.join(status['evidence']) if status['evidence'] else 'none'}"
            ),
        )
    except FileNotFoundError:
        return ToolResult(False, error=f"DDLE task not found: {task_id}")
    except Exception as exc:  # noqa: BLE001 - report errors as ToolResult, don't raise
        return ToolResult(False, error=f"DDLE task status failed: {exc}")


def list_tasks() -> ToolResult:
    """List all DDLE tasks saved to disk.

    Returns:
        ToolResult with task IDs and their states.
    """
    try:
        from controller.persistence import list_tasks

        task_ids = list_tasks()
        if not task_ids:
            return ToolResult(True, output="No DDLE tasks found.")

        lines = ["DDLE tasks:"]
        for task_id in task_ids:
            try:
                controller = _controller()
                status = controller.get_status(task_id)
                lines.append(
                    f"  {task_id}: state={status['state']} "
                    f"iteration={status['iteration']}/{status['max_iterations']}"
                )
            except Exception:
                lines.append(f"  {task_id}: state=unknown")
        return ToolResult(True, output="\n".join(lines))
    except Exception as exc:  # noqa: BLE001 - report errors as ToolResult, don't raise
        return ToolResult(False, error=f"DDLE list tasks failed: {exc}")


__all__ = ["start_task", "task_status", "list_tasks"]
