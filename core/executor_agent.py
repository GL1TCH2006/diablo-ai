"""M3.1 — ExecutorAgent: orchestration boundary between planner and execution engine."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config.permissions import PermissionLevel, PermissionManager, ToolBlockedError
from core.cancellation import CancellationToken, cancellation_requested
from core.events import EventBus
from core.logging_setup import get_logger
from core.request_context import RequestContext
from tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec

from .async_executor import AsyncExecutor, ExecutionOutcome, ExecutionResult

log = get_logger("executor_agent")


@dataclass
class StepExecutionInfo:
    """Information about a step being executed."""
    step_id: str
    tool: str
    args: dict
    status: str = "pending"  # pending, running, completed, failed, cancelled
    outcome: Optional["ExecutionOutcome"] = None
    start_time: float = 0.0
    end_time: float = 0.0


@dataclass
class PlanExecutionResult:
    """Result of executing a full plan."""
    success: bool
    completed_steps: int
    total_steps: int
    outcomes: list = field(default_factory=list)
    step_infos: dict[str, StepExecutionInfo] = field(default_factory=dict)
    error: str = ""
    cancelled: bool = False
    timeout: bool = False
    duration_ms: int = 0


class ExecutorAgent:
    """Orchestration boundary between planner-generated work and the execution engine.

    Accepts validated task descriptions from the planner, dispatches tasks through
    the AsyncExecutor, enforces permissions and confirmations, tracks execution
    state, and forwards progress events via callbacks.
    """

    def __init__(
        self,
        async_executor: "AsyncExecutor",
        registry: ToolRegistry,
        permissions: PermissionManager,
        events: EventBus,
        confirmer=None,
    ):
        self._async_executor = async_executor
        self._registry = registry
        self._permissions = permissions
        self._events = events
        self._confirmer = confirmer
        self._task_provider = None

        # Execution state
        self._active_plan = None
        self._step_infos: dict[str, StepExecutionInfo] = {}
        self._plan_start_time = 0.0
        self._plan_end_time = 0.0

        # Callbacks
        self._on_step_start: Optional[Callable[[str, Any], None]] = None
        self._on_step_progress: Optional[Callable[[str, Any, dict], None]] = None
        self._on_step_complete: Optional[Callable[[str, Any, "ExecutionOutcome"], None]] = None
        self._on_plan_start: Optional[Callable[[str], None]] = None
        self._on_plan_complete: Optional[Callable[[str, Any], None]] = None

        # Set up callbacks on the async executor
        self._async_executor.set_callbacks(
            on_start=self._on_step_started,
            on_progress=self._on_step_progressed,
            on_complete=self._on_step_completed,
        )

    def set_task_provider(self, provider=None) -> None:
        """Register a callable returning the authoritative task id."""
        self._task_provider = provider
        self._async_executor.set_task_provider(provider)

    def set_callbacks(
        self,
        on_step_start: Optional[Callable[[str, Any], None]] = None,
        on_step_progress: Optional[Callable[[str, Any, dict], None]] = None,
        on_step_complete: Optional[Callable[[str, Any, "ExecutionOutcome"], None]] = None,
        on_plan_start: Optional[Callable[[str], None]] = None,
        on_plan_complete: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        """Register streaming callbacks for plan execution."""
        self._on_step_start = on_step_start
        self._on_step_progress = on_step_progress
        self._on_step_complete = on_step_complete
        self._on_plan_start = on_plan_start
        self._on_plan_complete = on_plan_complete

    def set_confirmer(self, confirmer=None) -> None:
        """Update the confirmer callback for permission prompts."""
        self._confirmer = confirmer
        self._async_executor.set_confirmer(confirmer)

    def _task_id(self) -> str:
        provider = self._task_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "")
        except Exception:
            return ""

    def _emit(self, kind: str, payload: dict) -> None:
        emit = getattr(self._events, "emit", None)
        if callable(emit):
            emit(kind, payload)

    def _context(self) -> dict:
        if not callable(self._async_executor._event_context):
            return {}
        try:
            value = self._async_executor._event_context()
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _emit_plan_start(self, plan_id: str) -> None:
        self._emit("plan_started", {**self._context(), "plan_id": plan_id})
        if self._on_plan_start:
            try:
                self._on_plan_start(plan_id)
            except Exception:
                pass

    def _emit_plan_complete(self, plan_id: str, result) -> None:
        self._emit("plan_completed", {**self._context(), "plan_id": plan_id, "success": result.success})
        if self._on_plan_complete:
            try:
                self._on_plan_complete(plan_id, result)
            except Exception:
                pass

    def _on_step_started(self, task_id: str, step: Any) -> None:
        """Called when a step starts executing."""
        step_id = getattr(step, "step_id", task_id)
        self._step_infos[step_id] = StepExecutionInfo(
            step_id=step_id,
            tool="",
            args={},
            status="running",
            start_time=time.perf_counter(),
        )
        self._emit("step_started", {**self._context(), "step_id": step_id})
        if self._on_step_start:
            try:
                self._on_step_start(step_id, step)
            except Exception:
                pass

    def _on_step_progressed(self, task_id: str, step: Any, progress: dict) -> None:
        step_id = getattr(step, "step_id", task_id)
        if step_id in self._step_infos:
            self._step_infos[task_id].outcome = None  # Will be set on completion
        if self._on_step_progress:
            try:
                self._on_step_progress(step_id, None, progress)
            except Exception:
                pass

    def _on_step_completed(self, task_id: str, step: Any, outcome: "ExecutionOutcome") -> None:
        step_id = getattr(step, "step_id", task_id)
        info = self._step_infos.get(step_id)
        if info:
            info.status = "completed" if outcome.success else ("cancelled" if outcome.cancelled else "failed")
            info.outcome = outcome
            info.end_time = time.perf_counter()

        self._emit("step_completed", {**self._context(), "step_id": step_id, "success": outcome.success})
        if self._on_step_complete:
            try:
                self._on_step_complete(step_id, None, outcome)
            except Exception:
                pass

    async def execute_plan(
        self,
        plan,
        context: RequestContext,
        on_step_start: Optional[Callable[[str, Any], None]] = None,
        on_step_progress: Optional[Callable[[str, Any, dict], None]] = None,
        on_step_complete: Optional[Callable[[str, Any, "ExecutionOutcome"], None]] = None,
        on_plan_start: Optional[Callable[[str], None]] = None,
        on_plan_complete: Optional[Callable[[str, Any], None]] = None,
    ) -> "PlanExecutionResult":
        """Execute a full plan with dependency gating and streaming callbacks.

        Args:
            plan: ExecutionPlanRecord with steps
            context: RequestContext for the execution
            on_step_start: Callback when step starts (step_id, step)
            on_step_progress: Callback for progress (step_id, step, progress_dict)
            on_step_complete: Callback when step completes (step_id, step, outcome)
            on_plan_start: Callback when plan starts (plan_id)
            on_plan_complete: Callback when plan completes (plan_id, result)

        Returns:
            PlanExecutionResult with aggregated outcomes
        """
        from core.task_manager import StepStatus

        # Override callbacks if provided
        if on_step_start:
            self._on_step_start = on_step_start
        if on_step_progress:
            self._on_step_progress = on_step_progress
        if on_step_complete:
            self._on_step_complete = on_step_complete
        if on_plan_start:
            self._on_plan_start = on_plan_start
        if on_plan_complete:
            self._on_plan_complete = on_plan_complete

        self._active_plan = plan
        self._step_infos = {}
        self._plan_start_time = time.perf_counter()

        plan_id = self._task_id() or f"plan_{id(plan)}"
        self._emit_plan_start(plan_id)

        outcomes = []
        completed = 0
        total = len(plan.steps) if plan and plan.steps else 0
        step_status: dict[str, str] = {}
        plan_cancelled = False

        def dependencies_satisfied(step) -> bool:
            return all(step_status.get(dep) == "done" for dep in (step.depends_on or ()))

        def get_next_step():
            for step in plan.steps:
                if step.status == "pending" and dependencies_satisfied(step):
                    return step
            return None

        # Fire on_step_start for initially runnable steps
        if self._on_step_start:
            for step in plan.steps:
                if step.status == "pending" and dependencies_satisfied(step):
                    try:
                        self._on_step_start(step.step_id, step)
                    except Exception:
                        pass

        try:
            while True:
                # Check cancellation
                if cancellation_requested(context.cancellation_token):
                    plan_cancelled = True
                    for step in plan.steps:
                        if step.status == "pending":
                            step.status = "cancelled"
                            step.detail = "Cancelled by request"
                    break

                # Find next executable step
                next_step = get_next_step()
                if not next_step:
                    break  # No more executable steps

                step_id = next_step.step_id
                tool = next_step.tool
                args = next_step.args if isinstance(next_step.args, dict) else {}

                # Mark as running
                next_step.status = "running"
                step_status[step_id] = "running"

                # Execute step with callbacks
                outcome = await self._async_executor.run_tool(
                    next_step.tool,
                    next_step.args if isinstance(next_step.args, dict) else {},
                    cancellation_token=context.cancellation_token,
                    on_start=lambda tid, s: self._on_step_started(tid, next_step),
                    on_progress=lambda tid, s, p: self._on_step_progressed(tid, next_step, p),
                    on_complete=lambda tid, s, o: self._on_step_completed(tid, next_step, o),
                )

                # Record outcome
                next_step.outcome = outcome
                outcomes.append(outcome)

                if outcome.cancelled:
                    next_step.status = "cancelled"
                    step_status[step_id] = "cancelled"
                elif outcome.success:
                    next_step.status = "done"
                    step_status[step_id] = "done"
                else:
                    next_step.status = "failed"
                    step_status[step_id] = "failed"

                # Record outcome on step
                next_step.detail = outcome.reason
                next_step.result_summary = outcome.display[:200] if outcome.display else ""

                if outcome.success:
                    completed += 1
                elif outcome.cancelled:
                    plan_cancelled = True
                    break
                else:
                    # On failure, check if we should continue or abort
                    # For now, stop on first failure unless we implement replanning
                    log.warning("Step %s failed: %s", step_id, outcome.reason)
                    break

        except asyncio.CancelledError:
            plan_cancelled = True
            raise
        finally:
            self._plan_end_time = time.perf_counter()
            duration_ms = int((self._plan_end_time - self._plan_start_time) * 1000)

            success = completed == total and total > 0 and not plan_cancelled
            result = PlanExecutionResult(
                success=success,
                completed_steps=completed,
                total_steps=total,
                outcomes=outcomes,
                step_infos=self._step_infos,
                error="" if success else ("Cancelled" if plan_cancelled else "Execution failed"),
                cancelled=plan_cancelled,
                timeout=False,
                duration_ms=duration_ms,
            )

            self._emit_plan_complete(plan_id, result)
            return result

    def cancel(self, task_id: str = "") -> bool:
        """Cancel the current plan execution."""
        if task_id:
            return self._async_executor.cancel_task(task_id)
        else:
            self._async_executor.cancel_all()
            return True

    def get_plan_status(self) -> dict:
        """Get current execution status of the plan."""
        if not self._active_plan:
            return {"status": "idle"}

        return {
            "plan_id": self._task_id(),
            "total_steps": len(self._active_plan.steps) if self._active_plan.steps else 0,
            "completed": sum(1 for s in self._active_plan.steps if s.status == "done"),
            "running": sum(1 for s in self._active_plan.steps if s.status == "running"),
            "pending": sum(1 for s in self._active_plan.steps if s.status == "pending"),
            "failed": sum(1 for s in self._active_plan.steps if s.status == "failed"),
            "cancelled": sum(1 for s in self._active_plan.steps if s.status == "cancelled"),
            "elapsed_ms": int((time.perf_counter() - self._plan_start_time) * 1000) if self._plan_start_time else 0,
        }

    def shutdown(self) -> None:
        """Shutdown the executor agent and underlying async executor."""
        self._async_executor.shutdown()
        self._active_plan = None
        self._step_infos.clear()


# Re-export for convenience
from .async_executor import ExecutionOutcome, ExecutionResult