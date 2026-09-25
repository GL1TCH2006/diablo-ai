"""Focused tests for the S2.3 Blender–Vision integration layer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blender.capabilities import BlenderCapabilityLayer
from blender.errors import BlenderNotFoundError, BlenderProcessError
from blender.runner import RunnerResult
from integration import (
    BlenderUnavailableError,
    BlenderVisionError,
    BlenderVisionIntegration,
    BlenderVisionCapabilityAdapter,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    ComparisonError,
    IntegrationResult,
    InvalidRenderError,
    RenderFailureError,
    VisionBackendUnavailableError,
    VisionOperationUnsupportedError,
    create_blender_vision_integration,
)
from vision.capabilities import VisionCapabilityLayer
from vision.errors import (
    VisionBackendNotFoundError,
    VisionError,
    VisionUnsupportedOperationError,
)


def _png(width: int = 64, height: int = 64) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x20\x30\x40" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeBlender:
    def __init__(self, root: Path, *, error: Exception | None = None, write: bool = True):
        self.root = Path(root)
        self.error = error
        self.write = write
        self.render_calls: list[tuple[str, dict]] = []
        self.closed = False
        self._last_error: dict | None = None

    def render(self, path, **kwargs):
        self.render_calls.append((str(path), dict(kwargs)))
        if self.error is not None:
            self._last_error = (
                self.error.to_dict() if hasattr(self.error, "to_dict") else {"message": str(self.error)}
            )
            raise self.error
        target = Path(path)
        if self.write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_png())
        return {
            "path": str(target),
            "exists": self.write,
            "size_bytes": target.stat().st_size if self.write else 0,
        }

    def last_error(self):
        return self._last_error

    def close(self):
        self.closed = True


class FakeVision:
    def __init__(self, *, error=None, compare_result=None, inspect_result=None):
        self.error = error
        self.compare_result = compare_result
        self.inspect_result = inspect_result
        self.calls: list[tuple[str, dict]] = []
        self.contexts: list[dict] = []
        self._last_error: dict | None = None

    def validate_image(self, path):
        self.calls.append(("validate_image", {"path": str(path)}))
        if self.error and self.error[0] == "validate_image":
            raise self.error[1]
        return {"path": str(path), "readable": True}

    def inspect_render(self, path, context=None):
        self.calls.append(("inspect_render", {"path": str(path)}))
        self.contexts.append(dict(context or {}))
        if self.error and self.error[0] == "inspect_render":
            raise self.error[1]
        return self.inspect_result or {"valid": True, "checks": {"readable": True}}

    def describe(self, path, **kwargs):
        self.calls.append(("describe", {"path": str(path), **kwargs}))
        self.contexts.append(dict(kwargs.get("context") or {}))
        if self.error and self.error[0] == "describe":
            raise self.error[1]
        return {"description": "deterministic fake description"}

    def compare(self, path, reference_path, **kwargs):
        self.calls.append(
            (
                "compare",
                {"path": str(path), "reference_path": str(reference_path), **kwargs},
            )
        )
        self.contexts.append(dict(kwargs.get("context") or {}))
        if self.error and self.error[0] == "compare":
            raise self.error[1]
        return self.compare_result or {"success": True, "identical": True}

    def last_error(self):
        return self._last_error


class FakeRunner:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.calls = []

    def resolve_exe(self):
        return "fake-blender"

    def run(self, request, timeout=None, scene_file=None):
        self.calls.append(request)
        operation = request["operation"]
        if operation == "render":
            target = Path(request["params"]["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_png(64, 64))
            return RunnerResult(
                True,
                operation,
                data={"path": str(target), "exists": True, "size_bytes": target.stat().st_size},
            )
        return RunnerResult(True, operation, data={})


def _integration(tmp_path, *, blender=None, vision=None):
    return BlenderVisionIntegration(
        blender=blender or FakeBlender(tmp_path / "blender"),
        vision=vision or VisionCapabilityLayer(),
    )


def test_public_api_exports():
    assert callable(create_blender_vision_integration)
    assert IntegrationResult.success_result("x", {}).to_dict()["success"] is True
    assert issubclass(BlenderVisionError, Exception)
    assert issubclass(InvalidRenderError, BlenderVisionError)
    assert issubclass(ComparisonError, BlenderVisionError)
    assert issubclass(VisionBackendUnavailableError, BlenderVisionError)
    assert issubclass(VisionOperationUnsupportedError, BlenderVisionError)


def test_render_to_vision_inspection(tmp_path):
    blender = FakeBlender(tmp_path / "blender")
    integration = _integration(tmp_path, blender=blender)
    result = integration.render_and_inspect(tmp_path / "render.png")
    assert result.success is True
    assert result.data["valid"] is True
    assert result.data["render"]["exists"] is True
    assert result.data["inspection"]["checks"]["readable"] is True
    assert result.data["render_path"].endswith("render.png")
    assert json.dumps(result.to_dict())


def test_render_to_vision_description(tmp_path):
    integration = _integration(tmp_path)
    result = integration.render_and_describe(
        tmp_path / "render.png",
        detail_level="high",
        focus="objects",
    )
    assert result.success is True
    assert result.data["description"]["description"]
    assert result.data["image"]["width"] == 64
    assert json.dumps(result.to_dict())


def test_render_options_are_forwarded(tmp_path):
    blender = FakeBlender(tmp_path / "blender")
    integration = _integration(tmp_path, blender=blender)
    integration.render_and_inspect(
        tmp_path / "render.png",
        resolution_x=320,
        resolution_y=240,
        img_format="PNG",
        engine="BLENDER_EEVEE",
        use_gpu=False,
    )
    _, options = blender.render_calls[0]
    assert options["resolution_x"] == 320
    assert options["resolution_y"] == 240
    assert options["img_format"] == "PNG"
    assert options["engine"] == "BLENDER_EEVEE"
    assert options["use_gpu"] is False


def test_compare_existing_renders_with_fake_fallback(tmp_path):
    current = tmp_path / "current.png"
    reference = tmp_path / "reference.png"
    current.write_bytes(_png())
    reference.write_bytes(_png())
    vision = FakeVision()
    integration = _integration(tmp_path, vision=vision)
    result = integration.compare_renders(current, reference, threshold=0.02)
    assert result.success is True
    assert result.data["comparison"]["identical"] is True
    assert result.data["reference_path"] == str(reference)


def test_render_and_compare_with_real_fallback(tmp_path):
    current = tmp_path / "current.png"
    reference = tmp_path / "reference.png"
    current.write_bytes(_png())
    reference.write_bytes(_png())
    integration = _integration(tmp_path)
    result = integration.render_and_compare(
        tmp_path / "render.png",
        reference,
    )
    assert result.success is True
    assert result.data["comparison"]["identical"] is True


def test_verify_render_returns_structured_validity(tmp_path):
    path = tmp_path / "render.png"
    path.write_bytes(_png())
    integration = _integration(tmp_path)
    result = integration.verify_render(path)
    assert result.success is True
    assert result.data["valid"] is True
    assert result.data["inspection"]["checks"]["valid_format"] is True


def test_missing_render_is_structured_failure(tmp_path):
    blender = FakeBlender(tmp_path / "blender", write=False)
    integration = _integration(tmp_path, blender=blender)
    with pytest.raises(InvalidRenderError) as exc_info:
        integration.render_and_inspect(tmp_path / "missing.png")
    error = exc_info.value.to_dict()
    assert error["error"] == "invalid_render"
    assert error["stage"] == "render_validation"
    assert integration.last_error() == error
    assert json.dumps(error)


def test_corrupt_render_is_structured_failure(tmp_path):
    blender = FakeBlender(tmp_path / "blender", write=True)
    original_render = blender.render

    def corrupt_render(path, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not an image")
        return {"path": str(target), "exists": True, "size_bytes": target.stat().st_size}

    blender.render = corrupt_render
    integration = _integration(tmp_path, blender=blender)
    with pytest.raises(InvalidRenderError):
        integration.render_and_inspect(tmp_path / "corrupt.png")
    assert integration.last_error()["error"] == "invalid_render"
    assert original_render is not None


def test_blender_unavailable_is_classified(tmp_path):
    source = BlenderNotFoundError(operation="render", message="Blender not found")
    integration = _integration(tmp_path, blender=FakeBlender(tmp_path, error=source))
    with pytest.raises(BlenderUnavailableError) as exc_info:
        integration.render_and_inspect(tmp_path / "render.png")
    assert exc_info.value.to_dict()["error"] == "blender_unavailable"
    assert exc_info.value.source_error["error"] == "blender_not_found"


def test_blender_render_failure_is_classified(tmp_path):
    source = BlenderProcessError(operation="render", message="Blender crashed", return_code=2)
    integration = _integration(tmp_path, blender=FakeBlender(tmp_path, error=source))
    with pytest.raises(RenderFailureError) as exc_info:
        integration.render_and_inspect(tmp_path / "render.png")
    payload = exc_info.value.to_dict()
    assert payload["error"] == "blender_render_failed"
    assert payload["source_error"]["return_code"] == 2
    assert payload["source_last_error"]["return_code"] == 2


def test_vision_backend_unavailable_is_classified(tmp_path):
    source = VisionBackendNotFoundError(operation="inspect_render", message="no backend")
    vision = FakeVision(error=("inspect_render", source))
    integration = _integration(tmp_path, vision=vision)
    path = tmp_path / "render.png"
    path.write_bytes(_png())
    with pytest.raises(VisionBackendUnavailableError) as exc_info:
        integration.inspect_render(path)
    assert exc_info.value.to_dict()["error"] == "vision_backend_unavailable"


def test_vision_unsupported_operation_is_classified(tmp_path):
    source = VisionUnsupportedOperationError(operation="describe", message="no semantic backend")
    vision = FakeVision(error=("describe", source))
    integration = _integration(tmp_path, vision=vision)
    path = tmp_path / "render.png"
    path.write_bytes(_png())
    with pytest.raises(VisionOperationUnsupportedError) as exc_info:
        integration.describe_render(path)
    assert exc_info.value.to_dict()["error"] == "vision_operation_unsupported"


def test_comparison_exception_is_classified(tmp_path):
    source = VisionError(
        operation="compare",
        message="different dimensions",
        code="comparison_failed",
    )
    vision = FakeVision(error=("compare", source))
    integration = _integration(tmp_path, vision=vision)
    current = tmp_path / "current.png"
    reference = tmp_path / "reference.png"
    current.write_bytes(_png())
    reference.write_bytes(_png())
    with pytest.raises(ComparisonError) as exc_info:
        integration.compare_renders(current, reference)
    assert exc_info.value.to_dict()["error"] == "comparison_failed"


def test_comparison_failure_payload_is_classified(tmp_path):
    vision = FakeVision(
        compare_result={
            "success": False,
            "error": {"code": "comparison_failed", "message": "pixel comparison failed"},
        }
    )
    integration = _integration(tmp_path, vision=vision)
    current = tmp_path / "current.png"
    reference = tmp_path / "reference.png"
    current.write_bytes(_png())
    reference.write_bytes(_png())
    with pytest.raises(ComparisonError):
        integration.compare_renders(current, reference)


def test_result_and_error_serialization_is_json_safe(tmp_path):
    integration = _integration(tmp_path)
    result = integration.render_and_inspect(tmp_path / "render.png")
    assert json.loads(json.dumps(result.to_dict()))["success"] is True

    failure = BlenderProcessError(operation="render", message="failure", return_code=1)
    integration = _integration(
        tmp_path,
        blender=FakeBlender(tmp_path / "other", error=failure),
    )
    with pytest.raises(RenderFailureError) as exc_info:
        integration.render_and_inspect(tmp_path / "render.png")
    assert json.loads(json.dumps(exc_info.value.to_dict()))["source_error"]["return_code"] == 1


def test_repeated_integration_calls_have_independent_correlation_ids(tmp_path):
    integration = _integration(tmp_path)
    first = integration.render_and_inspect(tmp_path / "first.png")
    second = integration.render_and_inspect(tmp_path / "second.png")
    assert first.request_id != second.request_id
    assert first.data["render_path"].endswith("first.png")
    assert second.data["render_path"].endswith("second.png")
    assert integration.last_error() is None


def test_context_is_correlated_to_vision_request(tmp_path):
    vision = FakeVision()
    integration = _integration(tmp_path, vision=vision)
    result = integration.inspect_render(_write_png(tmp_path / "render.png"), context={"scene": "demo"})
    context = vision.contexts[-1]
    assert context["scene"] == "demo"
    assert context["source"] == "blender_vision_integration"
    assert context["integration_operation"] == "inspect_render"
    assert context["request_id"] == result.request_id


def test_injected_layers_are_not_closed_by_integration(tmp_path):
    blender = FakeBlender(tmp_path / "blender")
    vision = VisionCapabilityLayer()
    integration = BlenderVisionIntegration(blender=blender, vision=vision)
    integration.close()
    assert blender.closed is False
    assert integration.closed is True


def test_owned_blender_session_is_released(tmp_path):
    integration = BlenderVisionIntegration(work_dir=tmp_path / "owned")
    session_file = integration.blender.session_file
    session_file.write_text("session", encoding="utf-8")
    integration.close()
    assert not session_file.exists()


def test_context_manager_closes_integration(tmp_path):
    integration = _integration(tmp_path)
    with integration as entered:
        assert entered is integration
    assert integration.closed is True


def test_capability_report_is_structured(tmp_path):
    integration = _integration(tmp_path)
    report = integration.capabilities()
    assert report["integration"] == "blender_vision"
    assert report["vision"]["fallback_available"] is True
    assert "inspect_render" in report["vision"]["supported_operations"]
    assert json.dumps(report)


def test_existing_s21_layer_can_be_composed(tmp_path):
    runner = FakeRunner(tmp_path)
    blender = BlenderCapabilityLayer(runner=runner, work_dir=tmp_path / "work")
    integration = BlenderVisionIntegration(blender=blender, vision=VisionCapabilityLayer())
    result = integration.render_and_describe(tmp_path / "render.png")
    assert result.success is True
    assert result.data["description"]["description"]
    assert runner.calls[0]["operation"] == "render"


def test_existing_s22_layer_can_be_composed_independently(tmp_path):
    vision = VisionCapabilityLayer()
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"),
        vision=vision,
    )
    result = integration.describe_render(_write_png(tmp_path / "render.png"))
    assert result.success is True
    assert integration.vision is vision
    assert result.data["description"]["metadata"]["width"] == 64


def test_default_stack_requires_no_gpu_or_vlm(tmp_path):
    integration = _integration(tmp_path)
    report = integration.capabilities()
    assert report["vision"]["available_backends"] == ["fallback"]
    assert all(not backend["requires_gpu"] for backend in report["vision"]["backends"] if backend["available"])
    assert integration.render_and_inspect(tmp_path / "render.png").success is True


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png())
    return path


# ------------------------------------------- S1 request context propagation


class RecordingIntegration:
    """Stand-in for BlenderVisionIntegration that records the context it is given."""

    def __init__(self):
        self.calls = []

    def _record(self, operation, **kwargs):
        context = kwargs.get("context")
        self.calls.append({"operation": operation, "params": dict(kwargs), "context": context})
        return SimpleNamespace(
            success=True,
            data={"render_path": "render.png", "valid": True, "context": context},
            error=None,
            to_dict=lambda: {
                "success": True,
                "data": {"render_path": "render.png", "valid": True, "context": context},
                "error": None,
            },
        )

    def render_and_inspect(self, path, **kwargs):
        return self._record("render_and_inspect", path=path, **kwargs)

    def render_and_describe(self, path, **kwargs):
        return self._record("render_and_describe", path=path, **kwargs)

    def render_and_compare(self, path, reference_path, **kwargs):
        return self._record("render_and_compare", path=path, reference_path=reference_path, **kwargs)

    def inspect_render(self, path, **kwargs):
        return self._record("inspect_render", path=path, **kwargs)

    def verify_render(self, path, **kwargs):
        return self._record("verify_render", path=path, **kwargs)

    def describe_render(self, path, **kwargs):
        return self._record("describe_render", path=path, **kwargs)

    def compare_renders(self, path, reference_path, **kwargs):
        return self._record("compare_renders", path=path, reference_path=reference_path, **kwargs)

    def capabilities(self):
        return {"integration": "blender_vision"}


class _RecordingVision:
    """Vision stub that records the context it is called with."""

    def __init__(self, seen):
        self._seen = seen

    def detect_capabilities(self):
        return {"vision_available": True, "available_backends": ["fallback"]}

    def validate_image(self, image_path):
        return {"path": str(image_path), "readable": True}

    def inspect_render(self, image_path, context=None):
        self._seen["inspect_render"] = context
        return {"valid": True, "checks": {"readable": True}}

    def describe(self, image_path, detail_level="medium", focus=None, context=None):
        self._seen["describe"] = context
        return {"description": "a render", "backend": "fallback"}


S1_CONTEXT = {"source": "s1_core", "run_id": "run-42", "task_id": "task-7"}


def _bv_request(operation="inspect_render", params=None, context=None, request_id="req-s23"):
    return CapabilityRequest(
        capability="blender_vision",
        operation=operation,
        params=dict(params or {}),
        context=dict(S1_CONTEXT if context is None else context),
        task_id="task-7",
        request_id=request_id,
    )


@pytest.mark.parametrize(
    "operation",
    [
        "render_and_inspect",
        "render_and_describe",
        "render_and_compare",
        "inspect_render",
        "verify_render",
        "describe_render",
        "compare_renders",
    ],
)
def test_s23_request_context_reaches_integration(operation):
    """Every S2.3 entry point receives the trusted S1 request context."""
    integration = RecordingIntegration()
    adapter = BlenderVisionCapabilityAdapter(integration)
    params = {"path": "render.png"}
    if operation in {"render_and_compare", "compare_renders"}:
        params["reference_path"] = "reference.png"

    result = adapter.execute(_bv_request(operation, params))

    assert result.status is CapabilityStatus.SUCCESS
    assert integration.calls, "integration was never called"
    seen = integration.calls[0]["context"]
    assert seen["run_id"] == "run-42"
    assert seen["task_id"] == "task-7"
    assert seen["source"] == "s1_core"


def test_s23_context_preserves_existing_params_behaviour():
    """Non-context params are forwarded verbatim and context is not duplicated."""
    integration = RecordingIntegration()
    adapter = BlenderVisionCapabilityAdapter(integration)

    adapter.execute(
        _bv_request(
            "render_and_inspect",
            {"path": "render.png", "resolution_x": 320, "resolution_y": 240, "img_format": "PNG"},
        )
    )

    call = integration.calls[0]
    assert call["params"]["path"] == "render.png"
    assert call["params"]["resolution_x"] == 320
    assert call["params"]["resolution_y"] == 240
    assert call["params"]["img_format"] == "PNG"
    assert "context" in call["params"]
    assert isinstance(call["context"], dict)


def test_s23_caller_context_is_layered_onto_trusted_context():
    """Non-provenance caller context is merged, not dropped."""
    integration = RecordingIntegration()
    adapter = BlenderVisionCapabilityAdapter(integration)

    adapter.execute(
        _bv_request(
            "inspect_render",
            {"path": "render.png", "context": {"focus": "lighting"}},
        )
    )

    seen = integration.calls[0]["context"]
    assert seen["focus"] == "lighting"
    assert seen["run_id"] == "run-42"
    assert seen["source"] == "s1_core"


def test_s23_request_id_preserved_and_internal_correlation_kept(tmp_path):
    """The S1 request identity survives while S2.3 keeps its own correlation id."""
    seen = {}
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"),
        vision=_RecordingVision(seen),
    )
    adapter = BlenderVisionCapabilityAdapter(integration)
    target = _write_png(tmp_path / "render.png")

    result = adapter.execute(_bv_request("inspect_render", {"path": str(target)}, request_id="req-s23"))

    assert result.status is CapabilityStatus.SUCCESS
    # S1 identity is what the caller correlates on
    assert result.request_id == "req-s23"
    assert result.task_id == "task-7"
    # S2.3 still stamps its own internal request id alongside the S1 context
    context = seen["inspect_render"]
    assert context["request_id"]
    assert context["request_id"] != "req-s23"
    assert context["integration_operation"] == "inspect_render"
    assert context["run_id"] == "run-42"
    assert context["task_id"] == "task-7"
    assert context["source"] == "s1_core"


def test_s23_integration_operation_still_stamped(tmp_path):
    """S2.3 keeps stamping integration_operation and its own request id."""
    seen = {}
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"),
        vision=_RecordingVision(seen),
    )
    adapter = BlenderVisionCapabilityAdapter(integration)
    target = _write_png(tmp_path / "render.png")

    adapter.execute(_bv_request("verify_render", {"path": str(target)}))

    context = seen["inspect_render"]
    assert context["integration_operation"] == "verify_render"
    assert context["request_id"]
    assert context["source"] == "s1_core"


def test_s23_internal_request_id_is_unique_per_call(tmp_path):
    """Each S2.3 call keeps its own correlation id."""
    seen = {}
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"),
        vision=_RecordingVision(seen),
    )
    adapter = BlenderVisionCapabilityAdapter(integration)
    target = _write_png(tmp_path / "render.png")

    adapter.execute(_bv_request("inspect_render", {"path": str(target)}, request_id="req-1"))
    first = seen["inspect_render"]["request_id"]
    adapter.execute(_bv_request("inspect_render", {"path": str(target)}, request_id="req-2"))
    second = seen["inspect_render"]["request_id"]

    assert first != second


def test_s23_context_reaches_vision(tmp_path):
    """The propagated context is the one Vision is actually called with."""
    seen = {}
    integration = BlenderVisionIntegration(
        blender=FakeBlender(tmp_path / "blender"),
        vision=_RecordingVision(seen),
    )
    adapter = BlenderVisionCapabilityAdapter(integration)
    target = _write_png(tmp_path / "render.png")

    adapter.execute(_bv_request("inspect_render", {"path": str(target)}))
    assert seen["inspect_render"]["run_id"] == "run-42"
    assert seen["inspect_render"]["task_id"] == "task-7"
    assert seen["inspect_render"]["source"] == "s1_core"
    assert seen["inspect_render"]["integration_operation"] == "inspect_render"

    adapter.execute(_bv_request("describe_render", {"path": str(target)}))
    assert seen["describe"]["run_id"] == "run-42"
    assert seen["describe"]["source"] == "s1_core"


def test_s23_provenance_not_overridable_through_s1_bridge():
    """Model-supplied provenance keys are stripped at the S1 boundary."""
    from tools.capability_bridge import CapabilityBridge
    from tools.registry import ToolExecutionContext

    integration = RecordingIntegration()
    registry = CapabilityRegistry()
    registry.register(BlenderVisionCapabilityAdapter(integration))
    bridge = CapabilityBridge(registry, None, [])

    result = bridge.execute(
        {
            "capability": "blender_vision",
            "operation": "inspect_render",
            "params": {
                "path": "render.png",
                "context": {"source": "spoofed", "run_id": "spoofed", "task_id": "spoofed"},
            },
        },
        ToolExecutionContext("run-99", "task-99", None),
    )

    assert result.success is True
    seen = integration.calls[0]["context"]
    assert seen["source"] == "s1_core"
    assert seen["run_id"] == "run-99"
    assert seen["task_id"] == "task-99"
    assert "spoofed" not in json.dumps(seen)


def test_s23_missing_context_is_tolerated():
    """A request without context still dispatches, with S2.3's own defaults."""
    integration = RecordingIntegration()
    adapter = BlenderVisionCapabilityAdapter(integration)
    request = CapabilityRequest(
        capability="blender_vision",
        operation="inspect_render",
        params={"path": "render.png"},
        context={},
        task_id="",
        request_id="req-empty",
    )

    result = adapter.execute(request)

    assert result.status is CapabilityStatus.SUCCESS
    assert result.request_id == "req-empty"
    assert integration.calls[0]["context"] == {}


def test_s23_adapter_stores_no_per_request_state():
    """Concurrent-safe: the adapter keeps no request state between calls."""
    integration = RecordingIntegration()
    adapter = BlenderVisionCapabilityAdapter(integration)
    adapter.execute(_bv_request("inspect_render", {"path": "a.png"}, request_id="req-a"))
    adapter.execute(_bv_request("describe_render", {"path": "b.png"}, request_id="req-b"))
    assert [call["operation"] for call in integration.calls] == ["inspect_render", "describe_render"]
    assert [call["context"]["run_id"] for call in integration.calls] == ["run-42", "run-42"]
    assert not hasattr(adapter, "_active_request")
    assert [name for name in vars(adapter) if not name.startswith("__")] == ["layer"]