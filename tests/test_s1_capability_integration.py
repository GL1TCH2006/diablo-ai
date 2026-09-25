from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.provider import AIProvider, AIResponse, ToolCall
from config.permissions import PermissionLevel, PermissionManager
from core.agent import Agent
from core.events import EventBus
from core.executor import Executor
from core.task_store import TaskStore
from core.verification import VerificationState
from integration import (
    BlenderCapabilityAdapter,
    BlenderVisionCapabilityAdapter,
    BlenderVisionWorkflowCapabilityAdapter,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityStatus,
    IntegrationResult,
    MockCapability,
    VisionCapabilityAdapter,
)
from main import build_capability_runtime, close_capability_runtime
from memory.rule_engine import RuleEngine
from memory.rules_store import RuleStore
from memory.preferences import PreferencesStore
from tools import build_registry
from tools.capability_bridge import build_capability_tool
from tools.registry import ToolRegistry


class _Provider(AIProvider):
    name = "capability-scripted"
    model_label = "capability-scripted/local"
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def is_available(self):
        return True

    def list_models(self):
        return []

    def chat(self, messages, tools=None, model=None):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return AIResponse(text="done", model=model)


class _Token:
    def __init__(self):
        self.cancelled = False

    def is_cancelled(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True


class _Capability:
    name = "test"

    def __init__(self, operations=("execute", "create_widget", "render_and_inspect")):
        self.operations = set(operations)
        self.requests = []

    def capabilities(self):
        return {"name": self.name, "available": True, "operations": sorted(self.operations)}

    def can_execute(self, operation):
        return operation in self.operations

    def execute(self, request):
        self.requests.append(request)
        if request.operation == "cancel_inside":
            request.cancellation_token.cancel()
            request.check_cancelled()
        if request.operation == "cancel_twice":
            request.cancellation_token.cancel()
            request.cancellation_token.cancel()
            request.check_cancelled()
        if request.operation == "cancel_after":
            request.cancellation_token.cancel()
        if request.operation == "render_and_inspect":
            return CapabilityResult(
                status=CapabilityStatus.SUCCESS,
                capability=request.capability,
                operation=request.operation,
                data={"render": {"path": str(request.params["path"])}, "valid": True, "inspection": {"valid": True}},
                request_id=request.request_id,
                task_id=request.task_id,
            )
        return CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability=request.capability,
            operation=request.operation,
            data={"executed": True, "operation": request.operation},
            request_id=request.request_id,
            task_id=request.task_id,
        )

    def cancel(self, task_id=""):
        return True


class _StatusCapability(_Capability):
    def __init__(self, status):
        super().__init__(operations=("execute",))
        self.status = status

    def execute(self, request):
        self.requests.append(request)
        return CapabilityResult(
            status=self.status,
            capability=request.capability,
            operation=request.operation,
            error=None if self.status is CapabilityStatus.SUCCESS else {"message": self.status.value},
            request_id=request.request_id,
            task_id=request.task_id,
        )


class _WorkflowCapability(_Capability):
    name = "blender_vision_workflow"

    def __init__(self, malformed=False):
        super().__init__(operations=("run", "analyze_reference", "compare_reference"))
        self.malformed = malformed
        self.context = None

    def execute(self, request):
        self.requests.append(request)
        self.context = dict(request.context)
        embedded_context = request.params.get("context")
        if isinstance(embedded_context, dict):
            self.context.update(embedded_context)
        path = str(request.params.get("path") or request.params.get("reference_path") or "")
        if request.operation == "analyze_reference":
            data = {
                "reference_path": path,
                "backend": "fallback",
                "evidence": {"kind": "deterministic", "semantic": False},
            }
        elif request.operation == "compare_reference":
            data = {
                "render_path": path,
                "reference_path": str(request.params.get("reference_path") or ""),
                "verified": True,
                "comparison": {"identical": True, "diff_ratio": 0.0},
            }
        else:
            data = {
                "path": path,
                "verified": True,
                "iterations": 1,
                "attempts": [{"iteration": 1, "valid": True, "render_path": path}],
                "render": {"path": path, "valid": True},
                "state": {"object_count": 1},
            }
            if self.malformed:
                data.pop("attempts")
        return CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability=request.capability,
            operation=request.operation,
            data=data,
            request_id=request.request_id,
            task_id=request.task_id,
        )


class _Blender:
    def __init__(self):
        self.calls = []

    def detect(self):
        return SimpleNamespace(installed=True)

    def inspect_scene(self):
        self.calls.append(("inspect_scene",))
        return {"objects": [], "camera": None}

    def list_objects(self, obj_type=""):
        self.calls.append(("list_objects", obj_type))
        return {"count": 0, "objects": []}

    def inspect_object(self, name):
        self.calls.append(("inspect_object", name))
        return {"name": name, "type": "MESH"}

    def create_object(self, primitive="cube", name=None, location=None, rotation=None, scale=None, **extra):
        self.calls.append(("create_object", primitive, name))
        return {"name": name or primitive, "type": "MESH"}

    def transform_object(self, name, location=None, rotation=None, scale=None):
        self.calls.append(("transform_object", name))
        return {"name": name, "location": location}

    def render(self, path, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"render")
        self.calls.append(("render", str(target)))
        return {"path": str(target), "exists": True}

    def save(self, path, fmt=None):
        self.calls.append(("save", str(path)))
        return {"path": str(path), "saved": True}

    def close(self):
        return None


class _Vision:
    def inspect_render(self, image_path, context=None):
        return {"path": str(image_path), "valid": True, "checks": {"dimensions": True}}

    def validate_image(self, image_path):
        return {"path": str(image_path), "readable": True, "width": 128, "height": 128}

    def inspect(self, image_path):
        return {"path": str(image_path), "valid": True, "width": 128, "height": 128}

    def describe(self, image_path, detail_level="medium", focus=None, context=None):
        return {"path": str(image_path), "description": "test image", "backend": "test"}

    def compare(self, image_path, reference_image, threshold=0.01, context=None):
        return {"path": str(image_path), "similar": True, "difference": 0.0}

    def last_error(self):
        return None


class _Integration:
    def render_and_inspect(self, path, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"render")
        return IntegrationResult.success_result(
            "render_and_inspect",
            {"render": {"path": str(target)}, "render_path": str(target), "valid": True, "inspection": {"valid": True}},
        )

    def last_error(self):
        return None

    def close(self):
        return None


def _response(*calls, text=""):
    return AIResponse(text=text, tool_calls=list(calls))


def _call(name, **args):
    return ToolCall(name=name, arguments=args)


def _tool_registry(capability_registry, settings, bus=None):
    registry = ToolRegistry()
    registry.register(build_capability_tool(capability_registry, bus, settings.allowed_roots))
    return registry


def _executor(capability_registry, settings, confirmer=None, bus=None, context=None):
    bus = bus or EventBus()
    confirmer = confirmer or (lambda *_: "allow")
    registry = _tool_registry(capability_registry, settings, bus)
    permissions = PermissionManager(settings)
    return Executor(
        registry,
        permissions,
        bus,
        confirmer=confirmer,
        event_context=(lambda: context) if context else None,
    )


def _agent(capability_registry, settings, provider, confirmer=None, bus=None):
    settings.sessions_dir = settings.data_dir / "capability-sessions"
    settings.memory_dir.mkdir(parents=True, exist_ok=True)
    bus = bus or EventBus()
    confirmer = confirmer or (lambda *_: "allow")
    registry = _tool_registry(capability_registry, settings, bus)
    preferences = PreferencesStore(settings.memory_dir)
    permissions = PermissionManager(settings, preferences)
    agent = Agent(
        provider=provider,
        settings=settings,
        registry=registry,
        permissions=permissions,
        events=bus,
        confirmer=confirmer,
        memory_preferences=preferences,
    )
    return agent, permissions, bus


def _png(path):
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return path


def test_build_registry_registers_one_generic_capability_tool(tmp_settings):
    capability_registry = CapabilityRegistry()
    capability_registry.register(MockCapability())
    registry = build_registry(tmp_settings, capability_registry, EventBus())
    assert "capability.execute" in registry.names()
    assert not any(name.startswith("blender.") for name in registry.names())


def test_production_capability_runtime_registers_s28_adapter(tmp_settings):
    bus = EventBus()
    runtime = build_capability_runtime(tmp_settings, bus)
    try:
        capability_registry, integration, blender, workflow = runtime
        adapter = capability_registry.get("blender_vision_workflow")
        assert isinstance(adapter, BlenderVisionWorkflowCapabilityAdapter)
        assert adapter.can_execute("render_and_verify")
        assert adapter.workflow is workflow
        assert workflow.integration is integration
        assert blender is not None
        tools = build_registry(tmp_settings, capability_registry, bus)
        capability_tool = tools.get("capability.execute")
        assert capability_tool is not None
        assert "blender_vision_workflow" in capability_tool.description
    finally:
        close_capability_runtime(runtime[3], runtime[1], runtime[2])


def test_bridge_passes_core_context_and_structured_result(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(
        capability_registry,
        tmp_settings,
        context={"run_id": "run-1", "task_id": "task-1"},
    )
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    assert outcome.success is True
    assert outcome.data["capability_result"]["status"] == "success"
    request = capability.requests[0]
    assert request.task_id == "task-1"
    assert request.context["run_id"] == "run-1"
    assert request.context["source"] == "s1_core"
    assert json.loads(outcome.output)["operation"] == "execute"


def test_bridge_protects_core_owned_context_fields(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(
        capability_registry,
        tmp_settings,
        context={"run_id": "core-run", "task_id": "core-task"},
    )
    outcome = executor.run(
        "capability.execute",
        {
            "capability": "test",
            "operation": "execute",
            "params": {"context": {"source": "model", "run_id": "fake", "task_id": "fake", "extra": "kept"}},
        },
    )
    assert outcome.success is True
    request = capability.requests[0]
    assert request.context["source"] == "s1_core"
    assert request.context["run_id"] == "core-run"
    assert request.context["task_id"] == "core-task"
    assert request.params["context"] == {"extra": "kept"}


def test_s28_embedded_context_cannot_replace_core_provenance(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "context.png")
    capability = _WorkflowCapability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(
        capability_registry,
        tmp_settings,
        context={"run_id": "core-run", "task_id": "core-task"},
    )
    outcome = executor.run(
        "capability.execute",
        {
            "capability": "blender_vision_workflow",
            "operation": "analyze_reference",
            "params": {
                "reference_path": str(image),
                "context": {"source": "model", "run_id": "fake", "task_id": "fake", "extra": "kept"},
            },
        },
    )
    assert outcome.success is True
    assert capability.context["source"] == "s1_core"
    assert capability.context["run_id"] == "core-run"
    assert capability.context["task_id"] == "core-task"
    assert capability.context["extra"] == "kept"


def test_blender_vision_missing_positive_valid_evidence_is_unverified(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "weak-evidence.png")
    capability = _Capability(operations=("render_and_inspect",))
    capability.name = "blender_vision"
    capability.execute = lambda request: CapabilityResult(
        status=CapabilityStatus.SUCCESS,
        capability=request.capability,
        operation=request.operation,
        data={"path": str(image)},
        request_id=request.request_id,
        task_id=request.task_id,
    )
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    outcome = executor.run(
        "capability.execute",
        {"capability": "blender_vision", "operation": "render_and_inspect", "params": {"path": str(image)}},
    )
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.UNVERIFIED


def test_capability_evidence_is_promoted_to_s1_verification(tmp_settings):
    capability = MockCapability(
        result=CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability="mock",
            operation="execute",
            data={"verified": True, "evidence": {"object": "cube"}},
        )
    )
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run("capability.execute", {"capability": "mock", "operation": "execute"})
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.PASSED
    assert outcome.verification.evidence == {"object": "cube"}


def test_compare_reference_evidence_is_not_treated_as_render_attempts(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "comparison.png")
    reference = _png(tmp_settings.scratch_dir / "comparison-reference.png")
    capability = _WorkflowCapability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run(
        "capability.execute",
        {
            "capability": "blender_vision_workflow",
            "operation": "compare_reference",
            "params": {"path": str(image), "reference_path": str(reference)},
        },
    )
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.PASSED
    assert outcome.verification.evidence["comparison"]["identical"] is True


def test_contradictory_comparison_evidence_cannot_pass(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "contradictory.png")
    reference = _png(tmp_settings.scratch_dir / "contradictory-reference.png")
    capability = _WorkflowCapability()
    capability.execute = lambda request: CapabilityResult(
        status=CapabilityStatus.SUCCESS,
        capability=request.capability,
        operation=request.operation,
        data={
            "render_path": str(image),
            "reference_path": str(reference),
            "verified": True,
            "threshold": 0.01,
            "comparison": {"success": False, "identical": True, "diff_ratio": 0.9},
        },
        request_id=request.request_id,
        task_id=request.task_id,
    )
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run(
        "capability.execute",
        {
            "capability": "blender_vision_workflow",
            "operation": "compare_reference",
            "params": {"path": str(image), "reference_path": str(reference)},
        },
    )
    assert outcome.success is True
    assert outcome.verification.state != VerificationState.PASSED


def test_explicit_negative_generic_evidence_fails_verification(tmp_settings):
    capability = MockCapability(
        result=CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability="mock",
            operation="execute",
            data={"verified": False, "evidence": {"reason": "rejected"}},
        )
    )
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run("capability.execute", {"capability": "mock", "operation": "execute"})
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.FAILED


def test_capability_preference_allow_cannot_bypass_sensitive_confirmation(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    preferences = PreferencesStore(tmp_settings.memory_dir)
    preferences.set_permission("capability.execute", "allow")
    permissions = PermissionManager(tmp_settings, preferences)
    bus = EventBus()
    tools = build_registry(tmp_settings, capability_registry, bus)
    outcome = Executor(tools, permissions, bus).run(
        "capability.execute",
        {"capability": "test", "operation": "create_widget"},
    )
    assert outcome.executed is False
    assert outcome.cancelled is True
    assert capability.requests == []


def test_passed_verification_is_downgraded_by_later_unverified_success(tmp_settings):
    first = MockCapability(
        result=CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability="mock",
            operation="execute",
            data={"verified": True, "evidence": {"object": "first"}},
        )
    )
    second = _Capability()
    second.name = "unverified"
    capability_registry = CapabilityRegistry()
    capability_registry.register(first)
    capability_registry.register(second)
    provider = _Provider([
        _response(
            _call("capability.execute", capability="mock", operation="execute"),
            _call("capability.execute", capability="unverified", operation="execute"),
        ),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider)
    agent.respond("run verified then unverified capability")
    assert agent._tasks.verification == VerificationState.UNVERIFIED
    assert agent._tasks.task_succeeded() is None


def test_s28_workflow_evidence_preserves_structured_verification(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "workflow.png")
    capability = _WorkflowCapability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    outcome = executor.run(
        "capability.execute",
        {"capability": "blender_vision_workflow", "operation": "run", "params": {"path": str(image)}},
    )
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.PASSED
    assert outcome.verification.evidence["path"] == str(image)
    assert outcome.verification.evidence["iterations"] == 1
    assert outcome.verification.evidence["attempts"][0]["valid"] is True
    assert outcome.verification.evidence["state"]["object_count"] == 1


def test_s28_reference_analysis_is_unverified(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "reference.png")
    capability = _WorkflowCapability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    outcome = executor.run(
        "capability.execute",
        {"capability": "blender_vision_workflow", "operation": "analyze_reference", "params": {"reference_path": str(image)}},
    )
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.UNVERIFIED
    assert outcome.verification.kind == "capability_analysis"


def test_s28_malformed_evidence_does_not_pass(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "malformed.png")
    capability = _WorkflowCapability(malformed=True)
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    outcome = executor.run(
        "capability.execute",
        {"capability": "blender_vision_workflow", "operation": "run", "params": {"path": str(image)}},
    )
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.FAILED


def test_core_agent_persists_capability_observation(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([
        _response(_call("capability.execute", capability="test", operation="execute")),
        _response(text="capability complete"),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider)
    agent.respond("run the registered capability")
    record = TaskStore(tmp_settings.sessions_dir).get(agent._tasks.task_id)
    assert record.status == "completed"
    assert record.metadata["tool_observations"][0]["operation"] == "execute"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.UNVERIFIED.value
    assert capability.requests[0].task_id == agent._tasks.task_id


def test_success_then_later_failure_invalidates_passed_verification(tmp_settings):
    first = MockCapability(
        result=CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability="mock",
            operation="execute",
            data={"verified": True, "evidence": {"object": "first"}},
        )
    )
    second = _StatusCapability(CapabilityStatus.FAILURE)
    second.name = "failure"
    capability_registry = CapabilityRegistry()
    capability_registry.register(first)
    capability_registry.register(second)
    tmp_settings.max_recovery_attempts = 0
    provider = _Provider([
        _response(
            _call("capability.execute", capability="mock", operation="execute"),
            _call("capability.execute", capability="failure", operation="execute"),
        ),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider)
    agent.respond("run successful then failing capability")
    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status == "failed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.FAILED.value
    assert agent._tasks.task_succeeded() is False
    assert record.metadata["lifecycle"]["verification_history"][-1]["state"] == VerificationState.PASSED.value
    assert record.metadata["lifecycle"]["verification_history"][-1]["evidence"]["object"] == "first"


def test_failure_before_verification_is_failed_and_not_successful(tmp_settings):
    capability = _StatusCapability(CapabilityStatus.FAILURE)
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    tmp_settings.max_recovery_attempts = 0
    provider = _Provider([_response(_call("capability.execute", capability="test", operation="execute"))])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider)
    agent.respond("run failing capability")
    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status == "failed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.FAILED.value
    assert agent._tasks.task_succeeded() is False


def test_successful_multi_step_task_keeps_passed_verification(tmp_settings):
    capability = MockCapability(
        result=CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability="mock",
            operation="execute",
            data={"verified": True, "evidence": {"object": "cube"}},
        )
    )
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([
        _response(
            _call("capability.execute", capability="mock", operation="execute"),
            _call("capability.execute", capability="mock", operation="execute"),
        ),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider)
    agent.respond("run two successful capabilities")
    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status == "completed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.PASSED.value
    assert agent._tasks.task_succeeded() is True


def test_failed_verification_stops_later_mutating_capability(tmp_settings):
    class MissingRenderCapability(_Capability):
        name = "blender"

        def __init__(self):
            super().__init__(operations=("render", "create_widget"))

        def execute(self, request):
            self.requests.append(request)
            if request.operation == "render":
                return CapabilityResult(
                    status=CapabilityStatus.SUCCESS,
                    capability=request.capability,
                    operation=request.operation,
                    data={"path": str(request.params["path"]), "exists": False},
                    request_id=request.request_id,
                    task_id=request.task_id,
                )
            return CapabilityResult(
                status=CapabilityStatus.SUCCESS,
                capability=request.capability,
                operation=request.operation,
                data={"executed": True},
                request_id=request.request_id,
                task_id=request.task_id,
            )

    capability = MissingRenderCapability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([
        _response(
            _call("capability.execute", capability="blender", operation="render", params={"path": str(tmp_settings.scratch_dir / "missing.png")}),
            _call("capability.execute", capability="blender", operation="create_widget"),
        ),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider, confirmer=lambda *_: "allow")
    agent.respond("render then mutate")
    assert [request.operation for request in capability.requests] == ["render"]
    assert agent._tasks.state.value == "failed"
    assert agent._tasks.task_succeeded() is False


def test_mutating_capability_requires_core_confirmation(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    prompts = []

    def confirmer(tool, description):
        prompts.append((tool, description))
        return "allow"

    executor = _executor(capability_registry, tmp_settings, confirmer=confirmer)
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "create_widget"})
    assert outcome.success is True
    assert prompts and prompts[0][0] == "capability.execute"
    assert outcome.level == str(PermissionLevel.SENSITIVE)


def test_mutating_capability_denial_prevents_dispatch(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "deny")
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "create_widget"})
    assert outcome.executed is False
    assert outcome.cancelled is True
    assert capability.requests == []


def test_agent_set_confirmer_synchronizes_executor(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider, confirmer=lambda *_: "allow")
    agent.set_confirmer(lambda *_: "deny")
    outcome = agent._executor.run(
        "capability.execute",
        {"capability": "test", "operation": "create_widget"},
    )
    assert outcome.executed is False
    assert outcome.cancelled is True
    assert capability.requests == []


def test_missing_confirmer_remains_fail_closed(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    bus = EventBus()
    tools = _tool_registry(capability_registry, tmp_settings, bus)
    outcome = Executor(tools, PermissionManager(tmp_settings), bus).run(
        "capability.execute",
        {"capability": "test", "operation": "create_widget"},
    )
    assert outcome.executed is False
    assert outcome.cancelled is True
    assert capability.requests == []


def _protected_rule_executor(tmp_settings, confirmer=None):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    store = RuleStore(tmp_settings.memory_dir / "rules.json")
    rule = store.create("protect capability", action="block", if_tool="capability.execute", priority=90)
    bus = EventBus()
    tools = build_registry(tmp_settings, capability_registry, bus)
    permissions = PermissionManager(tmp_settings)
    permissions.rule_engine = RuleEngine(store)
    executor = Executor(tools, permissions, bus, confirmer=confirmer)
    return executor, capability, store, rule


def test_rule_enable_cannot_silently_weaken_protection(tmp_settings):
    executor, capability, store, rule = _protected_rule_executor(tmp_settings)
    protected = {"capability": "test", "operation": "execute"}
    before = executor.run("capability.execute", protected)
    mutation = executor.run("rules.enable", {"rule_id": rule.id, "enabled": False})
    after = executor.run("capability.execute", protected)
    assert before.status == "blocked"
    assert mutation.cancelled is True
    assert after.status == "blocked"
    assert store.get(rule.id).enabled is True
    assert capability.requests == []


def test_rule_update_cannot_silently_weaken_protection(tmp_settings):
    executor, capability, store, rule = _protected_rule_executor(tmp_settings)
    protected = {"capability": "test", "operation": "execute"}
    before = executor.run("capability.execute", protected)
    mutation = executor.run("rules.update", {"rule_id": rule.id, "if_tool": "other.tool"})
    after = executor.run("capability.execute", protected)
    assert before.status == "blocked"
    assert mutation.cancelled is True
    assert after.status == "blocked"
    assert store.get(rule.id).if_tool == "capability.execute"
    assert capability.requests == []


def test_explicitly_authorized_rule_change_remains_available(tmp_settings):
    prompts = []

    def confirmer(tool, description):
        prompts.append((tool, description))
        return "allow"

    executor, capability, store, rule = _protected_rule_executor(tmp_settings, confirmer=confirmer)
    before = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    mutation = executor.run("rules.enable", {"rule_id": rule.id, "enabled": False})
    after = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    assert before.status == "blocked"
    assert mutation.success is True
    assert after.success is True
    assert store.get(rule.id).enabled is False
    assert len(capability.requests) == 1
    assert any(tool == "rules.enable" for tool, _ in prompts)


def test_rule_mutation_preference_allow_still_requires_confirmation(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    store = RuleStore(tmp_settings.memory_dir / "rules.json")
    rule = store.create("protect capability", action="block", if_tool="capability.execute", priority=90)
    preferences = PreferencesStore(tmp_settings.memory_dir)
    preferences.set_permission("rules.enable", "allow")
    permissions = PermissionManager(tmp_settings, preferences)
    permissions.rule_engine = RuleEngine(store)
    bus = EventBus()
    tools = build_registry(tmp_settings, capability_registry, bus)
    outcome = Executor(tools, permissions, bus).run("rules.enable", {"rule_id": rule.id, "enabled": False})
    assert outcome.cancelled is True
    assert store.get(rule.id).enabled is True


def test_blocked_script_operation_never_dispatches(tmp_settings):
    capability = _Capability(operations=("execute_script",))
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "execute_script"})
    assert outcome.status == "blocked"
    assert capability.requests == []


def test_plan_validation_rejects_invalid_capability_before_dispatch(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    registry = _tool_registry(capability_registry, tmp_settings)
    from core.planner import ExecutionPlan, PlanStep, PlanValidationError

    plan = ExecutionPlan(
        goal="invalid capability",
        steps=(PlanStep("step-1", "capability.execute", {"capability": "test", "operation": "missing"}),),
    )
    with pytest.raises(PlanValidationError):
        plan.validate(registry)
    assert capability.requests == []


@pytest.mark.parametrize(
    "status",
    [
        CapabilityStatus.FAILURE,
        CapabilityStatus.UNAVAILABLE,
        CapabilityStatus.INVALID_REQUEST,
        CapabilityStatus.PERMISSION_REQUIRED,
    ],
)
def test_capability_statuses_are_preserved_as_structured_failures(tmp_settings, status):
    capability = _StatusCapability(status)
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    assert outcome.success is False
    assert outcome.data["capability_result"]["status"] == status.value
    assert outcome.error


def test_cancellation_during_pre_execute_does_not_dispatch(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    token = _Token()
    bus = EventBus()
    tools = _tool_registry(capability_registry, tmp_settings, bus)
    executor = Executor(
        tools,
        PermissionManager(tmp_settings),
        bus,
        before_execute=lambda *_: token.cancel(),
    )
    outcome = executor.run(
        "capability.execute",
        {"capability": "test", "operation": "execute"},
        token,
    )
    assert outcome.cancelled is True
    assert outcome.executed is False
    assert capability.requests == []


def test_pre_dispatch_cancellation_is_not_dispatched(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    token = _Token()
    token.cancel()
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run(
        "capability.execute",
        {"capability": "test", "operation": "execute"},
        token,
    )
    assert outcome.cancelled is True
    assert outcome.executed is False
    assert capability.requests == []


def test_cooperative_cancellation_is_reported_by_bridge(tmp_settings):
    capability = _Capability(operations=("cancel_inside",))
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    token = _Token()
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run(
        "capability.execute",
        {"capability": "test", "operation": "cancel_inside"},
        token,
    )
    assert outcome.cancelled is True
    assert outcome.status == "cancelled"
    assert outcome.data["capability_result"]["status"] == "cancelled"
    assert capability.requests


def test_post_completion_cancellation_preserves_side_effect_data(tmp_settings):
    capability = _Capability(operations=("cancel_after",))
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    token = _Token()
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run(
        "capability.execute",
        {"capability": "test", "operation": "cancel_after"},
        token,
    )
    assert outcome.cancelled is True
    assert outcome.success is True
    assert outcome.executed is True
    assert outcome.data["capability_result"]["status"] == "success"


def test_cancellation_stops_remaining_batch_steps(tmp_settings):
    capability = _Capability(operations=("cancel_inside",))
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([
        _response(
            _call("capability.execute", capability="test", operation="cancel_inside"),
            _call("capability.execute", capability="test", operation="cancel_inside"),
        ),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider)
    token = _Token()
    reply = agent.respond("run a capability batch", token)
    assert "cancel" in reply.lower()
    assert len(capability.requests) == 1
    assert agent._tasks.state.value == "cancelled"
    assert agent._tasks.verification == VerificationState.UNVERIFIED
    assert agent._tasks.task_succeeded() is None


def test_repeated_cancellation_is_idempotent(tmp_settings):
    capability = _Capability(operations=("cancel_twice",))
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    token = _Token()
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run(
        "capability.execute",
        {"capability": "test", "operation": "cancel_twice"},
        token,
    )
    assert outcome.cancelled is True
    assert outcome.data["capability_result"]["status"] == "cancelled"
    assert token.cancelled is True


def test_capability_exception_is_isolated_as_structured_failure(tmp_settings):
    class ExplodingCapability(_Capability):
        def execute(self, request):
            self.requests.append(request)
            raise RuntimeError("capability exploded")

    capability_registry = CapabilityRegistry()
    capability_registry.register(ExplodingCapability())
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    assert outcome.success is False
    assert outcome.data["capability_result"]["status"] == "failure"
    assert "capability exploded" in outcome.error


def test_capability_events_use_existing_event_bus(tmp_settings):
    capability = _Capability()
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    bus = EventBus()
    events = []
    bus.subscribe("capability_started", lambda payload: events.append(payload))
    bus.subscribe("capability_success", lambda payload: events.append(payload))
    executor = _executor(capability_registry, tmp_settings, bus=bus, context={"task_id": "task-events"})
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    assert outcome.success is True
    assert [event["type"] for event in events] == ["capability_started", "capability_success"]
    assert events[0]["task_id"] == "task-events"


def test_render_evidence_drives_core_verification(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "render.png")
    capability = _Capability()
    capability.name = "blender_vision"
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([
        _response(_call("capability.execute", capability="blender_vision", operation="render_and_inspect", params={"path": str(image)})),
        _response(text="render verified"),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider, confirmer=lambda *_: "allow")
    agent.respond("render and inspect an image")
    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status == "completed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.PASSED.value
    assert record.metadata["lifecycle"]["verification_evidence"]["path"] == str(image)


def test_missing_render_evidence_fails_core_verification(tmp_settings):
    missing = tmp_settings.scratch_dir / "missing.png"
    capability = _Capability()
    capability.name = "blender_vision"
    capability_registry = CapabilityRegistry()
    capability_registry.register(capability)
    provider = _Provider([
        _response(_call("capability.execute", capability="blender_vision", operation="render_and_inspect", params={"path": str(missing)})),
        _response(text="render failed verification"),
    ])
    agent, _, _ = _agent(capability_registry, tmp_settings, provider, confirmer=lambda *_: "allow")
    agent.respond("render and inspect a missing image")
    record = agent._task_store.get(agent._tasks.task_id)
    assert record.status == "failed"
    assert record.metadata["lifecycle"]["verification"] == VerificationState.FAILED.value


def test_blender_adapter_operations_are_reachable_through_s1(tmp_settings):
    blender = _Blender()
    capability_registry = CapabilityRegistry()
    capability_registry.register(BlenderCapabilityAdapter(blender))
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    render_path = tmp_settings.scratch_dir / "scene.png"
    calls = [
        ("inspect_scene", {}),
        ("list_objects", {}),
        ("create_object", {"primitive": "cube", "name": "Cube"}),
        ("transform_object", {"name": "Cube", "location": [1, 2, 3]}),
        ("render", {"path": str(render_path)}),
        ("save", {"path": str(tmp_settings.scratch_dir / "scene.blend")}),
    ]
    for operation, params in calls:
        outcome = executor.run("capability.execute", {"capability": "blender", "operation": operation, "params": params})
        assert outcome.success is True, operation
    assert [call[0] for call in blender.calls] == [item[0] for item in calls]
    assert render_path.is_file()


def test_vision_adapter_operations_are_reachable_through_s1(tmp_settings):
    image = _png(tmp_settings.scratch_dir / "image.png")
    reference = _png(tmp_settings.scratch_dir / "reference.png")
    capability_registry = CapabilityRegistry()
    capability_registry.register(VisionCapabilityAdapter(_Vision()))
    executor = _executor(capability_registry, tmp_settings)
    calls = [
        ("validate_image", {"image_path": str(image)}),
        ("inspect", {"image_path": str(image)}),
        ("inspect_render", {"image_path": str(image)}),
        ("describe", {"image_path": str(image), "detail_level": "low"}),
        ("compare", {"image_path": str(image), "reference_image": str(reference)}),
    ]
    for operation, params in calls:
        outcome = executor.run("capability.execute", {"capability": "vision", "operation": operation, "params": params})
        assert outcome.success is True, operation


def test_blender_vision_adapter_evidence_is_preserved(tmp_settings):
    image_path = tmp_settings.scratch_dir / "integration.png"
    capability_registry = CapabilityRegistry()
    capability_registry.register(BlenderVisionCapabilityAdapter(_Integration()))
    executor = _executor(capability_registry, tmp_settings, confirmer=lambda *_: "allow")
    outcome = executor.run(
        "capability.execute",
        {"capability": "blender_vision", "operation": "render_and_inspect", "params": {"path": str(image_path)}},
    )
    assert outcome.success is True
    assert outcome.verification.state == VerificationState.PASSED
    assert outcome.data["capability_result"]["data"]["valid"] is True
    assert image_path.is_file()


def test_s1_does_not_pass_a_second_registry_authorization_callback(tmp_settings):
    class RecordingRegistry(CapabilityRegistry):
        def __init__(self):
            super().__init__()
            self.kwargs = None

        def execute(self, request, **kwargs):
            self.kwargs = kwargs
            return super().execute(request, **kwargs)

    capability = _Capability()
    capability_registry = RecordingRegistry()
    capability_registry.register(capability)
    executor = _executor(capability_registry, tmp_settings)
    outcome = executor.run("capability.execute", {"capability": "test", "operation": "execute"})
    assert outcome.success is True
    assert capability_registry.kwargs["authorize"] if "authorize" in capability_registry.kwargs else True
    assert "authorize" not in capability_registry.kwargs
