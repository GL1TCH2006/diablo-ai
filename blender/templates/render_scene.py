"""Render template.

Run directly inside Blender (executes the operations live):

    blender.exe --background --python blender/templates/render_scene.py -- <output.png>

The render uses the current scene's camera and lights; a default camera/light
are created if the scene has none. Output format (PNG/JPEG/BMP/WEBP/TIFF/OPEN_EXR)
is inferred from the file extension.
"""

import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import diablo_blender_ops as ops  # noqa: E402  (path setup must come first)


def _target_path():
    try:
        return sys.argv[sys.argv.index("--") + 1]
    except (ValueError, IndexError):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "diablo_render.png")


def main():
    scene = ops.op_inspect_scene({})
    if not scene.get("cameras"):
        ops.op_configure_camera({"name": "Cam", "location": [7, -6, 5], "rotation": [55, 0, 45], "make_active": True})
    if not scene.get("lights"):
        ops.op_configure_light({"name": "Sun", "light_type": "SUN", "rotation": [50, 0, 30], "energy": 3.0})

    path = _target_path()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "bmp": "BMP", "webp": "WEBP",
           "tif": "TIFF", "tiff": "TIFF", "exr": "OPEN_EXR"}.get(ext, "PNG")

    result = ops.op_render({"path": path, "format": fmt, "resolution_x": 640, "resolution_y": 480})
    print("[render] " + json.dumps(result))


if __name__ == "__main__":
    main()