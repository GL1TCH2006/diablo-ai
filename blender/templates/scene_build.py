"""Saved scene building demo — combines objects, materials, camera, lighting.

Run directly inside Blender (executes the operations live):

    blender.exe --background --python blender/templates/scene_build.py -- <output.blend>

Or use through Diablo's capability layer, which performs the same JSON
operations across a persistent managed session.
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
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "diablo_demo.blend")


def main():
    ops.op_create_object({"primitive": "cube", "name": "Hero", "location": [0, 0, 0.5]})
    ops.op_create_object({"primitive": "plane", "name": "Floor", "scale": [5, 5, 1]})
    ops.op_create_object({"primitive": "sphere", "name": "Orb", "location": [2.5, 0, 0.5], "scale": [0.6, 0.6, 0.6]})

    ops.op_create_material({"name": "Crimson", "base_color": [0.85, 0.15, 0.2], "metallic": 0.6, "roughness": 0.3})
    ops.op_create_material({"name": "FloorMat", "base_color": [0.15, 0.15, 0.18], "roughness": 0.7})
    ops.op_assign_material({"object_name": "Hero", "material_name": "Crimson"})
    ops.op_assign_material({"object_name": "Floor", "material_name": "FloorMat"})

    ops.op_configure_camera({"name": "Cam", "location": [8, -7, 5], "rotation": [55, 0, 45], "lens": 42, "make_active": True})
    ops.op_configure_light({"name": "Sun", "light_type": "SUN", "rotation": [50, 0, 30], "energy": 3.0})

    target = _target_path()
    result = ops.op_save({"path": target})
    print("[saved] " + json.dumps(result))


if __name__ == "__main__":
    main()