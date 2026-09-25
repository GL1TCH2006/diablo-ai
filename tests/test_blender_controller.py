"""Isolated tests for the S2.1 Blender capability layer.

These tests never launch Blender, never need a GPU, network, Ollama, mic or
speakers. All subprocess behaviour is faked at the ``BlenderRunner._execute``
level or replaced with a fake runner injected into the capability layer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import blender
import blender.controller as controller
from blender.capabilities import BlenderCapabilityLayer
from blender.errors import (
    BlenderNotFoundError,
    BlenderProcessError,
    BlenderProtocolError,
    BlenderScriptError,
    BlenderTimeoutError,
)
from blender.protocol import build_request, parse_result, to_json_safe, vec3
from blender.render_inspect import inspect_render
from blender.runner import BlenderRunner, RunnerResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "blender" / "scripts"


class FakeSubprocess:
    """Drop-in replacement for the ``subprocess`` module (detection tests)."""

    def __init__(self, stdout: str, returncode: int = 0):
        self._stdout = stdout
        self._returncode = returncode

    def run(self, *args, **kwargs):
        return SimpleNamespace(returncode=self._returncode, stdout=self._stdout, stderr="")


class FakeRunner:
    """Injected runner; records requests, returns canned per-op responses."""

    def __init__(self):
        self.requests: list[dict] = []
        self.scene_files: list = []
        self.responses: dict[str, object] = {}

    def resolve_exe(self) -> str:
        return "C:/fake/blender.exe"

    def run(self, request, timeout=None, scene_file=None):
        self.requests.append(request)
        self.scene_files.append(scene_file)
        op = request["operation"]
        if op in self.responses:
            value = self.responses[op]
            if isinstance(value, Exception):
                raise value
            return RunnerResult(True, op, data=value, return_code=0)
        return RunnerResult(True, op, data={"ok": True}, return_code=0)


class ScriptFailingRunner(FakeRunner):
    """Returns a structured script failure for a chosen set of operations."""

    def __init__(self, failing: set[str]):
        super().__init__()
        self.failing = failing

    def run(self, request, timeout=None, scene_file=None):
        self.requests.append(request)
        self.scene_files.append(scene_file)
        op = request["operation"]
        if op in self.failing:
            return RunnerResult(
                False, op, error_code="blender_script_error",
                error_message="boom", return_code=0, stdout="", stderr="",
            )
        return RunnerResult(True, op, data={"ok": True}, return_code=0)


def install_fake_blender(tmp_path) -> Path:
    root = tmp_path / "Blender Foundation"
    (root / "Blender 5.2").mkdir(parents=True)
    exe = root / "Blender 5.2" / "blender.exe"
    exe.write_text("", encoding="utf-8")
    return exe


def layer_with(runner, tmp_path) -> BlenderCapabilityLayer:
    return BlenderCapabilityLayer(runner=runner, work_dir=tmp_path / "work")


# --------------------------------------------------------------- detection


def test_detect_blender_found_and_version(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    monkeypatch.setattr(controller, "_PROBE_PATHS", [exe.parent.parent])
    fake = FakeSubprocess(stdout="Blender 5.2.2\nBKE_threads_init: ...\n")
    monkeypatch.setattr(controller, "subprocess", fake)

    info = controller.detect_blender()
    assert info.installed is True
    assert info.version == "5.2.2"
    assert info.path == str(exe)
    assert info.errors == []


def test_detect_blender_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "_PROBE_PATHS", [tmp_path / "does-not-exist"])
    info = controller.detect_blender()
    assert info.installed is False
    assert info.errors


def test_legacy_blender_controller_compat(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "_PROBE_PATHS", [tmp_path / "nope"])
    bc = controller.BlenderController()
    desc = bc.describe()
    assert isinstance(desc, str)
    assert "Blender" in desc
    assert isinstance(bc.info, controller.BlenderInfo)


def test_package_exports_legacy_and_new_api():
    from blender import (
        BlenderCapabilityLayer,
        BlenderController,
        BlenderInfo,
        BlenderRunner,
        detect_blender,
    )

    assert all(
        callable(x) or isinstance(x, type)
        for x in (BlenderCapabilityLayer, BlenderController, BlenderRunner)
    )


def test_blender_controller_describe_with_real_detection_path(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "_PROBE_PATHS", [tmp_path / "absent"])
    info = controller.detect_blender()
    assert not info.installed


# ---------------------------------------------------------------- protocol


def test_build_request_wire_format():
    req = build_request(
        "create_object",
        {"primitive": "cube", "location": [1, 2, 3]},
        request_id="abc123",
    )
    assert req["operation"] == "create_object"
    assert req["request_id"] == "abc123"
    assert req["params"] == {"primitive": "cube", "location": [1, 2, 3]}
    assert json.loads(json.dumps(req)) == req


def test_build_request_session_field():
    req = build_request("save", {"path": "x.blend"}, session={"open_file": "a", "save_file": "b"})
    assert req["_session"] == {"open_file": "a", "save_file": "b"}
    plain = build_request("save", {"path": "x.blend"})
    assert "_session" not in plain


def test_parse_result_accepts_success_and_failure():
    ok = parse_result(
        json.dumps({"success": True, "operation": "inspect_scene", "data": {"a": 1}, "error": None}),
        "inspect_scene",
    )
    assert ok["success"] is True and ok["data"] == {"a": 1}
    fail = parse_result(
        json.dumps({"success": False, "operation": "inspect_scene", "data": None,
                    "error": {"code": "blender_script_error", "message": "m"}}),
        "inspect_scene",
    )
    assert fail["success"] is False


def test_parse_result_rejects_bad_payloads():
    with pytest.raises(ValueError):
        parse_result("", "inspect_scene")
    with pytest.raises(ValueError):
        parse_result('{"nope": true}', "inspect_scene")
    with pytest.raises(ValueError):
        parse_result('{"success": true, "operation": "other", "data": {}}', "inspect_scene")


def test_json_safe_helpers():
    assert vec3([1]) == [1.0, 0.0, 0.0]
    assert vec3(None) == [0.0, 0.0, 0.0]
    assert vec3((1, 2, 3)) == [1.0, 2.0, 3.0]
    assert to_json_safe(float("nan")) is None
    assert to_json_safe((1, 2, 3)) == [1, 2, 3]
    assert to_json_safe({"v": 1.5}) == {"v": 1.5}


# ---------------------------------------------------------------- runner


def test_command_construction(tmp_path):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR)
    req = build_request("inspect_scene")
    rfile = tmp_path / "r.json"
    ofile = tmp_path / "o.json"

    cmd = runner.build_command(req, rfile, ofile)
    assert cmd[0] == str(exe)
    assert "--background" in cmd
    assert "--factory-startup" in cmd
    assert "--python" in cmd
    sep = cmd.index("--")
    assert sep > 0
    assert cmd[sep + 1] == str(rfile)
    assert cmd[sep + 2] == str(ofile)

    session = tmp_path / "session.blend"
    session.write_text("x", encoding="utf-8")
    with_session = runner.build_command(req, rfile, ofile, scene_file=session)
    assert with_session[1] == str(session)  # positional .blend right after exe
    assert "--background" in with_session

    missing = runner.build_command(req, rfile, ofile, scene_file=tmp_path / "absent.blend")
    assert missing[1] == "--background"


def test_runner_missing_exe_raises_not_found(tmp_path):
    runner = BlenderRunner(exe=str(tmp_path / "missing" / "blender.exe"))
    with pytest.raises(BlenderNotFoundError) as ei:
        runner.run(build_request("inspect_scene"))
    assert ei.value.to_dict()["error"] == "blender_not_found"


def test_runner_timeout(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR, timeout=5)

    def boom(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(runner, "_execute", boom)
    with pytest.raises(BlenderTimeoutError) as ei:
        runner.run(build_request("inspect_scene"))
    assert ei.value.timeout == 5
    assert ei.value.to_dict()["error"] == "blender_timeout"


def test_runner_nonzero_return_code(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR)

    def fail(cmd, timeout):
        return SimpleNamespace(returncode=2, stdout="", stderr="crash")

    monkeypatch.setattr(runner, "_execute", fail)
    with pytest.raises(BlenderProcessError) as ei:
        runner.run(build_request("list_objects"))
    assert ei.value.return_code == 2
    assert "crash" in ei.value.stderr


def _write_result_for(cmd, payload_text):
    out = Path(cmd[cmd.index("--") + 2])
    out.write_text(payload_text, encoding="utf-8")


def test_runner_malformed_json(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR)

    def junk(cmd, timeout):
        _write_result_for(cmd, "{not json")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_execute", junk)
    with pytest.raises(BlenderProtocolError) as ei:
        runner.run(build_request("inspect_scene"))
    assert "malformed" in ei.value.message.lower()


def test_runner_missing_result_file(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR)

    def silent(cmd, timeout):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_execute", silent)
    with pytest.raises(BlenderProtocolError):
        runner.run(build_request("inspect_scene"))


def test_runner_success_response(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR)

    def good(cmd, timeout):
        _write_result_for(
            cmd,
            json.dumps({"success": True, "operation": "inspect_scene",
                        "data": {"object_count": 3}, "error": None, "request_id": "rid9"}),
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner, "_execute", good)
    result = runner.run(build_request("inspect_scene", request_id="rid9"))
    assert result.success is True
    assert result.data == {"object_count": 3}
    assert result.request_id == "rid9"
    assert result.return_code == 0


def test_runner_script_failure_response(tmp_path, monkeypatch):
    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR)

    def failed(cmd, timeout):
        _write_result_for(
            cmd,
            json.dumps({"success": False, "operation": "create_object", "data": None,
                        "error": {"code": "blender_script_error", "message": "no space left"}}),
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_execute", failed)
    result = runner.run(build_request("create_object", {"primitive": "cube"}))
    assert result.success is False
    assert result.error_code == "blender_script_error"
    assert result.error_message == "no space left"


def test_runner_command_uses_utf8_text_and_timeout(tmp_path, monkeypatch):
    captured = {}

    def spy(cmd, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        _write_result_for(cmd, json.dumps({"success": True, "operation": "save",
                                           "data": {"path": "x"}, "error": None}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    exe = install_fake_blender(tmp_path)
    runner = BlenderRunner(exe=exe, scripts_dir=SCRIPTS_DIR, timeout=77)
    monkeypatch.setattr(runner, "_execute", spy)
    runner.run(build_request("save", {"path": "x.blend"}), timeout=42)
    assert captured["timeout"] == 42
    assert runner.last_result is not None


# ------------------------------------------------------------ capability layer


def test_capability_basic_roundtrip(tmp_path):
    runner = FakeRunner()
    layer = layer_with(runner, tmp_path)
    data = layer.inspect_scene()
    assert data == {"ok": True}
    assert layer.last_error() is None
    assert runner.requests[0]["operation"] == "inspect_scene"
    assert "_session" in runner.requests[0]


def test_capability_session_file_passed_after_first_save(tmp_path):
    runner = FakeRunner()
    layer = layer_with(runner, tmp_path)
    layer.inspect_scene()
    # session path is always forwarded; open_file stays empty until Blender saves it
    assert str(runner.scene_files[0]) == str(layer.session_file)
    assert runner.requests[0]["_session"]["open_file"] == ""
    assert runner.requests[0]["_session"]["save_file"] == str(layer.session_file)

    layer.session_file.write_text("x", encoding="utf-8")
    layer.create_object("cube")
    assert str(runner.scene_files[1]) == str(layer.session_file)
    assert runner.requests[1]["_session"]["open_file"] == str(layer.session_file)
    assert runner.requests[1]["_session"]["save_file"] == str(layer.session_file)


def test_capability_script_error_raises_and_records(tmp_path):
    runner = ScriptFailingRunner({"create_object"})
    layer = layer_with(runner, tmp_path)
    with pytest.raises(BlenderScriptError) as ei:
        layer.create_object("cube", name="Bad")
    assert ei.value.code == "blender_script_error"
    assert "boom" in ei.value.message
    err = layer.last_error()
    assert err["error"] == "blender_script_error"
    assert err["operation"] == "create_object"


def test_capability_infra_error_propagates(tmp_path):
    class TimeoutRunner(FakeRunner):
        def run(self, request, timeout=None, scene_file=None):
            raise BlenderTimeoutError(operation=request["operation"], message="slow", timeout=1)

    layer = BlenderCapabilityLayer(runner=TimeoutRunner(), work_dir=tmp_path / "work")
    with pytest.raises(BlenderTimeoutError):
        layer.inspect_scene()
    assert layer.last_error()["error"] == "blender_timeout"


def test_capability_close_preserves_saved_files(tmp_path):
    layer = layer_with(FakeRunner(), tmp_path)
    layer.session_file.write_text("session", encoding="utf-8")
    artifact = tmp_path / "work" / "render.png"
    artifact.write_text("png", encoding="utf-8")
    layer.close()
    assert artifact.is_file()  # saved renders/exports survive
    assert not layer.session_file.exists()  # scratch session file is released


def test_capability_object_material_camera_light_params_serializable(tmp_path):
    runner = FakeRunner()
    layer = layer_with(runner, tmp_path)
    layer.create_object("cube", name="C", location=[1, 2, 3], rotation=[0, 0, 90], scale=[1, 1, 1])
    layer.create_material("Mat", base_color=[1, 0, 0], metallic=0.7, roughness=0.2, alpha=1.0)
    layer.configure_camera("Cam", location=[1, 2, 3], rotation=[90, 0, 0], lens=50, make_active=True)
    layer.configure_light("Sun", "SUN", location=[0, 1, 0], energy=2.0, color=[1, 1, 1])

    for req in runner.requests:
        json.dumps(req)  # must never raise

    assert runner.requests[0]["params"]["location"] == [1, 2, 3]
    assert runner.requests[0]["params"]["rotation"] == [0, 0, 90]
    assert runner.requests[1]["params"]["metallic"] == 0.7
    assert runner.requests[1]["params"]["alpha"] == 1.0
    assert runner.requests[2]["params"]["lens"] == 50
    assert runner.requests[2]["params"]["make_active"] is True
    assert runner.requests[3]["params"]["light_type"] == "SUN"
    assert runner.requests[3]["params"]["energy"] == 2.0


def test_capability_all_operations_build_requests(tmp_path):
    runner = FakeRunner()
    layer = layer_with(runner, tmp_path)
    layer.delete_object("X")
    layer.transform_object("X", scale=[2, 2, 2])
    layer.modify_object("X", new_name="Y", hidden=True)
    layer.assign_material("Y", "M")
    layer.execute_script("bpy.ops.mesh.primitive_cube_add()")
    layer.save(str(tmp_path / "a.blend"))
    layer.export(str(tmp_path / "b.obj"))
    layer.render(str(tmp_path / "r.png"), resolution_x=320, resolution_y=240, img_format="PNG")

    ops_seen = [r["operation"] for r in runner.requests]
    assert ops_seen == [
        "delete_object", "transform_object", "modify_object", "assign_material",
        "execute_script", "save", "export", "render",
    ]
    render_params = runner.requests[-1]["params"]
    assert render_params["resolution_x"] == 320
    assert render_params["resolution_y"] == 240
    assert render_params["format"] == "PNG"
    assert runner.requests[-2]["params"]["format"] == ""  # inferred from filename


def test_capability_last_error_clears_on_success(tmp_path):
    runner = ScriptFailingRunner({"inspect_object"})
    layer = layer_with(runner, tmp_path)
    try:
        layer.inspect_object("none")
    except BlenderScriptError:
        pass
    assert layer.last_error() is not None
    layer.inspect_scene()
    assert layer.last_error() is None


# ---------------------------------------------------------- render inspection


def test_inspect_render_missing(tmp_path):
    result = inspect_render(tmp_path / "missing.png")
    assert result["success"] is False
    assert result["reason"] == "not_found"


def _make_png(width: int, height: int) -> bytes:
    """Standard-conforming PNG built from stdlib only (works with or without Pillow)."""
    import struct
    import zlib

    def chunk(typ, payload):
        body = typ + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\x20\x30\x40" * width for _ in range(height))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def test_inspect_render_valid_png(tmp_path):
    target = tmp_path / "frame.png"
    target.write_bytes(_make_png(10, 20))
    result = inspect_render(target)
    assert result["success"] is True
    assert result["readable"] is True
    assert result["width"] == 10
    assert result["height"] == 20
    assert result["size_bytes"] > 0
    assert result["image_format"] == "PNG"


def test_inspect_render_valid_png_with_pillow_if_available(tmp_path):
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return  # PIL not installed in this environment - fallback already covered
    target = tmp_path / "frame.png"
    Image.new("RGB", (10, 20), (200, 30, 30)).save(target)
    result = inspect_render(target)
    assert result["success"] is True
    assert result["readable"] is True
    assert result["width"] == 10
    assert result["height"] == 20
    assert result["size_bytes"] > 0
    assert result["image_format"] == "PNG"


def test_inspect_render_unreadable_file(tmp_path):
    target = tmp_path / "garbage.png"
    target.write_bytes(b"not really an image container")
    result = inspect_render(target)
    assert result["success"] is True  # exists and non-empty
    assert result["readable"] is False
    assert result["reason"].startswith("unreadable_image")


def test_inspect_render_empty_file(tmp_path):
    target = tmp_path / "empty.png"
    target.write_bytes(b"")
    result = inspect_render(target)
    assert result["success"] is False
    assert result["reason"] == "empty_file"


def test_inspect_render_truncated_png_is_not_readable(tmp_path):
    """A PNG with a valid header but no image data must not read as a render."""
    pytest.importorskip("PIL")
    from PIL import Image

    target = tmp_path / "truncated.png"
    Image.new("RGB", (128, 96), (10, 20, 30)).save(target, format="PNG")
    data = target.read_bytes()
    # keep the signature and a complete IHDR, drop the pixel data
    target.write_bytes(data[:44])

    result = inspect_render(target)
    assert result["readable"] is False
    assert result["reason"].startswith("unreadable_image")


def test_inspect_render_header_only_png_is_not_readable(tmp_path):
    """A hand-built PNG header claiming a size must not read as a render."""
    target = tmp_path / "header_only.png"
    target.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (128).to_bytes(4, "big")
        + (96).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 40
    )

    result = inspect_render(target)
    assert result["readable"] is False
    assert result["reason"].startswith("unreadable_image")


def test_inspect_render_complete_png_is_still_readable(tmp_path):
    """The truncation guard must not reject a complete render."""
    pytest.importorskip("PIL")
    from PIL import Image

    target = tmp_path / "complete.png"
    Image.new("RGB", (128, 96), (10, 20, 30)).save(target, format="PNG")

    result = inspect_render(target)
    assert result["readable"] is True
    assert result["reason"] is None
    assert (result["width"], result["height"]) == (128, 96)


# ---------------------------------------------------- blender-side scripts


def test_blender_side_scripts_and_templates_compile():
    for name in ("runner.py", "diablo_blender_ops.py"):
        source = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        compile(source, name, "exec")

    templates = PROJECT_ROOT / "blender" / "templates"
    for path in sorted(templates.glob("*.py")):
        compile(path.read_text(encoding="utf-8"), path.name, "exec")


def test_blender_side_runner_dispatch_uses_expected_operations():
    source = (SCRIPTS_DIR / "diablo_blender_ops.py").read_text(encoding="utf-8")
    for op in ("inspect_scene", "create_object", "create_material",
               "configure_camera", "configure_light", "render", "save", "export"):
        assert f"def op_{op}" in source


# ----------------------------------------------------------- error reporting


def test_error_to_dict_structured(tmp_path):
    err = BlenderProcessError(
        operation="render", message="rc=1", command=["blender", "--background"],
        return_code=1, stdout="out", stderr="err",
    )
    payload = err.to_dict()
    assert payload["error"] == "blender_process_error"
    assert payload["operation"] == "render"
    assert payload["return_code"] == 1
    assert json.dumps(payload)  # JSON-safe

    assert issubclass(BlenderScriptError, Exception)
    assert str(BlenderNotFoundError(message="missing")) == "blender_not_found | missing"