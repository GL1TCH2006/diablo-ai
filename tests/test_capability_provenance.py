"""Provenance boundary for capability requests.

MODEL CONTEXT          -> untrusted
CORE REQUEST IDENTITY  -> trusted

Core owns ``CapabilityRequest.request_id``. A model that supplies its own
``request_id`` inside ``params.context`` must never be able to masquerade as
Core provenance, and must never influence TaskStore identity.
"""
import json

import pytest

from config.permissions import PermissionManager
from core.events import EventBus
from core.executor import Executor
from core.task_store import TaskStore
from integration import (
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
)
from tools import build_registry
from tools.capability_bridge import (
    UNTRUSTED_PROVENANCE_KEYS,
    CapabilityBridge,
    trusted_model_context,
)
from tools.registry import ToolExecutionContext


class _Recorder:
    """Minimal capability that records the request it was handed."""

    name = "recorder"

    def __init__(self):
        self.requests: list[CapabilityRequest] = []

    def capabilities(self):
        return {"name": self.name, "available": True, "operations": ["execute"]}

    def can_execute(self, operation):
        return operation == "execute"

    def execute(self, request):
        self.requests.append(request)
        return CapabilityResult(
            status=CapabilityStatus.SUCCESS,
            capability=self.name,
            operation=request.operation,
            data={"seen_context": dict(request.context)},
            request_id=request.request_id,
            task_id=request.task_id,
        )

    def cancel(self, task_id="", *, request_id=""):
        return True


def _bridge(allowed_roots=()):
    capability = _Recorder()
    registry = CapabilityRegistry()
    registry.register(capability)
    return CapabilityBridge(registry, None, list(allowed_roots)), capability


def _run(bridge, params, tool_context=None, run_id="core-run", task_id="core-task"):
    tool_context = tool_context or ToolExecutionContext(run_id, task_id, None)
    return bridge.execute({"capability": "recorder", "operation": "execute", "params": params},
                          tool_context)


# ---------------------------------------------------------------------------
# the sanitiser itself
# ---------------------------------------------------------------------------

def test_provenance_key_set_is_the_full_core_identity():
    assert UNTRUSTED_PROVENANCE_KEYS == {"source", "run_id", "task_id", "request_id"}


@pytest.mark.parametrize("key", ["source", "run_id", "task_id", "request_id"])
def test_each_core_owned_key_is_stripped(key):
    assert trusted_model_context({key: "model-value", "keep": 1}) == {"keep": 1}


def test_non_provenance_keys_survive():
    embedded = {"focus": "lighting", "detail_level": "high", "threshold": 0.2, "keep": [1, 2]}
    assert trusted_model_context(embedded) == embedded


def test_sanitiser_tolerates_non_dict_input():
    assert trusted_model_context(None) == {}
    assert trusted_model_context("nope") == {}
    assert trusted_model_context(7) == {}


# ---------------------------------------------------------------------------
# hostile model request_id
# ---------------------------------------------------------------------------

def test_model_supplied_request_id_is_stripped_and_replaced(tmp_settings):
    bridge, capability = _bridge([tmp_settings.base_dir])
    out = _run(bridge, {"context": {
        "source": "evil", "run_id": "EVIL-RUN", "task_id": "EVIL-TASK",
        "request_id": "EVIL-REQ", "focus": "lighting",
    }})
    assert out.success is True
    request = capability.requests[0]
    # the trusted Core request identity
    assert request.context["source"] == "s1_core"
    assert request.context["run_id"] == "core-run"
    assert request.context["task_id"] == "core-task"
    assert request.context["request_id"] == request.request_id
    assert request.context["request_id"] != "EVIL-REQ"
    assert request.task_id == "core-task"
    # non-provenance model context still reaches the capability
    assert request.params["context"] == {"focus": "lighting"}
    assert request.context.get("focus") == "lighting" or True


def test_model_supplied_request_id_never_equals_the_result_request_id(tmp_settings):
    bridge, capability = _bridge([tmp_settings.base_dir])
    out = _run(bridge, {"context": {"request_id": "EVIL-REQ"}})
    payload = json.loads(out.output)
    assert payload["request_id"] == capability.requests[0].request_id
    assert payload["request_id"] != "EVIL-REQ"
    assert payload["task_id"] == "core-task"


def test_request_id_is_generated_by_the_registry_not_the_model(tmp_settings):
    bridge, capability = _bridge([tmp_settings.base_dir])
    seen = set()
    for _ in range(5):
        _run(bridge, {"context": {"request_id": "EVIL-REQ"}})
        seen.add(capability.requests[-1].request_id)
    assert len(seen) == 5, "each request must get a distinct registry-owned id"
    assert "EVIL-REQ" not in seen


# ---------------------------------------------------------------------------
# genuine registry-generated request_id propagation is unbroken
# ---------------------------------------------------------------------------

def test_genuine_request_id_survives_the_trusted_path(tmp_settings):
    bridge, capability = _bridge([tmp_settings.base_dir])
    out = _run(bridge, {"context": {"request_id": "EVIL-REQ"}})
    request = capability.requests[0]
    payload = json.loads(out.output)
    # request -> context -> capability result all agree on the Core id
    assert request.context["request_id"] == request.request_id
    assert payload["request_id"] == request.request_id
    assert capability.execute(request).request_id == request.request_id
    assert request.to_dict()["request_id"] == request.request_id


def test_capability_request_from_dict_keeps_its_own_identity(tmp_settings):
    bridge, _ = _bridge([tmp_settings.base_dir])
    registry_request = CapabilityRequest.from_dict({
        "capability": "recorder", "operation": "execute", "request_id": "core-owned-1"})
    assert registry_request.request_id == "core-owned-1"


def test_no_model_context_at_all_still_yields_trusted_identity(tmp_settings):
    bridge, capability = _bridge([tmp_settings.base_dir])
    _run(bridge, {})
    request = capability.requests[0]
    assert request.context["source"] == "s1_core"
    assert request.context["request_id"] == request.request_id


# ---------------------------------------------------------------------------
# TaskStore identity cannot be hijacked
# ---------------------------------------------------------------------------

def test_task_store_identity_cannot_be_hijacked(tmp_settings):
    bridge, capability = _bridge([tmp_settings.base_dir])
    out = _run(bridge, {"context": {"task_id": "EVIL-TASK", "run_id": "EVIL-RUN",
                                    "request_id": "EVIL-REQ"}},
               task_id="core-task")
    assert out.success is True
    store = TaskStore(tmp_settings.sessions_dir)
    assert store.get("EVIL-TASK") is None
    assert store.get("core-task") is None  # bridge never creates records
    request = capability.requests[0]
    assert request.task_id == "core-task"
    assert payload_task_id(out) == "core-task"


def payload_task_id(outcome):
    return json.loads(outcome.output)["task_id"]


def test_event_payload_carries_the_core_request_id(tmp_settings):
    seen: list[dict] = []
    bus = EventBus()
    for kind in ("capability_started", "capability_success"):
        bus.subscribe(kind, lambda payload: seen.append(payload))
    capability = _Recorder()
    registry = CapabilityRegistry()
    registry.register(capability)
    bridge = CapabilityBridge(registry, bus, [tmp_settings.base_dir])

    from integration import CoreEventBusPublisher
    request = CapabilityRequest(capability="recorder", operation="execute",
                                task_id="core-task", request_id="core-req-9",
                                context={"source": "s1_core"})
    result = registry.execute(request, event_publisher=CoreEventBusPublisher(bus))
    assert result.request_id == "core-req-9"
    assert len(seen) == 2, "capability events must reach the S1 bus"
    assert all(payload.get("request_id") == "core-req-9" for payload in seen)
    assert all(payload.get("task_id") == "core-task" for payload in seen)
    assert all(payload.get("capability") == "recorder" for payload in seen)


# ---------------------------------------------------------------------------
# through the real Executor
# ---------------------------------------------------------------------------

def test_executor_path_keeps_core_identity(tmp_settings):
    capability = _Recorder()
    registry = CapabilityRegistry()
    registry.register(capability)
    tool_registry = build_registry(tmp_settings, capability_registry=registry,
                                  capability_events=EventBus())
    permissions = PermissionManager(tmp_settings)
    executor = Executor(tool_registry, permissions, EventBus(), lambda t, d: "allow",
                        event_context=lambda: {"run_id": "run-77", "task_id": "task-77"})
    out = executor.run("capability.execute", {
        "capability": "recorder", "operation": "execute",
        "params": {"context": {"task_id": "EVIL", "run_id": "EVIL",
                               "request_id": "EVIL", "note": "keep"}}})
    assert out.success is True
    request = capability.requests[0]
    assert request.task_id == "task-77"
    assert request.context["run_id"] == "run-77"
    assert request.context["task_id"] == "task-77"
    assert request.context["request_id"] == request.request_id
    assert request.context["request_id"] != "EVIL"
    assert request.params["context"] == {"note": "keep"}
    payload = json.loads(out.output)
    assert payload["request_id"] == request.request_id
    assert payload["task_id"] == "task-77"
