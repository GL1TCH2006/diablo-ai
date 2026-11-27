"""Diablo Blender operations library (executes inside Blender's Python).

Every ``op_*`` function takes a params dict and returns a JSON-safe dict.
Do not import/rely on anything outside the Blender interpreter; this module is
loaded directly by ``runner.py`` inside Blender.

Conventions:
    * location / scale: ``[x, y, z]`` floats in Blender units
    * rotation: ``[x, y, z]`` **degrees** (Euler XYZ); converted to radians here
    * colours: ``[r, g, b]`` floats in 0..1
    * errors are reported by raising; the dispatcher turns them into JSON
"""

import math
import os

import bpy


# ----------------------------------------------------------------- helpers


def _vec(value, default=(0.0, 0.0, 0.0)):
    if value is None:
        value = default
    out = [float(v) for v in value]
    while len(out) < 3:
        out.append(0.0)
    return out[:3]


def _deg(value, default=(0.0, 0.0, 0.0)):
    return [math.radians(v) for v in _vec(value, default)]


def _col(value, default=(1.0, 1.0, 1.0)):
    return _vec(value, default)


def _radians_to_degrees(euler):
    return [round(math.degrees(v), 6) for v in euler]


def _obj_or_raise(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError("object not found: %r" % (name,))
    return obj


def _unique_object_name(base):
    candidate = base
    index = 1
    while candidate in bpy.data.objects:
        candidate = "%s.%03d" % (base, index)
        index += 1
    return candidate


def _object_summary(obj):
    out = {
        "name": obj.name,
        "type": obj.type,
        "location": [float(v) for v in obj.location],
        "rotation_degrees": _radians_to_degrees(
            obj.rotation_euler
            if hasattr(obj, "rotation_euler")
            and obj.rotation_mode == "XYZ"
            else obj.matrix_world.to_euler()
        ),
        "scale": [float(v) for v in obj.scale],
        "dims": [float(v) for v in obj.dimensions],
        "hide_viewport": bool(obj.hide_viewport),
        "parent": obj.parent.name if obj.parent else None,
        "children": [c.name for c in obj.children],
    }
    if getattr(obj, "data", None) is not None:
        data = obj.data
        if getattr(data, "materials", None) is not None:
            out["materials"] = [m.name if m else None for m in data.materials]
        if getattr(data, "vertices", None) is not None:
            out["vertices"] = len(data.vertices)
        if getattr(data, "polygons", None) is not None:
            out["polygons"] = len(data.polygons)
    if obj.type == "CAMERA":
        out["lens_mm"] = float(obj.data.lens)
    if obj.type == "LIGHT":
        light = obj.data
        out["light_type"] = light.type
        out["energy"] = float(light.energy)
        out["color"] = [float(c) for c in light.color]
    return out


# ------------------------------------------------------------- inspections


def op_inspect_scene(params):
    scene = bpy.context.scene
    objects = list(bpy.data.objects)
    names = [o.name for o in objects]
    return {
        "name": scene.name,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "object_count": len(names),
        "objects": names,
        "cameras": [o.name for o in objects if o.type == "CAMERA"],
        "lights": [o.name for o in objects if o.type == "LIGHT"],
        "materials": [m.name for m in bpy.data.materials],
        "world": scene.world.name if scene.world else None,
        "active_camera": scene.camera.name if scene.camera else None,
        "render": {
            "engine": scene.render.engine,
            "resolution_x": scene.render.resolution_x,
            "resolution_y": scene.render.resolution_y,
            "file_format": scene.render.image_settings.file_format,
        },
        "units": {
            "system": scene.unit_settings.system,
            "length_unit": scene.unit_settings.length_unit,
            "rotation_unit": _unit_rotation(scene),
        },
    }


def _unit_rotation(scene) -> str | None:
    """Return rotational unit name, tolerating the 4.1+ rename of the property."""
    us = scene.unit_settings
    return getattr(us, "system_rotation", None) or getattr(us, "rotation_unit", None)


def op_list_objects(params):
    obj_type = (params.get("type") or "").upper()
    items = [
        {"name": o.name, "type": o.type}
        for o in bpy.data.objects
        if not obj_type or o.type == obj_type
    ]
    return {"count": len(items), "objects": items}


def op_inspect_object(params):
    return _object_summary(_obj_or_raise(params.get("name")))


# ---------------------------------------------------------------- creation


_PRIMITIVES = {"cube", "sphere", "cylinder", "cone", "plane", "torus"}


def op_create_object(params):
    primitive = (params.get("primitive") or "cube").lower()
    if primitive not in _PRIMITIVES:
        raise ValueError("unsupported primitive %r (use one of %s)" % (primitive, ", ".join(sorted(_PRIMITIVES))))

    loc = _vec(params.get("location", (0.0, 0.0, 0.0)))
    rot = _deg(params.get("rotation", (0.0, 0.0, 0.0)))
    sca = _vec(params.get("scale", (1.0, 1.0, 1.0)))
    requested = (params.get("name") or "").strip() or (primitive.title() + "Object")
    target_name = _unique_object_name(requested)

    if primitive == "cube":
        bpy.ops.mesh.primitive_cube_add(
            size=float(params.get("size", 2.0)), location=loc, rotation=rot, scale=sca
        )
    elif primitive == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=float(params.get("size", 1.0)), location=loc, rotation=rot, scale=sca
        )
    elif primitive == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            radius=float(params.get("size", 1.0)), depth=float(params.get("depth", 2.0)),
            location=loc, rotation=rot, scale=sca,
        )
    elif primitive == "cone":
        bpy.ops.mesh.primitive_cone_add(
            radius1=float(params.get("size", 1.0)), radius2=0.0,
            depth=float(params.get("depth", 2.0)), location=loc, rotation=rot, scale=sca,
        )
    elif primitive == "plane":
        bpy.ops.mesh.primitive_plane_add(
            size=float(params.get("size", 2.0)), location=loc, rotation=rot, scale=sca
        )
    elif primitive == "torus":
        kwargs = {}
        if params.get("major_radius") is not None:
            kwargs["major_radius"] = float(params["major_radius"])
        if params.get("minor_radius") is not None:
            kwargs["minor_radius"] = float(params["minor_radius"])
        bpy.ops.mesh.primitive_torus_add(
            location=loc, rotation=rot, scale=sca, **kwargs
        )

    obj = bpy.context.active_object
    if obj is None:
        raise ValueError("primitive created but no active object was selected")
    obj.name = target_name
    return _object_summary(obj)


def op_delete_object(params):
    names = params.get("names") or []
    if params.get("name"):
        names.append(params["name"])
    seen = []
    for name in set(names):
        obj = bpy.data.objects.get(name)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
            seen.append(name)
    missing = [n for n in set(names) if n not in seen]
    return {"deleted": seen, "count": len(seen), "missing": missing}


def op_modify_object(params):
    obj = _obj_or_raise(params.get("name"))
    if "new_name" in params and params["new_name"]:
        obj.name = str(params["new_name"])
    if "hidden" in params:
        obj.hide_viewport = bool(params["hidden"])
    if "select" in params:
        obj.select_set(bool(params["select"]))
    return _object_summary(obj)


def op_transform_object(params):
    obj = _obj_or_raise(params.get("name"))
    if "location" in params:
        obj.location = _vec(params["location"])
    if "rotation" in params:
        obj.rotation_euler = _deg(params["rotation"])
    if "scale" in params:
        obj.scale = _vec(params["scale"], (1.0, 1.0, 1.0))
    return _object_summary(obj)


# --------------------------------------------------------------- materials


def _principled_bsdf(material):
    nodes = material.node_tree.nodes
    node = nodes.get("Principled BSDF")
    if node is not None:
        return node
    for candidate in nodes:
        if candidate.type == "BSDF_PRINCIPLED":
            return candidate
    raise ValueError("material %r has no Principled BSDF node" % (material.name,))


def op_create_material(params):
    name = (params.get("name") or "").strip() or "DiabloMaterial"
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
    material.use_fake_user = True  # keep the datablock alive across session saves
    material.use_nodes = True
    bsdf = _principled_bsdf(material)

    color = _col(params.get("base_color", (0.8, 0.2, 0.2)))
    alpha = float(params.get("alpha", 1.0))
    bsdf.inputs["Base Color"].default_value = [color[0], color[1], color[2], alpha]
    if params.get("metallic") is not None:
        bsdf.inputs["Metallic"].default_value = float(params["metallic"])
    if params.get("roughness") is not None:
        bsdf.inputs["Roughness"].default_value = float(params["roughness"])

    return {
        "name": material.name,
        "base_color": [float(c) for c in color],
        "alpha": alpha,
        "metallic": bsdf.inputs["Metallic"].default_value,
        "roughness": bsdf.inputs["Roughness"].default_value,
    }


def op_assign_material(params):
    obj = _obj_or_raise(params.get("object_name"))
    material_name = params.get("material_name")
    if not material_name:
        raise ValueError("material_name is required")
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise ValueError("material not found: %r" % (material_name,))
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "materials"):
        raise ValueError("object %r does not accept materials" % (obj.name,))
    data.materials.clear()
    data.materials.append(material)
    return {
        "object": obj.name,
        "material": material.name,
        "slots": len(data.materials),
    }


# ------------------------------------------------------------------ camera


def op_configure_camera(params):
    name = (params.get("name") or "").strip() or "Camera"
    camera = bpy.data.cameras.get(name)
    obj = bpy.data.objects.get(name)
    if camera is None:
        camera = bpy.data.cameras.new(name)
        obj = bpy.data.objects.new(name, camera)
        bpy.context.collection.objects.link(obj)
    elif obj is None:
        obj = bpy.data.objects.new(name, camera)
        bpy.context.collection.objects.link(obj)

    if "location" in params:
        obj.location = _vec(params["location"])
    if "rotation" in params:
        obj.rotation_euler = _deg(params["rotation"])
    if params.get("lens") is not None:
        camera.lens = float(params["lens"])
    if params.get("make_active"):
        bpy.context.scene.camera = obj

    return {
        "name": obj.name,
        "type": "CAMERA",
        "location": [float(v) for v in obj.location],
        "rotation_degrees": _radians_to_degrees(obj.rotation_euler),
        "lens_mm": float(camera.lens),
        "active": bool(bpy.context.scene.camera == obj),
    }


# ------------------------------------------------------------------ lights


_LIGHT_TYPES = {"POINT", "SUN", "AREA", "SPOT"}


def op_configure_light(params):
    name = (params.get("name") or "").strip() or "DiabloLight"
    light_type = (params.get("light_type") or "POINT").upper()
    if light_type not in _LIGHT_TYPES:
        raise ValueError("unsupported light type %r" % (light_type,))

    light = bpy.data.lights.get(name)
    obj = bpy.data.objects.get(name)
    if light is None:
        light = bpy.data.lights.new(name, light_type)
        obj = bpy.data.objects.new(name, light)
        bpy.context.collection.objects.link(obj)
    else:
        if light.type != light_type:
            light.type = light_type
        if obj is None:
            obj = bpy.data.objects.new(name, light)
            bpy.context.collection.objects.link(obj)

    if "location" in params:
        obj.location = _vec(params["location"])
    if "rotation" in params:
        obj.rotation_euler = _deg(params["rotation"])
    light.energy = float(params.get("energy", 1.0))
    light.color = _col(params.get("color", (1.0, 1.0, 1.0)))
    if params.get("size") is not None and light.type in ("AREA", "SPOT"):
        light.size = float(params["size"])

    out = {
        "name": obj.name,
        "type": "LIGHT",
        "light_type": light.type,
        "location": [float(v) for v in obj.location],
        "rotation_degrees": _radians_to_degrees(obj.rotation_euler),
        "energy": float(light.energy),
        "color": [float(c) for c in light.color],
    }
    if light.type in ("AREA", "SPOT"):
        out["size"] = float(light.size)
    elif light.type == "SPOT":
        out["angle"] = float(light.angle)
    return out


# ------------------------------------------------------------------- script


def op_execute_script(params):
    script = params.get("script") or ""
    if not script.strip():
        raise ValueError("empty script")
    namespace = {"bpy": bpy, "__name__": "__diablo_blender_script__"}
    try:
        exec(compile(script, "<diablo-blender-script>", "exec"), namespace)  # noqa: S102 - explicit Diablo escape hatch
    except Exception as exc:  # noqa: BLE001 - report script errors to caller
        raise ValueError(str(exc)) from exc
    return {"executed": True}


# ---------------------------------------------------------------- save/export


def op_save(params):
    path = params.get("path") or ""
    if not path:
        raise ValueError("save path is required")
    if not path.lower().endswith(".blend"):
        path += ".blend"
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return {
        "path": path,
        "exists": os.path.isfile(path),
        "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
    }


def _enable_addon(module):
    """Best-effort addon enable; OBJ is core (no addon) on Blender 4.1+."""
    import sys as _sys

    try:
        bpy.ops.preferences.addon_enable(module=module)
    except Exception as exc:  # noqa: BLE001 - operator exists as core on modern Blender
        _sys.stderr.write("[diablo] addon enable failed for %r: %s\n" % (module, exc))


def _export_via(operator_name, **kwargs) -> None:
    """Call an export operator, giving a clear error if it is unavailable."""
    parts = operator_name.split(".")
    try:
        op = getattr(getattr(bpy.ops, parts[0]), parts[1])
    except AttributeError as exc:  # noqa: BLE001
        raise ValueError(
            "export operator %r is unavailable (enable the corresponding addon)" % operator_name
        ) from exc
    op(**kwargs)


def op_export(params):
    path = params.get("path") or ""
    if not path:
        raise ValueError("export path is required")
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    target = (params.get("format") or "").lower()
    if not target:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        target = {"glb": "glb", "gltf": "gltf"}.get(ext, ext)

    if target == "obj":
        _enable_addon("io_scene_obj")
        _export_via("wm.obj_export", filepath=path)
    elif target == "fbx":
        _enable_addon("io_scene_fbx")
        _export_via("export_scene.fbx", filepath=path)
    elif target in ("gltf", "glb"):
        _enable_addon("io_scene_gltf2")
        export_format = "GLB" if target == "glb" else "GLTF_SEPARATE"
        _export_via("export_scene.gltf", filepath=path, export_format=export_format)
    else:
        raise ValueError("unsupported export format %r (obj/fbx/gltf/glb)" % (target,))

    exists = os.path.isfile(path)
    if not exists:
        raise ValueError("export reported success but no file was produced: %s" % path)
    return {
        "path": path,
        "format": target,
        "exists": True,
        "size_bytes": os.path.getsize(path),
    }


# ------------------------------------------------------------------- render


_RENDER_FORMATS = {
    "PNG": "PNG", "JPEG": "JPEG", "JPG": "JPEG", "BMP": "BMP",
    "TIFF": "TIFF", "TGA": "TARGA", "TARGA": "TARGA", "WEBP": "WEBP",
    "OPEN_EXR": "OPEN_EXR", "EXR": "OPEN_EXR",
}

_RENDER_ENGINES = {"BLENDER_EEVEE_NEXT", "EEVEE_NEXT", "BLENDER_EEVEE", "EEVEE", "CYCLES", "BLENDER_WORKBENCH", "WORKBENCH"}


def op_render(params):
    path = params.get("path") or ""
    if not path:
        raise ValueError("render path is required")
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    scene = bpy.context.scene
    if params.get("engine"):
        engine = str(params["engine"]).upper()
        if engine not in _RENDER_ENGINES:
            raise ValueError("unsupported render engine %r" % (params["engine"],))
        if engine == "EEVEE":
            engine = "BLENDER_EEVEE"
        if engine == "EEVEE_NEXT":
            engine = "BLENDER_EEVEE_NEXT"
        if engine == "WORKBENCH":
            engine = "BLENDER_WORKBENCH"
        scene.render.engine = engine

    if params.get("resolution_x") or params.get("resolution_y"):
        scene.render.resolution_percentage = 100
    if params.get("resolution_x"):
        scene.render.resolution_x = int(params["resolution_x"])
    if params.get("resolution_y"):
        scene.render.resolution_y = int(params["resolution_y"])

    img_format = (params.get("format") or "PNG").upper()
    if img_format not in _RENDER_FORMATS:
        raise ValueError("unsupported render format %r" % (params["format"],))
    scene.render.image_settings.file_format = _RENDER_FORMATS[img_format]

    if params.get("use_gpu") and scene.render.engine == "CYCLES":
        try:
            scene.cycles.device = "GPU"
        except Exception:  # noqa: BLE001 - GPU opt-in is not fatal
            pass

    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)

    if not os.path.isfile(path):
        raise ValueError("render reported success but no file was produced: %s" % path)
    return {
        "path": path,
        "exists": True,
        "size_bytes": os.path.getsize(path),
        "engine": scene.render.engine,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
    }