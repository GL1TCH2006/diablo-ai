"""Diablo Blender dispatch runner (executes inside Blender's Python).

Usage (constructed by :class:`blender.runner.BlenderRunner`)::

    blender.exe [scene.blend] --background --python runner.py -- <request.json> <result.json>

Behaviour:
    * reads the JSON request (operation / params / optional session file paths)
    * opens the persisted session ``.blend`` when one is given and exists
    * dispatches to ``diablo_blender_ops.op_<operation>``
    * re-saves the session file after mutating operations
    * writes a structured JSON result; never corrupts the caller's state
"""

import json
import os
import sys
import traceback


def _user_args():
    try:
        return list(sys.argv[sys.argv.index("--") + 1:])
    except ValueError:
        return []


def _load_request(request_file):
    with open(request_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_result(output_file, result):
    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)


def _main():
    output_file = ""
    try:
        args = _user_args()
        if len(args) < 2:
            raise ValueError("usage: runner.py -- <request.json> <result.json>")
        request_file, output_file = args[0], args[1]

        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import diablo_blender_ops as ops  # noqa: PLC0415 - import after path fix
        import bpy  # noqa: PLC0415

        request = _load_request(request_file)
        session = request.get("_session") or {}
        open_file = session.get("open_file") or ""
        save_file = session.get("save_file") or ""

        if open_file and os.path.isfile(open_file):
            bpy.ops.wm.open_mainfile(filepath=os.path.abspath(open_file))

        operation = request.get("operation") or ""
        params = request.get("params") or {}
        result = {
            "success": True,
            "operation": operation,
            "data": None,
            "error": None,
            "request_id": request.get("request_id", ""),
        }

        try:
            handler = getattr(ops, "op_" + operation, None)
            if handler is None:
                result["success"] = False
                result["error"] = {
                    "code": "unknown_operation",
                    "message": "unknown operation: %r" % (operation,),
                    "traceback": "",
                }
            else:
                data = handler(params)
                result["data"] = data
        except Exception as exc:  # noqa: BLE001 - structured script error report
            result["success"] = False
            result["error"] = {
                "code": "blender_script_error",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }

        read_only = operation in ("inspect_scene", "list_objects", "inspect_object")
        if save_file and result["success"] and not read_only:
            parent = os.path.dirname(os.path.abspath(save_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(save_file))

        _write_result(output_file, result)
        print("[diablo-blender] operation=%s success=%s" % (operation, result["success"]))
    except Exception as exc:  # noqa: BLE001 - last resort structured failure
        error = {"code": "runner_failure", "message": str(exc), "traceback": traceback.format_exc()}
        if output_file:
            try:
                _write_result(output_file, {
                    "success": False, "operation": "", "data": None,
                    "error": error, "request_id": "",
                })
            except Exception:  # noqa: BLE001
                pass
        else:
            traceback.print_exc()


if __name__ == "__main__":
    _main()