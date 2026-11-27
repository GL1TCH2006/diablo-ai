"""S2.7 Core-facing capability contract tests."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from blender import BlenderCapabilityLayer
from core.events import EventBus
from integration import (
    BlenderCapabilityAdapter,
    BlenderVisionCapabilityAdapter,
    Capability,
    CapabilityEvent,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    CoreEventBusPublisher,
    MockCapability,
    VisionCapabilityAdapter,
)
from ui.bridge import CoreEventBusBridge
from voice import CancellationToken, InterruptionManager


class FakeBlender:
    def detect(self):
        return SimpleNamespace(installed=True)

    def inspect_scene(self):
        return {"object_count": 1}

    def render(self, path, **kwargs):
        return {"path": str(path), "exists": True}

    def close(self):
        return None


class FakeVision:
    def detect_capabilities(self):
        return {"available": True, "supported_operations": ["inspect_render"]}

    def inspect_render(self, image_path, context=None):
        return {"valid": True, "checks": {"readable": True}}

    def describe(self, image_path, **kwargs):
        return {"description": "A rendered image"}


class FakeBlenderVision:
    def render_and_inspect(self, path, **kwargs):
        return {"success": True, "data": {"render_path": str(path), "valid": True}}

    def inspect_render(self, path, **kwargs):
        return {"success": True, "data": {"valid": True}}

    def close(self):
        return None


class MockVerifier:
    def verify(self, result: CapabilityResult) -> tuple[bool, str]:
        if result.success and result.data and result.data.get("evidence"):
            return True, "capability evidence verified"
        return False, "capability evidence missing"


class MockCoreTaskState:
    def __init__(self):
        self.status = "pending"
        self.outcome = None

    def complete(self, outcome):
        self.status = "completed"
        self.outcome = outcome

    def cancel(self, reason):
        self.status = "cancelled"
        self.outcome = reason


class MockCoreAgent:
    def __init__(self, registry: CapabilityRegistry, state: MockCoreTaskState):
        self.registry = registry
        self.state = state
        self.verifier = MockVerifier()

    def run(self, request: CapabilityRequest):
        result = self.registry.execute(request, authorize=lambda _: True)
        if result.success:
            verified, reason = self.verifier.verify(result)
            if verified:
                self.state.complete(reason)
        return result


class BlockingCapability(MockCapability):
    def __init__(self):
        super().__init__(operations=("execute",))
        self.started: dict[str, threading.Event] = {}
        self.releases: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()

    def _event_pair(self, task_id):
        with self._events_lock:
            return self.started.setdefault(task_id, threading.Event()), self.releases.setdefault(task_id, threading.Event())

    def execute(self, request):
        request.check_cancelled()
        started, release = self._event_pair(request.task_id)
        self.requests.append(request)
        started.set()
        release.wait(2.0)
        request.check_cancelled()
        return CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability=request.capability,
            operation=request.operation,
            data={"executed": True, "task_id": request.task_id},
            request_id=request.request_id,
            task_id=request.task_id,
        )


class RequestScopedBlockingCapability(MockCapability):
    def __init__(self, name="scoped"):
        super().__init__(operations=("execute",))
        self.name = name
        self._pairs_lock = threading.Lock()
        self._pairs: dict[str, tuple[threading.Event, threading.Event]] = {}

    def pair(self, request_id):
        with self._pairs_lock:
            pair = self._pairs.get(request_id)
            if pair is None:
                pair = (threading.Event(), threading.Event())
                self._pairs[request_id] = pair
            return pair

    def execute(self, request):
        started, release = self.pair(request.request_id)
        started.set()
        release.wait(3.0)
        request.check_cancelled()
        return CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability=request.capability,
            operation=request.operation,
            data={"request_id": request.request_id},
            request_id=request.request_id,
            task_id=request.task_id,
        )


def _run_blocking(registry, request, results):
    results.append(registry.execute(request))


def test_capability_request_and_result_serialization():
    request = CapabilityRequest.from_dict(
        {
            "capability": "mock",
            "operation": "execute",
            "params": {"value": 3},
            "task_id": "task-1",
            "request_id": "request-1",
        }
    )
    assert request.to_dict()["params"] == {"value": 3}
    result = CapabilityResult(
        status=CapabilityStatus.SUCCESS,
        capability="mock",
        operation="execute",
        data={"evidence": True},
        request_id=request.request_id,
        task_id=request.task_id,
    )
    payload = result.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert CapabilityResult.from_dict(payload).status is CapabilityStatus.SUCCESS


def test_capability_event_serialization():
    event = CapabilityEvent(
        type="capability_started",
        message="started",
        capability="mock",
        operation="execute",
    )
    payload = event.to_dict()
    assert CapabilityEvent.from_dict(payload).type == event.type
    assert json.dumps(payload)


def test_registry_selects_capability_and_returns_structured_result():
    capability = MockCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    request = CapabilityRequest(capability="mock", operation="execute", task_id="task-1")
    result = registry.execute(request)
    assert result.success is True
    assert result.data == {"executed": True}
    assert result.request_id == request.request_id
    assert capability.requests == [request]


def test_registry_reports_permission_required_without_execution():
    capability = MockCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    result = registry.execute(
        CapabilityRequest(capability="mock", operation="execute"),
        authorize=lambda _: False,
    )
    assert result.status is CapabilityStatus.PERMISSION_REQUIRED
    assert capability.requests == []


def test_registry_reports_unavailable_and_invalid_requests():
    registry = CapabilityRegistry()
    unavailable = registry.execute(CapabilityRequest(capability="missing", operation="execute"))
    assert unavailable.status is CapabilityStatus.UNAVAILABLE
    registry.register(MockCapability())
    invalid = registry.execute(CapabilityRequest(capability="mock", operation="missing"))
    assert invalid.status is CapabilityStatus.INVALID_REQUEST


def test_registry_maps_capability_exceptions_without_traceback_as_primary():
    class BrokenCapability(MockCapability):
        def execute(self, request):
            raise RuntimeError("capability exploded")

    registry = CapabilityRegistry()
    registry.register(BrokenCapability())
    result = registry.execute(CapabilityRequest(capability="mock", operation="execute"))
    assert result.status is CapabilityStatus.FAILURE
    assert result.error["message"] == "capability exploded"
    assert "Traceback" not in json.dumps(result.to_dict())


def test_registry_maps_permission_exception():
    class PermissionCapability(MockCapability):
        def execute(self, request):
            raise PermissionError("confirmation required")

    registry = CapabilityRegistry()
    registry.register(PermissionCapability())
    result = registry.execute(CapabilityRequest(capability="mock", operation="execute"))
    assert result.status is CapabilityStatus.PERMISSION_REQUIRED


def test_core_mock_agent_runs_capability_then_verifies_and_completes():
    capability = MockCapability(
        result=CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability="mock",
            operation="execute",
            data={"evidence": {"object": "cube"}},
        )
    )
    registry = CapabilityRegistry()
    registry.register(capability)
    state = MockCoreTaskState()
    core = MockCoreAgent(registry, state)
    result = core.run(CapabilityRequest(capability="mock", operation="execute", task_id="task-1"))
    assert result.success is True
    assert state.status == "completed"
    assert "verified" in state.outcome


def test_core_mock_task_cancellation_uses_existing_token_contract():
    manager = InterruptionManager()
    token = manager.token_for("task-cancel")
    manager.request_cancel("task-cancel")
    capability = MockCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    state = MockCoreTaskState()
    core = MockCoreAgent(registry, state)
    result = core.run(
        CapabilityRequest(
            capability="mock",
            operation="execute",
            task_id="task-cancel",
            cancellation_token=token,
        )
    )
    assert result.status is CapabilityStatus.CANCELLED
    assert capability.requests == []
    state.cancel("user_interruption")
    assert state.status == "cancelled"


def test_core_event_bus_publisher_reuses_existing_event_bus():
    bus = EventBus()
    received = []
    bridge = CoreEventBusBridge(bus)
    bridge.subscribe_to_events(received.append)
    publisher = CoreEventBusPublisher(bus)
    publisher.publish(
        CapabilityEvent(
            type="task_done",
            message="Task completed",
            capability="mock",
            operation="execute",
            status="success",
        )
    )
    assert received[0].type == "success"
    assert received[0].message == "Task completed"


def test_blender_adapter_maps_existing_operations():
    adapter = BlenderCapabilityAdapter(FakeBlender())
    assert adapter.can_execute("render") is True
    result = adapter.execute(CapabilityRequest(capability="blender", operation="inspect_scene"))
    assert result.success is True
    assert result.data == {"object_count": 1}


def test_vision_adapter_maps_existing_operations():
    adapter = VisionCapabilityAdapter(FakeVision())
    result = adapter.execute(
        CapabilityRequest(capability="vision", operation="inspect_render", params={"image_path": "render.png"})
    )
    assert result.success is True
    assert result.data["valid"] is True


def test_blender_vision_adapter_maps_existing_result():
    adapter = BlenderVisionCapabilityAdapter(FakeBlenderVision())
    result = adapter.execute(
        CapabilityRequest(capability="blender_vision", operation="render_and_inspect", params={"path": "render.png"})
    )
    assert result.success is True
    assert result.data["valid"] is True


def test_adapter_discovery_operations_are_structured():
    registry = CapabilityRegistry()
    registry.register(BlenderCapabilityAdapter(FakeBlender()))
    registry.register(VisionCapabilityAdapter(FakeVision()))
    reports = registry.capabilities()
    assert {report["name"] for report in reports} == {"blender", "vision"}
    assert all("operations" in report for report in reports)


def test_generic_cancellation_token_is_not_voice_specific():
    token = CancellationToken("generic-task")
    request = CapabilityRequest(capability="mock", operation="execute", cancellation_token=token)
    token.request("stop", source="external")
    registry = CapabilityRegistry()
    registry.register(MockCapability())
    result = registry.execute(request)
    assert result.status is CapabilityStatus.CANCELLED


def test_direct_capability_adapter_pre_cancelled_request_is_structured():
    adapter = BlenderCapabilityAdapter(FakeBlender())
    token = CancellationToken("direct-pre-cancel")
    token.request("stop", source="test")
    result = adapter.execute(CapabilityRequest(capability="blender", operation="inspect_scene", task_id="direct-pre-cancel", cancellation_token=token))
    assert result.status is CapabilityStatus.CANCELLED
    assert result.error["error"] == "cancelled"


def test_registry_pre_cancelled_request_is_not_dispatched():
    capability = BlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token = CancellationToken("pre-cancel")
    token.request("stop", source="test")
    result = registry.execute(CapabilityRequest(capability="mock", operation="execute", task_id="pre-cancel", cancellation_token=token))
    assert result.status is CapabilityStatus.CANCELLED
    assert capability.requests == []
    assert registry._active_requests == {}


def test_registry_cancels_active_explicit_token():
    capability = BlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token = CancellationToken("active")
    results = []
    thread = threading.Thread(target=_run_blocking, args=(registry, CapabilityRequest(capability="mock", operation="execute", task_id="active", cancellation_token=token), results))
    thread.start()
    assert capability.started["active"].wait(1.0)
    cancellation = registry.cancel("mock", "active")
    assert cancellation.status is CapabilityStatus.CANCELLED
    assert token.is_cancelled() is True
    capability.releases["active"].set()
    thread.join(1.0)
    assert results[0].status is CapabilityStatus.CANCELLED
    assert registry._active_requests == {}


def test_registry_observes_cancellation_at_cooperative_boundary():
    class BoundaryCapability(MockCapability):
        def execute(self, request):
            request.check_cancelled()
            request.cancellation_token.request("boundary", source="test")
            request.check_cancelled()
            return CapabilityResult(status=CapabilityStatus.SUCCESS, capability=request.capability, operation=request.operation)

    registry = CapabilityRegistry()
    registry.register(BoundaryCapability())
    token = CancellationToken("boundary")
    result = registry.execute(CapabilityRequest(capability="mock", operation="execute", task_id="boundary", cancellation_token=token))
    assert result.status is CapabilityStatus.CANCELLED
    assert token.is_cancelled() is True


def test_registry_repeated_active_cancellation_is_safe():
    capability = BlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token = CancellationToken("repeat")
    results = []
    thread = threading.Thread(target=_run_blocking, args=(registry, CapabilityRequest(capability="mock", operation="execute", task_id="repeat", cancellation_token=token), results))
    thread.start()
    assert capability.started["repeat"].wait(1.0)
    assert registry.cancel("mock", "repeat").status is CapabilityStatus.CANCELLED
    assert registry.cancel("mock", "repeat").status is CapabilityStatus.CANCELLED
    capability.releases["repeat"].set()
    thread.join(1.0)
    assert results[0].status is CapabilityStatus.CANCELLED


def test_registry_cancellation_after_completion_does_not_undo_success():
    capability = MockCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token = CancellationToken("completed")
    result = registry.execute(CapabilityRequest(capability="mock", operation="execute", task_id="completed", cancellation_token=token))
    cancellation = registry.cancel("mock", "completed")
    assert result.status is CapabilityStatus.SUCCESS
    assert result.data == {"executed": True}
    assert cancellation.status is CapabilityStatus.FAILURE
    assert token.is_cancelled() is False


def test_registry_cleans_active_requests_and_isolates_task_ids():
    capability = BlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token_a = CancellationToken("task-a")
    token_b = CancellationToken("task-b")
    results = []
    request_a = CapabilityRequest(capability="mock", operation="execute", task_id="task-a", cancellation_token=token_a)
    request_b = CapabilityRequest(capability="mock", operation="execute", task_id="task-b", cancellation_token=token_b)
    thread_a = threading.Thread(target=_run_blocking, args=(registry, request_a, results))
    thread_b = threading.Thread(target=_run_blocking, args=(registry, request_b, results))
    thread_a.start()
    thread_b.start()
    assert capability.started["task-a"].wait(1.0)
    assert capability.started["task-b"].wait(1.0)
    assert registry.cancel("mock", "task-a").status is CapabilityStatus.CANCELLED
    capability.releases["task-a"].set()
    capability.releases["task-b"].set()
    thread_a.join(1.0)
    thread_b.join(1.0)
    by_task = {result.task_id: result.status for result in results}
    assert by_task == {"task-a": CapabilityStatus.CANCELLED, "task-b": CapabilityStatus.SUCCESS}
    assert token_b.is_cancelled() is False
    assert registry._active_requests == {}


def _same_task_scenario(cancel_label=None, capability_name="scoped"):
    capability = RequestScopedBlockingCapability(capability_name)
    registry = CapabilityRegistry()
    registry.register(capability)
    requests = {}
    tokens = {}
    for label in ("a", "b"):
        token = CancellationToken("shared-task")
        request = CapabilityRequest(
            capability=capability.name,
            operation="execute",
            task_id="shared-task",
            cancellation_token=token,
        )
        requests[label] = request
        tokens[label] = token
    results = {}

    def run(request):
        results[request.request_id] = registry.execute(request)

    threads = []
    for request in requests.values():
        thread = threading.Thread(target=run, args=(request,))
        thread.start()
        threads.append(thread)
    for request in requests.values():
        assert capability.pair(request.request_id)[0].wait(1.0)
    active_during = len(registry._active_requests)
    cancel_result = None
    if cancel_label is not None:
        target = requests[cancel_label]
        cancel_result = registry.cancel_request(target.request_id)
    for request in requests.values():
        capability.pair(request.request_id)[1].set()
    for thread in threads:
        thread.join(2.0)
    return {
        "capability": capability,
        "registry": registry,
        "requests": requests,
        "tokens": tokens,
        "results": results,
        "cancel_result": cancel_result,
        "active_during": active_during,
    }


def test_same_capability_same_task_cancel_by_request_id_isolates_peer():
    outcome = _same_task_scenario(cancel_label="a")
    requests = outcome["requests"]
    tokens = outcome["tokens"]
    results = outcome["results"]
    assert outcome["active_during"] == 2
    assert outcome["cancel_result"].status is CapabilityStatus.CANCELLED
    assert results[requests["a"].request_id].status is CapabilityStatus.CANCELLED
    assert results[requests["b"].request_id].status is CapabilityStatus.SUCCESS
    assert tokens["a"].is_cancelled() is True
    assert tokens["b"].is_cancelled() is False
    assert outcome["registry"]._active_requests == {}


def test_same_capability_same_task_reverse_cancellation_isolates_peer():
    outcome = _same_task_scenario(cancel_label="b")
    requests = outcome["requests"]
    tokens = outcome["tokens"]
    results = outcome["results"]
    assert outcome["active_during"] == 2
    assert outcome["cancel_result"].status is CapabilityStatus.CANCELLED
    assert results[requests["a"].request_id].status is CapabilityStatus.SUCCESS
    assert results[requests["b"].request_id].status is CapabilityStatus.CANCELLED
    assert tokens["a"].is_cancelled() is False
    assert tokens["b"].is_cancelled() is True
    assert outcome["registry"]._active_requests == {}


def test_request_scoped_cancel_requires_request_id_when_ambiguous():
    capability = RequestScopedBlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    requests = [
        CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=CancellationToken("shared"))
        for _ in range(2)
    ]
    threads = [threading.Thread(target=registry.execute, args=(request,)) for request in requests]
    for thread in threads:
        thread.start()
    for request in requests:
        assert capability.pair(request.request_id)[0].wait(1.0)
    assert len(registry._active_requests) == 2
    ambiguous = registry.cancel("scoped", "shared")
    assert ambiguous.status is CapabilityStatus.FAILURE
    assert ambiguous.error["error"] == "ambiguous_request"
    for request in requests:
        capability.pair(request.request_id)[1].set()
    for thread in threads:
        thread.join(2.0)
    assert registry._active_requests == {}


def test_different_task_ids_remain_isolated_under_request_scoped_cancel():
    capability = RequestScopedBlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token_a = CancellationToken("task-a")
    token_b = CancellationToken("task-b")
    request_a = CapabilityRequest(capability="scoped", operation="execute", task_id="task-a", cancellation_token=token_a)
    request_b = CapabilityRequest(capability="scoped", operation="execute", task_id="task-b", cancellation_token=token_b)
    results = {}
    threads = [
        threading.Thread(target=lambda r=request_a: results.__setitem__(r.request_id, registry.execute(r))),
        threading.Thread(target=lambda r=request_b: results.__setitem__(r.request_id, registry.execute(r))),
    ]
    for thread in threads:
        thread.start()
    assert capability.pair(request_a.request_id)[0].wait(1.0)
    assert capability.pair(request_b.request_id)[0].wait(1.0)
    assert len(registry._active_requests) == 2
    assert registry.cancel("scoped", "task-a", request_id=request_a.request_id).status is CapabilityStatus.CANCELLED
    capability.pair(request_a.request_id)[1].set()
    capability.pair(request_b.request_id)[1].set()
    for thread in threads:
        thread.join(2.0)
    assert results[request_a.request_id].status is CapabilityStatus.CANCELLED
    assert results[request_b.request_id].status is CapabilityStatus.SUCCESS
    assert token_b.is_cancelled() is False
    assert registry._active_requests == {}


def test_different_capabilities_never_interfere():
    capability_a = RequestScopedBlockingCapability("alpha")
    capability_b = RequestScopedBlockingCapability("beta")
    registry = CapabilityRegistry()
    registry.register(capability_a)
    registry.register(capability_b)
    token_a = CancellationToken("shared")
    token_b = CancellationToken("shared")
    request_a = CapabilityRequest(capability="alpha", operation="execute", task_id="shared", cancellation_token=token_a)
    request_b = CapabilityRequest(capability="beta", operation="execute", task_id="shared", cancellation_token=token_b)
    results = {}
    threads = [
        threading.Thread(target=lambda r=request_a: results.__setitem__(r.request_id, registry.execute(r))),
        threading.Thread(target=lambda r=request_b: results.__setitem__(r.request_id, registry.execute(r))),
    ]
    for thread in threads:
        thread.start()
    assert capability_a.pair(request_a.request_id)[0].wait(1.0)
    assert capability_b.pair(request_b.request_id)[0].wait(1.0)
    assert len(registry._active_requests) == 2
    assert registry.cancel("alpha", "shared", request_id=request_a.request_id).status is CapabilityStatus.CANCELLED
    capability_a.pair(request_a.request_id)[1].set()
    capability_b.pair(request_b.request_id)[1].set()
    for thread in threads:
        thread.join(2.0)
    assert results[request_a.request_id].status is CapabilityStatus.CANCELLED
    assert results[request_b.request_id].status is CapabilityStatus.SUCCESS
    assert token_b.is_cancelled() is False
    assert registry._active_requests == {}


def test_multiple_same_task_requests_cancel_exactly_one():
    capability = RequestScopedBlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    requests = [
        CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=CancellationToken("shared"))
        for _ in range(3)
    ]
    results = {}
    threads = [threading.Thread(target=lambda r=request: results.__setitem__(r.request_id, registry.execute(r))) for request in requests]
    for thread in threads:
        thread.start()
    for request in requests:
        assert capability.pair(request.request_id)[0].wait(1.0)
    assert len(registry._active_requests) == 3
    assert registry.cancel_request(requests[1].request_id).status is CapabilityStatus.CANCELLED
    for request in requests:
        capability.pair(request.request_id)[1].set()
    for thread in threads:
        thread.join(2.0)
    statuses = sorted(results[request.request_id].status.value for request in requests)
    assert statuses == ["cancelled", "success", "success"]
    assert results[requests[1].request_id].status is CapabilityStatus.CANCELLED
    assert registry._active_requests == {}


def test_repeated_request_scoped_cancellation_is_deterministic():
    capability = RequestScopedBlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    token_a = CancellationToken("shared")
    token_b = CancellationToken("shared")
    request_a = CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=token_a)
    request_b = CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=token_b)
    results = {}
    threads = [
        threading.Thread(target=lambda r=request_a: results.__setitem__(r.request_id, registry.execute(r))),
        threading.Thread(target=lambda r=request_b: results.__setitem__(r.request_id, registry.execute(r))),
    ]
    for thread in threads:
        thread.start()
    assert capability.pair(request_a.request_id)[0].wait(1.0)
    assert capability.pair(request_b.request_id)[0].wait(1.0)
    first = registry.cancel_request(request_a.request_id)
    second = registry.cancel_request(request_a.request_id)
    assert first.status is CapabilityStatus.CANCELLED
    assert second.status is CapabilityStatus.CANCELLED
    assert first.error is None
    assert token_b.is_cancelled() is False
    capability.pair(request_a.request_id)[1].set()
    capability.pair(request_b.request_id)[1].set()
    for thread in threads:
        thread.join(2.0)
    assert results[request_a.request_id].status is CapabilityStatus.CANCELLED
    assert results[request_b.request_id].status is CapabilityStatus.SUCCESS
    assert registry._active_requests == {}


def test_cancellation_after_completion_does_not_touch_new_same_task_request():
    capability = RequestScopedBlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    completed = CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=CancellationToken("shared"))
    completed_result = registry.execute(completed)
    assert completed_result.status is CapabilityStatus.SUCCESS
    fresh_token = CancellationToken("shared")
    fresh = CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=fresh_token)
    results = {}
    thread = threading.Thread(target=lambda: results.__setitem__(fresh.request_id, registry.execute(fresh)))
    thread.start()
    assert capability.pair(fresh.request_id)[0].wait(1.0)
    late = registry.cancel("scoped", "shared", request_id=completed.request_id)
    assert late.status is CapabilityStatus.FAILURE
    capability.pair(fresh.request_id)[1].set()
    thread.join(2.0)
    assert results[fresh.request_id].status is CapabilityStatus.SUCCESS
    assert fresh_token.is_cancelled() is False
    assert registry._active_requests == {}


def test_pre_cancelled_request_does_not_affect_later_same_task_request():
    capability = RequestScopedBlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    pre_token = CancellationToken("shared")
    pre_token.request("stop", source="test")
    pre = CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=pre_token)
    pre_result = registry.execute(pre)
    assert pre_result.status is CapabilityStatus.CANCELLED
    later_token = CancellationToken("shared")
    later = CapabilityRequest(capability="scoped", operation="execute", task_id="shared", cancellation_token=later_token)
    results = {}
    thread = threading.Thread(target=lambda: results.__setitem__(later.request_id, registry.execute(later)))
    thread.start()
    assert capability.pair(later.request_id)[0].wait(1.0)
    capability.pair(later.request_id)[1].set()
    thread.join(2.0)
    assert results[later.request_id].status is CapabilityStatus.SUCCESS
    assert later_token.is_cancelled() is False
    assert registry._active_requests == {}


def test_active_entries_return_to_zero_after_same_task_completion():
    outcome = _same_task_scenario(cancel_label=None)
    assert outcome["active_during"] == 2
    assert outcome["registry"]._active_requests == {}
    statuses = sorted(result.status.value for result in outcome["results"].values())
    assert statuses == ["success", "success"]


def test_contract_module_has_no_s1_core_dependency():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "integration" / "contracts.py").read_text(encoding="utf-8")
    assert "core.agent" not in source
    assert "config.permissions" not in source
    assert "from core" not in source


# ------------------------------------------- status alias parity (S2.7)


def test_capability_status_accepts_completed_aliases():
    """completed/complete/done deserialize to SUCCESS, matching WorkflowStatus."""
    for raw in ("completed", "complete", "done", "COMPLETED", "Complete", " done "):
        assert CapabilityStatus.from_value(raw) is CapabilityStatus.SUCCESS


def test_capability_status_alias_parity_with_workflow_status():
    """Both halves of the contract accept the same input vocabulary."""
    from integration import WorkflowStatus

    pairs = {
        "completed": CapabilityStatus.SUCCESS,
        "complete": CapabilityStatus.SUCCESS,
        "done": CapabilityStatus.SUCCESS,
        "failed": CapabilityStatus.FAILURE,
        "error": CapabilityStatus.FAILURE,
        "canceled": CapabilityStatus.CANCELLED,
        "permission": CapabilityStatus.PERMISSION_REQUIRED,
    }
    for raw, expected in pairs.items():
        assert CapabilityStatus.from_value(raw) is expected
        assert WorkflowStatus.from_value(raw).value == expected.value


def test_capability_status_existing_values_preserved():
    """Every canonical value and pre-existing alias still resolves identically."""
    for member in CapabilityStatus:
        assert CapabilityStatus.from_value(member.value) is member
    for raw, expected in (
        ("success", CapabilityStatus.SUCCESS),
        ("failure", CapabilityStatus.FAILURE),
        ("cancelled", CapabilityStatus.CANCELLED),
        ("permission_required", CapabilityStatus.PERMISSION_REQUIRED),
        ("unavailable", CapabilityStatus.UNAVAILABLE),
        ("invalid_request", CapabilityStatus.INVALID_REQUEST),
        ("failed", CapabilityStatus.FAILURE),
        ("error", CapabilityStatus.FAILURE),
        ("canceled", CapabilityStatus.CANCELLED),
        ("permission", CapabilityStatus.PERMISSION_REQUIRED),
    ):
        assert CapabilityStatus.from_value(raw) is expected


def test_capability_status_normalization_preserved():
    """Case, dashes, spaces and hyphens still normalize."""
    assert CapabilityStatus.from_value("PERMISSION-REQUIRED") is CapabilityStatus.PERMISSION_REQUIRED
    assert CapabilityStatus.from_value("invalid request") is CapabilityStatus.INVALID_REQUEST
    assert CapabilityStatus.from_value("  Success  ") is CapabilityStatus.SUCCESS
    assert CapabilityStatus.from_value(CapabilityStatus.SUCCESS) is CapabilityStatus.SUCCESS


@pytest.mark.parametrize("raw", ["totally_unknown", "nonsense", "successful", "succeeded", "cancelling", "true"])
def test_capability_status_unknown_still_rejected(raw):
    """Widening the aliases must not make unknown statuses resolvable."""
    with pytest.raises(ValueError):
        CapabilityStatus.from_value(raw)


def test_capability_status_result_round_trip():
    """to_dict -> from_dict is stable for every status."""
    for member in CapabilityStatus:
        result = CapabilityResult(status=member, capability="c", operation="o")
        payload = result.to_dict()
        assert payload["status"] == member.value
        assert CapabilityResult.from_dict(payload).status is member


def test_capability_status_completed_alias_deserializes_result():
    """A persisted 'completed' status loads as SUCCESS rather than raising."""
    payload = {
        "status": "completed",
        "capability": "blender",
        "operation": "render",
        "data": {"ok": True},
        "request_id": "r1",
        "task_id": "t1",
    }
    result = CapabilityResult.from_dict(payload)
    assert result.status is CapabilityStatus.SUCCESS
    assert result.capability == "blender"
    assert result.operation == "render"
    assert result.data == {"ok": True}
    assert result.request_id == "r1"
    assert result.task_id == "t1"
    # serialization is unchanged: it still writes the canonical value
    assert result.to_dict()["status"] == "success"


def test_capability_status_serialization_unchanged():
    """No new members or wire values were introduced."""
    assert [member.value for member in CapabilityStatus] == [
        "success",
        "failure",
        "cancelled",
        "permission_required",
        "unavailable",
        "invalid_request",
    ]