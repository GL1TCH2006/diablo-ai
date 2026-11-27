"""M2.12 P1 — request-scoped execution context.

Covers RequestContext isolation and defaults, the bounded request gate as the
scheduler seam, and the guarantee that the pre-existing Agent surface
(``agent._tasks`` and friends) still reads and writes the active context.
"""
import threading
import time

import pytest

from core.agent import Agent
from core.request_context import (
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    BoundedRequestGate,
    RequestContext,
)
from core.task_manager import TaskManager, TaskState
from core.verification import VerificationState
from voice.cancellation import CancellationToken


# ---------------------------------------------------------------------------
# default state
# ---------------------------------------------------------------------------

def test_fresh_context_initialises_deterministically():
    a, b = RequestContext(), RequestContext()
    assert a.tasks is not b.tasks
    assert a.cancellation_token is None
    assert a.router_verify_ok is True
    assert a.escalations_used == 0
    assert a.offline_fallback_used is False
    assert a.pending_goal == ""
    assert a.recovered_task_ids == []
    assert a.terminal_event_emitted is False
    assert a.last_model is None


def test_default_max_concurrent_requests_is_single_flight():
    assert DEFAULT_MAX_CONCURRENT_REQUESTS == 1
    assert BoundedRequestGate().max_concurrent_requests == 1


def test_gate_rejects_a_nonsensical_limit():
    with pytest.raises(ValueError):
        BoundedRequestGate(0)
    with pytest.raises(ValueError):
        BoundedRequestGate(-3)


# ---------------------------------------------------------------------------
# request isolation
# ---------------------------------------------------------------------------

def test_two_contexts_do_not_share_tasks():
    a, b = RequestContext(), RequestContext()
    a.tasks.begin_task("alpha")
    assert a.tasks.task_title == "alpha"
    assert b.tasks.task_title == ""
    assert a.tasks.run_id != b.tasks.run_id


def test_two_contexts_do_not_share_cancellation_token():
    a, b = RequestContext(), RequestContext()
    token = CancellationToken(task_id="A")
    a.cancellation_token = token
    assert b.cancellation_token is None
    token.request("stop", source="test")
    assert a.is_cancelled() is True
    assert b.is_cancelled() is False


def test_two_contexts_do_not_share_recovered_task_ids():
    a, b = RequestContext(), RequestContext()
    a.recovered_task_ids.append("task-1")
    assert b.recovered_task_ids == []
    assert a.recovered_task_ids == ["task-1"]


def test_two_contexts_do_not_share_pending_goal():
    a, b = RequestContext(), RequestContext()
    a.pending_goal = "ship it"
    assert b.pending_goal == ""


def test_two_contexts_do_not_share_terminal_event_state():
    a, b = RequestContext(), RequestContext()
    a.terminal_event_emitted = True
    assert b.terminal_event_emitted is False


def test_two_contexts_do_not_share_model_routing_state():
    a, b = RequestContext(), RequestContext()
    a.router_verify_ok = False
    a.escalations_used = 2
    a.offline_fallback_used = True
    a.last_model = "some-model"
    assert b.router_verify_ok is True
    assert b.escalations_used == 0
    assert b.offline_fallback_used is False
    assert b.last_model is None


def test_request_identity_is_task_manager_identity_not_a_second_id():
    ctx = RequestContext()
    assert ctx.run_id == ctx.tasks.run_id
    assert ctx.task_id == ctx.tasks.task_id
    # identity is read-only: a second identity cannot be introduced
    with pytest.raises(AttributeError):
        ctx.task_id = "hijacked"
    with pytest.raises(AttributeError):
        ctx.run_id = "hijacked"


def test_recovered_ids_drain_exactly_once():
    ctx = RequestContext()
    ctx.recovered_task_ids.extend(["a", "b"])
    assert ctx.take_recovered_task_ids() == ["a", "b"]
    assert ctx.take_recovered_task_ids() == []
    assert ctx.recovered_task_ids == []


def test_inherit_pending_recoveries_moves_and_clears():
    ambient, fresh = RequestContext(), RequestContext()
    ambient.recovered_task_ids.append("orphan")
    returned = fresh.inherit_pending_recoveries(ambient)
    assert returned is fresh
    assert fresh.recovered_task_ids == ["orphan"]
    assert ambient.recovered_task_ids == []


# ---------------------------------------------------------------------------
# bounded request gate (the scheduler seam)
# ---------------------------------------------------------------------------

def test_gate_serialises_at_the_default_limit():
    gate = BoundedRequestGate()
    order = []

    def worker(tag):
        with gate:
            order.append(f"{tag}-in")
            time.sleep(0.05)
            order.append(f"{tag}-out")

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order in (["A-in", "A-out", "B-in", "B-out"],
                     ["B-in", "B-out", "A-in", "A-out"])
    assert gate.peak_in_flight == 1
    assert gate.admitted_total == 2
    assert gate.in_flight == 0


def test_gate_is_reentrant_for_the_owning_thread():
    gate = BoundedRequestGate()
    with gate:
        assert gate.in_flight == 1
        with gate:                      # nested acquire must not deadlock or double-count
            assert gate.in_flight == 1
        assert gate.in_flight == 1
    assert gate.in_flight == 0
    assert gate.admitted_total == 1


def test_gate_release_without_acquire_is_an_error():
    gate = BoundedRequestGate()
    with pytest.raises(RuntimeError):
        gate.release()


def test_gate_represents_a_higher_limit_without_enabling_it_by_default():
    """The limit is representable now; P1 must not turn it on."""
    gate = BoundedRequestGate(4)
    assert gate.max_concurrent_requests == 4
    assert BoundedRequestGate().max_concurrent_requests == 1
    assert DEFAULT_MAX_CONCURRENT_REQUESTS == 1


# ---------------------------------------------------------------------------
# Agent integration: legacy surface still maps onto the active context
# ---------------------------------------------------------------------------

def _agent(tmp_settings, tmp_path, calls=None):
    from ai.provider import AIProvider, AIResponse, ToolCall
    from config.permissions import PermissionManager
    from core.events import EventBus
    from tools import build_registry

    class _P(AIProvider):
        name, model_label, available = "scripted", "scripted/p1", True

        def __init__(self):
            self.queue = list(calls or [])
            self.turn = 0

        def is_available(self):
            return True

        def list_models(self):
            return []

        def chat(self, messages, tools=None, model=None):
            self.turn += 1
            if self.queue and self.turn == 1:
                return AIResponse(text="", tool_calls=[self.queue.pop(0)])
            return AIResponse(text="ok")

    return Agent(provider=_P(), settings=tmp_settings,
                 registry=build_registry(tmp_settings),
                 permissions=PermissionManager(tmp_settings), events=EventBus(),
                 confirmer=lambda tool, desc: "allow",
                 model_name="", model_catalog=[])


def test_agent_exposes_an_active_request_context(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    assert isinstance(agent.request_context, RequestContext)
    assert agent.request_context is agent._request_context


def test_agent_defaults_to_single_flight(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    assert agent.max_concurrent_requests == 1
    assert agent.request_gate.max_concurrent_requests == 1


def test_legacy_aliases_read_and_write_the_active_context(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    ctx = agent.request_context

    agent._tasks = TaskManager()
    assert ctx.tasks is agent._tasks

    agent._pending_goal = "goal text"
    assert ctx.pending_goal == "goal text"
    assert agent._pending_goal == "goal text"

    agent._router_verify_ok = False
    assert ctx.router_verify_ok is False
    agent._escalations_used = 3
    assert ctx.escalations_used == 3
    agent._offline_fallback_used = True
    assert ctx.offline_fallback_used is True
    agent._terminal_event_emitted = True
    assert ctx.terminal_event_emitted is True
    agent._last_model = "m"
    assert ctx.last_model == "m"
    token = CancellationToken(task_id="T")
    agent._active_cancellation_token = token
    assert ctx.cancellation_token is token
    agent._recovered_task_ids = ["x"]
    assert ctx.recovered_task_ids == ["x"]


def test_agent_holds_no_request_state_on_the_instance_dict(tmp_settings, tmp_path):
    """Request state must live on the context, not as Agent instance attributes."""
    agent = _agent(tmp_settings, tmp_path)
    ctx_fields = {f.name for f in RequestContext.__dataclass_fields__.values()}
    for name in ctx_fields:
        assert name not in vars(agent), f"{name} must not be an Agent instance attribute"
    assert "_request_lock" not in vars(agent)


def test_each_respond_gets_its_own_context(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    first = agent.request_context
    agent.respond("first request")
    second = agent.request_context
    assert second is not first
    assert first is not agent.request_context
    # the completed context stays installed for post-request inspection
    assert agent._tasks is second.tasks
    assert second.tasks.task_title == "first request"


def test_sequential_requests_do_not_leak_state(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    agent.respond("alpha %plan ship the thing")
    first_id = agent.request_context.run_id
    assert agent.request_context.pending_goal == "ship the thing"
    assert agent._escalations_used == 0
    assert agent._router_verify_ok is True

    agent.respond("beta")
    second = agent.request_context
    assert second.pending_goal == ""
    assert second.run_id != first_id
    assert second.recovered_task_ids == []
    assert second.terminal_event_emitted is True


def test_gate_counts_admissions_across_requests(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    agent.respond("one")
    agent.respond("two")
    assert agent.request_gate.admitted_total == 2
    assert agent.request_gate.in_flight == 0
    assert agent.request_gate.peak_in_flight == 1


def test_recovered_ids_are_reported_once_by_the_next_request(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    agent.request_context.recovered_task_ids.append("pending-recovery")
    agent.respond("hello there")
    assert agent.request_context.recovered_task_ids == []


def test_cancellation_token_is_request_scoped(tmp_settings, tmp_path):
    from ai.provider import ToolCall
    call = ToolCall(name="filesystem.write_file",
                    arguments={"path": str(tmp_path / "p1.txt"), "content": "x"}, id="c1")
    agent = _agent(tmp_settings, tmp_path, calls=[call])
    token = CancellationToken(task_id="T1")

    seen = {}
    original = agent._record_execution

    def spy(tool, args, outcome, step=None):
        seen["token"] = agent.request_context.cancellation_token
        seen["cancelled"] = agent.request_context.is_cancelled()
        seen["run_id"] = agent.request_context.run_id
        return original(tool, args, outcome, step)

    agent._record_execution = spy
    agent.respond("write a file to disk", cancellation_token=token)
    assert seen["token"] is token, "the token must be live on the context during the request"
    assert seen["cancelled"] is False
    # the request identity is the context's TaskManager identity
    assert seen["run_id"] == agent.request_context.run_id

    # pre-existing behaviour: the token is released when the request ends
    assert agent.request_context.cancellation_token is None
    agent.respond("hello there")
    assert agent.request_context.cancellation_token is None
    assert agent.request_context.is_cancelled() is False


def test_cancelled_request_does_not_cancel_the_next(tmp_settings, tmp_path):
    from ai.provider import ToolCall
    call = ToolCall(name="filesystem.write_file",
                    arguments={"path": str(tmp_path / "p2.txt"), "content": "x"}, id="c1")
    agent = _agent(tmp_settings, tmp_path, calls=[call])
    token = CancellationToken(task_id="T1")
    token.request("stop", source="test")
    agent.respond("write a file to disk", cancellation_token=token)
    cancelled_run = agent.request_context.run_id
    assert agent.request_context.tasks.state is TaskState.CANCELLED
    assert not (tmp_path / "p2.txt").exists(), "a pre-cancelled request must not execute"

    agent.respond("hello there")
    assert agent.request_context.run_id != cancelled_run
    assert agent.request_context.is_cancelled() is False
    assert agent.request_context.tasks.state is not TaskState.CANCELLED


def test_verification_state_is_request_scoped(tmp_settings, tmp_path):
    agent = _agent(tmp_settings, tmp_path)
    agent.respond("hello there")
    first = agent.request_context
    assert first.tasks.verification in (VerificationState.NOT_REQUIRED,
                                        VerificationState.UNVERIFIED)
    agent.respond("hello there again")
    second = agent.request_context
    assert second is not first
    assert second.tasks is not first.tasks
    # mutating the finished context's verification cannot affect the new one
    first.tasks.set_verification(VerificationState.FAILED, "synthetic")
    assert second.tasks.verification is not VerificationState.FAILED
