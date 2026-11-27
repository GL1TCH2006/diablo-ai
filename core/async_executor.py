"""M3.1 — Async execution foundation with bounded concurrency and streaming callbacks."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config.permissions import PermissionLevel, PermissionManager, ToolBlockedError
from core.cancellation import CancellationToken, cancellation_requested
from core.events import EventBus
from core.logging_setup import get_logger
from tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec

log = get_logger("async_executor")


@dataclass
class ExecutionOutcome:
    """Result of a single tool execution. Mirrors core.executor.ExecutionOutcome."""
    tool: str
    allowed: bool = False
    approved: bool = False
    requires_confirmation: bool = False
    executed: bool = False
    success: bool = False
    cancelled: bool = False
    output: str = ""
    error: str = ""
    reason: str = ""
    duration_ms: int = 0
    level: str = ""
    status: str = "pending"
    exit_code: int | None = None
    data: dict | None = None
    verification: object | None = None

    @property
    def display(self) -> str:
        if self.success:
            return self.output.strip()
        return self.error or self.reason or "No result."


@dataclass
class ExecutionResult:
    """Aggregated result of a plan execution."""
    success: bool
    completed_steps: int
    total_steps: int
    outcomes: list = field(default_factory=list)
    error: str = ""
    cancelled: bool = False
    timeout: bool = False


class AsyncExecutor:
    """Async executor with bounded concurrency, streaming callbacks, and cancellation.

    Replaces the synchronous Executor.run() with async execution using a thread pool
    for blocking tool calls. Provides streaming callbacks for progress reporting
    and integrates with the existing cancellation token system.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionManager,
        events: EventBus,
        confirmer=None,
        max_result_chars: int = 8000,
        max_concurrent: int = 4,
        before_execute=None,
        state_listener=None,
        event_context=None,
        # Tool execution timeouts (seconds). 0 or negative disables timeout.
        tool_terminal_timeout: float = 120.0,
        tool_python_timeout: float = 300.0,
        tool_blender_timeout: float = 600.0,
        tool_generic_timeout: float = 60.0,
    ):
        self._registry = registry
        self._permissions = permissions
        self._events = events
        self._confirmer = confirmer
        self._max_result_chars = max_result_chars
        self._before_execute = before_execute
        self._state_listener = state_listener
        self._event_context = event_context
        self._task_provider = None

        # Concurrency control
        self._max_concurrent = max(1, max_concurrent)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._thread_pool = ThreadPoolExecutor(max_workers=self._max_concurrent)

        # Tool-specific timeouts
        self._tool_timeouts = {
            "terminal.execute": tool_terminal_timeout,
            "python.run_script": tool_python_timeout,
            "blender.": tool_blender_timeout,  # prefix match for blender tools
        }
        self._default_tool_timeout = tool_generic_timeout

        # Task tracking
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._task_cancellation_tokens: dict[str, CancellationToken] = {}
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_task_start: Optional[Callable[[str, Any], None]] = None
        self._on_task_progress: Optional[Callable[[str, Any, dict], None]] = None
        self._on_task_complete: Optional[Callable[[str, Any, "ExecutionOutcome"], None]] = None

        # Tool timeout cache
        self._tool_timeouts = {
            "terminal.execute": tool_terminal_timeout,
            "python.run_script": tool_python_timeout,
            "blender.": tool_blender_timeout,
        }
        self._default_tool_timeout = tool_generic_timeout

    def set_task_provider(self, provider=None) -> None:
        """Register a callable returning the authoritative task id."""
        self._task_provider = provider

    def set_callbacks(
        self,
        on_start: Optional[Callable[[str, Any], None]] = None,
        on_progress: Optional[Callable[[str, Any, dict], None]] = None,
        on_complete: Optional[Callable[[str, Any, "ExecutionOutcome"], None]] = None,
    ) -> None:
        """Register streaming callbacks."""
        self._on_task_start = on_start
        self._on_task_progress = on_progress
        self._on_task_complete = on_complete

    def set_confirmer(self, confirmer=None) -> None:
        """Update the confirmer callback for permission prompts."""
        self._confirmer = confirmer

    def run(
        self,
        tool: str,
        args: dict,
        cancellation_token: CancellationToken | object | None = None,
    ) -> "ExecutionOutcome":
        """Synchronous wrapper for run_tool (backward compatibility with Executor.run).
        
        Handles both synchronous and asynchronous calling contexts.
        """
        import asyncio
        import concurrent.futures
        
        # Check if we're in an async context
        try:
            loop = asyncio.get_running_loop()
            # We're in an async context - run the tool synchronously in thread pool
            # by submitting to the thread pool and waiting on the future
            # This is a simplified version that executes the tool function directly
            
            spec = self._registry.get(tool) if isinstance(tool, str) else None
            if spec is None:
                outcome = ExecutionOutcome(tool=tool)
                outcome.status = "error"
                outcome.reason = f"Unknown tool: {tool}"
                outcome.error = outcome.reason
                return outcome
            
            if not isinstance(args, dict):
                outcome = ExecutionOutcome(tool=tool)
                outcome.status = "error"
                outcome.reason = f"Arguments for {tool} must be an object."
                outcome.error = outcome.reason
                return outcome
            
            if cancellation_requested(cancellation_token):
                outcome = ExecutionOutcome(tool=tool)
                outcome.status = "cancelled"
                outcome.cancelled = True
                outcome.reason = "Cancelled by request."
                return outcome
            
            # Permission evaluation
            try:
                decision = self._permissions.evaluate(
                    tool, args, spec.level, spec.level_for, self._task_id()
                )
            except Exception as exc:
                outcome = ExecutionOutcome(tool=tool)
                outcome.status = "error"
                outcome.reason = f"Permission evaluation failed: {exc}"
                outcome.error = outcome.reason
                return outcome
            
            if decision.blocked:
                outcome = ExecutionOutcome(tool=tool)
                outcome.status = "blocked"
                outcome.reason = decision.reason
                return outcome
            
            if decision.requires_confirmation:
                if self._confirmer is None:
                    outcome = ExecutionOutcome(tool=tool)
                    outcome.status = "cancelled"
                    outcome.reason = "Cannot prompt for confirmation: no confirmer available."
                    return outcome
                # Call confirmer in thread pool to avoid blocking event loop
                future = self._thread_pool.submit(lambda: self._confirmer(tool, spec.describe_args(args)))
                choice = future.result()
                if choice == "deny":
                    outcome = ExecutionOutcome(tool=tool)
                    outcome.status = "cancelled"
                    outcome.reason = "Denied by user."
                    outcome.cancelled = True
                    return outcome
                if choice == "block":
                    self._permissions.session_block(tool)
                    outcome = ExecutionOutcome(tool=tool)
                    outcome.status = "blocked"
                    outcome.reason = "Blocked by user (session)."
                    return outcome
                if choice not in ("allow", "always"):
                    outcome = ExecutionOutcome(tool=tool)
                    outcome.status = "cancelled"
                    outcome.reason = "Cancelled because confirmation returned no valid allow decision."
                    outcome.cancelled = True
                    return outcome
                if choice == "always":
                    self._events.emit_tool(tool, "allowed_already", "approved for this execution")
                # Continue to tool execution
            
            # Execute tool in thread pool synchronously
            call_args = dict(args)
            if spec.wants_context:
                context = self._context()
                call_args["_tool_context"] = {
                    "run_id": str(self._context().get("run_id") or ""),
                    "task_id": str(self._context().get("task_id") or "") or None,
                    "cancellation_token": cancellation_token,
                }
            
            if "timeout" not in call_args:
                timeout = self._get_tool_timeout(tool)
                if timeout and timeout > 0:
                    call_args["timeout"] = int(timeout)
            
            # Submit to thread pool and wait for result (synchronous)
            future = self._thread_pool.submit(lambda: spec.fn(**call_args))
            result = future.result()
            
            # Convert result to ExecutionOutcome
            outcome = ExecutionOutcome(tool=tool)
            outcome.allowed = True
            outcome.executed = True
            outcome.success = bool(result and result.success)
            outcome.cancelled = bool(result and getattr(result, "cancelled", False))
            outcome.output = self._trim(result.output) if result else ""
            outcome.error = result.error if result else ""
            outcome.exit_code = result.exit_code if result else None
            outcome.data = result.data if result and isinstance(result.data, dict) else None
            outcome.verification = getattr(result, "verification", None) if result else None
            outcome.reason = "ok" if outcome.success else (outcome.error or "Tool reported failure.")
            outcome.status = "cancelled" if outcome.cancelled else ("success" if outcome.success else "error")
            
            if cancellation_requested(cancellation_token):
                outcome.cancelled = True
                outcome.status = "cancelled"
                outcome.reason = "Cancelled after execution; remaining work was stopped."
            
            return outcome
        except RuntimeError:
            # No running loop - use asyncio.run with full async path
            return asyncio.run(self.run_tool(tool, args, cancellation_token))

    def _task_id(self) -> str:
        provider = self._task_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "")
        except Exception:
            return ""

    def _get_tool_timeout(self, tool: str) -> float:
        """Get the configured timeout for a specific tool."""
        if tool in self._tool_timeouts:
            return self._tool_timeouts[tool]
        for prefix, timeout in self._tool_timeouts.items():
            if prefix.endswith(".") and tool.startswith(prefix):
                return timeout
        return self._default_tool_timeout

    def _emit(self, kind: str, payload: dict) -> None:
        emit = getattr(self._events, "emit", None)
        if callable(emit):
            emit(kind, payload)

    def _context(self) -> dict:
        if not callable(self._event_context):
            return {}
        try:
            value = self._event_context()
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _notify_permission(self) -> None:
        if not callable(self._state_listener):
            return
        try:
            self._state_listener("awaiting_permission")
        except Exception as exc:
            log.warning("permission state listener failed: %s", exc)

    def _trim(self, text: str) -> str:
        from ai.tokenizer import head_tail
        return head_tail(text or "", self._max_result_chars, 600)

    async def _run_tool_sync(
        self,
        tool: str,
        args: dict,
        cancellation_token: CancellationToken | object | None,
        spec: ToolSpec,
        task_id: str,
    ) -> "ExecutionOutcome":
        """Execute a tool synchronously in the thread pool."""
        start = time.perf_counter()
        outcome = ExecutionOutcome(tool=tool)
        spec = self._registry.get(tool) if isinstance(tool, str) else None

        if spec is None:
            outcome.status = "error"
            outcome.reason = f"Unknown tool: {tool}"
            outcome.error = outcome.reason
            return outcome

        if not isinstance(args, dict):
            outcome.status = "error"
            outcome.reason = f"Arguments for {tool} must be an object."
            outcome.error = outcome.reason
            return outcome

        if cancellation_requested(cancellation_token):
            outcome.status = "cancelled"
            outcome.cancelled = True
            outcome.reason = "Cancelled by request."
            return outcome

        # Permission evaluation
        try:
            decision = self._permissions.evaluate(
                tool, args, spec.level, spec.level_for, self._task_id()
            )
        except Exception as exc:
            outcome.status = "error"
            outcome.reason = f"Permission evaluation failed: {exc}"
            outcome.error = outcome.reason
            return outcome

        if spec.explicit_confirmation and decision.allow and not decision.requires_confirmation:
            decision.level = PermissionLevel.SENSITIVE
            decision.allow = False
            decision.requires_confirmation = True
            decision.reason = "Explicit confirmation is required for policy changes."

        outcome.allowed = decision.allow
        outcome.approved = decision.allow and not decision.requires_confirmation
        outcome.requires_confirmation = decision.requires_confirmation
        outcome.level = str(decision.level)
        outcome.reason = decision.reason

        if decision.blocked:
            outcome.status = "blocked"
            return outcome

        if decision.requires_confirmation:
            self._notify_permission()
            self._emit("permission_required", {
                **self._context(),
                "tool": tool,
                "description": spec.describe_args(args),
                "level": outcome.level,
            })
            self._events.emit_tool(tool, "pending_confirmation", spec.describe_args(args))
            if self._confirmer is None:
                outcome.approved = False
                outcome.cancelled = True
                outcome.status = "cancelled"
                outcome.reason = "Cancelled because no confirmation channel is available."
                return outcome
            try:
                choice = self._confirmer(tool, spec.describe_args(args))
            except Exception as exc:
                outcome.status = "error"
                outcome.error = f"Confirmation failed: {exc}"
                outcome.reason = outcome.error
                return outcome
            if choice == "deny":
                outcome.cancelled = True
                outcome.status = "cancelled"
                outcome.reason = "Denied by user."
                return outcome
            if choice == "block":
                self._permissions.session_block(tool)
                outcome.status = "blocked"
                outcome.reason = "Blocked by user (session)."
                return outcome
            if choice == "always":
                self._events.emit_tool(tool, "allowed_already", "approved for this execution")
            if choice not in ("allow", "always"):
                outcome.cancelled = True
                outcome.status = "cancelled"
                outcome.reason = "Cancelled because confirmation returned no valid allow decision."
                return outcome
            outcome.approved = True

        if cancellation_requested(cancellation_token):
            outcome.cancelled = True
            outcome.status = "cancelled"
            outcome.reason = "Cancelled by request."
            return outcome

        if callable(self._before_execute):
            try:
                self._before_execute(tool, args)
            except Exception as exc:
                outcome.status = "error"
                outcome.error = f"Pre-execution integration failed: {exc}"
                outcome.reason = outcome.error
                return outcome

        if cancellation_requested(cancellation_token):
            outcome.cancelled = True
            outcome.status = "cancelled"
            outcome.reason = "Cancelled before dispatch."
            return outcome

        outcome.executed = True
        outcome.status = "running"

        # Emit start event
        self._emit("tool_started", {**self._context(), "tool": tool, "status": "running"})
        self._events.emit_tool(tool, "running", "")

        # Fire on_start callback
        if self._on_task_start:
            try:
                self._on_task_start(task_id, None)
            except Exception:
                pass

        try:
            call_args = dict(args)
            if spec.wants_context:
                context = self._context()
                call_args["_tool_context"] = {
                    "run_id": str(self._context().get("run_id") or ""),
                    "task_id": str(self._context().get("task_id") or "") or None,
                    "cancellation_token": cancellation_token,
                }

            # Inject tool-specific timeout if the tool accepts a timeout parameter
            if "timeout" not in call_args:
                timeout = self._get_tool_timeout(tool)
                if timeout and timeout > 0:
                    call_args["timeout"] = int(timeout)

            # Execute in thread pool
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._thread_pool,
                lambda: spec.fn(**call_args)
            )

            outcome.success = bool(result and result.success)
            outcome.cancelled = bool(result and getattr(result, "cancelled", False))
            outcome.output = self._trim(result.output) if result else ""
            outcome.error = result.error if result else ""
            outcome.exit_code = result.exit_code if result else None
            outcome.data = result.data if result and isinstance(result.data, dict) else None
            outcome.verification = getattr(result, "verification", None) if result else None
            outcome.reason = "ok" if outcome.success else (outcome.error or "Tool reported failure.")
            outcome.status = "cancelled" if outcome.cancelled else ("success" if outcome.success else "error")

        except ToolBlockedError as exc:
            outcome.success = False
            outcome.error = str(exc)
            outcome.reason = outcome.error
            outcome.allowed = False
            outcome.status = "blocked"
            log.info("tool=%s blocked in tool layer: %s", tool, exc)
        except Exception as exc:
            outcome.success = False
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.reason = outcome.error
            outcome.status = "error"
            log.error("Tool %s raised: %s", tool, exc, exc_info=True)

        if cancellation_requested(cancellation_token):
            outcome.cancelled = True
            outcome.status = "cancelled"
            outcome.reason = "Cancelled after execution; remaining work was stopped."

        outcome.duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "tool=%s status=%s executed=%s success=%s duration_ms=%s reason=%s",
            tool, outcome.status, outcome.executed, outcome.success, outcome.duration_ms, outcome.reason or "-",
        )

        return outcome

    async def run_tool(
        self,
        tool: str,
        args: dict,
        cancellation_token: CancellationToken | object | None = None,
        on_start: Optional[Callable[[str, Any], None]] = None,
        on_progress: Optional[Callable[[str, Any, dict], None]] = None,
        on_complete: Optional[Callable[[str, Any, "ExecutionOutcome"], None]] = None,
    ) -> "ExecutionOutcome":
        """Execute a single tool asynchronously with streaming callbacks.

        Args:
            tool: Tool name to execute
            args: Tool arguments
            cancellation_token: Cancellation token for cooperative cancellation
            on_start: Callback when tool starts (task_id, step_info)
            on_progress: Callback for progress updates (task_id, step_info, progress_dict)
            on_complete: Callback when tool completes (task_id, step_info, outcome)

        Returns:
            ExecutionOutcome with the tool result
        """
        task_id = self._task_id() or f"task_{id(object())}"
        spec = self._registry.get(tool) if isinstance(tool, str) else None

        if spec is None:
            outcome = ExecutionOutcome(tool=tool)
            outcome.status = "error"
            outcome.reason = f"Unknown tool: {tool}"
            outcome.error = outcome.reason
            return outcome

        # Acquire semaphore for concurrency control
        async with self._semaphore:
            # Create task-specific cancellation token
            task_token = CancellationToken(task_id=task_id)
            self._task_cancellation_tokens[task_id] = task_token

            # Chain with parent cancellation token
            async def check_cancellation():
                while True:
                    if cancellation_requested(cancellation_token) or task_token.is_cancelled():
                        task_token.request("Cancelled by parent", source="parent")
                        break
                    await asyncio.sleep(0.05)

            cancel_monitor = asyncio.create_task(check_cancellation())

            try:
                # Emit start event
                self._emit("tool_started", {**self._context(), "tool": tool, "status": "running"})
                self._events.emit_tool(tool, "running", "")

                if on_start:
                    try:
                        on_start(task_id, None)
                    except Exception:
                        pass

                # Run tool in thread pool
                outcome = await self._run_tool_sync(tool, args, cancellation_token, spec, task_id)

                if on_complete:
                    try:
                        on_complete(task_id, None, outcome)
                    except Exception:
                        pass

                return outcome

            finally:
                cancel_monitor.cancel()
                try:
                    await cancel_monitor
                except asyncio.CancelledError:
                    pass
                self._task_cancellation_tokens.pop(task_id, None)

    async def execute_plan(
        self,
        plan,
        context,
        on_step_start=None,
        on_step_progress=None,
        on_step_complete=None,
    ):
        """Execute a full plan with dependency gating and streaming callbacks.

        Args:
            plan: ExecutionPlanRecord with steps
            context: RequestContext for the execution
            on_step_start: Callback when step starts (task_id, step)
            on_step_progress: Callback for progress (task_id, step, progress_dict)
            on_step_complete: Callback when step completes (task_id, step, outcome)

        Returns:
            ExecutionResult with aggregated outcomes
        """
        from core.task_manager import StepStatus

        outcomes = []
        completed = 0
        total = len(plan.steps) if plan and plan.steps else 0

        # Track step statuses for dependency gating
        step_status: dict[str, str] = {}

        def step_satisfied(step_id: str) -> bool:
            return step_status.get(step_id) == "done"

        def dependencies_satisfied(step) -> bool:
            return all(step_satisfied(dep) for dep in (step.depends_on or ()))

        # Fire on_step_start for initial queued steps
        if on_step_start:
            for step in plan.steps:
                if step.status.value == "pending" and dependencies_satisfied(step):
                    try:
                        on_step_start(step.step_id, step)
                    except Exception:
                        pass

        while True:
            # Find next executable step
            next_step = None
            for step in plan.steps:
                if step.status.value == "pending" and dependencies_satisfied(step):
                    next_step = step
                    break

            if not next_step:
                break  # No more executable steps

            # Check cancellation
            if cancellation_requested(context.cancellation_token):
                # Mark remaining pending steps as cancelled
                for step in plan.steps:
                    if step.status.value == "pending":
                        step.status = "cancelled"
                        step.detail = "Cancelled by request"
                break

            # Execute step
            step_id = next_step.step_id
            tool = next_step.tool
            args = next_step.args if isinstance(next_step.args, dict) else {}

            # Mark as running
            next_step.status = StepStatus.RUNNING
            step_status[step_id] = "running"

            if on_step_start:
                try:
                    on_step_start(step_id, next_step)
                except Exception:
                    pass

            # Execute with callbacks
            async def progress_wrapper(progress_dict):
                if on_step_progress:
                    try:
                        on_step_progress(step_id, next_step, progress_dict)
                    except Exception:
                        pass

            outcome = await self.run_tool(
                tool,
                args,
                cancellation_token=context.cancellation_token,
                on_progress=progress_wrapper,
            )

            outcomes.append(outcome)

            # Update step status
            if outcome.cancelled:
                next_step.status = StepStatus.CANCELLED
                step_status[step_id] = "cancelled"
            elif outcome.success:
                next_step.status = StepStatus.DONE
                step_status[step_id] = "done"
                completed += 1
            else:
                next_step.status = StepStatus.FAILED
                step_status[step_id] = "failed"

            # Record outcome on step
            next_step.detail = outcome.reason
            next_step.result_summary = outcome.display[:200] if outcome.display else ""

            if on_step_complete:
                try:
                    on_step_complete(step_id, next_step, outcome)
                except Exception:
                    pass

            # Check if we should continue
            if outcome.cancelled or (not outcome.success and not outcome.cancelled):
                # On failure/cancel, check if we should replan or abort
                # For now, stop on first failure unless we implement replanning
                if not outcome.cancelled:
                    log.warning("Step %s failed: %s", step_id, outcome.reason)

            # Check for cancellation after step
            if cancellation_requested(context.cancellation_token):
                break

        success = completed == total and total > 0
        return ExecutionResult(
            success=success,
            completed_steps=completed,
            total_steps=total,
            outcomes=outcomes,
            cancelled=cancellation_requested(context.cancellation_token),
        )

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a specific task by its ID."""
        token = self._task_cancellation_tokens.get(task_id)
        if token:
            token.request("Cancelled by request", source="executor_agent")
            return True
        return False

    def cancel_all(self) -> None:
        """Cancel all active tasks."""
        for token in self._task_cancellation_tokens.values():
            token.request("Cancelled all tasks", source="executor_agent")

    def shutdown(self) -> None:
        """Shutdown the executor, cancelling all tasks and shutting down the thread pool."""
        self.cancel_all()
        # Wait for active tasks to complete cancellation
        for task in self._active_tasks.values():
            if not task.done():
                task.cancel()
        # Shutdown thread pool
        self._thread_pool.shutdown(wait=True, cancel_futures=True)
        self._active_tasks.clear()
        self._task_cancellation_tokens.clear()

    def _get_tool_timeout(self, tool: str) -> float:
        """Get the configured timeout for a specific tool."""
        if tool in self._tool_timeouts:
            return self._tool_timeouts[tool]
        for prefix, timeout in self._tool_timeouts.items():
            if prefix.endswith(".") and tool.startswith(prefix):
                return timeout
        return self._default_tool_timeout