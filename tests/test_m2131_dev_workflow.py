"""M2.13.1 - Development workflow tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from config.permissions import PermissionManager
from config.settings import Settings
from core.agent import Agent
from core.dev_workflow import DevWorkflow, DevWorkflowPhase
from core.events import EventBus
from core.planner import ExecutionPlan, ExecutionPlanRecord, PlanStep, PlanStepRecord, StepStatus
from core.task_manager import StepStatus
from core.verification import VerificationResult, VerificationState
from tools.registry import ToolResult
from memory.preferences import PreferencesStore
from tools import build_registry


def _settings(tmp: Path) -> Settings:
    data = tmp / "data"
    return Settings(
        base_dir=tmp,
        ai_provider="ollama",
        ollama_host="http://x",
        ai_model="",
        workspace_root=tmp / "p",
        data_dir=data,
        memory_dir=data / "memory",
        logs_dir=data / "logs",
        backups_dir=data / "backups",
        scratch_dir=tmp / "w",
        sessions_dir=data / "sessions",
        voice_enabled=False,
        log_level="INFO",
        allowed_roots=[tmp],
        raw={},
    )


def _agent(tmp: Path, provider_script=None):
    """Create an agent with a scripted provider for testing."""
    from types import SimpleNamespace

    def _response(*calls, text=""):
        return SimpleNamespace(tool_calls=list(calls), text=text)

    def _call(name, **kwargs):
        return SimpleNamespace(name=name, arguments=kwargs, id=f"c{name}-{len(kwargs)}")

    class ScriptedProvider:
        def __init__(self, script):
            self.script = list(script)
            self.model_label = "test"
            self.name = "test"

        def chat(self, messages, tools=None, model=None):
            if self.script:
                return self.script.pop(0)
            return _response(text="done")

    s = _settings(tmp)
    prefs = PreferencesStore(s.memory_dir)
    return Agent(
        provider=ScriptedProvider(provider_script or []),
        settings=s,
        registry=build_registry(s),
        permissions=PermissionManager(s, prefs),
        events=EventBus(),
        confirmer=None,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )


class TestDevWorkflow:
    """Tests for the autonomous development workflow."""

    def test_workflow_phases_exist(self):
        """All expected phases are defined."""
        assert DevWorkflowPhase.INSPECT
        assert DevWorkflowPhase.PLAN
        assert DevWorkflowPhase.IMPLEMENT
        assert DevWorkflowPhase.TEST
        assert DevWorkflowPhase.REPAIR
        assert DevWorkflowPhase.VERIFY
        assert DevWorkflowPhase.REPORT
        assert DevWorkflowPhase.COMPLETE
        assert DevWorkflowPhase.FAILED

    def test_workflow_state_initialization(self):
        """DevWorkflowState initializes with correct defaults."""
        from core.dev_workflow import DevWorkflowState

        state = DevWorkflowState()
        assert state.phase == DevWorkflowPhase.INSPECT
        assert state.project_path == ""
        assert state.max_repair_attempts == 3
        assert state.repair_attempts == 0
        assert state.files_created == []
        assert state.files_modified == []
        assert state.test_results == []
        assert state.verification_evidence == {}
        assert state.error == ""

    def test_create_dev_workflow(self, tmp_path: Path):
        """Factory creates a DevWorkflow instance."""
        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path))
        assert workflow is not None
        assert workflow.max_repair_attempts == 3

    def test_custom_max_repair_attempts(self, tmp_path: Path):
        """Workflow respects custom max_repair_attempts."""
        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path), max_repair_attempts=5)
        assert workflow.max_repair_attempts == 5
        assert workflow.state.max_repair_attempts == 5

    def test_phase_inspect_runs(self, tmp_path: Path):
        """Inspect phase runs project.info and project.files."""
        project_root = tmp_path / "test_project"
        project_root.mkdir()
        (project_root / "main.py").write_text("print('hello')\n")

        agent = _agent(tmp_path, [
            ToolResult(success=True, output="Project: test (python)", error=None),
            ToolResult(success=True, output="main.py", error=None),
        ])
        workflow = DevWorkflow(agent, _settings(tmp_path))
        workflow.state.project_path = str(project_root)

        workflow._phase_inspect()

        assert "Project: test" in workflow.state.project_profile.get("info", "")
        assert "main.py" in workflow.state.project_profile.get("files", "")

    def test_phase_test_with_project_verify_pass(self, tmp_path: Path):
        """Test phase runs project.verify and succeeds."""
        # Create a minimal Python project with a passing test
        project_root = tmp_path / "test_proj"
        project_root.mkdir()
        (project_root / "test_foo.py").write_text("def test_pass():\n    assert True\n")

        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path))
        workflow.state.project_path = str(project_root)

        workflow._phase_test()

        # project.verify should run and pass
        assert workflow.state.phase == DevWorkflowPhase.VERIFY
        assert len(workflow.state.test_results) == 1
        assert workflow.state.test_results[0]["success"] is True

    def test_phase_test_with_project_verify_fail_triggers_repair(self, tmp_path: Path):
        """Test phase triggers repair when project.verify fails."""
        from core.verification import VerificationResult, VerificationState

        agent = _agent(tmp_path, [
            ToolResult(
                success=True,
                output="2 failed",
                error="test_foo failed",
                verification=VerificationResult(VerificationState.FAILED, "failed", "project_tests", {"failed": 2}),
            ),
        ])
        workflow = DevWorkflow(agent, _settings(tmp_path))
        workflow.state.project_path = "/fake/path"

        workflow._phase_test()

        assert workflow.state.phase == DevWorkflowPhase.REPAIR
        assert len(workflow.state.test_results) == 1
        assert workflow.state.test_results[0]["success"] is False

    def test_phase_verify_runs_independent_verification(self, tmp_path: Path):
        """Verify phase runs independent project.verify."""
        # Create a minimal Python project with a passing test
        project_root = tmp_path / "test_proj"
        project_root.mkdir()
        (project_root / "test_foo.py").write_text("def test_pass():\n    assert True\n")

        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path))
        workflow.state.project_path = str(project_root)

        workflow._phase_verify()

        assert workflow.state.verification_evidence
        assert "tests_passed" in workflow.state.verification_evidence
        assert workflow.state.verification_evidence["tests_passed"] >= 1

    def test_generate_report(self, tmp_path: Path):
        """Report generation produces structured output."""
        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path))
        workflow.state.project_path = "/test/project"
        workflow.state.started_at = "20240101-120000"
        workflow.state.completed_at = "20240101-123000"
        workflow.state.phase = DevWorkflowPhase.COMPLETE
        workflow.state.files_created = ["main.py", "test_main.py"]
        workflow.state.test_results = [
            {"command": "pytest", "success": True, "output": "5 passed"},
        ]
        workflow.state.verification_evidence = {"tests_passed": 5}

        report = workflow._generate_report()

        assert "DEVELOPMENT WORKFLOW REPORT" in report
        assert "/test/project" in report
        assert "main.py" in report
        assert "test_main.py" in report
        assert "PASS" in report
        assert "verification" in report.lower()

    def test_build_failure_context(self, tmp_path: Path):
        """Failure context builder extracts key info."""
        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path))
        workflow.state.test_results = [
            {"command": "pytest", "success": False, "error": "AssertionError: expected 5 got 4", "exit_code": 1},
        ]

        context = workflow._build_failure_context()
        assert "AssertionError" in context
        assert "expected 5 got 4" in context

    def test_workflow_run_creates_report(self, tmp_path: Path):
        """Full workflow run produces a report."""
        project_root = tmp_path / "test_project"
        project_root.mkdir()
        (project_root / "main.py").write_text("print('hello')\n")

        agent = _agent(tmp_path, [
            ToolResult(success=True, output="Project: test (python)", error=None),
            ToolResult(success=True, output="main.py", error=None),
            type("R", (), {"tool_calls": [], "text": "Plan created"})(),
            type("R", (), {"tool_calls": [], "text": "Implementation done"})(),
            ToolResult(
                success=True, output="1 passed",
                verification=type("V", (), {
                    "state": type("S", (), {"value": "passed"})(),
                    "evidence": {"tests_passed": 1}
                })(),
            ),
            ToolResult(
                success=True, output="1 passed",
                verification=type("V", (), {
                    "state": type("S", (), {"value": "passed"})(),
                    "evidence": {"tests_passed": 1}
                })(),
            ),
        ])
        workflow = DevWorkflow(agent, _settings(tmp_path), max_repair_attempts=2)

        result = workflow.run("Add a hello world function", str(project_root))

        assert result.phase in (DevWorkflowPhase.COMPLETE, DevWorkflowPhase.FAILED)
        assert result.final_report
        assert "DEVELOPMENT WORKFLOW REPORT" in result.final_report


class TestDevWorkflowIntegration:
    """Integration tests with real-ish components."""

    def test_workflow_uses_existing_architecture(self, tmp_path: Path):
        """Workflow uses Agent, Executor, RecoveryPolicy, VerificationLedger."""
        agent = _agent(tmp_path)
        workflow = DevWorkflow(agent, _settings(tmp_path))

        # Check all components are wired
        assert workflow.agent is agent
        assert workflow.recovery_policy is not None
        assert hasattr(workflow.agent, "_executor")
        assert hasattr(workflow.agent, "_verifier")
        assert hasattr(workflow.agent, "_tasks")
        assert hasattr(workflow.agent._tasks, "ledger")