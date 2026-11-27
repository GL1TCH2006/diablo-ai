"""M3.1 — PlannerAgent: extracted planner logic with clear execution boundary."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from config.settings import Settings
from core.cancellation import cancellation_requested
from core.events import EventBus
from core.planner import (
    ExecutionPlan,
    ExecutionPlanRecord,
    Intent,
    Planner,
    PLAN_SCHEMA_VERSION,
    _now,
)
from core.request_context import RequestContext
from core.task_manager import StepStatus
from tools.registry import ToolRegistry

log = None  # Lazy import to avoid circular imports


def _get_logger():
    global log
    if log is None:
        from core.logging_setup import get_logger
        log = get_logger("planner_agent")
    return log


@dataclass
class ValidationResult:
    """Result of plan validation."""
    valid: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class ReplanResult:
    """Result of a replanning operation."""
    plan: "ExecutionPlan"
    replanned: bool
    reason: str
    retry_count: int = 0


class PlannerAgent:
    """Extracted planner logic with clear execution boundary.

    Separates planning concerns from execution, enabling independent testing
    and clearer separation of concerns per M3.1 architecture.
    """

    def __init__(
        self,
        settings: Settings,
        router: Any,  # ModelRouter
        events: EventBus,
        registry: ToolRegistry,
    ):
        self._settings = settings
        self._router = router
        self._events = events
        self._registry = registry
        self._planner = Planner(settings)

    @property
    def planner(self) -> Planner:
        return self._planner

    @property
    def router(self) -> Any:
        return self._router

    def create_plan(self, goal: str, context: RequestContext) -> "ExecutionPlan":
        """Create an execution plan from a user goal.

        Args:
            goal: The user's goal or request
            context: Request context with task_id, cancellation_token, etc.

        Returns:
            Validated ExecutionPlan ready for execution
        """
        # Check cancellation before planning
        if cancellation_requested(context.cancellation_token):
            return ExecutionPlan(goal=goal, steps=())

        # Try fast intent first
        intent = self._planner.fast_intent(goal)
        if intent is None:
            # Check for coding intent
            coding_intent = self._planner.coding_intent(goal)
            if coding_intent:
                intent = self._planner.fast_intent(f"{coding_intent}: {goal}")

        if intent is None:
            # No fast intent matched, return empty plan for LLM to handle
            return ExecutionPlan(goal=goal, steps=())

        # Build plan from intent
        plan = ExecutionPlan.from_intent(intent, goal=goal or intent.summary)

        # Annotate with expected results for verification
        plan = self._annotate_intent_expectations(plan, intent)

        # Validate against registry
        validation = self.validate_plan(plan)
        if not validation.valid:
            _get_logger().warning("Plan validation failed: %s", validation.errors)
            # Return empty plan to let LLM handle it
            return ExecutionPlan(goal=goal, steps=())

        return plan

    def _annotate_intent_expectations(self, plan: "ExecutionPlan", intent: Intent) -> "ExecutionPlan":
        """Populate expected_result where the fast-intent contract knows it.

        Returns a new ExecutionPlan (never mutates the frozen plan in place).
        """
        name = getattr(intent, "name", "")
        new_steps = []
        for step in plan.steps:
            if name == "create_hello_python" and step.tool == "python.run_script":
                new_steps.append(dataclasses.replace(step, expected_result={"type": "output_contains", "value": "Hello World"}))
            elif name == "open_app" and step.tool == "applications.open":
                new_steps.append(dataclasses.replace(step, expected_result={"type": "output_contains", "value": "opened"}))
            else:
                new_steps.append(step)
        return dataclasses.replace(plan, steps=tuple(new_steps))

    def validate_plan(self, plan: "ExecutionPlan") -> "ValidationResult":
        """Validate an execution plan against the tool registry.

        Checks:
        - All tools exist in registry
        - Required arguments present
        - Argument types match schema
        - No dependency cycles
        - All dependencies exist
        """
        errors = []

        if not plan.goal or not plan.goal.strip():
            errors.append("plan goal must be a non-empty string")

        if not plan.steps:
            errors.append("plan must contain at least one step")

        known_ids: set[str] = set()
        for index, step in enumerate(plan.steps):
            if not isinstance(step.step_id, str) or not step.step_id.strip():
                errors.append(f"step {index + 1} has an invalid id")
                continue
            if step.step_id in known_ids:
                errors.append(f"duplicate plan step id: {step.step_id}")
                continue
            known_ids.add(step.step_id)

            if not isinstance(step.tool, str) or not step.tool.strip():
                errors.append(f"step {step.step_id} has an invalid tool")
                continue

            if not isinstance(step.args, dict):
                errors.append(f"step {step.step_id} arguments must be an object")
                continue

            spec = self._registry.get(step.tool)
            if spec is None:
                errors.append(f"step {step.step_id} references unknown tool {step.tool}")
                continue

            missing = [name for name in (spec.required or []) if name not in step.args]
            if missing:
                errors.append(f"step {step.step_id} is missing required arguments: {', '.join(missing)}")

            properties = (spec.parameters or {}).get("properties", {}) or {}
            for name, value in step.args.items():
                schema = properties.get(name)
                if isinstance(schema, dict) and "type" in schema:
                    if not self._matches_type(value, schema["type"]):
                        errors.append(f"step {step.step_id} argument {name!r} must be {schema['type']}")

            if callable(spec.validate_args):
                try:
                    spec.validate_args(step.args)
                except Exception as exc:
                    errors.append(f"step {step.step_id} arguments are invalid: {exc}")

            dependencies = step.depends_on or ()
            if not isinstance(dependencies, (tuple, list)):
                errors.append(f"step {step.step_id} dependencies must be a sequence")
            else:
                for dependency in dependencies:
                    if dependency == step.step_id:
                        errors.append(f"step {step.step_id} cannot depend on itself")
                    elif dependency not in known_ids:
                        errors.append(f"step {step.step_id} has unknown dependency {dependency}")

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def _matches_type(self, value: Any, expected: str) -> bool:
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "null":
            return value is None
        return True

    def validate_plan_record(self, plan: "ExecutionPlanRecord") -> "ValidationResult":
        """Validate an ExecutionPlanRecord."""
        return self.validate_plan(self._plan_record_to_plan(plan))

    def _plan_record_to_plan(self, record) -> "ExecutionPlan":
        from core.planner import ExecutionPlan, PlanStep
        steps = []
        for item in record.steps:
            steps.append(PlanStep(
                step_id=item.step_id,
                tool=item.tool,
                args=item.args,
                depends_on=item.depends_on,
                expected_result=item.expected_result,
                recovery_hint=item.recovery_hint,
            ))
        return ExecutionPlan(goal=record.goal, steps=tuple(steps))

    def replan(
        self,
        plan,
        failed_step_id: str,
        reason: str,
        context: Optional[RequestContext] = None,
    ) -> "ExecutionPlan":
        """Generate a revised plan after a step failure.

        Args:
            plan: Current ExecutionPlanRecord
            failed_step_id: ID of the step that failed
            reason: Reason for failure
            context: Optional request context for cancellation checks

        Returns:
            Revised ExecutionPlan with recovery steps
        """
        from core.planner import ExecutionPlan, PlanStep, StepStatus
        from core.planner import ExecutionPlanRecord

        # Check cancellation
        if context and cancellation_requested(context.cancellation_token):
            from core.planner import ExecutionPlan
            return ExecutionPlan(goal=plan.goal, steps=())

        # Get the failed step
        failed_step = plan.step(failed_step_id)
        if not failed_step:
            _get_logger().warning("Replan requested for unknown step: %s", failed_step_id)
            return self._plan_record_to_plan(plan)

        # Check if step is retryable
        if failed_step.status not in (StepStatus.FAILED,):
            _get_logger().warning("Replan requested for non-failed step: %s (%s)", failed_step_id, failed_step.status.value)
            return self._plan_record_to_plan(plan)

        # Check retry budget
        # For now, delegate to recovery policy - just return original plan
        # Full replanning logic would go here in M3.2+
        _get_logger().info("Replan requested for step %s: %s", failed_step_id, reason)
        return self._plan_record_to_plan(plan)

    def annotate_intent_expectations(self, plan: "ExecutionPlanRecord") -> None:
        """Populate expected_result where the fast-intent contract knows it."""
        # This is a no-op for now - expectations are set at plan creation
        pass


class ValidationResult:
    def __init__(self, valid: bool, errors: list[str] = None):
        self.valid = valid
        self.errors = errors or []