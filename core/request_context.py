"""M2.12 P1 — request-scoped execution state.

Ownership model
---------------
=========================  ==================================================
``Agent``                  shared / orchestration state: provider, settings,
                           tool registry, permissions, event bus, planner,
                           executor, task store, change tracker, memory/rules
                           stores, model router. Long-lived.
``RequestContext``         state for exactly one user request. Created per
                           ``Agent.respond()`` call and never shared between
                           requests.
``CapabilityRegistry``     the registry that owns request execution identity
                           (``CapabilityRequest.request_id``) and
                           request-scoped cancellation. S1 does not add a
                           second cancellation registry.
``S2 layers``              capability and session state. Reached only through
                           ``capability.execute`` and never imported here.
=========================  ==================================================

Why this exists
---------------
Before M2.12 the nine values below lived directly on the ``Agent`` instance.
That made request state ownership implicit and forced the whole orchestrator to
be single-flight behind one instance lock, even though the capability registry
underneath already supports genuinely concurrent, independently cancellable
requests.

This module moves that state into one explicit, inspectable object. It
deliberately does **not** add a second request identity: the authoritative S1
request identity is still ``TaskManager.run_id`` / ``TaskManager.task_id``,
which is exactly what already flows into the capability layer as
``run_id``/``task_id``. ``RequestContext`` exposes them as read-only
convenience properties so ownership is obvious without forking identity.

Extensibility
-------------
The intended next layer is a bounded request scheduler::

    RequestContext
        -> bounded request scheduler
        -> execution plan
        -> capability execution
        -> verification
        -> persistence / recovery

Only the first layer exists today. :class:`BoundedRequestGate` is the seam for
the second; it defaults to ``max_concurrent_requests = 1`` so observable
behaviour is unchanged.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from core.cancellation import CancellationToken
from core.task_manager import TaskManager

__all__ = [
    "DEFAULT_MAX_CONCURRENT_REQUESTS",
    "BoundedRequestGate",
    "RequestContext",
]

#: P1 ships single-flight on purpose. A later task may raise this without
#: redesigning :class:`RequestContext`; the gate already models the limit.
DEFAULT_MAX_CONCURRENT_REQUESTS = 1


@dataclass
class RequestContext:
    """All state owned by a single user request.

    A fresh instance is created for every ``Agent.respond()`` call. Two
    contexts never share mutable state: the ``tasks`` manager, the cancellation
    token, the pending goal, the drained recovery list and the model-routing
    counters are all per-instance.
    """

    tasks: TaskManager = field(default_factory=TaskManager)
    cancellation_token: CancellationToken | object | None = None
    router_verify_ok: bool = True
    escalations_used: int = 0
    offline_fallback_used: bool = False
    pending_goal: str = ""
    recovered_task_ids: list[str] = field(default_factory=list)
    terminal_event_emitted: bool = False
    last_model: Any = None

    # -- identity -----------------------------------------------------
    # The authoritative S1 request identity stays TaskManager's. These are
    # read-only views; assigning them would fork identity, so they are
    # intentionally read-only.

    @property
    def run_id(self) -> str:
        """Authoritative S1 run identity (== ``tasks.run_id``)."""
        return self.tasks.run_id

    @property
    def task_id(self) -> str:
        """Authoritative S1 task identity (== ``tasks.task_id``)."""
        return self.tasks.task_id

    def is_cancelled(self) -> bool:
        """Whether this request's cancellation token has been signalled."""
        from core.cancellation import cancellation_requested

        return cancellation_requested(self.cancellation_token)

    def take_recovered_task_ids(self) -> list[str]:
        """Drain and return task ids recovered before this request.

        Reconciliation runs at construction time, so the pending list is
        emitted once by the next request and then cleared. Seeding a fresh
        context from the ambient one preserves that exactly.
        """
        drained = list(self.recovered_task_ids)
        self.recovered_task_ids.clear()
        return drained

    def inherit_pending_recoveries(self, other: "RequestContext") -> "RequestContext":
        """Take over ``other``'s undrained recovery ids and return ``self``."""
        self.recovered_task_ids = list(other.recovered_task_ids)
        other.recovered_task_ids = []
        return self


class BoundedRequestGate:
    """Admission control for :meth:`Agent.respond`.

    Replaces the raw instance lock with an explicit, inspectable limit. The
    gate is re-entrant for the owning thread so that behaviour at
    ``max_concurrent_requests = 1`` is identical to the previous
    ``threading.RLock``; a nested call from a thread that already holds a slot
    does not consume a second one.

    The gate guards *shared* Agent state (the change tracker's single active
    session, the conversation transcript, ``PermissionManager.last_decision``
    and the ``tools.runtime_context`` singleton), not request state, which now
    lives in each :class:`RequestContext`.
    """

    def __init__(self, max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS) -> None:
        requested = int(max_concurrent_requests)
        if requested < 1:
            raise ValueError("max_concurrent_requests must be at least 1")
        self._limit = requested
        self._condition = threading.Condition(threading.RLock())
        self._local = threading.local()
        self._in_flight = 0
        self._peak_in_flight = 0
        self._admitted_total = 0

    @property
    def max_concurrent_requests(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        with self._condition:
            return self._peak_in_flight

    @property
    def admitted_total(self) -> int:
        with self._condition:
            return self._admitted_total

    def _depth(self) -> int:
        return int(getattr(self._local, "depth", 0))

    def acquire(self) -> None:
        if self._depth():
            self._local.depth = self._depth() + 1
            return
        with self._condition:
            while self._in_flight >= self._limit:
                self._condition.wait()
            self._in_flight += 1
            self._admitted_total += 1
            if self._in_flight > self._peak_in_flight:
                self._peak_in_flight = self._in_flight
        self._local.depth = 1

    def release(self) -> None:
        depth = self._depth()
        if depth <= 0:
            raise RuntimeError("release() without a matching acquire()")
        if depth > 1:
            self._local.depth = depth - 1
            return
        self._local.depth = 0
        with self._condition:
            if self._in_flight:
                self._in_flight -= 1
            self._condition.notify()

    def __enter__(self) -> "BoundedRequestGate":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
