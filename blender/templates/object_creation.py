"""Object creation + transform template.

Run directly inside Blender (executes the operations live):

    blender.exe --background --python blender/templates/object_creation.py

Or use through Diablo's capability layer, which performs the same JSON
operations in a managed subprocess session. Rotation is expressed in DEGREES.
"""

import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import diablo_blender_ops as ops  # noqa: E402  (path setup must come first)


def main():
    cube = ops.op_create_object({"primitive": "cube", "name": "Base", "location": [0, 0, 0.5]})
    print("[create cube] " + json.dumps(cube))

    sphere = ops.op_create_object({"primitive": "sphere", "name": "Orb", "location": [3, 0, 0.5], "scale": [0.5, 0.5, 0.5]})
    print("[create sphere] " + json.dumps(sphere))

    cone = ops.op_create_object({"primitive": "cone", "name": "Pyramid", "location": [-3, 0, 0.5]} )
    print("[create cone] " + json.dumps(cone))

    moved = ops.op_transform_object({"name": "Base", "location": [0, 2, 1], "rotation": [0, 0, 45], "scale": [1.5, 1.5, 1.5]})
    print("[transform cube] " + json.dumps(moved))

    renamed = ops.op_modify_object({"name": "Pyramid", "new_name": "Obelisk"})
    print("[rename cone] " + json.dumps(renamed))

    removed = ops.op_delete_object({"name": "Orb"})
    print("[delete sphere] " + json.dumps(removed))


if __name__ == "__main__":
    main()