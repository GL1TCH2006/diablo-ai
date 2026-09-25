"""M2.10 - Blender capability concurrency contract regression tests.

The layer owns a single session ``.blend`` that every mutating operation reads
and rewrites in place, so operations on one layer are serialized behind a
per-instance lock. These tests prove that contract rather than assuming it: the
recording runner below fails the test if two Blender operations ever overlap.
"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

import pytest

from blender.capabilities import BlenderCapabilityLayer
from blender.errors import BlenderScriptError
from blender.runner import RunnerResult
from integration.contracts import (
    BlenderCapabilityAdapter,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityStatus,
)
from integration.workflows import BlenderVisionWorkflow, BlenderVisionWorkflowCapabilityAdapter
from voice.cancellation import CancellationToken

REAL_BLENDER = os.environ.get("DIABLO_REAL_BLENDER_TESTS") == "1"
requires_real_blender = pytest.mark.skipif(
    not REAL_BLENDER, reason="set DIABLO_REAL_BLENDER_TESTS=1 to run real Blender tests"
)


def _png_bytes(width: int = 128, height: int = 128) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (40, 70, 110, 255)).save(buf, format="PNG")
    return buf.getvalue()


class RecordingRunner:
    """Fake Blender runner that records whether operations ever overlap."""

    def __init__(self, hold: float = 0.02, fail_ops: tuple[str, ...] = ()) -> None:
        self.hold = hold
        self.fail_ops = set(fail_ops)
        self.calls: list[dict] = []
        self.overlaps: list[str] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    # BlenderCapabilityLayer also exposes runner.resolve_exe()
    def resolve_exe(self) -> str:
        return "<fake-blender>"

    def run(self, request, timeout=None, scene_file=None):
        operation = request.get("operation", "")
        with self._lock:
            self._active += 1
            if self._active > self.max_active:
                self.max_active = self._active
            if self._active > 1:
                self.overlaps.append(operation)
            self.calls.append({"operation": operation, "active": self._active,
                               "request_id": request.get("request_id", "")})
        try:
            time.sleep(self.hold)
            if operation in self.fail_ops:
                return RunnerResult(success=False, operation=operation,
                                    error_code="blender_script_error",
                                    error_message="induced failure", return_code=0)
            data: dict = {"operation": operation, "active_while_running": self._active}
            if operation == "render":
                path = Path(str((request.get("params") or {}).get("path", "")))
                if path.name:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(_png_bytes())
                data["path"] = str(path)
                data["exists"] = True
            # Mirror the real dispatch script: mutating operations re-save the
            # session file, read-only ones do not.
            if operation not in ("inspect_scene", "list_objects", "inspect_object"):
                save_file = str((request.get("_session") or {}).get("save_file") or "")
                if save_file:
                    session = Path(save_file)
                    session.parent.mkdir(parents=True, exist_ok=True)
                    session.write_bytes(b"BLENDER-SESSION")
            return RunnerResult(success=True, operation=operation, data=data,
                                request_id=request.get("request_id", ""))
        finally:
            with self._lock:
                self._active -= 1


def _layer(tmp_path, **kw):
    return BlenderCapabilityLayer(runner=RecordingRunner(**kw), work_dir=str(tmp_path / "work"))


# --------------------------------------------------------------- A, B, J, K
def test_two_concurrent_operations_are_serialized(tmp_path):
    """A. Two concurrent operations on one layer never overlap."""
    layer = _layer(tmp_path, hold=0.05)
    errors: list[Exception] = []

    def op():
        try:
            layer.inspect_scene()
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=op) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert layer.runner.max_active == 1, layer.runner.overlaps
    assert layer.runner.overlaps == []
    assert len(layer.runner.calls) == 2


def test_four_concurrent_operations_are_serialized(tmp_path):
    """B. Four concurrent operations on one layer never overlap."""
    layer = _layer(tmp_path, hold=0.03)
    results: list[dict] = []
    errors: list[Exception] = []

    def op():
        try:
            results.append(layer.create_object(primitive="cube", name=f"Cube{id(results)}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=op) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert layer.runner.max_active == 1, layer.runner.overlaps
    assert layer.runner.overlaps == []
    assert len(results) == 4


def test_operations_do_not_interleave_across_operation_types(tmp_path):
    """K. Mixed operation types are serialized, so no torn session is observed."""
    layer = _layer(tmp_path, hold=0.02)
    errors: list[Exception] = []

    def op(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    work = [
        lambda: layer.inspect_scene(),
        lambda: layer.create_object(primitive="cube", name="A"),
        lambda: layer.create_material(name="M"),
        lambda: layer.configure_camera(name="Cam", make_active=True),
        lambda: layer.configure_light(name="Key"),
        lambda: layer.inspect_scene(),
    ]
    threads = [threading.Thread(target=op, args=(fn,)) for fn in work]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert layer.runner.max_active == 1, layer.runner.overlaps
    assert sorted(c["operation"] for c in layer.runner.calls) == sorted(
        ["inspect_scene", "create_object", "create_material",
         "configure_camera", "configure_light", "inspect_scene"]
    )


def test_session_file_is_written_and_removed_on_close(tmp_path):
    """I, J. Cleanup removes the session file and leaves no stale state."""
    layer = _layer(tmp_path, hold=0.0)
    layer.create_object(primitive="cube", name="Persisted")
    assert layer.session_file.is_file()
    session = layer.session_file
    layer.close()
    assert not session.exists()
    assert layer.last_error() is None


def test_no_stale_session_files_after_concurrent_operations(tmp_path):
    """J. Concurrent operations leave exactly one owned session file, then none."""
    layer = _layer(tmp_path, hold=0.01)
    errors: list[Exception] = []

    def op(index: int):
        try:
            layer.create_object(primitive="cube", name=f"Cube{index}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=op, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    work = tmp_path / "work"
    assert sorted(p.name for p in work.iterdir()) == ["session.blend"]
    assert layer.session_file.is_file()
    assert layer.runner.max_active == 1, layer.runner.overlaps
    layer.close()
    assert list(work.iterdir()) == []


def test_close_removes_blender_backup_siblings(tmp_path):
    """J. Blender's .blend1 backups are layer-owned and must not survive close()."""
    layer = _layer(tmp_path, hold=0.0)
    work = tmp_path / "work"
    layer.create_object(primitive="cube", name="A")
    # Blender creates a .blend1 backup on every re-save; emulate a real run.
    for n in (1, 2):
        (work / f"session.blend{n}").write_bytes(b"BLENDER-BACKUP")
    # a caller-owned file in the same work dir must survive
    (work / "keepme.txt").write_text("caller data")

    assert sorted(p.name for p in work.iterdir()) == [
        "keepme.txt", "session.blend", "session.blend1", "session.blend2",
    ]
    layer.close()

    assert not layer.session_file.exists()
    assert not (work / "session.blend1").exists()
    assert not (work / "session.blend2").exists()
    assert (work / "keepme.txt").read_text() == "caller data"
    assert sorted(p.name for p in work.iterdir()) == ["keepme.txt"]


def test_close_is_idempotent(tmp_path):
    """I. Repeated cleanup is safe and does not raise."""
    layer = _layer(tmp_path, hold=0.0)
    layer.create_object(primitive="cube", name="A")
    layer.close()
    layer.close()
    assert not layer.session_file.exists()


# ------------------------------------------------------------- C, D identity
def _registry_with_layer(layer):
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(layer))
    return registry


def test_same_task_id_different_request_ids(tmp_path):
    """C. Same task_id, different request_ids: both succeed, identity preserved."""
    layer = _layer(tmp_path, hold=0.03)
    registry = _registry_with_layer(layer)
    out: dict[str, object] = {}

    def run(name: str, request_id: str):
        request = CapabilityRequest(
            capability="blender", operation="inspect_scene",
            task_id="TASK-SHARED", request_id=request_id,
            cancellation_token=CancellationToken("TASK-SHARED"),
        )
        out[name] = registry.execute(request)

    threads = [threading.Thread(target=run, args=("A", "req-A")),
               threading.Thread(target=run, args=("B", "req-B"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert out["A"].status is CapabilityStatus.SUCCESS
    assert out["B"].status is CapabilityStatus.SUCCESS
    assert out["A"].request_id == "req-A"
    assert out["B"].request_id == "req-B"
    assert out["A"].task_id == "TASK-SHARED"
    assert out["B"].task_id == "TASK-SHARED"
    assert layer.runner.max_active == 1, layer.runner.overlaps


def test_different_task_ids_are_independent(tmp_path):
    """D. Different task_ids and request_ids stay isolated under concurrency."""
    layer = _layer(tmp_path, hold=0.03)
    registry = _registry_with_layer(layer)
    out: dict[str, object] = {}

    def run(index: int):
        request = CapabilityRequest(
            capability="blender", operation="inspect_scene",
            task_id=f"TASK-{index}", request_id=f"req-{index}",
            cancellation_token=CancellationToken(f"TASK-{index}"),
        )
        out[str(index)] = registry.execute(request)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    for i in range(4):
        assert out[str(i)].status is CapabilityStatus.SUCCESS
        assert out[str(i)].request_id == f"req-{i}"
        assert out[str(i)].task_id == f"TASK-{i}"
    assert layer.runner.max_active == 1, layer.runner.overlaps
    assert len(registry._active_requests) == 0


# ------------------------------------------------------- E, F, G cancellation
class _PollingCapability:
    """Capability that observes its own cancellation token while running."""

    name = "polling"

    def __init__(self, hold: float = 0.4) -> None:
        self.hold = hold
        self.entered = threading.Semaphore(0)
        self.cancel_calls: list[tuple[str, str]] = []

    def capabilities(self):
        return {"name": self.name, "available": True, "operations": ["execute"]}

    def can_execute(self, operation):
        return operation == "execute"

    def execute(self, request):
        self.entered.release()
        deadline = time.perf_counter() + self.hold
        while time.perf_counter() < deadline:
            if request.is_cancelled():
                from integration.contracts import CapabilityResult

                return CapabilityResult.failure(request, CapabilityStatus.CANCELLED, "observed")
            time.sleep(0.005)
        request.check_cancelled()
        from integration.contracts import CapabilityResult

        return CapabilityResult(status=CapabilityStatus.SUCCESS, capability=self.name,
                                operation="execute", data={"ok": True},
                                request_id=request.request_id, task_id=request.task_id)

    def cancel(self, task_id="", *, request_id=""):
        self.cancel_calls.append((task_id, request_id))
        return True


def _cancel_pair(cancel_id: str, tmp_path):
    capability = _PollingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    tokens = {n: CancellationToken("TASK-C") for n in ("req-A", "req-B")}
    requests = {
        n: CapabilityRequest(capability="polling", operation="execute", task_id="TASK-C",
                             request_id=n, cancellation_token=tokens[n])
        for n in ("req-A", "req-B")
    }
    out: dict[str, object] = {}
    threads = [threading.Thread(target=lambda n=n: out.__setitem__(n, registry.execute(requests[n])))
               for n in ("req-A", "req-B")]
    for t in threads:
        t.start()
    capability.entered.acquire(timeout=10)
    capability.entered.acquire(timeout=10)
    time.sleep(0.05)
    result = registry.cancel("polling", "TASK-C", request_id=cancel_id)
    for t in threads:
        t.join(timeout=30)
    return out, tokens, result, capability


def test_cancel_a_while_b_active(tmp_path):
    """E. Cancelling A leaves B successful and only A cancelled."""
    out, tokens, result, capability = _cancel_pair("req-A", tmp_path)
    assert result.status is CapabilityStatus.CANCELLED
    assert out["req-A"].status is CapabilityStatus.CANCELLED
    assert out["req-B"].status is CapabilityStatus.SUCCESS
    assert tokens["req-A"].is_cancelled() is True
    assert tokens["req-B"].is_cancelled() is False
    assert capability.cancel_calls == [("TASK-C", "req-A")]


def test_reverse_cancellation_while_other_active(tmp_path):
    """F. Reverse: cancelling B leaves A successful and only B cancelled."""
    out, tokens, result, capability = _cancel_pair("req-B", tmp_path)
    assert result.status is CapabilityStatus.CANCELLED
    assert out["req-A"].status is CapabilityStatus.SUCCESS
    assert out["req-B"].status is CapabilityStatus.CANCELLED
    assert tokens["req-A"].is_cancelled() is False
    assert tokens["req-B"].is_cancelled() is True
    assert capability.cancel_calls == [("TASK-C", "req-B")]


def test_repeated_cancellation_is_idempotent(tmp_path):
    """G. Repeated cancellation of the same request stays stable."""
    from integration.contracts import CapabilityResult

    capability = _PollingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token = CancellationToken("TASK-R")
    request = CapabilityRequest(capability="polling", operation="execute", task_id="TASK-R",
                                request_id="req-R", cancellation_token=token)
    out: dict[str, object] = {}
    thread = threading.Thread(target=lambda: out.__setitem__("r", registry.execute(request)))
    thread.start()
    capability.entered.acquire(timeout=10)
    time.sleep(0.05)
    results = [registry.cancel("polling", "TASK-R", request_id="req-R") for _ in range(3)]
    thread.join(timeout=30)

    assert all(r.status is CapabilityStatus.CANCELLED for r in results)
    assert isinstance(out["r"], CapabilityResult)
    assert out["r"].status is CapabilityStatus.CANCELLED
    assert token.is_cancelled() is True
    assert len(registry._active_requests) == 0


def test_cancelled_request_never_starts_a_blender_subprocess(tmp_path):
    """E. A request cancelled before dispatch spawns no Blender process."""
    layer = _layer(tmp_path, hold=0.0)
    registry = _registry_with_layer(layer)
    token = CancellationToken("TASK-X")
    token.request("cancelled up front", source="test")
    result = registry.execute(CapabilityRequest(
        capability="blender", operation="inspect_scene", task_id="TASK-X",
        request_id="req-X", cancellation_token=token))

    assert result.status is CapabilityStatus.CANCELLED
    assert layer.runner.calls == []
    assert len(registry._active_requests) == 0


# ------------------------------------------------------------------- H failure
def test_one_operation_fails_while_another_succeeds(tmp_path):
    """H. A failing operation does not corrupt or block a concurrent success."""
    layer = _layer(tmp_path, hold=0.02, fail_ops=("create_material",))
    registry = _registry_with_layer(layer)
    out: dict[str, object] = {}

    def ok():
        out["ok"] = registry.execute(CapabilityRequest(
            capability="blender", operation="inspect_scene",
            task_id="TASK-H", request_id="req-ok"))

    def bad():
        out["bad"] = registry.execute(CapabilityRequest(
            capability="blender", operation="create_material", params={"name": "M"},
            task_id="TASK-H", request_id="req-bad"))

    threads = [threading.Thread(target=ok), threading.Thread(target=bad)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert out["ok"].status is CapabilityStatus.SUCCESS
    assert out["bad"].status is CapabilityStatus.FAILURE
    assert (out["bad"].error or {}).get("message")
    assert out["ok"].request_id == "req-ok"
    assert out["bad"].request_id == "req-bad"
    assert layer.runner.max_active == 1, layer.runner.overlaps
    assert len(registry._active_requests) == 0


def test_layer_raises_typed_error_and_records_last_error(tmp_path):
    """H. The typed failure path still works inside the serialized section."""
    layer = _layer(tmp_path, hold=0.0, fail_ops=("save",))
    with pytest.raises(BlenderScriptError):
        layer.save(tmp_path / "out.blend")
    assert layer.last_error() is not None
    assert layer.last_error().get("message") == "induced failure"


# ----------------------------------------------------------------- M workflow
class _StubVision:
    preferred_backend = "fallback"

    def detect_capabilities(self):
        return {"vision_available": True, "available_backends": ["fallback"]}

    def inspect_render(self, image_path, context=None):
        from vision.capabilities import create_vision_layer

        return create_vision_layer().inspect_render(str(image_path))

    def compare(self, image_path, reference_image, threshold=0.01, context=None):
        from vision.capabilities import create_vision_layer

        return create_vision_layer().compare(str(image_path), str(reference_image), threshold)

    def describe(self, image_path, detail_level="medium", focus=None, context=None):
        from vision.capabilities import create_vision_layer

        return create_vision_layer().describe(str(image_path), detail_level, focus, context)

    def inspect(self, image_path):
        from vision.capabilities import create_vision_layer

        return create_vision_layer().inspect(str(image_path))


def test_s28_workflow_still_serializes_and_verifies(tmp_path):
    """M. S2.8 keeps its own _run_lock and still verifies a real render."""
    layer = _layer(tmp_path, hold=0.0)
    workflow = BlenderVisionWorkflow(blender=layer, vision=_StubVision())
    registry = CapabilityRegistry()
    registry.register(BlenderVisionWorkflowCapabilityAdapter(workflow))
    out = tmp_path / "wf.png"
    result = registry.execute(CapabilityRequest(
        capability="blender_vision_workflow", operation="run",
        params={"plan": {"objects": [{"name": "Cube"}]}, "output_path": str(out)},
        task_id="TASK-WF", request_id="req-wf"))

    assert result.status is CapabilityStatus.SUCCESS, result.error
    assert result.data["verified"] is True
    assert result.request_id == "req-wf"
    assert result.task_id == "TASK-WF"
    assert out.is_file()
    assert layer.runner.max_active == 1, layer.runner.overlaps
    assert len(workflow._tokens) == 0
    workflow.close()
    # The layer was injected, so the workflow does not own its teardown: the
    # session file is released by the layer's own close().
    assert layer.session_file.is_file()
    layer.close()
    assert not layer.session_file.exists()


# ------------------------------------------------------- L real Blender smoke
@requires_real_blender
def test_real_blender_concurrent_operations_serialize(tmp_path):
    """L. Real Blender 5.2.2: concurrent operations queue and both render."""
    from blender.controller import detect_blender

    info = detect_blender()
    if not info.installed:
        pytest.skip("Blender is not installed")
    layer = BlenderCapabilityLayer(work_dir=str(tmp_path / "real"), timeout=300.0)
    errors: list[Exception] = []
    outputs: list[str] = []

    def op(index: int):
        target = tmp_path / f"real_{index}.png"
        try:
            layer.create_object(primitive="cube", name=f"RealCube{index}")
            layer.render(target, resolution_x=96, resolution_y=96)
            outputs.append(str(target))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=op, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=900)

    assert not errors, errors
    assert len(outputs) == 2
    for path in outputs:
        assert Path(path).is_file() and Path(path).stat().st_size > 1000
    layer.close()
    assert not layer.session_file.exists()


@requires_real_blender
def test_real_blender_single_operation_still_succeeds(tmp_path):
    """L. A single real Blender render is unaffected by the lock."""
    from blender.controller import detect_blender

    info = detect_blender()
    if not info.installed:
        pytest.skip("Blender is not installed")
    layer = BlenderCapabilityLayer(work_dir=str(tmp_path / "single"), timeout=300.0)
    try:
        layer.create_object(primitive="sphere", name="SoloSphere")
        target = tmp_path / "solo.png"
        layer.render(target, resolution_x=128, resolution_y=128)
        assert target.is_file() and target.stat().st_size > 1000
        inspection = layer.inspect_render(target)
        assert inspection["readable"] is True
        assert inspection["width"] == 128 and inspection["height"] == 128
    finally:
        layer.close()
