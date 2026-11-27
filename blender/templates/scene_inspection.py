"""Scene inspection template.

Run directly inside Blender (executes the operations live):

    blender.exe --background --python blender/templates/scene_inspection.py

Or use through Diablo's capability layer, which performs the same JSON
operation in a managed subprocess session.
"""

import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import diablo_blender_ops as ops  # noqa: E402  (path setup must come first)


def main():
    scene = ops.op_inspect_scene({})
    print("[scene] " + json.dumps(scene, indent=2))

    listing = ops.op_list_objects({})
    print("[objects] " + json.dumps(listing, indent=2))

    for item in listing["objects"]:
        print("[inspect] " + json.dumps(ops.op_inspect_object({"name": item["name"]}), indent=2))


if __name__ == "__main__":
    main()