from __future__ import annotations

import json
import struct
import threading
import zlib
from pathlib import Path

import pytest

from config.permissions import PermissionManager
from config.settings import Settings
from core.events import EventBus
from core.executor import Executor
from integration import (
    BlenderVisionIntegration,
    BlenderVisionWorkflow,
    BlenderVisionWorkflowCapabilityAdapter,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityStatus,
    ScenePlan,
    SceneState,
    WorkflowStatus,
    WorkflowValidationError,
    create_blender_vision_workflow,
)
from main import build_capability_runtime, close_capability_runtime
from tools import build_registry
from ui import CoreEventBusBridge, UIStatus
from vision import VisionCapabilityLayer
from voice import CancellationToken


def _png(width: int = 64, height: int = 64) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x20\x30\x40" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeBlender:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.materials: dict[str, dict] = {}
        self.cameras: dict[str, dict] = {}
        self.lights: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def _record(self, operation, kwargs):
        self.calls.append((operation, dict(kwargs)))

    def inspect_scene(self):
        self._record("inspect_scene", {})
        return {
            "name": "Scene",
            "object_count": len(self.objects),
            "objects": list(self.objects),
            "cameras": list(self.cameras),
            "lights": list(self.lights),
            "materials": list(self.materials),
            "active_camera": next(iter(self.cameras), ""),
            "render": {"engine": "BLENDER_EEVEE_NEXT", "resolution_x": 640, "resolution_y": 480},
            "units": {"system": "METRIC"},
        }

    def create_object(self, primitive="cube", name="", location=None, rotation=None, scale=None, **extra):
        self._record("create_object", {"primitive": primitive, "name": name, "location": location, "rotation": rotation, "scale": scale, **extra})
        actual = name or f"{primitive.title()}Object{len(self.objects) + 1}"
        value = {"name": actual, "type": "MESH", "location": location or [0, 0, 0], "scale": scale or [1, 1, 1]}
        self.objects[actual] = value
        return value

    def create_material(self, name="", base_color=None, **kwargs):
        self._record("create_material", {"name": name, "base_color": base_color, **kwargs})
        actual = name or f"Material{len(self.materials) + 1}"
        value = {"name": actual, "base_color": base_color or [0.8, 0.8, 0.8], **kwargs}
        self.materials[actual] = value
        return value

    def assign_material(self, object_name, material_name):
        self._record("assign_material", {"object_name": object_name, "material_name": material_name})
        if object_name not in self.objects:
            raise ValueError("missing object")
        if material_name not in self.materials:
            raise ValueError("missing material")
        return {"object": object_name, "material": material_name}

    def configure_camera(self, name="", location=None, rotation=None, lens=None, make_active=False):
        self._record("configure_camera", {"name": name, "location": location, "rotation": rotation, "lens": lens, "make_active": make_active})
        actual = name or f"Camera{len(self.cameras) + 1}"
        self.cameras[actual] = {"name": actual, "location": location or [0, 0, 0], "lens": lens, "active": make_active}
        return self.cameras[actual]

    def configure_light(self, name="", light_type="POINT", location=None, rotation=None, energy=1.0, color=None, size=None):
        self._record("configure_light", {"name": name, "light_type": light_type, "location": location, "rotation": rotation, "energy": energy, "color": color, "size": size})
        actual = name or f"Light{len(self.lights) + 1}"
        self.lights[actual] = {"name": actual, "light_type": light_type, "energy": energy}
        return self.lights[actual]

    def transform_object(self, name, **kwargs):
        self._record("transform_object", {"name": name, **kwargs})
        self.objects[name].update(kwargs)
        return self.objects[name]

    def modify_object(self, name, **kwargs):
        self._record("modify_object", {"name": name, **kwargs})
        return self.objects[name]

    def delete_object(self, name):
        self._record("delete_object", {"name": name})
        return {"deleted": [name], "count": 1, "missing": []}

    def save(self, path):
        self._record("save", {"path": str(path)})
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"blend")
        return {"path": str(target), "exists": True, "size_bytes": target.stat().st_size}

    def render(self, path, **kwargs):
        self._record("render", {"path": str(path), **kwargs})
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png())
        return {"path": str(target), "exists": True, "size_bytes": target.stat().st_size}

    def close(self):
        self.closed = True


class FakeVision:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.preferred_backend = "fallback"

    def validate_image(self, image_path):
        self.calls.append(("validate_image", {"image_path": str(image_path)}))
        return {"path": str(image_path), "readable": True, "width": 64, "height": 64}

    def inspect_render(self, image_path, context=None):
        self.calls.append(("inspect_render", {"image_path": str(image_path), "context": dict(context or {})}))
        return {"valid": True, "checks": {"readable": True}, "image": {"width": 64, "height": 64}}

    def describe(self, image_path, **kwargs):
        self.calls.append(("describe", {"image_path": str(image_path), **kwargs}))
        return {
            "description": "64x64 PNG image; deterministic description",
            "metadata": {"width": 64, "height": 64},
            "render_hints": {"likely_blender_render": True},
        }

    def compare(self, image_path, reference_image, **kwargs):
        self.calls.append(("compare", {"image_path": str(image_path), "reference_image": str(reference_image), **kwargs}))
        return {"identical": True, "diff_ratio": 0.0}


class FakeIntegration:
    def __init__(self, render_values=None):
        self.blender = FakeBlender()
        self.vision = FakeVision()
        self.render_values = list(render_values or [True])
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def render_and_inspect(self, path, **kwargs):
        self.calls.append(("render_and_inspect", {"path": str(path), **kwargs}))
        valid = self.render_values[min(len(self.calls) - 1, len(self.render_values) - 1)]
        render = self.blender.render(path, **kwargs)
        inspection = self.vision.inspect_render(path, context=kwargs.get("context"))
        if not valid:
            inspection = {"valid": False, "checks": {"readable": False}}
        return {"render": render, "render_path": str(path), "inspection": inspection, "valid": valid}

    def describe_render(self, path, **kwargs):
        self.calls.append(("describe_render", {"path": str(path), **kwargs}))
        return {"render_path": str(path), "description": self.vision.describe(path, **kwargs), "image": {"width": 64, "height": 64}}

    def compare_renders(self, path, reference_path, **kwargs):
        self.calls.append(("compare_renders", {"path": str(path), "reference_path": str(reference_path), **kwargs}))
        return {"render_path": str(path), "reference_path": str(reference_path), "comparison": self.vision.compare(path, reference_path, **kwargs)}

    def close(self):
        self.closed = True


def _workflow(tmp_path, **kwargs):
    integration = kwargs.pop("integration", None) or FakeIntegration()
    return BlenderVisionWorkflow(integration, work_dir=tmp_path / "work", **kwargs), integration


def test_scene_plan_and_state_are_compact_and_serializable():
    plan = ScenePlan.from_dict({
        "objects": [{"name": "Hero", "primitive": "cube"}],
        "max_iterations": 99,
        "render": {"path": "render.png"},
    })
    state = SceneState.from_payload({"name": "Scene", "objects": ["Hero", "Floor"], "object_count": 2})
    assert plan.max_iterations == 8
    assert plan.render["path"] == "render.png"
    assert state.to_compact()["objects"] == ["Hero", "Floor"]
    assert json.loads(json.dumps(plan.to_dict()))["max_iterations"] == 8
    assert len(state.to_context(80)) <= 80


def test_invalid_scene_plan_is_structured():
    with pytest.raises(WorkflowValidationError) as exc_info:
        ScenePlan(comparison_threshold=2)
    assert exc_info.value.to_dict()["error"] == "invalid_workflow"


def test_build_scene_composes_objects_materials_camera_and_lights(tmp_path):
    workflow, integration = _workflow(tmp_path)
    result = workflow.build_scene({
        "objects": [{"name": "Hero", "primitive": "cube", "material": "Red"}],
        "materials": [{"name": "Red", "base_color": [1, 0, 0]}],
        "camera": {"name": "Camera", "make_active": True},
        "lights": [{"name": "Sun", "light_type": "SUN"}],
    })
    assert result.status is WorkflowStatus.SUCCESS
    assert result.data["state"]["object_count"] == 1
    operations = [name for name, _ in integration.blender.calls]
    assert operations.index("create_material") < operations.index("create_object") < operations.index("assign_material")
    assert "configure_camera" in operations
    assert "configure_light" in operations
    workflow.close()
    assert integration.closed is False


def test_render_and_verify_retries_with_bounded_refinement(tmp_path):
    integration = FakeIntegration([False, True])
    workflow = BlenderVisionWorkflow(integration)
    refinements = []

    def refine(payload, iteration):
        refinements.append((payload["iteration"], iteration))
        return {"operations": [{"operation": "create_object", "name": "Correction"}]}

    result = workflow.render_and_verify(
        tmp_path / "render.png",
        max_iterations=2,
        refine=refine,
    )
    assert result.status is WorkflowStatus.SUCCESS
    assert result.data["iterations"] == 2
    assert result.data["attempts"][0]["valid"] is False
    assert refinements == [(1, 1)]
    assert len([call for call in integration.calls if call[0] == "render_and_inspect"]) == 2


def test_workflow_composes_existing_integration_and_fallback_vision(tmp_path):
    integration = BlenderVisionIntegration(
        blender=FakeBlender(),
        vision=VisionCapabilityLayer(),
    )
    workflow = BlenderVisionWorkflow(integration)
    result = workflow.render_and_verify(tmp_path / "render.png")
    assert result.status is WorkflowStatus.SUCCESS
    assert result.data["render"]["render"]["exists"] is True
    assert result.data["render"]["inspection"]["valid"] is True


def test_render_and_verify_returns_structured_failure_at_bound(tmp_path):
    integration = FakeIntegration([False])
    workflow = BlenderVisionWorkflow(integration)
    result = workflow.render_and_verify(tmp_path / "render.png", max_iterations=3)
    assert result.status is WorkflowStatus.FAILURE
    assert result.error["error"] == "verification_failed"
    assert len(result.data["attempts"]) == 1


def test_partial_render_evidence_fails_closed(tmp_path):
    integration = FakeIntegration()

    def partial(path, **kwargs):
        render = integration.blender.render(path, **kwargs)
        return {"render": render, "render_path": str(path), "inspection": {"valid": True}}

    integration.render_and_inspect = partial
    workflow = BlenderVisionWorkflow(integration)
    result = workflow.render_and_verify(tmp_path / "partial.png")
    assert result.status is WorkflowStatus.FAILURE
    assert result.data["verified"] is False
    assert result.data["attempts"][0]["evidence"]["render_valid"] is False


def test_render_artifact_must_match_requested_path(tmp_path):
    integration = FakeIntegration()
    requested = tmp_path / "requested.png"
    unrelated = tmp_path / "unrelated.png"
    unrelated.write_bytes(_png())

    def wrong_artifact(path, **kwargs):
        return {"valid": True, "inspection": {"valid": True}, "render": {"path": str(unrelated), "exists": True}}

    integration.render_and_inspect = wrong_artifact
    workflow = BlenderVisionWorkflow(integration)
    result = workflow.render_and_verify(requested)
    assert result.status is WorkflowStatus.FAILURE
    assert result.data["verified"] is False


def test_comparison_success_without_match_evidence_fails_closed(tmp_path):
    integration = FakeIntegration()

    def success_only(path, reference_path, **kwargs):
        return {"comparison": {"success": True}}

    integration.compare_renders = success_only
    workflow = BlenderVisionWorkflow(integration)
    reference = tmp_path / "reference.png"
    reference.write_bytes(_png())
    result = workflow.render_and_verify(tmp_path / "render.png", reference_path=reference)
    assert result.status is WorkflowStatus.FAILURE
    assert result.data["verified"] is False


def test_invalid_render_artifact_fails_closed(tmp_path):
    integration = FakeIntegration()

    def invalid(path, **kwargs):
        return {"valid": True, "inspection": {"valid": True}, "render": {"path": str(path), "exists": False}}

    integration.render_and_inspect = invalid
    workflow = BlenderVisionWorkflow(integration)
    result = workflow.render_and_verify(tmp_path / "invalid.png")
    assert result.status is WorkflowStatus.FAILURE
    assert result.data["verified"] is False


def test_unavailable_comparison_cannot_verify(tmp_path):
    integration = FakeIntegration()

    def unavailable(path, reference_path, **kwargs):
        return {"comparison": {"success": False, "identical": True}}

    integration.compare_renders = unavailable
    workflow = BlenderVisionWorkflow(integration)
    reference = tmp_path / "reference.png"
    reference.write_bytes(_png())
    result = workflow.render_and_verify(tmp_path / "render.png", reference_path=reference)
    assert result.status is WorkflowStatus.FAILURE
    assert result.data["verified"] is False


def test_verifier_callbacks_require_explicit_verdicts(tmp_path):
    workflow, _ = _workflow(tmp_path)
    for callback in (lambda payload: None, lambda payload: {"details": "no verdict"}, lambda payload: {"evidence": "text"}, lambda payload: "malformed"):
        result = workflow.render_and_verify(tmp_path / "callback.png", verifier=callback)
        assert result.status is WorkflowStatus.FAILURE
        assert result.data["verified"] is False
    valid = workflow.render_and_verify(tmp_path / "callback-valid.png", verifier=lambda payload: {"verified": True})
    assert valid.status is WorkflowStatus.SUCCESS
    assert valid.data["verified"] is True


def test_capability_registry_cancels_active_workflow_and_cleans_binding(tmp_path):
    integration = FakeIntegration()
    started = threading.Event()
    release = threading.Event()

    def blocking(path, **kwargs):
        started.set()
        release.wait(2.0)
        render = integration.blender.render(path, **kwargs)
        return {"render": render, "render_path": str(path), "inspection": {"valid": True}, "valid": True}

    integration.render_and_inspect = blocking
    workflow = BlenderVisionWorkflow(integration)
    adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
    registry = CapabilityRegistry()
    registry.register(adapter)
    token = CancellationToken("active-workflow")
    request = CapabilityRequest(
        capability=adapter.name,
        operation="render_and_verify",
        params={"path": str(tmp_path / "active.png")},
        task_id="active-workflow",
        cancellation_token=token,
    )
    results = []
    thread = threading.Thread(target=lambda: results.append(registry.execute(request)))
    thread.start()
    assert started.wait(1.0)
    cancellation = registry.cancel(adapter.name, "active-workflow")
    assert cancellation.status.value == "cancelled"
    assert token.is_cancelled() is True
    release.set()
    thread.join(1.0)
    assert results[0].status.value == "cancelled"
    assert registry._active_requests == {}
    assert workflow._tokens == {}


def test_reference_analysis_separates_deterministic_evidence(tmp_path):
    workflow, _ = _workflow(tmp_path)
    reference = tmp_path / "reference.png"
    reference.write_bytes(_png())
    result = workflow.analyze_reference(reference)
    assert result.status is WorkflowStatus.SUCCESS
    assert result.data["evidence"]["kind"] == "deterministic"
    assert result.data["evidence"]["deterministic"] is True
    assert result.data["evidence"]["semantic"] is False
    assert result.data["description"]["metadata"]["width"] == 64


def test_existing_event_bus_receives_workflow_activity(tmp_path):
    bus = EventBus()
    events = []
    for kind in ("status", "tool", "task", "workflow_completed"):
        bus.subscribe(kind, lambda payload, kind=kind: events.append((kind, payload)))
    workflow, _ = _workflow(tmp_path, events=bus)
    result = workflow.render_and_verify(tmp_path / "render.png")
    assert result.success is True
    assert any(kind == "status" and payload["value"] == "ready" for kind, payload in events)
    assert any(kind == "tool" and payload["status"] == "success" for kind, payload in events)
    assert any(kind == "task" for kind, payload in events)


def test_workflow_cancellation_is_visible_through_core_event_bridge(tmp_path):
    bus = EventBus()
    bridge = CoreEventBusBridge(bus)
    events = []
    bridge.subscribe_to_events(events.append)
    bus.emit_status("state", "cancelled")
    assert events[-1].status == UIStatus.CANCELLED.value
    bridge.close()


def test_workflow_cancellation_uses_existing_token(tmp_path):
    integration = FakeIntegration()
    workflow = BlenderVisionWorkflow(integration)
    token = CancellationToken("render-task")
    token.request("stop", source="test")
    result = workflow.render_and_verify(tmp_path / "render.png", task_id="render-task", cancellation_token=token)
    assert result.status is WorkflowStatus.CANCELLED
    assert integration.calls == []
    assert result.error["error"] == "cancelled"


def test_workflow_capability_adapter_pre_cancelled_request_is_structured(tmp_path):
    integration = FakeIntegration()
    workflow = BlenderVisionWorkflow(integration)
    adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
    token = CancellationToken("workflow-pre-cancel")
    token.request("stop", source="audit")
    result = adapter.execute(
        CapabilityRequest(
            capability=adapter.name,
            operation="render_and_verify",
            params={"path": str(tmp_path / "pre-cancelled.png")},
            task_id="workflow-pre-cancel",
            cancellation_token=token,
        )
    )
    assert result.status is CapabilityStatus.CANCELLED
    assert result.error["error"] == "cancelled"
    assert integration.calls == []


def test_capability_adapter_preserves_core_contract(tmp_path):
    integration = FakeIntegration()
    workflow = BlenderVisionWorkflow(integration)
    adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
    registry = CapabilityRegistry()
    registry.register(adapter)
    request = CapabilityRequest(
        capability=adapter.name,
        operation="render_and_verify",
        params={"path": str(tmp_path / "render.png")},
        context={"source": "test"},
        task_id="task-1",
    )
    result = registry.execute(request, authorize=lambda _: True)
    assert result.status.value == "success"
    assert result.data["verified"] is True
    assert result.request_id == request.request_id
    assert result.task_id == "task-1"


def test_owned_workflow_closes_integration_but_injected_workflow_does_not(tmp_path):
    owned = create_blender_vision_workflow(work_dir=tmp_path / "owned")
    assert owned.integration.blender.session_file.parent.exists()
    owned.close()
    assert owned.closed is True
    assert owned.integration.closed is True

    integration = FakeIntegration()
    injected = create_blender_vision_workflow(integration)
    injected.close()
    assert integration.closed is False


def test_workflow_results_are_json_safe(tmp_path):
    workflow, _ = _workflow(tmp_path)
    result = workflow.inspect_scene_state()
    payload = result.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    workflow.close()


class BlockingWorkflowIntegration(FakeIntegration):
    def __init__(self):
        super().__init__()
        self.render_started = threading.Event()
        self.render_release = threading.Event()

    def render_and_inspect(self, path, **kwargs):
        self.calls.append(("render_and_inspect", {"path": str(path), **kwargs}))
        self.render_started.set()
        if not self.render_release.wait(5.0):
            raise RuntimeError("workflow render release timed out")
        render = self.blender.render(path, **kwargs)
        inspection = self.vision.inspect_render(path, context=kwargs.get("context"))
        return {"render": render, "render_path": str(path), "inspection": inspection, "valid": True}


def _run_workflow_same_task_scenario(tmp_path, cancel_label):
    integration = BlockingWorkflowIntegration()
    workflow = BlenderVisionWorkflow(integration)
    adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
    registry = CapabilityRegistry()
    registry.register(adapter)
    requests = {}
    tokens = {}
    for label in ("a", "b"):
        token = CancellationToken("shared-task")
        request = CapabilityRequest(
            capability=adapter.name,
            operation="render_and_verify",
            params={"path": str(tmp_path / f"{label}.png")},
            task_id="shared-task",
            cancellation_token=token,
        )
        requests[label] = request
        tokens[label] = token
    results = {}

    def run(request):
        results[request.request_id] = registry.execute(request)

    threads = []
    for request in requests.values():
        thread = threading.Thread(target=run, args=(request,))
        thread.start()
        threads.append(thread)
    assert integration.render_started.wait(2.0)
    deadline = threading.Event()
    while len(registry._active_requests) < 2 and not deadline.wait(0.005):
        if all(not thread.is_alive() for thread in threads):
            break
    assert len(registry._active_requests) == 2
    cancel_result = registry.cancel_request(requests[cancel_label].request_id)
    integration.render_release.set()
    for thread in threads:
        thread.join(3.0)
    workflow.close()
    return {
        "results": results,
        "tokens": tokens,
        "requests": requests,
        "cancel_result": cancel_result,
        "active_after": dict(registry._active_requests),
    }


def test_workflow_same_task_request_scoped_cancel_isolates_peer(tmp_path):
    outcome = _run_workflow_same_task_scenario(tmp_path, cancel_label="a")
    requests = outcome["requests"]
    tokens = outcome["tokens"]
    results = outcome["results"]
    assert outcome["cancel_result"].status is CapabilityStatus.CANCELLED
    assert results[requests["a"].request_id].status is CapabilityStatus.CANCELLED
    assert results[requests["b"].request_id].status is CapabilityStatus.SUCCESS
    assert tokens["a"].is_cancelled() is True
    assert tokens["b"].is_cancelled() is False
    assert outcome["active_after"] == {}


def test_workflow_same_task_reverse_request_scoped_cancel_isolates_peer(tmp_path):
    outcome = _run_workflow_same_task_scenario(tmp_path, cancel_label="b")
    requests = outcome["requests"]
    tokens = outcome["tokens"]
    results = outcome["results"]
    assert outcome["cancel_result"].status is CapabilityStatus.CANCELLED
    assert results[requests["a"].request_id].status is CapabilityStatus.SUCCESS
    assert results[requests["b"].request_id].status is CapabilityStatus.CANCELLED
    assert tokens["a"].is_cancelled() is False
    assert tokens["b"].is_cancelled() is True
    assert outcome["active_after"] == {}


def _production_same_task_scenario(tmp_path, cancel_label):
    settings = Settings(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "data" / "memory",
        logs_dir=tmp_path / "data" / "logs",
        backups_dir=tmp_path / "data" / "backups",
        sessions_dir=tmp_path / "data" / "sessions",
        scratch_dir=tmp_path / "workspace",
        allowed_roots=[tmp_path],
    )
    bus = EventBus()
    capability_registry, integration, blender, workflow, windows = build_capability_runtime(settings, bus)
    fake = BlockingWorkflowIntegration()
    workflow._integration = fake
    try:
        tool_registry = build_registry(settings, capability_registry=capability_registry, capability_events=bus)
        permissions = PermissionManager(settings)
        executor = Executor(
            tool_registry,
            permissions,
            bus,
            confirmer=lambda tool, description: "allow",
            event_context=lambda: {"run_id": "run-1", "task_id": "same-task"},
        )
        tokens = {label: CancellationToken("same-task") for label in ("a", "b")}
        args = {
            label: {
                "capability": "blender_vision_workflow",
                "operation": "render_and_verify",
                "params": {"path": str(tmp_path / f"prod_{label}.png")},
            }
            for label in ("a", "b")
        }
        outcomes = {}

        def run(label):
            outcomes[label] = executor.run("capability.execute", args[label], tokens[label])

        threads = [threading.Thread(target=run, args=(label,)) for label in ("a", "b")]
        for thread in threads:
            thread.start()
        assert fake.render_started.wait(3.0)
        deadline = threading.Event()
        while len(capability_registry._active_requests) < 2 and not deadline.wait(0.005):
            if all(not thread.is_alive() for thread in threads):
                break
        active_entries = len(capability_registry._active_requests)
        assert active_entries == 2
        active_requests = list(capability_registry._active_requests.values())
        target = next(request for request in active_requests if request.cancellation_token is tokens[cancel_label])
        peer = next(request for request in active_requests if request.cancellation_token is tokens["a" if cancel_label == "b" else "b"])
        assert target.task_id == peer.task_id == "same-task"
        assert target.capability == peer.capability
        cancel_result = capability_registry.cancel_request(target.request_id)
        fake.render_release.set()
        for thread in threads:
            thread.join(10.0)
        return {
            "outcomes": outcomes,
            "tokens": tokens,
            "cancel_result": cancel_result,
            "active_entries": active_entries,
            "active_after": dict(capability_registry._active_requests),
            "workflow_tokens_after": dict(workflow._tokens),
        }
    finally:
        close_capability_runtime(workflow, integration, blender)


def test_production_same_task_concurrent_cancellation_isolates_peer(tmp_path):
    outcome = _production_same_task_scenario(tmp_path, cancel_label="a")
    outcomes = outcome["outcomes"]
    tokens = outcome["tokens"]
    assert outcome["active_entries"] == 2
    assert outcome["cancel_result"].status is CapabilityStatus.CANCELLED
    assert outcomes["a"].status == "cancelled"
    assert outcomes["b"].status == "success"
    assert tokens["a"].is_cancelled() is True
    assert tokens["b"].is_cancelled() is False
    assert outcome["active_after"] == {}
    assert outcome["workflow_tokens_after"] == {}


def test_production_same_task_reverse_cancellation_isolates_peer(tmp_path):
    outcome = _production_same_task_scenario(tmp_path, cancel_label="b")
    outcomes = outcome["outcomes"]
    tokens = outcome["tokens"]
    assert outcome["active_entries"] == 2
    assert outcome["cancel_result"].status is CapabilityStatus.CANCELLED
    assert outcomes["a"].status == "success"
    assert outcomes["b"].status == "cancelled"
    assert tokens["a"].is_cancelled() is False
    assert tokens["b"].is_cancelled() is True
    assert outcome["active_after"] == {}
    assert outcome["workflow_tokens_after"] == {}


# ------------------------------------------- S1 request context propagation (S2.8)


class _ContextRecordingWorkflow:
    """Workflow stub that records the context each capability call receives."""

    def __init__(self):
        self.calls = []
        self._closed = False

    def capabilities(self):
        return {"name": "blender_vision_workflow", "available": True, "operations": ["run"]}

    def can_execute(self, operation):
        return operation == "run"

    def _record(self, context=None, **kwargs):
        self.calls.append({"context": context, "kwargs": dict(kwargs)})
        return type(
            "R",
            (),
            {
                "status": WorkflowStatus.SUCCESS,
                "success": True,
                "data": {"verified": True},
                "error": None,
                "metadata": {},
                "duration_ms": 1.0,
            },
        )()

    def run(self, plan=None, **kwargs):
        return self._record(**{k: v for k, v in kwargs.items() if k == "context"})

    def build_scene(self, plan=None, **kwargs):
        return self._record(**{k: v for k, v in kwargs.items() if k == "context"})

    def render_and_verify(self, path=None, **kwargs):
        return self._record(**{k: v for k, v in kwargs.items() if k == "context"})

    def inspect_scene_state(self, **kwargs):
        return self._record(**{k: v for k, v in kwargs.items() if k == "context"})

    def analyze_reference(self, reference_path=None, **kwargs):
        return self._record(**{k: v for k, v in kwargs.items() if k == "context"})

    def compare_reference(self, path=None, reference_path=None, **kwargs):
        return self._record(**{k: v for k, v in kwargs.items() if k == "context"})

    def bind_task(self, task_id, token=None, *, request_id=""):
        return token

    def release_task(self, task_id="", *, request_id=""):
        return None

    def cancel(self, task_id="", *, request_id="", source="workflow", reason="user_interruption"):
        return False


S1_CTX = {"source": "s1_core", "run_id": "run-88", "task_id": "task-88"}


@pytest.mark.parametrize(
    "operation,params",
    [
        ("run", {"output_path": "out.png"}),
        ("render_and_verify", {"path": "out.png"}),
        ("analyze_reference", {"reference_path": "ref.png"}),
        ("compare_reference", {"path": "out.png", "reference_path": "ref.png"}),
    ],
)
def test_s28_context_propagation_remains_correct(operation, params):
    """S2.8 still forwards the trusted S1 request context."""
    workflow = _ContextRecordingWorkflow()
    adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
    request = CapabilityRequest(
        capability="blender_vision_workflow",
        operation=operation,
        params=dict(params),
        context=dict(S1_CTX),
        task_id="task-88",
        request_id="req-s28",
    )

    result = adapter.execute(request)

    assert result.status is CapabilityStatus.SUCCESS
    assert workflow.calls, "workflow was never called"
    seen = workflow.calls[0]["context"]
    assert seen["source"] == "s1_core"
    assert seen["run_id"] == "run-88"
    assert seen["task_id"] == "task-88"
    assert result.request_id == "req-s28"
    assert result.task_id == "task-88"


def test_s28_context_layers_caller_context_onto_trusted_context():
    """S2.8 keeps merging non-provenance caller context on top."""
    workflow = _ContextRecordingWorkflow()
    adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
    request = CapabilityRequest(
        capability="blender_vision_workflow",
        operation="run",
        params={"output_path": "out.png", "context": {"focus": "camera"}},
        context=dict(S1_CTX),
        task_id="task-88",
        request_id="req-s28",
    )

    adapter.execute(request)

    seen = workflow.calls[0]["context"]
    assert seen["focus"] == "camera"
    assert seen["run_id"] == "run-88"
    assert seen["source"] == "s1_core"


def test_s28_and_s23_agree_on_context_propagation():
    """Both capability halves propagate the same trusted context shape."""
    from integration import BlenderVisionCapabilityAdapter

    seen_s23 = {}

    def inspect_render(path, context=None):
        seen_s23["context"] = context
        return type(
            "R",
            (),
            {
                "success": True,
                "error": None,
                "to_dict": staticmethod(lambda: {"success": True, "data": {"valid": True}, "error": None}),
            },
        )()

    s23_layer = type("L", (), {"inspect_render": staticmethod(inspect_render)})()
    s28_workflow = _ContextRecordingWorkflow()

    s23 = BlenderVisionCapabilityAdapter(s23_layer)
    s28 = BlenderVisionWorkflowCapabilityAdapter(s28_workflow)
    s23_result = s23.execute(
        CapabilityRequest(
            capability="blender_vision",
            operation="inspect_render",
            params={"path": "out.png"},
            context=dict(S1_CTX),
            task_id="task-88",
            request_id="req-s23",
        )
    )
    s28_result = s28.execute(
        CapabilityRequest(
            capability="blender_vision_workflow",
            operation="run",
            params={"output_path": "out.png"},
            context=dict(S1_CTX),
            task_id="task-88",
            request_id="req-s28",
        )
    )

    assert s23_result.request_id == "req-s23"
    assert s28_result.request_id == "req-s28"
    for key in ("source", "run_id", "task_id"):
        assert seen_s23["context"][key] == S1_CTX[key]
        assert s28_workflow.calls[0]["context"][key] == S1_CTX[key]