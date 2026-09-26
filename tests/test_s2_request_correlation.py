"""Trusted S1 request correlation must survive the S2 boundary unchanged.

S1 owns the authoritative request identity: its capability bridge sanitizes
model-supplied provenance and pins the trusted ``request_id`` onto
``CapabilityRequest.context``. S2 must carry that value through to the backend
untouched, and must never author or replace it. S2's own per-call correlation
id is published separately as ``s2_internal_request_id``.
"""

from __future__ import annotations

import threading

import pytest

from integration import (
    BlenderVisionCapabilityAdapter,
    BlenderVisionIntegration,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityStatus,
)
from integration.workflows import (
    BlenderVisionWorkflow,
    BlenderVisionWorkflowCapabilityAdapter,
)
from tools.capability_bridge import CapabilityBridge, trusted_model_context
from tools.registry import ToolExecutionContext
from vision.capabilities import VisionCapabilityLayer

from .test_blender_vision_integration import FakeBlender, _write_png

TRUSTED = "core-trusted-request-id"
S23_OPERATIONS = (
    "render_and_inspect",
    "render_and_describe",
    "render_and_compare",
    "inspect_render",
    "verify_render",
    "describe_render",
    "compare_renders",
)


class RecordingVision:
    """Real vision behaviour, with every context-taking call recorded."""

    preferred_backend = "fallback"

    def __init__(self):
        self._real = VisionCapabilityLayer()
        self.contexts: list[dict] = []
        self._lock = threading.Lock()

    def _record(self, context):
        with self._lock:
            self.contexts.append(dict(context or {}))
        return self.contexts[-1]

    @property
    def last(self) -> dict:
        return self.contexts[-1] if self.contexts else {}

    def detect_capabilities(self):
        return self._real.detect_capabilities()

    def validate_image(self, image_path):
        return self._real.validate_image(image_path)

    def inspect(self, image_path):
        return self._real.inspect(image_path)

    def inspect_render(self, image_path, context=None):
        self._record(context)
        return self._real.inspect_render(str(image_path))

    def describe(self, image_path, detail_level="medium", focus=None, context=None):
        self._record(context)
        return self._real.describe(str(image_path), detail_level, focus, context)

    def compare(self, image_path, reference_image, threshold=0.01, context=None):
        self._record(context)
        return self._real.compare(str(image_path), str(reference_image), threshold)


class WorkflowBlender(FakeBlender):
    """FakeBlender plus the scene-building operations S2.8 needs."""

    def inspect_scene(self):
        return {
            "name": "Scene", "object_count": 0, "objects": [],
            "cameras": [{"name": "Camera"}], "lights": [{"name": "Light"}],
            "materials": [], "active_camera": "Camera", "render": {},
        }

    def list_objects(self, obj_type=""):
        return {"count": 0, "objects": []}

    def create_object(self, primitive="cube", name=None, **kwargs):
        return {"name": name or primitive, "type": "MESH"}

    def create_material(self, name=None, **kwargs):
        return {"name": name or "Material"}

    def assign_material(self, object_name="", material_name=""):
        return {"object_name": object_name, "material_name": material_name}

    def configure_camera(self, name=None, **kwargs):
        return {"name": name or "Camera", "active": True}

    def configure_light(self, name=None, **kwargs):
        return {"name": name or "Light"}

    def save(self, path):
        return {"path": str(path), "exists": True}


def _s1_request(operation="inspect_render", params=None, request_id=TRUSTED,
                task_id="task-9", run_id="run-9", capability="blender_vision"):
    """A request shaped exactly as S1's capability bridge builds it."""
    context = {"source": "s1_core", "run_id": run_id, "task_id": task_id}
    context["request_id"] = request_id  # S1 pins the trusted id here
    return CapabilityRequest(
        capability=capability,
        operation=operation,
        params=dict(params or {}),
        context=context,
        task_id=task_id,
        request_id=request_id,
    )


def _params(operation, tmp_path):
    params = {"path": str(_write_png(tmp_path / "render.png"))}
    if operation in {"render_and_compare", "compare_renders"}:
        params["reference_path"] = str(_write_png(tmp_path / "reference.png"))
    return params


def _s23(tmp_path):
    vision = RecordingVision()
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"), vision=vision
    )
    return integration, BlenderVisionCapabilityAdapter(integration), vision


def _s28(tmp_path):
    vision = RecordingVision()
    integration = BlenderVisionIntegration(
        blender=WorkflowBlender(tmp_path / "blender"), vision=vision
    )
    workflow = BlenderVisionWorkflow(integration=integration)
    registry = CapabilityRegistry()
    registry.register(BlenderVisionWorkflowCapabilityAdapter(workflow))
    return workflow, registry, vision


# ------------------------------------------------------- 1, 2, 3, 5, 6, 7
@pytest.mark.parametrize("operation", S23_OPERATIONS)
def test_trusted_request_id_reaches_backend_unchanged(operation, tmp_path):
    """The backend observes the trusted S1 request id, not an S2 replacement."""
    _, adapter, vision = _s23(tmp_path)
    request = _s1_request(operation, _params(operation, tmp_path))

    result = adapter.execute(request)

    assert result.status is CapabilityStatus.SUCCESS, result.error
    # 3. CapabilityResult carries the trusted S1 id
    assert result.request_id == TRUSTED
    # 5, 6. run_id / task_id unchanged
    assert result.task_id == "task-9"
    # 1, 2. the backend sees the same trusted id
    assert vision.last["request_id"] == TRUSTED
    assert vision.last["run_id"] == "run-9"
    assert vision.last["task_id"] == "task-9"
    # 7. the operation stamp is still applied
    assert vision.last["integration_operation"] == operation
    # S2's own id is present but strictly separate
    assert vision.last["s2_internal_request_id"]
    assert vision.last["s2_internal_request_id"] != TRUSTED


def test_s2_never_authors_request_id_when_none_supplied(tmp_path):
    """9. With no trusted id, S2 still refuses to author request_id itself."""
    _, adapter, vision = _s23(tmp_path)
    request = CapabilityRequest(
        capability="blender_vision", operation="inspect_render",
        params={"path": str(_write_png(tmp_path / "render.png"))},
        context={}, task_id="", request_id=TRUSTED,
    )

    result = adapter.execute(request)

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.request_id == TRUSTED
    assert "request_id" not in vision.last
    assert vision.last["s2_internal_request_id"]


def test_internal_id_is_per_call_and_never_replaces_trusted(tmp_path):
    """Two calls: two internal ids, one stable trusted id per request."""
    _, adapter, vision = _s23(tmp_path)
    target = str(_write_png(tmp_path / "render.png"))

    first = adapter.execute(_s1_request("inspect_render", {"path": target}, request_id="core-A"))
    internal_a = vision.last["s2_internal_request_id"]
    trusted_a = vision.last["request_id"]
    second = adapter.execute(_s1_request("inspect_render", {"path": target}, request_id="core-B"))
    internal_b = vision.last["s2_internal_request_id"]
    trusted_b = vision.last["request_id"]

    assert (trusted_a, trusted_b) == ("core-A", "core-B")
    assert (first.request_id, second.request_id) == ("core-A", "core-B")
    assert internal_a != internal_b


# ---------------------------------------------------------------- 4, spoofing
def test_spoofed_request_id_is_rejected_at_the_s1_boundary(tmp_path):
    """4. A model-supplied request_id never becomes authoritative."""
    _, adapter, vision = _s23(tmp_path)
    registry = CapabilityRegistry()
    registry.register(adapter)
    bridge = CapabilityBridge(registry, None, [])
    target = str(_write_png(tmp_path / "render.png"))

    out = bridge.execute(
        {"capability": "blender_vision", "operation": "inspect_render",
         "params": {"path": target,
                    "context": {"source": "evil", "run_id": "evil",
                                "task_id": "evil", "request_id": "SPOOFED"}}},
        ToolExecutionContext("core-run", "core-task", None),
    )
    payload = (out.data or {}).get("capability_result") or {}

    assert out.success is True, out.error
    assert payload["request_id"] != "SPOOFED"
    assert payload["task_id"] == "core-task"
    assert "SPOOFED" not in out.output
    # the trusted id really is the bridge-generated one, and it reaches Vision
    assert payload["request_id"] == vision.last["request_id"]
    assert vision.last["request_id"] != "SPOOFED"
    # sanitization itself is untouched
    assert trusted_model_context({"request_id": "SPOOFED"}) == {}


def test_trusted_id_survives_a_context_carrying_hostile_keys(tmp_path):
    """Hostile sibling keys do not displace the trusted id."""
    _, adapter, vision = _s23(tmp_path)
    request = CapabilityRequest(
        capability="blender_vision", operation="inspect_render",
        params={"path": str(_write_png(tmp_path / "render.png")),
                "context": {"unrelated": "kept"}},
        context={"source": "s1_core", "run_id": "run-9", "task_id": "task-9",
                 "request_id": TRUSTED},
        task_id="task-9", request_id=TRUSTED,
    )

    adapter.execute(request)

    assert vision.last["request_id"] == TRUSTED
    assert vision.last["unrelated"] == "kept"


# ------------------------------------------------------------------ 8, S2.8
def test_s28_workflow_path_preserves_the_trusted_request_id(tmp_path):
    """8. The S2.8 workflow path delivers the trusted id to the backend."""
    workflow, registry, vision = _s28(tmp_path)
    request = _s1_request(
        "run",
        {"plan": {"objects": [{"name": "Cube"}]},
         "output_path": str(tmp_path / "wf.png")},
        capability="blender_vision_workflow",
    )

    result = registry.execute(request)

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.request_id == TRUSTED
    assert result.task_id == "task-9"
    assert vision.last["request_id"] == TRUSTED
    assert vision.last["run_id"] == "run-9"
    assert vision.last["task_id"] == "task-9"
    assert vision.last["s2_internal_request_id"] != TRUSTED
    workflow.close()


def test_s28_analyze_reference_preserves_the_trusted_request_id(tmp_path):
    """S2.8's reference-analysis path also carries the trusted id."""
    workflow, registry, vision = _s28(tmp_path)
    request = _s1_request(
        "analyze_reference",
        {"reference_path": str(_write_png(tmp_path / "ref.png"))},
        capability="blender_vision_workflow",
    )

    result = registry.execute(request)

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.request_id == TRUSTED
    assert vision.last["request_id"] == TRUSTED
    workflow.close()


def test_s28_compare_path_preserves_the_trusted_request_id(tmp_path):
    """S2.8's comparison path also carries the trusted id."""
    workflow, registry, vision = _s28(tmp_path)
    same = str(_write_png(tmp_path / "same.png"))
    request = _s1_request(
        "compare_reference", {"path": same, "reference_path": same},
        capability="blender_vision_workflow",
    )

    result = registry.execute(request)

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.request_id == TRUSTED
    assert vision.last["request_id"] == TRUSTED
    workflow.close()


def test_s28_render_and_verify_preserves_the_trusted_request_id(tmp_path):
    """The render_and_verify evidence path also carries the trusted id."""
    workflow, registry, vision = _s28(tmp_path)
    request = _s1_request(
        "render_and_verify",
        {"plan": {"objects": [{"name": "Cube"}]},
         "output_path": str(tmp_path / "rv.png")},
        capability="blender_vision_workflow",
    )

    result = registry.execute(request)

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.request_id == TRUSTED
    assert (result.data or {}).get("verified") is True
    assert vision.last["request_id"] == TRUSTED
    workflow.close()


def test_concurrent_requests_keep_their_own_trusted_id(tmp_path):
    """Concurrent requests never cross-contaminate the trusted id."""
    _, adapter, vision = _s23(tmp_path)
    registry = CapabilityRegistry()
    registry.register(adapter)
    target = str(_write_png(tmp_path / "render.png"))
    out = {}

    def run(index):
        request = _s1_request("inspect_render", {"path": target},
                              request_id=f"core-{index}", task_id=f"task-{index}",
                              run_id=f"run-{index}")
        out[index] = registry.execute(request)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    for i in range(4):
        assert out[i].request_id == f"core-{i}"
        assert out[i].task_id == f"task-{i}"
    delivered = [c.get("request_id") for c in vision.contexts]
    assert sorted(delivered) == ["core-0", "core-1", "core-2", "core-3"]


def test_integration_result_envelope_keeps_its_own_id(tmp_path):
    """The IntegrationResult envelope id stays distinct and is not the trusted id."""
    vision = RecordingVision()
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"), vision=vision
    )
    result = integration.inspect_render(
        str(_write_png(tmp_path / "render.png")), context={"request_id": TRUSTED}
    )

    assert result.success is True
    assert result.request_id
    assert result.request_id != TRUSTED
    assert vision.last["request_id"] == TRUSTED
    assert vision.last["s2_internal_request_id"] == result.request_id
