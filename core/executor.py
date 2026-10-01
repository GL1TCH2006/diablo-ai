from __future__ import annotations

import time
from dataclasses import dataclass

from config.permissions import PermissionLevel, PermissionManager, ToolBlockedError
from core.cancellation import CancellationToken, cancellation_requested
from core.events import EventBus
from core.logging_setup import get_logger
from tools.registry import ToolExecutionContext, ToolRegistry

log = get_logger("executor")


@dataclass
class ExecutionOutcome:
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


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionManager,
        events: EventBus,
        confirmer=None,
        max_result_chars: int = 8000,
        before_execute=None,
        state_listener=None,
        event_context=None,
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

    def set_task_provider(self, provider=None) -> None:
        """Register a callable returning the authoritative task id.

        Set by the Agent. Optional: with no provider, ``task``-scoped rules never
        match, which is the safe direction - they cannot widen to global.
        """
        self._task_provider = provider

    def set_confirmer(self, confirmer=None) -> None:
        self._confirmer = confirmer

    def _task_id(self) -> str:
        """The authoritative task id for the call about to be evaluated.

        Supplied by the Agent via :meth:`set_task_provider`, so permission
        evaluation can match a ``task``-scoped rule without the task id ever
        travelling in tool arguments.
        """
        provider = self._task_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "")
        except Exception:  # noqa: BLE001 - a task-id probe must never block a call
            return ""

    _RESULT_TAIL = 600

    def _trim(self, text: str) -> str:
        from ai.tokenizer import head_tail
        return head_tail(text or "", self._max_result_chars, self._RESULT_TAIL)

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

    def _finish(self, outcome: ExecutionOutcome, start: float) -> None:
        outcome.duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "tool=%s status=%s executed=%s success=%s duration_ms=%s reason=%s",
            outcome.tool,
            outcome.status,
            outcome.executed,
            outcome.success,
            outcome.duration_ms,
            outcome.reason or "-",
        )

    def _emit_completed(self, outcome: ExecutionOutcome) -> None:
        payload = {
            **self._context(),
            "tool": outcome.tool,
            "status": outcome.status,
            "allowed": outcome.allowed,
            "approved": outcome.approved,
            "executed": outcome.executed,
            "success": outcome.success,
            "cancelled": outcome.cancelled,
            "reason": outcome.reason,
            "error": outcome.error,
            "duration_ms": outcome.duration_ms,
            "exit_code": outcome.exit_code,
            "verification": getattr(outcome.verification, "state", None),
        }
        self._emit("tool_completed", payload)
        self._events.emit_tool(outcome.tool, outcome.status, outcome.display)

    def run(
        self,
        tool: str,
        args: dict,
        cancellation_token: CancellationToken | object | None = None,
    ) -> ExecutionOutcome:
        start = time.perf_counter()
        outcome = ExecutionOutcome(tool=tool)
        spec = self._registry.get(tool) if isinstance(tool, str) else None
        if spec is None:
            outcome.status = "error"
            outcome.reason = f"Unknown tool: {tool}"
            outcome.error = outcome.reason
            self._finish(outcome, start)
            self._emit_completed(outcome)
            return outcome
        if not isinstance(args, dict):
            outcome.status = "error"
            outcome.reason = f"Arguments for {tool} must be an object."
            outcome.error = outcome.reason
            self._finish(outcome, start)
            self._emit_completed(outcome)
            return outcome
        if cancellation_requested(cancellation_token):
            outcome.status = "cancelled"
            outcome.cancelled = True
            outcome.reason = "Cancelled by request."
            self._finish(outcome, start)
            self._emit_completed(outcome)
            return outcome

        try:
            # The task id comes from the executor's own authoritative source, not
            # from ``args``, so a task-scoped rule is matched against the real
            # task and the model cannot steer which task it is judged under.
            decision = self._permissions.evaluate(
                tool, args, spec.level, spec.level_for, self._task_id())
        except Exception as exc:
            outcome.status = "error"
            outcome.reason = f"Permission evaluation failed: {exc}"
            outcome.error = outcome.reason
            self._finish(outcome, start)
            self._emit_completed(outcome)
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
            self._finish(outcome, start)
            self._emit_completed(outcome)
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
                self._finish(outcome, start)
                self._emit_completed(outcome)
                return outcome
            try:
                choice = self._confirmer(tool, spec.describe_args(args))
            except Exception as exc:
                outcome.status = "error"
                outcome.error = f"Confirmation failed: {exc}"
                outcome.reason = outcome.error
                self._finish(outcome, start)
                self._emit_completed(outcome)
                return outcome
            if choice == "deny":
                outcome.cancelled = True
                outcome.status = "cancelled"
                outcome.reason = "Denied by user."
                self._finish(outcome, start)
                self._emit_completed(outcome)
                return outcome
            if choice == "block":
                self._permissions.session_block(tool)
                outcome.status = "blocked"
                outcome.reason = "Blocked by user (session)."
                self._finish(outcome, start)
                self._emit_completed(outcome)
                return outcome
            if choice == "always":
                self._events.emit_tool(tool, "allowed_already", "approved for this execution")
            if choice not in ("allow", "always"):
                outcome.cancelled = True
                outcome.status = "cancelled"
                outcome.reason = "Cancelled because confirmation returned no valid allow decision."
                self._finish(outcome, start)
                self._emit_completed(outcome)
                return outcome
            outcome.approved = True

        if cancellation_requested(cancellation_token):
            outcome.cancelled = True
            outcome.status = "cancelled"
            outcome.reason = "Cancelled by request."
            self._finish(outcome, start)
            self._emit_completed(outcome)
            return outcome

        if callable(self._before_execute):
            try:
                self._before_execute(tool, args)
            except Exception as exc:
                outcome.status = "error"
                outcome.error = f"Pre-execution integration failed: {exc}"
                outcome.reason = outcome.error
                self._finish(outcome, start)
                self._emit_completed(outcome)
                return outcome
        if cancellation_requested(cancellation_token):
            outcome.cancelled = True
            outcome.status = "cancelled"
            outcome.reason = "Cancelled before dispatch."
            self._finish(outcome, start)
            self._emit_completed(outcome)
            return outcome
        outcome.executed = True
        outcome.status = "running"
        self._emit("tool_started", {**self._context(), "tool": tool, "status": "running"})
        self._events.emit_tool(tool, "running", "")

        try:
            call_args = dict(args)
            if spec.wants_context:
                context = self._context()
                call_args["_tool_context"] = ToolExecutionContext(
                    run_id=str(context.get("run_id") or ""),
                    task_id=str(context.get("task_id") or "") or None,
                    cancellation_token=cancellation_token,
                )
            result = spec.fn(**call_args)
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
            self._events.emit_tool(tool, "error", outcome.error)
            log.error("Tool %s raised: %s", tool, exc, exc_info=True)

        if cancellation_requested(cancellation_token):
            outcome.cancelled = True
            outcome.status = "cancelled"
            outcome.reason = "Cancelled after execution; remaining work was stopped."
        self._finish(outcome, start)
        self._emit_completed(outcome)
        return outcome
