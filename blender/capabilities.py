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

import inspect
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from core.logging_setup import get_logger

from .errors import (
    BlenderCancelledError,
    BlenderClosedError,
    BlenderError,
    BlenderScriptError,
    C_SCRIPT_ERROR,
)
from .operations import is_interruptible, requires_session_revalidation
from .protocol import build_request
from .render_inspect import inspect_render as _inspect_render_file
from .runner import BlenderRunner

log = get_logger("blender.capabilities")

#: Leading bytes of a session file we are willing to reuse. Blender writes either
#: a plain ``BLENDER`` header or a gzip container when compression is enabled.
_SESSION_MAGIC = (b"BLENDER", b"\x1f\x8b")

#: A session smaller than this cannot be a real ``.blend``.
_MIN_SESSION_BYTES = 12


def session_file_is_valid(path: Path) -> bool:
    """Whether a session file is plausibly intact and safe to reuse.

    Conservative by design. After a forced termination the session may have been
    caught mid-write, and reusing a torn ``.blend`` produces the confusing
    "Cannot change old file" failure. When this returns ``False`` the caller
    discards the session rather than reusing it; a fresh session is cheap, a
    corrupt one is not.
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size < _MIN_SESSION_BYTES:
            return False
        with path.open("rb") as handle:
            head = handle.read(len(_SESSION_MAGIC[0]))
    except OSError:
        return False
    return any(head.startswith(magic) for magic in _SESSION_MAGIC)


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

    Cancellation
    ------------
    Cancellation is honoured before a subprocess starts and, when a token is
    bound via :meth:`cancellation_scope`, while one is running. A running child
    is terminated (with a bounded kill fallback) and reaped, and the result is
    reported as cancellation rather than as a generic failure. Operations are
    split by the authoritative inventory in :mod:`blender.operations`:

    * read-only operations cannot corrupt the session, so terminating them is
      always safe;
    * session-mutating operations can be caught mid-save, so after a forced
      termination the session is revalidated and discarded when it is not
      provably intact, rather than reused blindly.
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
        # Only a layer-created workspace is removed wholesale on close. A
        # caller-supplied directory is left in place with the session artifacts
        # cleared out of it.
        self._owns_work_dir = work_dir is None
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
        # Cancellation token for the call currently in flight on this thread.
        self._cancellation = threading.local()
        self._closed = False
        # Whether a forced termination has left the session unproven.
        self._session_suspect = False

    # -------------------------------------------------------------- helpers

    @property
    def runner(self) -> BlenderRunner:
        return self._runner

    @property
    def blender_exe(self) -> str:
        return self._runner.resolve_exe()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has run. A closed layer accepts no operations."""
        return self._closed

    def detect(self):
        """Return the detection result (BlenderInfo), unchanged API.

        Detection is cached, so repeated calls do not re-probe the filesystem or
        spawn ``blender --version`` again. See
        :func:`blender.controller.detect_blender`.
        """
        from .controller import detect_blender

        return detect_blender()

    def last_error(self) -> dict | None:
        """Most recent error as JSON-safe dict, or None after a success.

        The payload carries the trusted ``request_id`` of the call that failed as
        diagnostic attribution, so a stored failure is never mistaken for
        belonging to a later request.
        """
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

    @contextmanager
    def cancellation_scope(self, token=None):
        """Bind a cancellation token for the calls made inside.

        Thread-local and re-entrant, mirroring :meth:`request_correlation`. With
        a token bound, a running Blender child is terminated instead of being
        waited out.
        """
        previous = getattr(self._cancellation, "token", None)
        self._cancellation.token = token
        try:
            yield self
        finally:
            self._cancellation.token = previous

    def _trusted_request_id(self) -> str:
        """The S1-owned request id for this call, or '' when there is none."""
        correlation = getattr(self._correlation, "value", None) or {}
        return str(correlation.get("request_id") or "")

    def _cancel_token(self):
        return getattr(self._cancellation, "token", None)

    def _runner_accepts_cancellation(self) -> bool:
        """Whether this runner's ``run`` accepts a ``cancel_token`` keyword.

        Test doubles and third-party runners may not. Probing the signature keeps
        them working unchanged, which is the same approach the capability
        contracts use for optional hooks.
        """
        run = getattr(self._runner, "run", None)
        if not callable(run):
            return False
        try:
            parameters = inspect.signature(run).parameters
        except (TypeError, ValueError):
            return False
        if "cancel_token" in parameters:
            return True
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    def _record_error(self, exc: BlenderError) -> None:
        """Store a failure with its diagnostic attribution.

        The current call's trusted id always wins, even if the exception
        instance is reused across calls: a recorded failure must never claim to
        belong to an earlier request.
        """
        exc.request_id = self._trusted_request_id()
        self._last_error = exc.to_dict()

    def _run(self, operation: str, params: dict | None = None, timeout: float | None = None) -> dict:
        # The whole operation is serialized: build_request stats session_file to
        # decide whether Blender opens it, and the runner then reads and rewrites
        # it. Both must happen with exclusive access to avoid a torn session.
        with self._session_lock:
            if self._closed:
                raise BlenderClosedError(
                    operation=operation,
                    message="blender capability layer is closed",
                    request_id=self._trusted_request_id(),
                )
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
            token = self._cancel_token()
            run_kwargs: dict = {"timeout": timeout or self.timeout,
                                "scene_file": self.session_file}
            if token is not None and self._runner_accepts_cancellation():
                run_kwargs["cancel_token"] = token
            try:
                result = self._runner.run(request, **run_kwargs)
            except BlenderCancelledError as exc:
                # A forced termination can leave the session mid-write. Never
                # reuse it unproven.
                self._after_forced_termination(operation, exc)
                self._record_error(exc)
                raise
            except BlenderError as exc:
                self._record_error(exc)
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
                    request_id=trusted_request_id,
                    diagnostics={"stdout_truncated": result.stdout_truncated,
                                "stderr_truncated": result.stderr_truncated},
                )
                self._record_error(exc)
                raise exc
            self._last_error = None
            return result.data if result.data is not None else {}

    def _after_forced_termination(self, operation: str, exc: BlenderCancelledError) -> None:
        """Revalidate the session after a child was killed mid-operation.

        Also records the ratified cancellation attribution: whether the child was
        actually terminated, and whether the operation was interruptible at all.
        Operations that do not write the session need no revalidation. Ones that
        do are checked, and an unprovable session is discarded so the next
        operation starts clean instead of inheriting a torn file.
        """
        exc.diagnostics.setdefault("terminated", exc.terminated)
        exc.diagnostics.setdefault("cancellation_terminated", exc.terminated)
        exc.diagnostics["interruptible"] = is_interruptible(operation)
        if not requires_session_revalidation(operation):
            exc.diagnostics["session_revalidated"] = False
            return
        if session_file_is_valid(self.session_file):
            self._session_suspect = False
            exc.diagnostics["session_revalidated"] = True
            exc.diagnostics["session_discarded"] = False
            return
        discarded = self._discard_session()
        self._session_suspect = False
        exc.diagnostics["session_revalidated"] = True
        exc.diagnostics["session_discarded"] = discarded
        log.warning(
            "session discarded after interrupted %s: %s", operation, self.session_file
        )

    def _discard_session(self) -> bool:
        """Remove the session and its backups. Returns whether anything went."""
        removed = False
        for path in self._session_artifacts():
            try:
                path.unlink()
                removed = True
            except OSError as exc:
                log.warning("could not remove session artifact %s: %s", path, exc)
        return removed

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
        """Release the session scratch files. Saved renders/exports are preserved.

        Safe to call repeatedly and safe to call while an operation is running:
        the session lock is taken first, so a session is never unlinked while a
        child is still using it. A layer-created workspace is removed entirely; a
        caller-supplied one is left in place with its session artifacts cleared.

        Cleanup never raises. A failure to delete must not turn an otherwise
        successful program into an unrelated error.
        """
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
            self._discard_session()
            if self._owns_work_dir:
                self._remove_work_dir()

    def _remove_work_dir(self) -> None:
        """Remove a layer-created workspace, tolerating any failure."""
        try:
            shutil.rmtree(self._work_dir, ignore_errors=False)
        except OSError as exc:
            # Locked files are routine on Windows while a child is closing.
            log.warning("could not remove blender work dir %s: %s", self._work_dir, exc)
            try:
                shutil.rmtree(self._work_dir, ignore_errors=True)
            except OSError:
                pass

    def _close_best_effort(self, timeout: float = 0.0) -> None:
        """Cleanup for the destructor: never block, never raise.

        A destructor must not stall the interpreter, so it only takes the session
        lock if the lock is immediately free (or available within ``timeout``).
        If an operation is still running, that operation's own path is
        responsible for finishing cleanup.
        """
        if timeout:
            acquired = self._session_lock.acquire(timeout=timeout)
        else:
            acquired = self._session_lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            if self._closed:
                return
            self._closed = True
            self._discard_session()
            if self._owns_work_dir:
                self._remove_work_dir()
        except Exception:  # noqa: BLE001 - destructor must not raise
            pass
        finally:
            self._session_lock.release()

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
            self._close_best_effort()
        except Exception:  # noqa: BLE001 - destructor must not raise
            pass