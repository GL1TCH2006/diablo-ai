"""M2.13.1 - Autonomous Development Workflow Coordinator.

This module provides a high-level workflow for autonomous development tasks,
building on the existing architecture:

- Agent with durable ExecutionPlan
- Tool context selection (M2.13.0)
- RecoveryPolicy for bounded repair
- VerificationLedger for evidence-based verification
- Project inspection tools (project.info, project.files, project.grep)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from config.settings import Settings
from core.agent import Agent
from core.events import EventBus
from core.planner import ExecutionPlan, ExecutionPlanRecord, PlanStepRecord, PlanStep, PlanValidationError
from core.recovery import RecoveryPolicy
from core.task_manager import StepStatus, TaskState
from core.verification import VerificationResult, VerificationState
from core.verification_ledger import VerificationLedger
from memory.preferences import PreferencesStore
from tools import build_registry


class DevWorkflowPhase(str, Enum):
    """Phases of the autonomous development workflow."""
    INSPECT = "inspect"
    PLAN = "plan"
    IMPLEMENT = "implement"
    TEST = "test"
    REPAIR = "repair"
    VERIFY = "verify"
    REPORT = "report"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class DevWorkflowState:
    """Current state of the development workflow."""
    phase: DevWorkflowPhase = DevWorkflowPhase.INSPECT
    project_path: str = ""
    project_profile: dict = field(default_factory=dict)
    plan: Optional[ExecutionPlanRecord] = None
    test_command: str = ""
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    test_results: list[dict] = field(default_factory=list)
    repair_attempts: int = 0
    max_repair_attempts: int = 3
    verification_evidence: dict = field(default_factory=dict)
    final_report: str = ""
    started_at: str = ""
    completed_at: str = ""
    error: str = ""


class DevWorkflow:
    """Coordinates the autonomous development workflow.

    Uses the existing Agent architecture with durable plans, recovery,
    and verification. Does not create a parallel planning/execution system.
    """

    def __init__(
        self,
        agent: Agent,
        settings: Settings,
        max_repair_attempts: int = 3,
    ):
        self.agent = agent
        self.settings = settings
        self.max_repair_attempts = max_repair_attempts
        self.state = DevWorkflowState()
        self.state.max_repair_attempts = max_repair_attempts
        self.recovery_policy = RecoveryPolicy(
            max_retries=max_repair_attempts,
            max_escalations=max(0, int(getattr(settings, "models_max_escalations", 1))),
        )

    def run(self, user_request: str, project_path: str) -> DevWorkflowState:
        """Execute the full development workflow for a request."""
        self.state = DevWorkflowState()
        self.state.project_path = project_path
        self.state.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.state.max_repair_attempts = self.max_repair_attempts

        try:
            # Phase 1: INSPECT - Understand the project
            self._phase_inspect()

            # Phase 2: PLAN - Create a development plan
            self._phase_plan(user_request)

            # Phase 3: IMPLEMENT - Execute the plan
            self._phase_implement()

            # Phase 4: TEST - Run project tests
            self._phase_test()

            # Phase 5: REPAIR - If tests fail, attempt bounded repair
            while self.state.phase == DevWorkflowPhase.REPAIR and self.state.repair_attempts < self.max_repair_attempts:
                self._phase_repair()
                if self.state.phase != DevWorkflowPhase.REPAIR:
                    break
                self._phase_test()

            # Phase 6: VERIFY - Independent verification
            self._phase_verify()

            # Phase 7: REPORT - Generate final report
            self._phase_report()

        except Exception as exc:
            self.state.phase = DevWorkflowPhase.FAILED
            self.state.error = str(exc)
            self.state.completed_at = time.strftime("%Y%m%d-%H%M%S")

        return self.state

    def _phase_inspect(self) -> None:
        """Phase 1: Inspect the project to understand its structure."""
        self.state.phase = DevWorkflowPhase.INSPECT
        self.agent._emit_lifecycle("dev_workflow_phase", phase="inspect", project=self.state.project_path)

        # Use existing project.info tool via Agent
        result = self.agent._executor.run("project.info", {"path": self.state.project_path}, None)
        if result.success:
            self.state.project_profile = {"info": result.output}
        else:
            self.state.project_profile = {"info": result.error}

        # List project files
        result = self.agent._executor.run("project.files", {"root": self.state.project_path}, None)
        if result.success:
            self.state.project_profile["files"] = result.output

        # Detect test command
        from coding.commands import detect_commands
        try:
            commands = detect_commands(self.settings.base_dir / self.state.project_path)
            if commands.test.exists:
                self.state.test_command = commands.test.command
        except Exception:
            pass

    def _phase_plan(self, user_request: str) -> None:
        """Phase 2: Create a structured development plan using the Agent's planner."""
        self.state.phase = DevWorkflowPhase.PLAN
        self.agent._emit_lifecycle("dev_workflow_phase", phase="plan")

        # Use the Agent's LLM loop to generate a plan
        # The Agent's _llm_loop already handles tool selection and plan creation
        planning_prompt = f"""
Project: {self.state.project_path}
Profile: {self.state.project_profile.get('info', 'Unknown')[:500]}
Test command: {self.state.test_command or 'Not detected'}

Request: {user_request}

Create a step-by-step development plan using available tools.
Focus on: inspect -> implement -> test -> verify
"""
        # This will trigger _llm_loop which creates an ExecutionPlan
        self.agent.respond(planning_prompt)
        self.state.plan = self.agent._active_plan

    def _phase_implement(self) -> None:
        """Phase 3: Execute the development plan."""
        self.state.phase = DevWorkflowPhase.IMPLEMENT
        self.agent._emit_lifecycle("dev_workflow_phase", phase="implement")

        if self.state.plan is None:
            self.state.phase = DevWorkflowPhase.FAILED
            self.state.error = "No plan created"
            return

        # The Agent's _llm_loop already handles plan execution
        # with recovery and verification built in
        # We just need to track what files were created/modified
        for step in self.state.plan.steps:
            if step.status == StepStatus.DONE:
                if step.tool.startswith("filesystem.write_file"):
                    path = step.args.get("path", "")
                    if path:
                        self.state.files_created.append(path)
                elif step.tool == "code.patch_file":
                    # Track modified files from patch
                    pass

    def _phase_test(self) -> None:
        """Phase 4: Run the project's test suite."""
        self.state.phase = DevWorkflowPhase.TEST
        self.agent._emit_lifecycle("dev_workflow_phase", phase="test")

        if not self.state.test_command:
            # Try to run project.verify
            result = self.agent._executor.run("project.verify", {"path": self.state.project_path}, None)
            verification_passed = (
                result.success
                and result.verification is not None
                and result.verification.state == VerificationState.PASSED
            )
            self.state.test_results.append({
                "command": "project.verify",
                "success": verification_passed,
                "output": result.output,
                "verification": result.verification.state.value if result.verification else "unknown",
            })
            if verification_passed:
                self.state.phase = DevWorkflowPhase.VERIFY
                return
            self.state.test_results[-1]["error"] = result.error or "Verification did not pass"
            self.state.phase = DevWorkflowPhase.REPAIR
            return

        # Run the detected test command via terminal.execute
        result = self.agent._executor.run("terminal.execute", {
            "command": self.state.test_command,
            "working_dir": self.state.project_path,
        }, None)

        test_passed = result.success and result.exit_code == 0
        self.state.test_results.append({
            "command": self.state.test_command,
            "success": test_passed,
            "output": result.output,
            "error": result.error,
            "exit_code": result.exit_code,
        })

        if test_passed:
            self.state.phase = DevWorkflowPhase.VERIFY
        else:
            self.state.phase = DevWorkflowPhase.REPAIR

    def _phase_repair(self) -> None:
        """Phase 5: Attempt bounded repair using RecoveryPolicy."""
        self.state.phase = DevWorkflowPhase.REPAIR
        self.state.repair_attempts += 1
        self.agent._emit_lifecycle("dev_workflow_phase", phase="repair", attempt=self.state.repair_attempts)

        # Use the Agent's built-in recovery mechanism
        # The Agent's _llm_loop already has recovery built in via _apply_recovery_policy
        # We just need to trigger a new LLM turn with the failure context
        failure_context = self._build_failure_context()

        repair_prompt = f"""
Previous attempt failed. Analyze the failure and propose a fix.

Project: {self.state.project_path}
Test command: {self.state.test_command}
Last test results: {self.state.test_results[-1] if self.state.test_results else 'None'}
Failure context: {failure_context}

Repair attempt {self.state.repair_attempts} of {self.max_repair_attempts}.
Focus on fixing the specific failure.
"""
        self.agent.respond(repair_prompt)

        # Check if repair produced a new plan
        if self.agent._active_plan:
            self.state.plan = self.agent._active_plan

    def _phase_verify(self) -> None:
        """Phase 6: Independent verification using VerificationLedger."""
        self.state.phase = DevWorkflowPhase.VERIFY
        self.agent._emit_lifecycle("dev_workflow_phase", phase="verify")

        # Run project.verify again for independent evidence
        result = self.agent._executor.run("project.verify", {"path": self.state.project_path}, None)

        if result.verification:
            self.state.verification_evidence = dict(result.verification.evidence or {})
            if result.verification.state == VerificationState.PASSED:
                self.state.completed_at = time.strftime("%Y%m%d-%H%M%S")
                self.state.phase = DevWorkflowPhase.REPORT
            else:
                self.state.verification_evidence = dict(result.verification.evidence)
                self.state.phase = DevWorkflowPhase.REPORT  # Report even if not passed

    def _phase_report(self) -> None:
        """Phase 7: Generate final development report."""
        self.state.phase = DevWorkflowPhase.REPORT
        self.agent._emit_lifecycle("dev_workflow_phase", phase="report")

        report = self._generate_report()
        self.state.final_report = report
        self.state.completed_at = time.strftime("%Y%m%d-%H%M%S")
        self.state.phase = DevWorkflowPhase.COMPLETE

    def _build_failure_context(self) -> str:
        """Build a concise failure context for repair."""
        if not self.state.test_results:
            return "No test results available"
        last = self.state.test_results[-1]
        parts = []
        if last.get("error"):
            parts.append(f"Error: {last['error'][:500]}")
        if last.get("output"):
            parts.append(f"Output: {last['output'][:500]}")
        if last.get("exit_code") is not None:
            parts.append(f"Exit code: {last['exit_code']}")
        return "\n".join(parts) if parts else "Unknown failure"

    def _generate_report(self) -> str:
        """Generate a concise structured final report."""
        lines = [
            "=== DEVELOPMENT WORKFLOW REPORT ===",
            f"Project: {self.state.project_path}",
            f"Started: {self.state.started_at}",
            f"Completed: {self.state.completed_at}",
            f"Final Phase: {self.state.phase.value}",
            f"Repair Attempts: {self.state.repair_attempts}",
            "",
            "--- Project Profile ---",
            self.state.project_profile.get("info", "Unknown")[:1000],
            "",
            "--- Files Created ---",
        ]
        for f in self.state.files_created:
            lines.append(f"  + {f}")
        if not self.state.files_created:
            lines.append("  (none)")

        lines.extend(["", "--- Test Results ---"])
        for tr in self.state.test_results:
            status = "PASS" if tr.get("success") else "FAIL"
            lines.append(f"  [{status}] {tr.get('command', 'unknown')}")
            if tr.get("error"):
                lines.append(f"    Error: {tr['error'][:200]}")

        lines.extend(["", "--- Verification ---"])
        if self.state.verification_evidence:
            lines.append(f"  Evidence: {self.state.verification_evidence}")
        else:
            lines.append("  No independent verification evidence")

        if self.state.error:
            lines.extend(["", f"--- Error ---", self.state.error])

        return "\n".join(lines)


def create_dev_workflow(agent: Agent, settings: Settings, max_repair_attempts: int = 3) -> DevWorkflow:
    """Factory function to create a DevWorkflow instance."""
    return DevWorkflow(agent, settings, max_repair_attempts)