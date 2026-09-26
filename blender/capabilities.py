from __future__ import annotations

"""Diablo's production Blender capability layer.

Composes the existing detection controller with the new subprocess runner and
JSON protocol. Each method sends one operation to a background Blender process.
Scene state is preserved across calls through a private session ``.blend`` file
in a temp workspace owned by this layer.

Methods never silently swallow Blender failures. Infrastructure problems
(executable missing, timeout, non-zero exit, unparseable result) raise the
typed exceptions from :mod:`blender.errors`; script-level failures raise
:class:`BlenderScriptError`. ``last_error()`` always exposes the most recent
failure as JSON-safe data for callers that prefer structured values.

Transform convention: location/scale are ``[x, y, z]`` floats in Blender units;
``rotation`` is ``[x, y, z]`` in **degrees** (Euler XYZ).
"""

import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from core.logging_setup import get_logger

from .errors import (
    BlenderError,
    BlenderScriptError,
    C_SCRIPT_ERROR,
)
from .protocol import build_request
from .render_inspect import inspect_render as _inspect_render_file
from .runner import BlenderRunner

log = get_logger("blender.capabilities")


class BlenderCapabilityLayer:
    """High-level Blender scene capabilities for Diablo (production track).

    Concurrency contract
    --------------------
    A layer owns exactly one session ``.blend`` file. Every mutating operation
    is a read-modify-write of that file: the dispatch runner opens the session
    and then saves it back to the same path. Two Blender processes doing that
    concurrently invalidate each other's handle, and the second save fails with
    "Cannot change old file (file saved with @)".

    Operations on a layer are therefore serialized behind a per-instance
    re-entrant lock. Concurrent callers queue rather than collide:

    * operations never interleave, so no request can observe another request's
      partially written session;
    * only one process ever writes the session file at a time;
    * scene state still accumulates across calls, which is what the workflow
      layer depends on when it builds a scene over several operations.

    Separate layers are independent: each owns a private ``work_dir`` and
    session file, so two layers must not be pointed at the same ``work_dir``.

    Cancellation is observed at the capability boundary, before a subprocess is
    started. An operation that is already running is never interrupted, because
    killing Blender mid-save is precisely what would corrupt the session.
    """

    def __init__(
        self,
        exe: str | Path | None = None,
        runner: BlenderRunner | None = None,
        work_dir: str | Path | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._runner = runner or BlenderRunner(exe=exe)
        self.timeout = timeout
        self._last_error: dict | None = None
        self._work_dir = Path(
            work_dir or tempfile.mkdtemp(prefix="diablo_blender_work_")
        )
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self._work_dir / "session.blend"
        # Serializes the read-modify-write of session_file. Held for the whole
        # subprocess invocation so concurrent callers queue instead of racing.
        self._session_lock = threading.RLock()
        # Trusted S1 request identity for the calls currently in flight on this
        # thread. Thread-local so concurrent requests never observe each other's
        # correlation.
        self._correlation = threading.local()

    # -------------------------------------------------------------- helpers

    @property
    def runner(self) -> BlenderRunner:
        return self._runner

    @property
    def blender_exe(self) -> str:
        return self._runner.resolve_exe()

    def detect(self):
        """Return the existing detection result (BlenderInfo), unchanged API."""
        from .controller import detect_blender

        return detect_blender()

    def last_error(self) -> dict | None:
        """Most recent error as JSON-safe dict, or None after a success."""
        return self._last_error

    @contextmanager
    def request_correlation(self, request_id: str = "", context: dict | None = None):
        """Bind the trusted S1 request identity for the calls made inside.

        S1 owns ``request_id``. The capability adapter enters this scope around
        the layer call so the Blender wire envelope carries that same id instead
        of an unrelated one minted here. The scope is thread-local, re-entrant
        and always restored, so it cannot leak across concurrent requests.
        """
        previous = getattr(self._correlation, "value", None)
        self._correlation.value = {
            "request_id": str(request_id or ""),
            "context": dict(context or {}),
        }
        try:
            yield self
        finally:
            self._correlation.value = previous

    def _run(self, operation: str, params: dict | None = None, timeout: float | None = None) -> dict:
        # The whole operation is serialized: build_request stats session_file to
        # decide whether Blender opens it, and the runner then reads and rewrites
        # it. Both must happen with exclusive access to avoid a torn session.
        with self._session_lock:
            correlation = getattr(self._correlation, "value", None) or {}
            trusted_request_id = str(correlation.get("request_id") or "")
            request = build_request(
                operation,
                params,
                request_id=trusted_request_id or None,
                session={
                    "open_file": str(self.session_file) if self.session_file.is_file() else "",
                    "save_file": str(self.session_file),
                },
            )
            try:
                result = self._runner.run(
                    request, timeout=timeout or self.timeout, scene_file=self.session_file
                )
            except BlenderError as exc:
                self._last_error = exc.to_dict()
                raise
            if not result.success:
                exc = BlenderScriptError(
                    operation=operation,
                    message=result.error_message or "blender script failed",
                    command=None,
                    return_code=result.return_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    code=result.error_code or C_SCRIPT_ERROR,
                )
                self._last_error = exc.to_dict()
                raise exc
            self._last_error = None
            return result.data if result.data is not None else {}

    def _session_artifacts(self) -> list[Path]:
        """The session file plus the Blender-generated backups beside it.

        Blender writes a ``.blend1`` backup on every re-save of the session, so a
        layer that performed more than one mutating operation owns those files
        too. Anything else in the work directory belongs to the caller.
        """
        artifacts: list[Path] = []
        if self.session_file.exists():
            artifacts.append(self.session_file)
        parent = self.session_file.parent
        if parent.is_dir():
            artifacts.extend(sorted(parent.glob(f"{self.session_file.name}[0-9]*")))
        return artifacts

    def close(self) -> None:
        """Release the session scratch files. Saved renders/exports are preserved."""
        try:
            for path in self._session_artifacts():
                path.unlink()
        except OSError:
            pass

    # ----------------------------------------------------------- inspection

    def inspect_scene(self) -> dict:
        """Scene metadata: objects, cameras, lights, materials, render settings."""
        return self._run("inspect_scene")

    def list_objects(self, obj_type: str = "") -> dict:
        """List objects: ``{"count": n, "objects": [{"name":..., "type":...}]}``."""
        return self._run("list_objects", {"type": obj_type or ""})

    def inspect_object(self, name: str) -> dict:
        """Full JSON-safe inspection of a single object."""
        return self._run("inspect_object", {"name": name})

    # --------------------------------------------------------------- objects

    def create_object(
        self,
        primitive: str = "cube",
        name: str | None = None,
        location=None,
        rotation=None,
        scale=None,
        **extra,
    ) -> dict:
        """Create a primitive (cube/sphere/cylinder/cone/plane/torus)."""
        params = {
            "primitive": primitive,
            "name": name or "",
            "location": location,
            "rotation": rotation,
            "scale": scale,
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        return self._run("create_object", params)

    def delete_object(self, name: str) -> dict:
        """Delete one object by exact name."""
        return self._run("delete_object", {"name": name})

    def modify_object(
        self, name: str, new_name: str | None = None, hidden: bool | None = None,
        select: bool | None = None,
    ) -> dict:
        """Rename / hide / select an object."""
        params: dict = {"name": name}
        if new_name is not None:
            params["new_name"] = new_name
        if hidden is not None:
            params["hidden"] = bool(hidden)
        if select is not None:
            params["select"] = bool(select)
        return self._run("modify_object", params)

    def transform_object(self, name: str, location=None, rotation=None, scale=None) -> dict:
        """Set location/rotation/scale components (rotation in degrees)."""
        params = {"name": name}
        if location is not None:
            params["location"] = location
        if rotation is not None:
            params["rotation"] = rotation
        if scale is not None:
            params["scale"] = scale
        return self._run("transform_object", params)

    # -------------------------------------------------------------- materials

    def create_material(
        self,
        name: str | None = None,
        base_color=None,
        metallic: float | None = None,
        roughness: float | None = None,
        alpha: float | None = None,
    ) -> dict:
        """Create a Principled-shader material."""
        params = {"name": name or ""}
        if base_color is not None:
            params["base_color"] = base_color
        if metallic is not None:
            params["metallic"] = metallic
        if roughness is not None:
            params["roughness"] = roughness
        if alpha is not None:
            params["alpha"] = alpha
        return self._run("create_material", params)

    def assign_material(self, object_name: str, material_name: str) -> dict:
        """Assign an existing material to an object's mesh."""
        return self._run("assign_material", {"object_name": object_name, "material_name": material_name})

    # --------------------------------------------------------------- camera

    def configure_camera(
        self,
        name: str | None = None,
        location=None,
        rotation=None,
        lens: float | None = None,
        make_active: bool = False,
    ) -> dict:
        """Create/update a camera (rotation in degrees, lens in mm)."""
        params = {"name": name or ""}
        if location is not None:
            params["location"] = location
        if rotation is not None:
            params["rotation"] = rotation
        if lens is not None:
            params["lens"] = lens
        params["make_active"] = bool(make_active)
        return self._run("configure_camera", params)

    # ---------------------------------------------------------------- lights

    def configure_light(
        self,
        name: str | None = None,
        light_type: str = "POINT",
        location=None,
        rotation=None,
        energy: float = 1.0,
        color=None,
        size: float | None = None,
    ) -> dict:
        """Create/update a light (POINT/SUN/AREA/SPOT)."""
        params = {
            "name": name or "",
            "light_type": light_type,
            "energy": energy,
        }
        if location is not None:
            params["location"] = location
        if rotation is not None:
            params["rotation"] = rotation
        if color is not None:
            params["color"] = color
        if size is not None:
            params["size"] = size
        return self._run("configure_light", params)

    # --------------------------------------------------------------- script

    def execute_script(self, script: str) -> dict:
        """Execute arbitrary Blender Python inside the session context."""
        return self._run("execute_script", {"script": script})

    # ---------------------------------------------------------- save / export

    def save(self, path: str | Path) -> dict:
        """Save the current session as a .blend file."""
        return self._run("save", {"path": str(path)})

    def export(self, path: str | Path, fmt: str | None = None) -> dict:
        """Export the scene (OBJ/FBX/GLTF/GLB). Format inferred from file name."""
        return self._run("export", {"path": str(path), "format": fmt or ""})

    # ---------------------------------------------------------------- render

    def render(
        self,
        path: str | Path,
        resolution_x: int | None = None,
        resolution_y: int | None = None,
        img_format: str = "PNG",
        engine: str | None = None,
        use_gpu: bool | None = None,
    ) -> dict:
        """Render the current scene to an image file."""
        params = {"path": str(path), "format": img_format or "PNG"}
        if resolution_x is not None:
            params["resolution_x"] = int(resolution_x)
        if resolution_y is not None:
            params["resolution_y"] = int(resolution_y)
        if engine:
            params["engine"] = engine
        if use_gpu is not None:
            params["use_gpu"] = bool(use_gpu)
        return self._run("render", params, timeout=max(self.timeout, self.timeout * 2))

    def inspect_render(self, path: str | Path) -> dict:
        """Basic technical validation of a rendered image file (local, no Blender)."""
        return _inspect_render_file(path)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - destructor must not raise
            pass