"""Camera + lighting template.

Run directly inside Blender (executes the operations live):

    blender.exe --background --python blender/templates/camera_setup.py

Or use through Diablo's capability layer. Rotation is expressed in DEGREES;
colours are [r, g, b] floats in 0..1.
"""

import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import diablo_blender_ops as ops  # noqa: E402  (path setup must come first)


def main():
    ops.op_create_object({"primitive": "cube", "name": "Hero", "location": [0, 0, 0.5]})

    camera = ops.op_configure_camera({
        "name": "Cam", "location": [7, -6, 5], "rotation": [55, 0, 45], "lens": 50, "make_active": True,
    })
    print("[camera] " + json.dumps(camera))

    sun = ops.op_configure_light({
        "name": "Sun", "light_type": "SUN", "rotation": [50, 0, 30], "energy": 3.0, "color": [1.0, 0.95, 0.85],
    })
    print("[light sun] " + json.dumps(sun))

    fill = ops.op_configure_light({
        "name": "Fill", "light_type": "AREA", "location": [-4, 4, 3], "rotation": [0, 0, 0],
        "energy": 20.0, "color": [0.7, 0.8, 1.0], "size": 2.0,
    })
    print("[light area] " + json.dumps(fill))


if __name__ == "__main__":
    main()