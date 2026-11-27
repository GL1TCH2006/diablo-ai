"""Real end-to-end Blender integration tests (Phase 4).

These tests run against the actual Blender executable when it is installed.
They create disposable scenes in temporary authorized locations, verify outputs,
and clean up only the artifacts they create.

They are skipped when Blender is not available, never touch user projects, and
never make claims about arbitrary 3D modeling intelligence beyond what is
actually verified.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from blender.capabilities import BlenderCapabilityLayer
from blender.controller import detect_blender
from blender.errors import BlenderClosedError, BlenderNotFoundError
from blender.render_inspect import inspect_render


def _blender_available() -> bool:
    info = detect_blender()
    return info.installed and bool(info.path) and Path(info.path).is_file()


pytestmark = pytest.mark.skipif(
    not _blender_available(),
    reason="Blender executable not found at standard install location",
)


def test_real_blender_detection_reports_version():
    info = detect_blender()
    assert info.installed is True
    assert info.version
    assert info.path
    assert Path(info.path).is_file()
    assert "5.2" in info.version or info.version.startswith("5.")


def test_real_blender_create_scene_and_save():
    """Create objects, material, light, camera; save a .blend; verify it exists."""
    tmp = Path(tempfile.mkdtemp(prefix="diablo_blender_real_"))
    try:
        layer = BlenderCapabilityLayer(work_dir=tmp / "work")
        try:
            # Build a simple bounded scene.
            cube = layer.create_object(
                primitive="cube", name="DiabloCube", location=[1.0, 0.0, 0.0],
                scale=[1.5, 1.5, 1.5],
            )
            assert cube["name"] == "DiabloCube"
            assert cube["location"] == [1.0, 0.0, 0.0]

            mat = layer.create_material(
                name="DiabloGold", base_color=[0.8, 0.6, 0.1],
                metallic=0.9, roughness=0.2,
            )
            assert abs(mat["metallic"] - 0.9) < 1e-5

            assigned = layer.assign_material(
                object_name=cube["name"], material_name=mat["name"],
            )
            assert assigned["slots"] == 1

            light = layer.configure_light(
                name="KeyLight", light_type="SUN", location=[5.0, 5.0, 5.0],
                energy=3.0,
            )
            assert light["light_type"] == "SUN"

            cam = layer.configure_camera(
                name="ShotCam", location=[0.0, -10.0, 5.0], rotation=[60.0, 0.0, 0.0],
                lens=35.0, make_active=True,
            )
            assert cam["active"] is True
            assert cam["lens_mm"] == 35.0

            # Inspect the scene and confirm artifacts are present.
            scene = layer.inspect_scene()
            assert scene["object_count"] >= 4  # cube + light + 2 cameras (default + ShotCam)
            assert "DiabloCube" in scene["objects"]
            assert "KeyLight" in scene["lights"]
            assert "ShotCam" in scene["cameras"]
            assert "DiabloGold" in scene["materials"]

            # Save to a user-authorized temp path and verify the file exists.
            blend_path = tmp / "diablo_test_scene.blend"
            saved = layer.save(str(blend_path))
            assert saved["exists"] is True
            assert saved["size_bytes"] > 0
            assert blend_path.is_file()

            # Verify the saved file is non-empty (Blender 5.2 write confirmed).
            assert blend_path.stat().st_size > 0

        finally:
            layer.close()

        # Clean up only test-created artifacts.
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        if Path(tmp).exists():
            shutil.rmtree(tmp, ignore_errors=True)


def test_real_blender_render_and_inspect():
    """Render a simple scene to PNG and independently verify the output file."""
    tmp = Path(tempfile.mkdtemp(prefix="diablo_blender_render_"))
    try:
        layer = BlenderCapabilityLayer(work_dir=tmp / "work")
        try:
            layer.create_object(primitive="cube", name="RenderCube")
            layer.configure_light(name="Light", light_type="SUN", energy=2.0)
            layer.configure_camera(name="Cam", location=[0, -8, 5], rotation=[55, 0, 0],
                                   make_active=True)

            render_path = tmp / "render.png"
            rendered = layer.render(
                str(render_path), resolution_x=320, resolution_y=240, img_format="PNG",
            )
            assert rendered["exists"] is True
            assert rendered["size_bytes"] > 0
            assert render_path.is_file()

            # Independent local verification (no Blender child).
            inspection = inspect_render(str(render_path))
            assert inspection["readable"] is True
            assert inspection["width"] == 320
            assert inspection["height"] == 240
        finally:
            layer.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_blender_invalid_save_path_rejected():
    tmp = Path(tempfile.mkdtemp(prefix="diablo_blender_badpath_"))
    try:
        layer = BlenderCapabilityLayer(work_dir=tmp / "work")
        try:
            from blender.errors import BlenderScriptError
            with pytest.raises(BlenderScriptError):
                layer.save("")  # Blender rejects empty save path
        finally:
            layer.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_blender_closed_layer_refuses_operations(tmp_path):
    layer = BlenderCapabilityLayer(work_dir=tmp_path / "work")
    layer.close()
    assert layer.closed is True
    with pytest.raises(BlenderClosedError):
        layer.inspect_scene()


def test_real_blender_detect_missing_executable_when_none():
    # We cannot uninstall Blender, but the not-found path is exercised via a
    # capability layer with a bogus runner in unit tests (test_blender_controller.py).
    # Here we simply confirm the real executable is detected.
    info = detect_blender()
    assert info.installed is True
    assert info.errors == []


def test_real_blender_cancellation_shape():
    """Cancellation is a typed error path; verify the error type imports cleanly."""
    from blender.errors import BlenderCancelledError
    assert BlenderCancelledError.code == "blender_cancelled"
    err = BlenderCancelledError(operation="render", message="cancelled")
    assert err.to_dict()["error"] == "blender_cancelled"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
