"""Material creation + assignment template.

Run directly inside Blender (executes the operations live):

    blender.exe --background --python blender/templates/material_setup.py

Or use through Diablo's capability layer. Colours are [r, g, b] floats in 0..1.
"""

import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import diablo_blender_ops as ops  # noqa: E402  (path setup must come first)


def main():
    ops.op_create_object({"primitive": "cube", "name": "Gem", "location": [0, 0, 0.5]})
    plane = ops.op_create_object({"primitive": "plane", "name": "Floor"})
    print("[create objects] " + json.dumps([plane]))

    ruby = ops.op_create_material({
        "name": "Ruby", "base_color": [0.9, 0.1, 0.2], "metallic": 0.8, "roughness": 0.2,
    })
    print("[material ruby] " + json.dumps(ruby))

    matte = ops.op_create_material({
        "name": "Matte", "base_color": [0.2, 0.5, 0.9], "metallic": 0.0, "roughness": 0.9,
    })
    print("[material matte] " + json.dumps(matte))

    print("[assign ruby] " + json.dumps(ops.op_assign_material({"object_name": "Gem", "material_name": "Ruby"})))
    print("[assign matte] " + json.dumps(ops.op_assign_material({"object_name": "Floor", "material_name": "Matte"})))


if __name__ == "__main__":
    main()