"""M2.15.2 - a cancelled Blender operation must actually stop.

The workflow checked its own cancellation token *between* steps, so a cancel
that landed between two Blender operations was caught. A cancel that landed
during a long ``render`` was not.

``BlenderCapabilityLayer._run`` reads ``self._cancel_token()``, which is only
populated by the layer's ``cancellation_scope`` context manager.
``BlenderVisionWorkflow._call_layer`` invoked the layer method directly and never
entered that scope, so the runner was handed ``cancel_token=None``, never polled
for cancellation, and let the Blender child run to completion after the user had
already cancelled. ``BlenderRunner._spawn_and_collect`` knows how to terminate a
child on cancellation - it was simply never told there was anything to poll.

These tests drive the production path (``BlenderVisionWorkflowCapabilityAdapter``
→ workflow → layer → runner) rather than the layer in isolation, because the
defect was in the seam between them and a layer-only test could not have found
it.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from blender.capabilities import BlenderCapabilityLayer
from blender.errors import BlenderCancelledError
from integration.contracts import (
    CapabilityRequest,
    CapabilityStatus,
)
from integration.workflows import (
    BlenderVisionWorkflow,
    BlenderVisionWorkflowCapabilityAdapter,
)
from voice.cancellation import CancellationToken


# ══════════════════════════════════════════════════════ doubles


class RecordingRunner:
    """A runner double that records what it was handed and can go slow.

    ``on_run`` lets a test simulate the child being cancelled mid-operation, the
    way a real ``BlenderRunner`` behaves when it polls a token while a render is
    still running.
    """

    def __init__(self, *, on_run=None, poll_cancel: bool = False) -> None:
        self.calls: list[dict] = []
        self.polls = 0
        self._on_run = on_run
        self._poll_cancel = poll_cancel

    def run(self, request, timeout=None, scene_file=None, cancel_token=None):
        self.calls.append({"cancel_token": cancel_token, "timeout": timeout})
        if self._on_run is not None:
            self._on_run(self, cancel_token)
        elif self._poll_cancel and cancel_token is not None:
            # Mirror the real runner: poll, and raise when cancelled.
            for _ in range(200):
                self.polls += 1
                if cancel_token.is_cancelled():
                    raise BlenderCancelledError(
                        operation="render", message="cancelled while rendering")
                time.sleep(0.005)
        return _ok()

    def stop(self, *_args, **_kwargs) -> bool:
        return True

    def last_result(self):
        return None


class _FakeIntegration:
    """The minimum ``BlenderVisionWorkflow`` needs to reach a Blender layer."""

    def __init__(self, blender):
        self.blender = blender
        self.vision = None

    def last_error(self):
        return None


def _ok():
    return type("Result", (), {
        "success": True, "data": {"objects": []}, "error_message": "",
        "return_code": 0, "stdout": "", "stderr": "", "error_code": "",
        "stdout_truncated": False, "stderr_truncated": False,
    })()


def _build(runner, tmp_path):
    # The workflow takes the layer through the real BlenderVisionIntegration, as
    # production does, so the adapter -> workflow -> layer -> runner path under
    # test is the one that actually runs.
    layer = BlenderCapabilityLayer(runner=runner, work_dir=str(tmp_path / "w"))
    workflow = BlenderVisionWorkflow(blender=layer)
    return layer, workflow, BlenderVisionWorkflowCapabilityAdapter(workflow)


def _request(token=None, task_id="t1", operation="inspect_scene_state"):
    return CapabilityRequest(
        capability="blender_vision_workflow", operation=operation,
        task_id=task_id, cancellation_token=token)


# ══════════════════════════════════════════════════════ 1. propagation


class TestTheTokenReachesTheRunner:
    def test_the_runner_is_handed_this_requests_token(self, tmp_path):
        """The property that was false before this task."""
        runner = RecordingRunner()
        layer, _workflow, adapter = _build(runner, tmp_path)
        token = CancellationToken("task-x")

        adapter.execute(_request(token))

        assert len(runner.calls) == 1, "the Blender layer was never called"
        assert runner.calls[0]["cancel_token"] is token, (
            "the runner was not handed the request's cancellation token, so it "
            "could not have observed a cancel")

    def test_the_layer_sees_the_token_while_the_call_is_in_flight(self, tmp_path):
        """Checked from inside the call, not after it.

        ``BlenderCapabilityLayer._run`` reads the thread-local, so a test that
        inspected it after the call returned would see ``None`` whether or not
        the binding ever happened.
        """
        seen: list[object] = []
        runner = RecordingRunner(on_run=lambda _r, _t: seen.append(
            _LAYER_HOLDER[0]._cancel_token()))
        layer, _workflow, adapter = _build(runner, tmp_path)
        _LAYER_HOLDER[0] = layer

        adapter.execute(_request(CancellationToken("task-x")))

        assert seen and seen[0] is not None, (
            "the layer's cancellation scope was never entered")

    def test_a_workflow_supplied_token_is_used_when_the_request_has_none(
            self, tmp_path):
        """``bind_task`` is the other way a token reaches a workflow; it must
        reach the runner too."""
        runner = RecordingRunner()
        layer, workflow, adapter = _build(runner, tmp_path)
        token = CancellationToken("task-x")
        workflow.bind_task("t1", token)

        adapter.execute(_request(task_id="t1"))

        assert runner.calls and runner.calls[0]["cancel_token"] is token, (
            "a task-bound token was not carried to the runner")


_LAYER_HOLDER: list = [None]


# ══════════════════════════════════════════════════════ 2. preflight


class TestPreflightCancellationPreventsWork:
    def test_a_cancelled_request_never_reaches_blender(self, tmp_path):
        runner = RecordingRunner()
        _layer, _workflow, adapter = _build(runner, tmp_path)
        token = CancellationToken("task-x")
        token.request("user cancelled", source="test")

        result = adapter.execute(_request(token))

        assert result.status is CapabilityStatus.CANCELLED
        assert runner.calls == [], (
            "a cancelled request still spawned a Blender child")

    def test_a_cancelled_token_produces_a_cancelled_capability_status(self, tmp_path):
        _runner, _workflow, adapter = _build(RecordingRunner(), tmp_path)
        token = CancellationToken("task-x")
        token.request("stop", source="test")

        result = adapter.execute(_request(token))

        assert result.status.value == "cancelled", (
            f"cancellation surfaced as {result.status.value}, which reads as an "
            "ordinary failure rather than a cancellation")


# ══════════════════════════════════════════════════════ 3. mid-operation


class TestMidOperationCancellationIsObserved:
    def test_a_cancel_during_the_call_is_observed_by_the_runner(self, tmp_path):
        """The case that was broken: cancel lands after the child started."""
        runner = RecordingRunner(poll_cancel=True)
        _layer, _workflow, adapter = _build(runner, tmp_path)
        token = CancellationToken("task-x")

        # Cancel from another thread once the child is genuinely running.
        def _cancel_soon():
            deadline = time.time() + 5.0
            while runner.polls == 0 and time.time() < deadline:
                time.sleep(0.005)
            token.request("user cancelled the render", source="test")

        canceller = threading.Thread(target=_cancel_soon, daemon=True)
        canceller.start()
        result = adapter.execute(_request(token))
        canceller.join(timeout=5)

        assert result.status is CapabilityStatus.CANCELLED, (
            f"a cancel during the call surfaced as {result.status.value}")
        assert runner.polls > 1, (
            "the runner polled fewer than twice, so it cannot have observed a "
            "cancel arriving during the operation")

    def test_a_cancelled_render_is_reported_as_terminated(self, tmp_path):
        """``BlenderCancelledError.terminated`` is the ratified attribution.
        Reporting a cancel that stopped nothing would be a lie in the other
        direction."""
        runner = RecordingRunner(on_run=lambda _r, _t: (_ for _ in ()).throw(
            BlenderCancelledError(operation="render", message="stopped",
                                   diagnostics={"cancellation_terminated": True})))
        _layer, _workflow, adapter = _build(runner, tmp_path)

        result = adapter.execute(_request(CancellationToken("task-x")))

        assert result.status is CapabilityStatus.CANCELLED

    def test_an_uncancelled_operation_is_not_reported_as_cancelled(self, tmp_path):
        """The negative control.

        Without it, a change that made every Blender call look cancelled would
        satisfy every test above.
        """
        runner = RecordingRunner()
        _layer, _workflow, adapter = _build(runner, tmp_path)

        result = adapter.execute(_request(CancellationToken("task-x")))

        assert result.status is CapabilityStatus.SUCCESS, (
            f"a healthy Blender call was reported as {result.status.value}")
        assert runner.polls == 0


# ══════════════════════════════════════════════════════ 4. no leak


class TestTheTokenDoesNotLeak:
    def test_the_layer_scope_is_exited_afterwards(self, tmp_path):
        """A token left bound would cancel the next, unrelated request."""
        runner = RecordingRunner()
        layer, _workflow, adapter = _build(runner, tmp_path)

        adapter.execute(_request(CancellationToken("task-x")))

        assert layer._cancel_token() is None, (
            "the cancellation scope was not exited; the next call would be "
            "cancelled by a token from a finished request")

    def test_a_later_request_is_not_cancelled_by_an_earlier_token(self, tmp_path):
        runner = RecordingRunner()
        _layer, _workflow, adapter = _build(runner, tmp_path)

        first = CancellationToken("task-x")
        first.request("cancelled", source="test")
        adapter.execute(_request(first))

        second = CancellationToken("task-y")
        result = adapter.execute(_request(second))

        assert result.status is CapabilityStatus.SUCCESS, (
            "an earlier request's cancellation was still in force")

    def test_two_threads_do_not_share_a_token(self, tmp_path):
        """The binding is thread-local, so a concurrent caller keeps its own."""
        runner = RecordingRunner()
        layer, _workflow, adapter = _build(runner, tmp_path)
        observed: dict[int, object] = {}

        def worker(index: int, token):
            observed[index] = layer._cancel_token()
            adapter.execute(_request(token))

        tokens = {i: CancellationToken(f"task-{i}") for i in range(4)}
        threads = [threading.Thread(target=worker, args=(i, t))
                   for i, t in tokens.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert layer._cancel_token() is None


# ══════════════════════════════════════════════════════ 5. state coherence


class TestStateStaysCoherent:
    def test_a_cancelled_workflow_does_not_publish_success(self, tmp_path):
        runner = RecordingRunner(on_run=lambda _r, _t: (_ for _ in ()).throw(
            BlenderCancelledError(operation="inspect_scene", message="stopped",
                                   diagnostics={"cancellation_terminated": True})))
        _layer, workflow, adapter = _build(runner, tmp_path)

        adapter.execute(_request(CancellationToken("task-x")))

        result = workflow.last_result()
        assert result is None or not result.success, (
            "a cancelled Blender operation was recorded as a success")

    def test_the_workflow_remains_usable_after_a_cancellation(self, tmp_path):
        """A cancelled operation must not poison the layer for the next one."""
        runner = RecordingRunner()
        _layer, workflow, adapter = _build(runner, tmp_path)

        token = CancellationToken("task-x")
        token.request("stop", source="test")
        adapter.execute(_request(token))

        result = adapter.execute(_request(CancellationToken("task-y")))

        assert result.status is CapabilityStatus.SUCCESS, (
            "the workflow stayed broken after a cancellation")

    def test_correlation_is_preserved_through_a_cancelled_call(self, tmp_path):
        """Cancellation must not become a way to shed the trusted request id."""
        runner = RecordingRunner(on_run=lambda _r, _t: (_ for _ in ()).throw(
            BlenderCancelledError(operation="inspect_scene", message="stopped",
                                   diagnostics={"cancellation_terminated": True})))
        _layer, _workflow, adapter = _build(runner, tmp_path)

        request = _request(CancellationToken("task-x"), task_id="task-42")
        result = adapter.execute(request)

        assert result.request_id == request.request_id, (
            "a cancelled result came back under a different request id")
        assert result.task_id == "task-42"


# ══════════════════════════════════════════════════════ 6. real Blender


REAL_BLENDER = os.environ.get("DIABLO_REAL_BLENDER_TESTS") == "1"


@pytest.mark.skipif(not REAL_BLENDER,
                    reason="set DIABLO_REAL_BLENDER_TESTS=1 to run real Blender tests")
class TestRealBlenderCancellation:
    """Against a real Blender child.

    The doubles above prove the token is delivered; only this proves a real
    child process is actually stopped, which is the property a user experiences.
    """

    def test_a_real_render_can_be_cancelled(self, tmp_path):
        from blender.capabilities import BlenderCapabilityLayer as RealLayer
        from integration.workflows import BlenderVisionWorkflow as RealWorkflow

        layer = RealLayer(work_dir=str(tmp_path / "w"), timeout=120.0)
        if not layer.detect().installed:
            pytest.skip("Blender is not installed on this machine")

        workflow = RealWorkflow(blender=layer)
        adapter = BlenderVisionWorkflowCapabilityAdapter(workflow)
        token = CancellationToken("task-real")

        def _cancel_soon():
            # Cancel while the render is in flight, not before it starts.
            time.sleep(1.5)
            token.request("cancelled by test", source="test")

        canceller = threading.Thread(target=_cancel_soon, daemon=True)
        canceller.start()
        try:
            result = adapter.execute(CapabilityRequest(
                capability="blender_vision_workflow",
                operation="render_and_verify",
                # A deliberately expensive render: enough samples at full
                # resolution that one second in, Blender is unambiguously still
                # working. Without that the render would finish first and the
                # test would pass for the wrong reason.
                params={
                    "path": str(tmp_path / "render.png"),
                    "scene_plan": {
                        "name": "CancelProbe",
                        "objects": [{"name": "Cube", "type": "mesh"}],
                        "camera": {"location": [6.0, -6.0, 5.0],
                                   "rotation": [63.6, 0.0, 46.7]},
                        "lights": [{"type": "SUN", "energy": 5.0}],
                        "render": {"resolution_x": 1920, "resolution_y": 1080,
                                   "samples": 512},
                    },
                },
                task_id="task-real", cancellation_token=token))
        finally:
            canceller.join(timeout=10)
            layer.close()

        assert result.status is CapabilityStatus.CANCELLED, (
            f"a real Blender render was not cancelled (status "
            f"{result.status.value}); the runner did not observe the token")