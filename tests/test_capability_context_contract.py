"""Cross-layer context dispatch contract for capability.execute.

The bridge owns context/provenance handling. Model-supplied context is
untrusted and is forwarded only to operations whose dispatch contract declares
a ``context`` parameter; operations that declare none must never receive an
unexpected ``context`` keyword.

Covered here:
  A. direct S2.1 operations that previously raised TypeError
  B. direct S2.2 operations that previously raised TypeError
  C. operations that legitimately consume context still receive it
  D. provenance spoofing is still rejected
  E. trusted request identity is unchanged
  F. benign model context survives where the contract allows it
"""
import inspect
import json

import pytest

from blender.capabilities import BlenderCapabilityLayer
from config.permissions import PermissionManager
from core.events import EventBus
from core.executor import Executor
from integration import (
    BlenderCapabilityAdapter,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityStatus,
    VisionCapabilityAdapter,
)
from tools import build_registry
from tools.capability_bridge import (
    CONTEXT_FREE_CAPABILITIES,
    CONTEXT_FREE_OPERATIONS,
    UNTRUSTED_PROVENANCE_KEYS,
    dispatch_params,
    operation_accepts_context,
    trusted_model_context,
)
from vision.capabilities import VisionCapabilityLayer

HOSTILE = {"source": "spoofed", "run_id": "spoofed",
           "task_id": "spoofed", "request_id": "spoofed"}


# ---------------------------------------------------------------------------
# contract declaration
# ---------------------------------------------------------------------------

def test_untrusted_key_set_is_unchanged():
    assert UNTRUSTED_PROVENANCE_KEYS == {"source", "run_id", "task_id", "request_id"}


def test_blender_is_declared_context_free():
    assert "blender" in CONTEXT_FREE_CAPABILITIES
    assert operation_accepts_context("blender", "inspect_scene") is False
    assert operation_accepts_context("blender", "render") is False


def test_context_free_vision_operations_are_declared():
    assert ("vision", "validate_image") in CONTEXT_FREE_OPERATIONS
    assert ("vision", "inspect") in CONTEXT_FREE_OPERATIONS
    assert operation_accepts_context("vision", "validate_image") is False
    assert operation_accepts_context("vision", "inspect") is False


@pytest.mark.parametrize("operation", ["describe", "compare", "inspect_render",
                                       "detect_objects", "extract_text", "analyze_ui"])
def test_context_aware_vision_operations_still_accept_context(operation):
    assert operation_accepts_context("vision", operation) is True


def test_integration_capabilities_accept_context():
    for operation in ("render_and_inspect", "render_and_describe", "render_and_compare",
                      "inspect_render", "verify_render", "describe_render", "compare_renders"):
        assert operation_accepts_context("blender_vision", operation) is True
    for operation in ("build_scene", "run", "render_and_verify", "inspect_scene_state",
                      "analyze_reference", "compare_reference"):
        assert operation_accepts_context("blender_vision_workflow", operation) is True


def test_declaration_matches_the_real_layer_signatures():
    """The declaration must agree with the actual S2 layer surface."""
    for operation in BlenderCapabilityAdapter._operations:
        method = getattr(BlenderCapabilityLayer, operation, None)
        if method is None:
            continue
        accepts = "context" in inspect.signature(method).parameters
        assert accepts is operation_accepts_context("blender", operation), operation
    for operation in VisionCapabilityAdapter._operations:
        method = getattr(VisionCapabilityLayer, operation, None)
        if method is None:
            continue
        accepts = "context" in inspect.signature(method).parameters
        assert accepts is operation_accepts_context("vision", operation), operation


# ---------------------------------------------------------------------------
# dispatch_params unit behaviour
# ---------------------------------------------------------------------------

def test_dispatch_params_drops_context_for_context_free_capability():
    params = {"name": "Cube", "context": dict(HOSTILE)}
    out = dispatch_params("blender", "create_object", params)
    assert out == {"name": "Cube"}
    assert "context" not in out


def test_dispatch_params_drops_context_for_context_free_operation():
    out = dispatch_params("vision", "validate_image",
                          {"image_path": "a.png", "context": dict(HOSTILE)})
    assert out == {"image_path": "a.png"}


def test_dispatch_params_sanitises_context_for_context_aware_operation():
    out = dispatch_params("vision", "describe",
                          {"image_path": "a.png", "context": {**HOSTILE, "focus": "lighting"}})
    assert out["context"] == {"focus": "lighting"}


def test_dispatch_params_leaves_unrelated_params_untouched():
    params = {"path": "x.png", "resolution_x": 64, "img_format": "PNG"}
    assert dispatch_params("blender", "render", dict(params)) == params


def test_dispatch_params_is_a_noop_without_context():
    params = {"path": "x.png"}
    assert dispatch_params("blender", "render", dict(params)) == params


def test_dispatch_params_ignores_non_dict_context():
    params = {"context": "not-a-dict"}
    assert dispatch_params("blender", "render", params) == params
    assert dispatch_params("vision", "describe", params) == params


# ---------------------------------------------------------------------------
# end-to-end through capability.execute
# ---------------------------------------------------------------------------

@pytest.fixture
def stack(tmp_settings, tmp_path):
    blender = BlenderCapabilityLayer(work_dir=tmp_path / "blender-work")
    vision = VisionCapabilityLayer()
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(blender))
    registry.register(VisionCapabilityAdapter(vision))
    bus = EventBus()
    tools = build_registry(tmp_settings, capability_registry=registry,
                           capability_events=bus)
    executor = Executor(tools, PermissionManager(tmp_settings), bus, lambda t, d: "allow",
                        event_context=lambda: {"run_id": "run-gate", "task_id": "task-gate"})
    yield executor, blender, vision, bus
    blender.close()


def _png(path):
    from PIL import Image
    Image.new("RGB", (8, 8), (10, 200, 10)).save(path, format="PNG")
    return str(path)


def _run(executor, capability, operation, params):
    outcome = executor.run("capability.execute",
                           {"capability": capability, "operation": operation, "params": params})
    return outcome, json.loads(outcome.output)


# --- A. direct S2.1 operations -------------------------------------------

@pytest.mark.parametrize("operation,params", [
    ("inspect_scene", {}),
    ("list_objects", {}),
    ("render", {"path": "gate-render.png"}),
    ("save", {"path": "gate-save.blend"}),
])
def test_a_direct_blender_operations_accept_a_model_context(stack, tmp_path, operation, params):
    executor, blender, _, _ = stack
    # Every path is resolved inside tmp_path so the test never writes into the repo.
    resolved = {k: str(tmp_path / v) if isinstance(v, str) else v for k, v in params.items()}
    outcome, payload = _run(executor, "blender", operation,
                            {**resolved, "context": dict(HOSTILE)})
    assert payload["status"] == "success", payload.get("error")
    assert outcome.success is True
    assert "TypeError" not in json.dumps(payload)


def test_a_blender_create_object_no_longer_leaks_context_into_extra(stack):
    """create_object takes **extra, so a stray context used to be swallowed silently."""
    executor, blender, _, _ = stack
    seen = {}
    original = blender.create_object

    def spy(*a, **kw):
        seen.update(kw)
        return original(*a, **kw)

    blender.create_object = spy
    outcome, payload = _run(executor, "blender", "create_object",
                            {"name": "CtxCube", "context": dict(HOSTILE)})
    assert payload["status"] == "success", payload.get("error")
    assert "context" not in seen


# --- B. direct S2.2 operations -------------------------------------------

@pytest.mark.parametrize("operation", ["validate_image", "inspect"])
def test_b_direct_vision_operations_accept_a_model_context(stack, tmp_path, operation):
    executor, _, _, _ = stack
    image = _png(tmp_path / "gate.png")
    outcome, payload = _run(executor, "vision", operation,
                            {"image_path": image, "context": dict(HOSTILE)})
    assert payload["status"] == "success", payload.get("error")
    assert "TypeError" not in json.dumps(payload)


# --- C. context-consuming operations still receive it ---------------------

def test_c_vision_describe_still_receives_sanitised_context(stack, tmp_path):
    executor, _, vision, _ = stack
    seen = []
    original = vision.describe

    def spy(image_path, detail_level="medium", focus=None, context=None):
        seen.append(dict(context or {}))
        return original(image_path, detail_level=detail_level, focus=focus, context=context)

    vision.describe = spy
    image = _png(tmp_path / "gate2.png")
    outcome, payload = _run(executor, "vision", "describe",
                            {"image_path": image,
                             "context": {**HOSTILE, "focus": "lighting"}})
    assert payload["status"] == "success", payload.get("error")
    assert seen and seen[-1] == {"focus": "lighting"}


def test_c_vision_compare_still_succeeds_with_context(stack, tmp_path):
    executor, _, _, _ = stack
    image = _png(tmp_path / "gate3.png")
    outcome, payload = _run(executor, "vision", "compare",
                            {"image_path": image, "reference_image": image,
                             "threshold": 0.01, "context": {**HOSTILE, "focus": "lighting"}})
    assert payload["status"] == "success", payload.get("error")


# --- D/E/F. provenance and trusted identity ------------------------------

class _Probe:
    name = "probe"

    def __init__(self):
        self.requests = []

    def capabilities(self):
        return {"name": self.name, "available": True, "operations": ["execute"]}

    def can_execute(self, operation):
        return operation == "execute"

    def execute(self, request):
        self.requests.append(request)
        return CapabilityResult(status=CapabilityStatus.SUCCESS, capability=self.name,
                                operation="execute", data={"ok": True},
                                request_id=request.request_id, task_id=request.task_id)

    def cancel(self, task_id="", *, request_id=""):
        return True


def _probe_stack(tmp_settings):
    probe = _Probe()
    registry = CapabilityRegistry()
    registry.register(probe)
    bus = EventBus()
    tools = build_registry(tmp_settings, capability_registry=registry, capability_events=bus)
    executor = Executor(tools, PermissionManager(tmp_settings), bus, lambda t, d: "allow",
                        event_context=lambda: {"run_id": "run-gate", "task_id": "task-gate"})
    return executor, probe


def test_d_model_provenance_is_still_stripped(tmp_settings):
    executor, probe = _probe_stack(tmp_settings)
    outcome, payload = _run(executor, "probe", "execute",
                            {"context": {**HOSTILE, "keep": "me"}})
    request = probe.requests[-1]
    assert request.params["context"] == {"keep": "me"}
    assert not any(key in request.params["context"] for key in UNTRUSTED_PROVENANCE_KEYS)


def test_d_trusted_context_is_core_owned(tmp_settings):
    executor, probe = _probe_stack(tmp_settings)
    _run(executor, "probe", "execute", {"context": dict(HOSTILE)})
    context = probe.requests[-1].context
    assert context["source"] == "s1_core"
    assert context["run_id"] == "run-gate"
    assert context["task_id"] == "task-gate"


def test_e_registry_request_id_remains_authoritative(tmp_settings):
    executor, probe = _probe_stack(tmp_settings)
    outcome, payload = _run(executor, "probe", "execute",
                            {"context": {"request_id": "spoofed"}})
    request = probe.requests[-1]
    assert request.context["request_id"] == request.request_id
    assert request.request_id != "spoofed"
    assert payload["request_id"] == request.request_id


def test_e_task_id_identity_unchanged(tmp_settings):
    executor, probe = _probe_stack(tmp_settings)
    _run(executor, "probe", "execute", {"context": {"task_id": "spoofed"}})
    request = probe.requests[-1]
    assert request.task_id == "task-gate"
    assert request.context["task_id"] == "task-gate"


def test_f_benign_context_preserved_where_the_contract_allows(tmp_settings):
    executor, probe = _probe_stack(tmp_settings)
    _run(executor, "probe", "execute",
         {"context": {"focus": "lighting", "threshold": 0.2, "tags": ["a"]}})
    assert probe.requests[-1].params["context"] == {
        "focus": "lighting", "threshold": 0.2, "tags": ["a"]}


def test_g_capability_execute_itself_is_unaffected(tmp_settings):
    executor, probe = _probe_stack(tmp_settings)
    outcome, payload = _run(executor, "probe", "execute", {})
    assert payload["status"] == "success"
    assert set(payload) >= {"status", "capability", "operation", "data", "error",
                            "request_id", "task_id", "metadata", "duration_ms"}


def test_trusted_model_context_helper_is_unchanged():
    assert trusted_model_context({**HOSTILE, "a": 1}) == {"a": 1}
    assert trusted_model_context(None) == {}
    assert trusted_model_context("x") == {}
