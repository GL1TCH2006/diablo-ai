"""Isolated tests for the S2.2 Vision Intelligence layer.

These tests never touch a GPU, external services, or downloaded models.
All image fixtures are generated in-memory with stdlib-only code.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import vision
from vision.backends import registry
from vision.backends.base import VisionBackend
from vision.backends.fallback import FallbackBackend
from vision.capabilities import VisionCapabilityLayer, create_vision_layer
from vision.errors import (
    VisionBackendError,
    VisionBackendNotFoundError,
    VisionError,
    VisionImageError,
    VisionModelError,
    VisionUnsupportedOperationError,
)
from vision.inspect import compare_images, inspect_image
from vision.loader import (
    SUPPORTED_EXTENSIONS,
    load_image_array,
    load_image_info,
    validate_image_path,
)
from vision.protocol import (
    SUPPORTED_OPERATIONS,
    VisionRequest,
    VisionResult,
    build_request,
    parse_result,
    to_json_safe,
)


def _make_png(width: int, height: int, mode: int = 2) -> bytes:
    """Standard-conforming PNG built from stdlib only (works with or without Pillow)."""
    import struct
    import zlib

    def chunk(typ, payload):
        body = typ + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, mode, 0, 0, 0))
    if mode == 2:  # RGB
        raw = b"".join(b"\x00" + b"\x20\x30\x40" * width for _ in range(height))
    else:  # RGBA
        raw = b"".join(
            b"\x00" + b"\x20\x30\x40\xff" * width for _ in range(height)
        )
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


@pytest.fixture(scope="session")
def png_file(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("vision")
    target = root / "test.png"
    target.write_bytes(_make_png(16, 12))
    return target


@pytest.fixture()
def layer() -> VisionCapabilityLayer:
    return VisionCapabilityLayer()


# --------------------------------------------------------------- image loading


def test_supported_extensions():
    assert ".png" in SUPPORTED_EXTENSIONS
    assert ".jpg" in SUPPORTED_EXTENSIONS
    assert ".jpeg" in SUPPORTED_EXTENSIONS
    assert ".webp" in SUPPORTED_EXTENSIONS


def test_load_image_info(png_file):
    info = load_image_info(png_file)
    assert info["readable"] is True
    assert info["width"] == 16
    assert info["height"] == 12
    assert info["size_bytes"] > 0
    assert info["path"] == str(png_file)
    assert json.dumps(info)  # JSON-safe


def test_validate_image_path_missing():
    with pytest.raises(VisionImageError) as ei:
        validate_image_path(Path("definitely-not-here.png"))
    assert ei.value.code == "not_found"


def test_validate_image_path_unsupported_extension(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("not an image", encoding="utf-8")
    with pytest.raises(VisionImageError) as ei:
        validate_image_path(target)
    assert ei.value.code == "unsupported_format"


def test_load_image_info_empty_file(tmp_path):
    target = tmp_path / "empty.png"
    target.write_bytes(b"")
    with pytest.raises(VisionImageError) as ei:
        load_image_info(target)
    assert ei.value.code == "empty_file"


def test_load_image_info_corrupt(tmp_path):
    target = tmp_path / "bad.png"
    target.write_bytes(b"not really an image container at all")
    with pytest.raises(VisionImageError):
        load_image_info(target)


def test_load_image_array_requires_deps(tmp_path, monkeypatch):
    """Array loading degrades gracefully when numpy/Pillow absent."""
    target = tmp_path / "x.png"
    target.write_bytes(_make_png(4, 4))
    try:
        import numpy  # noqa: PLC0415
        import PIL  # noqa: PLC0415, F401

        # deps present; array load should succeed
        arr, info = load_image_array(target)
        assert arr.shape[:2] == (4, 4)
        assert info["width"] == 4
    except ImportError:
        # numpy/PIL not installed -> must raise VisionImageError cleanly
        with pytest.raises(VisionImageError):
            load_image_array(target)


# -------------------------------------------------------------- metadata inspection


def test_inspect_image_basic(png_file):
    result = inspect_image(png_file)
    assert result["success"] is True
    assert result["image"]["width"] == 16
    assert result["image"]["height"] == 12
    assert result["image"]["format"] == "PNG"
    assert json.dumps(result)  # JSON-safe


def test_inspect_image_has_analysis(png_file):
    result = inspect_image(png_file)
    assert "analysis" in result
    assert "entropy" in result.get("analysis", {})
    assert "dominant_colors" in result.get("analysis", {})
    assert "edge_density" in result.get("analysis", {})
    assert "uniformity" in result.get("analysis", {})


# ------------------------------------------------------------ deterministic fallback


def test_fallback_describe(png_file):
    backend = FallbackBackend()
    req = VisionRequest(operation="describe", image_path=str(png_file))
    result = backend.inference(req)
    assert result.success is True
    assert result.backend == "fallback"
    assert "description" in result.data
    assert "metadata" in result.data
    assert result.data["metadata"]["width"] == 16
    assert json.dumps(result.to_dict())


def test_fallback_compare_identical(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    content = _make_png(8, 8)
    a.write_bytes(content)
    b.write_bytes(content)
    backend = FallbackBackend()
    req = VisionRequest(
        operation="compare",
        image_path=str(a),
        params={"reference_image": str(b)},
    )
    result = backend.inference(req)
    assert result.success is True
    assert result.data["identical"] is True
    assert result.data["max_pixel_diff"] == 0.0


def test_fallback_compare_no_reference(tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(_make_png(4, 4))
    backend = FallbackBackend()
    req = VisionRequest(operation="compare", image_path=str(a))
    result = backend.inference(req)
    assert result.success is False
    assert result.data is None


def test_fallback_inspect_render(tmp_path):
    target = tmp_path / "render.png"
    target.write_bytes(_make_png(64, 64))
    backend = FallbackBackend()
    req = VisionRequest(operation="inspect_render", image_path=str(target))
    result = backend.inference(req)
    assert result.success is True
    assert result.data["valid"] is True
    assert result.data["checks"]["has_dimensions"] is True


def test_fallback_unsupported_operation(png_file):
    backend = FallbackBackend()
    req = VisionRequest(operation="detect_objects", image_path=str(png_file))
    result = backend.inference(req)
    assert result.success is False
    assert result.error["code"] == "unsupported_operation"


# ---------------------------------------------------------------- protocol/serialization


def test_build_request_wire_format(png_file):
    req = build_request("describe", str(png_file), params={"detail_level": "high"})
    assert req["operation"] == "describe"
    assert req["image_path"] == str(png_file)
    assert req["params"] == {"detail_level": "high"}
    assert json.loads(json.dumps(req)) == req


def test_build_request_rejects_unknown_operation(png_file):
    with pytest.raises(ValueError):
        build_request("frobnicate", str(png_file))


def test_vision_result_serialization(png_file):
    ok = VisionResult.success_result("describe", {"description": "x"}, request_id="abc", backend="fallback")
    d = ok.to_dict()
    assert d["success"] is True
    assert d["operation"] == "describe"
    assert json.dumps(d)

    fail = VisionResult.failure_result(
        "describe", {"code": "backend_error", "message": "boom"}, request_id="abc"
    )
    d2 = fail.to_dict()
    assert d2["success"] is False
    assert d2["error"]["code"] == "backend_error"


def test_parse_result(png_file):
    text = json.dumps(
        {
            "success": True,
            "operation": "describe",
            "data": {"description": "x"},
            "error": None,
            "request_id": "r1",
            "backend": "fallback",
        }
    )
    parsed = parse_result(text, "describe")
    assert parsed["success"] is True


def test_parse_result_bad_json():
    with pytest.raises(ValueError):
        parse_result("{not json", "describe")


def test_to_json_safe_helpers():
    assert to_json_safe(float("nan")) is None
    assert to_json_safe((1, 2)) == [1, 2]
    assert to_json_safe({"a": 1.5}) == {"a": 1.5}


def test_vision_request_validate_missing_path():
    req = VisionRequest(operation="describe", image_path="")
    with pytest.raises(ValueError):
        req.validate()


# ------------------------------------------------------- backend unavailable behavior


class BrokenBackend(VisionBackend):
    """A backend that reports unavailable (e.g., model not installed)."""

    name = "broken"
    description = "Test backend that is always unavailable"
    supported_operations = frozenset({"describe"})

    def check_availability(self) -> tuple[bool, str]:
        return False, "model weights not found"

    def inference(self, request: VisionRequest) -> VisionResult:
        raise AssertionError("should never be called")


class ExplodingBackend(VisionBackend):
    """A backend that is available but crashes during inference."""

    name = "exploding"
    description = "Test backend that always crashes"
    supported_operations = frozenset({"describe"})

    def check_availability(self) -> tuple[bool, str]:
        return True, "ok"

    def inference(self, request: VisionRequest) -> VisionResult:
        raise RuntimeError("gpu exploded")


def test_unavailable_backend_not_selected(png_file):
    backend = BrokenBackend()
    registry.register(backend, priority=1000)
    try:
        assert backend.is_available() is False
        # Capability layer should fall back to fallback backend
        layer = VisionCapabilityLayer(preferred_backend="broken")
        # preferred unavailable -> should pick fallback or none
        assert layer._select_backend("describe") is not None
    finally:
        registry._backends.remove((1000, backend))


def test_backend_availability_report(png_file):
    backend = BrokenBackend()
    registry.register(backend, priority=900)
    try:
        info = backend.get_info()
        assert info["available"] is False
        assert info["name"] == "broken"
        assert "model weights" in info["reason"]
    finally:
        registry._backends.remove((900, backend))


def test_exploding_backend_raises_structured(png_file):
    backend = ExplodingBackend()
    registry.register(backend, priority=0)
    try:
        layer = VisionCapabilityLayer(preferred_backend="exploding")
        with pytest.raises(VisionError):
            layer.describe(str(png_file))
        err = layer.last_error()
        assert err is not None
        assert err["error"] == "vision_backend_error"
        assert "gpu exploded" in err["message"]
    finally:
        registry._backends.remove((0, backend))


# ------------------------------------------------------------ capability detection


def test_capability_detection_fallback_always_available(layer):
    caps = layer.detect_capabilities()
    assert caps["vision_available"] is True
    assert "fallback" in caps["available_backends"]
    assert caps["fallback_available"] is True
    assert "describe" in caps["supported_operations"]


def test_capability_layer_uses_fallback_by_default(layer, png_file):
    caps = layer.detect_capabilities()
    assert "fallback" in caps["available_backends"]


def test_unsupported_operation_raises(layer, png_file):
    with pytest.raises(VisionUnsupportedOperationError):
        layer.detect_objects(str(png_file))


# ------------------------------------------------------------ high-level API


def test_layer_describe(layer, png_file):
    result = layer.describe(str(png_file))
    assert "description" in result
    assert result["metadata"]["width"] == 16


def test_layer_validate_image(layer, png_file):
    info = layer.validate_image(png_file)
    assert info["readable"] is True
    assert info["width"] == 16


def test_layer_inspect(layer, png_file):
    result = layer.inspect(png_file)
    assert result["success"] is True


def test_layer_compare(layer, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(_make_png(8, 8))
    b.write_bytes(_make_png(8, 8))
    result = layer.compare(str(a), str(b))
    assert result["identical"] is True


def test_layer_inspect_render(layer, tmp_path):
    target = tmp_path / "render.png"
    target.write_bytes(_make_png(64, 64))
    result = layer.inspect_render(str(target))
    assert result["valid"] is True


def test_layer_last_error_clears_on_success(layer, png_file):
    with pytest.raises(VisionUnsupportedOperationError):
        layer.detect_objects(str(png_file))
    assert layer.last_error() is not None
    layer.describe(str(png_file))
    assert layer.last_error() is None


def test_create_vision_layer_factory():
    layer = create_vision_layer()
    assert isinstance(layer, VisionCapabilityLayer)


# ------------------------------------------------------------ error type checks


def test_error_types_structured():
    err = VisionImageError(
        operation="load",
        message="cannot read",
        image_path="x.png",
        code="unreadable",
    )
    d = err.to_dict()
    assert d["error"] == "unreadable"
    assert d["operation"] == "load"
    assert d["image_path"] == "x.png"
    assert json.dumps(d)

    assert issubclass(VisionError, Exception)
    assert issubclass(VisionBackendError, VisionError)
    assert issubclass(VisionBackendNotFoundError, VisionError)
    assert issubclass(VisionImageError, VisionError)
    assert issubclass(VisionModelError, VisionError)
    assert issubclass(VisionUnsupportedOperationError, VisionError)


def test_condense_short_summary():
    err = VisionImageError(operation="load", message="something went wrong")
    summary = vision.condense(err)
    assert summary.startswith("vision_image_error")
    assert "something went wrong" in summary


# --------------------------------------------------- Blender render compatibility


def test_blender_render_roundtrip_compat(layer, tmp_path):
    """Simulated Blender-render -> vision-inspect flow used by S2.1 later."""
    render = tmp_path / "blender_render.png"
    render.write_bytes(_make_png(320, 240))
    result = layer.inspect_render(str(render))
    assert result["valid"] is True
    assert result["image"]["width"] == 320
    assert result["image"]["height"] == 240
    assert json.dumps(result)


def test_vision_error_struct_consumable_by_capability_layer(layer, png_file):
    """The S2.1 layer contract (last_error dict) is mirrored for vision."""
    with pytest.raises(VisionUnsupportedOperationError):
        layer.extract_text(str(png_file))
    err = layer.last_error()
    assert isinstance(err, dict)
    assert json.dumps(err)


def test_supported_operations_constant():
    assert "describe" in SUPPORTED_OPERATIONS
    assert "compare" in SUPPORTED_OPERATIONS
    assert "inspect_render" in SUPPORTED_OPERATIONS
    assert "detect_objects" in SUPPORTED_OPERATIONS
    assert "extract_text" in SUPPORTED_OPERATIONS
    assert "analyze_ui" in SUPPORTED_OPERATIONS


# -------------------------------------------------- optional VLM backend behavior


def test_local_vlm_backend_reports_unavailable_without_deps(png_file):
    """Optional model backend degrades gracefully when torch/transformers absent."""
    from vision.backends.local_vlm import LocalVLMBackend

    backend = LocalVLMBackend({"model_path": "C:/nonexistent/model"})
    available, reason = backend.check_availability()
    assert available is False
    assert reason  # explanatory reason present


def test_local_vlm_inference_fails_structurally_when_unavailable(png_file):
    from vision.backends.local_vlm import LocalVLMBackend

    backend = LocalVLMBackend({"model_path": "C:/nonexistent/model"})
    req = VisionRequest(operation="describe", image_path=str(png_file))
    result = backend.inference(req)
    assert result.success is False
    assert result.data is None
    assert result.error is not None
    assert "code" in result.error
    assert json.dumps(result.to_dict())


def test_local_vlm_requires_explicit_model_path():
    from vision.backends.local_vlm import LocalVLMBackend

    backend = LocalVLMBackend({})
    available, reason = backend.check_availability()
    assert available is False
    # Unavailable whether deps missing OR model_path missing
    assert "torch/transformers" in reason or "model_path" in reason


def test_local_vlm_opt_in_registration():
    """register_local_vlm must not change default availability."""
    from vision.backends.local_vlm import register_local_vlm

    backend = register_local_vlm({})
    try:
        assert backend.name == "local_vlm"
        # without torch/transformers the default capability report is unchanged
        layer2 = VisionCapabilityLayer()
        caps = layer2.detect_capabilities()
        assert caps["fallback_available"] is True
    finally:
        registry._backends = [(p, b) for p, b in registry._backends if b.name != "local_vlm"]


def test_no_hardware_dependency_in_default_stack(layer):
    """The default stack must never touch a GPU."""
    caps = layer.detect_capabilities()
    for backend in caps["backends"]:
        if backend["available"]:
            assert backend["requires_gpu"] is False


# ------------------------------------------- truncated / incomplete renders


def test_validate_image_rejects_truncated_png(tmp_path):
    """A PNG with a valid header but no image data must not validate as readable."""
    pytest.importorskip("PIL")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (128, 96), (10, 20, 30)).save(buffer, format="PNG")
    data = buffer.getvalue()
    target = tmp_path / "truncated.png"
    target.write_bytes(data[:44])

    with pytest.raises(VisionError) as excinfo:
        create_vision_layer().validate_image(target)
    assert excinfo.value.to_dict()["error"] in {"unreadable", "backend_error"}


def test_inspect_render_rejects_truncated_png(tmp_path):
    """A content-free render must never pass structural inspection."""
    pytest.importorskip("PIL")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (128, 96), (10, 20, 30)).save(buffer, format="PNG")
    data = buffer.getvalue()
    target = tmp_path / "truncated_render.png"
    target.write_bytes(data[:44])

    with pytest.raises(VisionError):
        create_vision_layer().inspect_render(target)


def test_inspect_render_rejects_header_only_png(tmp_path):
    """A hand-built PNG header must never pass structural inspection."""
    target = tmp_path / "header_only.png"
    target.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (128).to_bytes(4, "big")
        + (96).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 40
    )

    with pytest.raises(VisionError):
        create_vision_layer().inspect_render(target)


def test_inspect_render_accepts_complete_png(tmp_path):
    """The truncation guard must not reject a complete render."""
    pytest.importorskip("PIL")
    from PIL import Image

    target = tmp_path / "complete_render.png"
    Image.new("RGBA", (160, 120), (10, 20, 30)).save(target, format="PNG")

    result = create_vision_layer().inspect_render(target)
    assert result["valid"] is True
    assert result["checks"]["readable"] is True