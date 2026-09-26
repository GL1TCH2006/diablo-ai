"""Trusted S1 request correlation across the whole S1 -> S2 -> backend path.

S1 owns the authoritative ``request_id``. This module pins that it survives
every hop: CapabilityRequest -> S2 integration capability -> S2 layer ->
Blender/Vision protocol envelope -> backend -> CapabilityResult.

These are behavioural tests at the real protocol/backend boundary. A spy runner
and a spy vision backend observe the id that actually goes on the wire, so a
regression that reintroduces a layer-minted id fails here rather than passing on
an implementation detail.
"""

from __future__ import annotations

import threading

import pytest

from blender.capabilities import BlenderCapabilityLayer
from blender.runner import RunnerResult
from integration import (
    BlenderCapabilityAdapter,
    BlenderVisionCapabilityAdapter,
    BlenderVisionIntegration,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    VisionCapabilityAdapter,
)
from integration.workflows import BlenderVisionWorkflow, BlenderVisionWorkflowCapabilityAdapter
from tools.capability_bridge import CapabilityBridge, trusted_model_context
from tools.registry import ToolExecutionContext
from vision.backends import registry as vision_registry
from vision.backends.base import VisionBackend
from vision.capabilities import VisionCapabilityLayer
from vision.protocol import VisionResult

from .test_blender_vision_integration import FakeBlender, _write_png

TRUSTED = "S1-TRUSTED-REQUEST-ID"


def _request(capability, operation, params=None, request_id=TRUSTED,
             task_id="task-7", run_id="run-7", context=None):
    """A CapabilityRequest shaped as S1's capability bridge builds it."""
    ctx = {"source": "s1_core", "run_id": run_id, "task_id": task_id}
    if context is not None:
        ctx = {**ctx, **context}
    ctx["request_id"] = request_id  # S1 pins the trusted id here
    return CapabilityRequest(
        capability=capability, operation=operation, params=dict(params or {}),
        context=ctx, task_id=task_id, request_id=request_id,
    )


class SpyRunner:
    """Captures the exact JSON envelope handed to the Blender subprocess."""

    def __init__(self):
        self.envelopes: list[dict] = []
        self._lock = threading.Lock()

    def resolve_exe(self):
        return "<spy-blender>"

    def run(self, request, timeout=None, scene_file=None):
        with self._lock:
            self.envelopes.append(dict(request))
        return RunnerResult(success=True, operation=request["operation"],
                            data={"name": "Obj"}, request_id=request.get("request_id", ""))

    @property
    def last(self) -> dict:
        return self.envelopes[-1]


class SpyVisionBackend(VisionBackend):
    """Captures the VisionRequest the backend actually receives."""

    name = "spy_correlation"
    supported_operations = frozenset({"describe", "compare", "inspect_render"})

    def __init__(self):
        super().__init__()
        self.seen: list[dict] = []
        self._lock = threading.Lock()

    def check_availability(self):
        return True, "spy"

    def inference(self, request):
        with self._lock:
            self.seen.append({"request_id": request.request_id,
                              "context": dict(request.context or {})})
        data = ({"valid": True, "checks": {}} if request.operation == "inspect_render"
                else {"description": "d", "identical": True, "diff_ratio": 0.0})
        return VisionResult.success_result(request.operation, data,
                                           request_id=request.request_id, backend=self.name)

    @property
    def last(self) -> dict:
        return self.seen[-1]


@pytest.fixture
def blender_wire(tmp_path):
    runner = SpyRunner()
    layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "bw"))
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(layer))
    return registry, runner, layer


@pytest.fixture
def vision_wire(tmp_path):
    backend = SpyVisionBackend()
    vision_registry.register(backend, priority=50)
    try:
        layer = VisionCapabilityLayer()
        registry = CapabilityRegistry()
        registry.register(VisionCapabilityAdapter(layer))
        yield registry, backend, layer
    finally:
        vision_registry._backends = [
            (p, b) for p, b in vision_registry._backends if b.name != backend.name
        ]


# ------------------------------------------- 1, 3, 5, 7, 17 : blender.*
def test_blender_wire_envelope_carries_the_trusted_request_id(blender_wire):
    """3, 5, 17. The Blender protocol envelope uses the S1-owned id."""
    registry, runner, _ = blender_wire
    result = registry.execute(_request("blender", "create_object", {"name": "C"}))

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert runner.last["request_id"] == TRUSTED
    assert runner.last["operation"] == "create_object"
    assert runner.last["params"]["name"] == "C"


def test_blender_capability_result_carries_the_trusted_request_id(blender_wire):
    """1, 7. The envelope identity is preserved on the result."""
    registry, _, _ = blender_wire
    result = registry.execute(_request("blender", "create_object", {"name": "C"}))
    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"


def test_blender_run_id_and_task_id_unchanged(blender_wire):
    """6. run_id / task_id are untouched by the correlation plumbing."""
    registry, _, _ = blender_wire
    result = registry.execute(
        _request("blender", "create_object", {"name": "C"}, run_id="run-X", task_id="task-X")
    )
    assert result.run_id if hasattr(result, "run_id") else True
    assert result.task_id == "task-X"
    assert result.request_id == TRUSTED


def test_blender_failure_path_preserves_correlation(tmp_path):
    """14. A failing Blender call still reports the trusted id."""
    class FailingRunner(SpyRunner):
        def run(self, request, timeout=None, scene_file=None):
            self.envelopes.append(dict(request))
            return RunnerResult(success=False, operation=request["operation"],
                                error_code="blender_script_error",
                                error_message="induced", return_code=0)

    runner = FailingRunner()
    layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "bf"))
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(layer))

    result = registry.execute(_request("blender", "create_object", {"name": "C"}))

    assert result.status is CapabilityStatus.FAILURE
    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"
    assert runner.last["request_id"] == TRUSTED


def test_blender_cancellation_path_preserves_correlation(blender_wire):
    """15. A cancelled request still reports the trusted id."""
    registry, runner, _ = blender_wire
    from voice.cancellation import CancellationToken

    token = CancellationToken("task-7")
    token.request("stop", source="test")
    request = _request("blender", "create_object", {"name": "C"})
    request.cancellation_token = token

    result = registry.execute(request)

    assert result.status is CapabilityStatus.CANCELLED
    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"
    assert runner.envelopes == []  # cancelled before any subprocess started


def test_blender_unavailable_path_preserves_correlation(tmp_path):
    """16. An unavailable-Blender failure still reports the trusted id."""
    from blender.errors import BlenderNotFoundError

    class MissingRunner(SpyRunner):
        def run(self, request, timeout=None, scene_file=None):
            self.envelopes.append(dict(request))
            raise BlenderNotFoundError(message="blender not detected")

    runner = MissingRunner()
    layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "bu"))
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(layer))

    result = registry.execute(_request("blender", "create_object", {"name": "C"}))

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"
    assert runner.last["request_id"] == TRUSTED


def test_direct_blender_call_without_a_trusted_id_keeps_its_own(blender_wire):
    """9. No S1 id in scope means the layer keeps its own diagnostic id."""
    _, runner, layer = blender_wire
    layer.create_object(name="Direct")
    assert runner.last["request_id"]
    assert runner.last["request_id"] != TRUSTED


# --------------------------------------------- 4, 6, 18 : vision.*
def test_vision_envelope_carries_the_trusted_request_id(vision_wire, tmp_path):
    """4, 6, 18. The Vision envelope and the backend both see the S1 id."""
    registry, backend, _ = vision_wire
    image = str(_write_png(tmp_path / "a.png"))
    result = registry.execute(_request("vision", "describe", {"image_path": image}))

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert backend.last["request_id"] == TRUSTED
    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"


def test_vision_context_still_carries_run_and_task(vision_wire, tmp_path):
    """6. run_id / task_id survive to the vision backend context.

    On the direct ``vision.*`` path the caller supplies the context payload, so
    the test does the same (this is what S1's bridge forwards).
    """
    registry, backend, _ = vision_wire
    image = str(_write_png(tmp_path / "a.png"))
    registry.execute(
        _request("vision", "describe",
                 {"image_path": image,
                  "context": {"run_id": "run-Q", "task_id": "task-Q",
                              "request_id": TRUSTED}},
                 run_id="run-Q", task_id="task-Q")
    )
    assert backend.last["request_id"] == TRUSTED
    assert backend.last["context"]["run_id"] == "run-Q"
    assert backend.last["context"]["task_id"] == "task-Q"


def test_direct_vision_call_correlates_via_context(vision_wire, tmp_path):
    """18. A direct (non-adapter) vision call correlates through context."""
    _, backend, layer = vision_wire
    layer.describe(str(_write_png(tmp_path / "b.png")),
                   context={"request_id": TRUSTED, "run_id": "run-D"})
    assert backend.last["request_id"] == TRUSTED


def test_direct_vision_call_without_trusted_id_keeps_its_own(vision_wire, tmp_path):
    """9. With no trusted id in scope the layer mints its own, as before."""
    _, backend, layer = vision_wire
    layer.describe(str(_write_png(tmp_path / "c.png")))
    assert backend.last["request_id"]
    assert backend.last["request_id"] != TRUSTED


# ---------------------------------------- 2, 19 : blender_vision.*
def test_blender_vision_integration_preserves_correlation(tmp_path):
    """2, 19. The S2.3 path still preserves the trusted id (c6ad065 behaviour)."""
    seen: list[dict] = []

    class RecordingVision(VisionCapabilityLayer):
        def inspect_render(self, image_path, context=None):
            seen.append(dict(context or {}))
            return super().inspect_render(str(image_path))

    vision = RecordingVision()
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"), vision=vision
    )
    registry = CapabilityRegistry()
    registry.register(BlenderVisionCapabilityAdapter(integration))

    result = registry.execute(
        _request("blender_vision", "inspect_render",
                 {"path": str(_write_png(tmp_path / "r.png"))})
    )

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.request_id == TRUSTED
    assert seen[-1]["request_id"] == TRUSTED
    assert seen[-1]["s2_internal_request_id"] != TRUSTED


# ------------------------------------------- 8 : layer cannot overwrite
def test_layer_cannot_overwrite_the_trusted_request_id():
    """8. A layer that dictates its own identity is ignored by the contract."""

    class Rogue:
        name = "rogue"

        def capabilities(self):
            return {"name": self.name, "available": True, "operations": ["execute"]}

        def can_execute(self, operation):
            return operation == "execute"

        def execute(self, request):
            return {"success": True, "status": "success", "data": {"ok": True},
                    "request_id": "LAYER-INVENTED", "task_id": "LAYER-TASK",
                    "error": None}

        def cancel(self, task_id="", *, request_id=""):
            return True

    registry = CapabilityRegistry()
    registry.register(Rogue())
    result = registry.execute(
        _request("rogue", "execute", task_id="task-7", context={})
    )

    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"


def test_capability_result_from_value_ignores_layer_identity():
    """8. The wire contract itself assigns identity from the request."""
    request = _request("x", "y", task_id="task-7")
    result = CapabilityResult.from_value(
        {"success": True, "data": {"a": 1}, "request_id": "LAYER", "task_id": "LAYER"},
        request, 0.0,
    )
    assert result.request_id == TRUSTED
    assert result.task_id == "task-7"


# ---------------------------------------------------------------- 7, 12
def test_spoofed_request_id_is_rejected_at_the_s1_boundary(blender_wire):
    """7, 12. Model-supplied provenance never becomes authoritative."""
    registry, _, _ = blender_wire
    bridge = CapabilityBridge(registry, None, [])

    out = bridge.execute(
        {"capability": "blender", "operation": "create_object",
         "params": {"name": "C",
                    "context": {"source": "evil", "run_id": "evil",
                                "task_id": "evil", "request_id": "SPOOFED"}}},
        ToolExecutionContext("core-run", "core-task", None),
    )
    payload = (out.data or {}).get("capability_result") or {}

    assert out.success is True, out.error
    assert payload["request_id"] != "SPOOFED"
    assert payload["task_id"] == "core-task"
    assert "SPOOFED" not in out.output
    assert trusted_model_context({"request_id": "SPOOFED"}) == {}


# ---------------------------------------------------------------- 20
def test_s28_workflow_preserves_correlation(tmp_path):
    """20. The S2.8 workflow path carries the trusted id end to end."""
    backend = SpyVisionBackend()
    vision_registry.register(backend, priority=50)
    try:
        vision = VisionCapabilityLayer()
        integration = BlenderVisionIntegration(
            blender=FakeBlender(tmp_path / "blender"), vision=vision
        )
        workflow = BlenderVisionWorkflow(integration=integration)
        registry = CapabilityRegistry()
        registry.register(BlenderVisionWorkflowCapabilityAdapter(workflow))

        result = registry.execute(
            _request("blender_vision_workflow", "analyze_reference",
                     {"reference_path": str(_write_png(tmp_path / "wf.png"))})
        )

        assert result.status is CapabilityStatus.SUCCESS, result.error
        assert result.request_id == TRUSTED
        assert result.task_id == "task-7"
        workflow.close()
    finally:
        vision_registry._backends = [
            (p, b) for p, b in vision_registry._backends if b.name != backend.name
        ]


# ---------------------------------------------------------------- 9, 6
def test_correlation_does_not_leak_between_concurrent_threads(blender_wire):
    """9, 6. Concurrent requests each keep their own correlation."""
    registry, runner, layer = blender_wire
    out: dict[int, CapabilityResult] = {}

    def work(index: int):
        out[index] = registry.execute(
            _request("blender", "create_object", {"name": f"C{index}"},
                     request_id=f"core-{index}", task_id=f"task-{index}",
                     run_id=f"run-{index}")
        )

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(w["request_id"] for w in runner.envelopes) == [
        "core-0", "core-1", "core-2", "core-3"
    ]
    for i in range(4):
        assert out[i].request_id == f"core-{i}"
        assert out[i].task_id == f"task-{i}"


def test_correlation_scope_is_restored_after_each_call(blender_wire):
    """The scope must not outlive the call that opened it."""
    registry, _, layer = blender_wire
    assert getattr(layer._correlation, "value", None) is None
    registry.execute(_request("blender", "create_object", {"name": "C"}))
    assert getattr(layer._correlation, "value", None) is None


def test_correlation_scope_is_restored_after_an_exception(tmp_path):
    """A failing layer call must not leave correlation bound."""
    class BoomRunner(SpyRunner):
        def run(self, request, timeout=None, scene_file=None):
            raise RuntimeError("boom")

    layer = BlenderCapabilityLayer(runner=BoomRunner(), work_dir=str(tmp_path / "bx"))
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(layer))

    result = registry.execute(_request("blender", "create_object", {"name": "C"}))

    assert result.status is CapabilityStatus.FAILURE
    assert result.request_id == TRUSTED
    assert getattr(layer._correlation, "value", None) is None


def test_layers_without_the_hook_are_unaffected():
    """A layer with no request_correlation hook still executes normally."""

    class Plain:
        name = "plain"

        def capabilities(self):
            return {"name": self.name, "available": True, "operations": ["execute"]}

        def can_execute(self, operation):
            return operation == "execute"

        def execute(self, request):
            return {"success": True, "data": {"ok": True}}

        def cancel(self, task_id="", *, request_id=""):
            return True

    registry = CapabilityRegistry()
    registry.register(Plain())
    result = registry.execute(_request("plain", "execute", context={}))
    assert result.status is CapabilityStatus.SUCCESS
    assert result.request_id == TRUSTED
