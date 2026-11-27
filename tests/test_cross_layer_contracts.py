"""Ratified cross-layer execution contracts (M2.12).

Locks the three contracts ratified in
``docs/contracts/cross_layer_execution_contracts.md`` so S2.9 cannot drift
from them without a test failure:

1. Request ID ownership - S1 owns ``request_id``; S2 cannot overwrite it.
2. Long-running cancellation - ``CANCELLED`` is distinct, and
   cancellation-pending travels in ``error``/``metadata`` rather than an
   invented status.
3. Error vocabulary - ``INVALID_REQUEST`` is reserved for caller-attributable
   errors and is never inferred from message wording.

These tests assert ratified S1 behaviour. They do not implement S2
subprocess supervision, and they do not change S2 code.
"""
import dataclasses

import pytest

from blender.capabilities import BlenderCapabilityLayer
from core.request_context import DEFAULT_MAX_CONCURRENT_REQUESTS, RequestContext
from integration import (
    BlenderCapabilityAdapter,
    CapabilityCancellationError,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
)
from integration.errors import BlenderVisionError

S1_REQUEST_ID = "s1-authoritative-request-id"
S1_TASK_ID = "s1-task-id"

# The nine request-scoped fields ratified by M2.12 P1. RequestContext must not
# grow a tenth field without a contract change, and in particular must never
# grow a request identity of its own.
RATIFIED_CONTEXT_FIELDS = frozenset(
    {
        "tasks",
        "cancellation_token",
        "router_verify_ok",
        "escalations_used",
        "offline_fallback_used",
        "pending_goal",
        "recovered_task_ids",
        "terminal_event_emitted",
        "last_model",
    }
)

# The status vocabulary is closed. S2.9 must not widen it.
RATIFIED_STATUSES = frozenset(
    {
        "success",
        "failure",
        "cancelled",
        "permission_required",
        "unavailable",
        "invalid_request",
    }
)


@pytest.fixture
def blender_adapter(tmp_path):
    """A real S2 Blender layer behind the real S1 adapter, Blender not launched."""
    layer = BlenderCapabilityLayer(work_dir=tmp_path / "work")
    return BlenderCapabilityAdapter(layer)


@pytest.fixture
def registry(blender_adapter):
    reg = CapabilityRegistry()
    reg.register(blender_adapter)
    return reg


def _request(**kwargs):
    payload = {
        "capability": "blender",
        "operation": "inspect_scene",
        "task_id": S1_TASK_ID,
        "request_id": S1_REQUEST_ID,
    }
    payload.update(kwargs)
    return CapabilityRequest(**payload)


def _respond(adapter, payload):
    """Make the S2 layer operation return ``payload`` verbatim."""
    setattr(adapter.layer, "inspect_scene", lambda **kwargs: payload)
    return payload


# ----------------------------------------------------------------------
# Contract 1 - request id ownership
# ----------------------------------------------------------------------


def test_capability_result_carries_the_s1_request_id(registry):
    """The core invariant: result id == trusted S1 id."""
    result = registry.execute(_request())
    assert result.request_id == S1_REQUEST_ID


def test_s2_layer_cannot_overwrite_the_trusted_request_id(registry, blender_adapter):
    """A layer that returns its own request_id is ignored by S1."""
    _respond(blender_adapter, {"status": "success", "success": True, "data": {"ok": True}, "request_id": "s2-minted-id"})
    result = registry.execute(_request())
    assert result.request_id == S1_REQUEST_ID
    assert result.request_id != "s2-minted-id"


def test_s2_layer_cannot_overwrite_the_trusted_task_id(registry, blender_adapter):
    _respond(blender_adapter, {"status": "success", "success": True, "data": {"ok": True}, "task_id": "s2-task-id"})
    result = registry.execute(_request())
    assert result.task_id == S1_TASK_ID
    assert result.task_id != "s2-task-id"


def test_trusted_identity_survives_every_status(registry, blender_adapter):
    """Identity is invariant across success, failure and cancellation."""
    cases = [
        ({"status": "success", "success": True, "data": {}}, CapabilityStatus.SUCCESS),
        ({"status": "failure", "success": False, "error": {"error": "boom", "message": "boom"}}, CapabilityStatus.FAILURE),
        ({"status": "cancelled", "success": False}, CapabilityStatus.CANCELLED),
        ({"status": "unavailable", "success": False}, CapabilityStatus.UNAVAILABLE),
    ]
    for payload, expected in cases:
        _respond(blender_adapter, payload)
        result = registry.execute(_request())
        assert result.status is expected, payload
        assert result.request_id == S1_REQUEST_ID, payload
        assert result.task_id == S1_TASK_ID, payload


def test_request_context_declares_no_request_id():
    """Contract 1: RequestContext must not introduce a second request identity."""
    assert not hasattr(RequestContext(), "request_id")
    assert "request_id" not in {f.name for f in dataclasses.fields(RequestContext)}


def test_request_context_holds_exactly_the_nine_ratified_fields():
    assert {f.name for f in dataclasses.fields(RequestContext)} == RATIFIED_CONTEXT_FIELDS


def test_request_context_identity_proxies_taskmanager():
    """run_id/task_id stay the authoritative TaskManager identities."""
    context = RequestContext()
    assert context.run_id == context.tasks.run_id
    assert context.task_id == context.tasks.task_id


def test_request_context_identity_is_read_only():
    context = RequestContext()
    for name in ("run_id", "task_id"):
        with pytest.raises(AttributeError):
            setattr(context, name, "spoofed")
    assert context.run_id == context.tasks.run_id
    assert context.task_id == context.tasks.task_id


def test_default_max_concurrent_requests_stays_one():
    assert DEFAULT_MAX_CONCURRENT_REQUESTS == 1


# ----------------------------------------------------------------------
# Contract 2 - long-running cancellation
# ----------------------------------------------------------------------


def test_cancelled_is_distinct_from_success_and_failure():
    assert CapabilityStatus.CANCELLED.value == "cancelled"
    assert CapabilityStatus.CANCELLED is not CapabilityStatus.SUCCESS
    assert CapabilityStatus.CANCELLED is not CapabilityStatus.FAILURE


def test_status_vocabulary_is_closed():
    assert {s.value for s in CapabilityStatus} == RATIFIED_STATUSES
    with pytest.raises(ValueError):
        CapabilityStatus.from_value("cancel_pending")


class _Cancelled:
    def is_cancelled(self):
        return True


def test_cancellation_before_execution_is_cancelled_not_failure(registry):
    result = registry.execute(_request(cancellation_token=_Cancelled()))
    assert result.status is CapabilityStatus.CANCELLED
    assert result.status is not CapabilityStatus.FAILURE


def test_cancellation_never_reports_success(registry):
    result = registry.execute(_request(cancellation_token=_Cancelled()))
    assert result.success is False
    assert result.cancelled is True


def test_cancellation_observed_during_execution_is_cancelled(registry, blender_adapter):
    def _cancel(**kwargs):
        raise CapabilityCancellationError(_request())

    setattr(blender_adapter.layer, "inspect_scene", _cancel)
    result = registry.execute(_request())
    assert result.status is CapabilityStatus.CANCELLED
    assert result.request_id == S1_REQUEST_ID


def test_cancellation_pending_channel_survives_the_cross_layer_path(registry, blender_adapter):
    """A non-interruptible op is reported as cancelled + structured metadata.

    This is the ratified channel. ``metadata.cancellation_terminated`` is how
    S2 says "I did not actually kill the child", so S1 must never see a bare
    ``cancelled`` it can read as a confirmed termination.
    """
    _respond(
        blender_adapter,
        {
            "status": "cancelled",
            "success": False,
            "error": {"error": "cancellation_pending", "message": "operation is not interruptible"},
            "metadata": {"cancellation_terminated": False, "interruptible": False},
        },
    )
    result = registry.execute(_request())
    assert result.status is CapabilityStatus.CANCELLED
    assert result.error["error"] == "cancellation_pending"
    assert result.metadata["cancellation_terminated"] is False
    assert result.metadata["interruptible"] is False


def test_confirmed_termination_is_distinguishable_from_pending(registry, blender_adapter):
    """Terminated and not-terminated must be distinguishable, never conflated."""
    _respond(
        blender_adapter,
        {
            "status": "cancelled",
            "success": False,
            "error": {"error": "cancelled", "message": "child terminated"},
            "metadata": {"cancellation_terminated": True, "interruptible": True},
        },
    )
    result = registry.execute(_request())
    assert result.status is CapabilityStatus.CANCELLED
    assert result.metadata["cancellation_terminated"] is True


def test_invented_cancel_pending_status_degrades_to_false_failure(registry, blender_adapter):
    """Ratified prohibition, pinned so S2.9 does not walk into it.

    The vocabulary is closed, so an invented status string resolves to
    FAILURE. A caller-requested cancellation is therefore misreported as a
    generic operational failure, which Contract 2 point 3 forbids, and S1 can
    no longer tell it apart from a real crash. The metadata channel does
    survive, which is precisely why cancellation-pending must be expressed as
    ``cancelled`` + metadata rather than as a new status.
    """
    _respond(
        blender_adapter,
        {
            "status": "cancel_pending",
            "success": False,
            "error": {"error": "cancellation_pending", "message": "not interruptible"},
            "metadata": {"cancellation_terminated": False},
        },
    )
    result = registry.execute(_request())
    assert result.status is CapabilityStatus.FAILURE
    assert result.status is not CapabilityStatus.CANCELLED
    assert result.metadata["cancellation_terminated"] is False
    assert result.request_id == S1_REQUEST_ID


def test_cancelled_round_trips_through_the_wire_dict():
    result = CapabilityResult.failure(
        _request(),
        CapabilityStatus.CANCELLED,
        "child terminated",
        code="cancelled",
        metadata={"cancellation_terminated": True},
    )
    restored = CapabilityResult.from_dict(result.to_dict())
    assert restored.status is CapabilityStatus.CANCELLED
    assert restored.cancelled is True
    assert restored.metadata["cancellation_terminated"] is True
    assert restored.request_id == S1_REQUEST_ID


# ----------------------------------------------------------------------
# Contract 3 - error vocabulary
# ----------------------------------------------------------------------


INFRASTRUCTURE_FAILURES = [
    ("blender_executable_missing", FileNotFoundError("blender executable not found")),
    ("subprocess_launch_failure", OSError("failed to launch subprocess")),
    ("infrastructure_permission_error", PermissionError("denied opening pipe")),
    ("backend_crash", RuntimeError("backend crashed")),
    ("timeout", TimeoutError("timed out after 30s")),
    ("session_corruption", BlenderVisionError("session corrupted", "unavailable")),
    ("temp_dir_cleanup_failure", OSError("failed to remove temporary directory")),
    ("internal_exception", ValueError("invalid internal state")),
    ("unknown_dispatch", Exception("unknown operation dispatch")),
    ("message_contains_invalid", ValueError("invalid argument for internal buffer")),
    ("message_contains_invalid_request", Exception("invalid request internally")),
]


@pytest.mark.parametrize(
    "label,exc",
    INFRASTRUCTURE_FAILURES,
    ids=[label for label, _ in INFRASTRUCTURE_FAILURES],
)
def test_infrastructure_failures_are_never_invalid_request(registry, blender_adapter, label, exc):
    def _raise(**kwargs):
        raise exc

    setattr(blender_adapter.layer, "inspect_scene", _raise)
    result = registry.execute(_request())
    assert result.status is not CapabilityStatus.INVALID_REQUEST, f"{label} -> {result.status.value}"
    assert result.status in {
        CapabilityStatus.FAILURE,
        CapabilityStatus.UNAVAILABLE,
        CapabilityStatus.PERMISSION_REQUIRED,
    }, f"{label} -> {result.status.value}"
    assert result.request_id == S1_REQUEST_ID


@pytest.mark.parametrize("word", ["invalid", "Invalid", "INVALID", "invalid request", "invalid_request"])
def test_invalid_wording_alone_never_produces_invalid_request(registry, blender_adapter, word):
    """Status must come from the exception type, not from message wording."""
    setattr(blender_adapter.layer, "inspect_scene", lambda **kwargs: (_ for _ in ()).throw(RuntimeError(word)))
    result = registry.execute(_request())
    assert result.status is CapabilityStatus.FAILURE


def test_missing_capability_is_unavailable_not_invalid_request(registry):
    result = registry.execute(CapabilityRequest(capability="nope", operation="x", request_id=S1_REQUEST_ID))
    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.status is not CapabilityStatus.INVALID_REQUEST


def test_unsupported_operation_is_invalid_request(registry):
    """Genuinely caller-attributable: the caller asked for something unsupported."""
    result = registry.execute(_request(operation="definitely_not_an_operation"))
    assert result.status is CapabilityStatus.INVALID_REQUEST
    assert result.request_id == S1_REQUEST_ID


def test_missing_capability_or_operation_is_invalid_request(registry):
    result = registry.execute(CapabilityRequest(capability="", operation="", request_id=S1_REQUEST_ID))
    assert result.status is CapabilityStatus.INVALID_REQUEST


def test_structurally_invalid_request_is_rejected_at_construction():
    with pytest.raises(ValueError):
        CapabilityRequest.from_dict({"capability": "blender"})


def test_capability_request_from_dict_preserves_supplied_identity():
    request = CapabilityRequest.from_dict(
        {"capability": "blender", "operation": "inspect_scene", "request_id": S1_REQUEST_ID, "task_id": S1_TASK_ID}
    )
    assert request.request_id == S1_REQUEST_ID
    assert request.task_id == S1_TASK_ID
