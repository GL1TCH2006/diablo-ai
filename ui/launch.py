"""M2.14.1 - production launch path for the floating companion UI.

Before this module the companion existed as real widgets, a real state machine
and a real agent bridge, and the product could not launch it: ``main.py`` built
only the rich console UI, and the only construction site for
:class:`~ui.companion.FloatingCompanionApp` was its own test. S2.4 was therefore
a shipped component with no product path, and the roadmap marked it complete.

This module is that path, and nothing more. It does not add a visual feature, a
state, or a widget. It supplies the three things that were missing:

1. A **real** bridge. :class:`~ui.bridge.CoreEventBusBridge` already existed and
   already refused to submit without a submitter; wiring it to
   ``Agent.respond`` is what makes the refusal unreachable in production.
2. A lifecycle. Tkinter must own the main thread, so the companion runs
   ``mainloop`` there while the controller's single daemon worker runs the agent
   turn. Shutdown closes the controller and the bridge in that order.
3. An honest failure. If Tk is unavailable, or the window cannot be created, the
   caller is told rather than the process quietly continuing with no UI.

Threading
---------
Tk runs on the calling (main) thread. ``UIController`` runs exactly one worker
thread, so at most one ``Agent.respond`` is in flight from this UI. The Agent's
own ``BoundedRequestGate`` (``max_concurrent_requests = 1``) is the backstop for
the case where something else is also driving the agent.

M2.16.UI - the edge companion
============================

:func:`build_edge_companion` and :func:`run_edge_companion` add the M2.16.UI edge
docked companion alongside the original floating window.

Two things about that are deliberate:

**The backend is in-process, not a child process.** Part 18 asks for a decision
between attaching to a running backend, launching one, or both. This repository's
Agent entry point is in-process and already the single owner of the request
lifecycle, so the companion *attaches* to the Agent it is handed and does not
spawn, monitor or orphan a backend of its own. Option C is therefore only
partially true: there is exactly one backend, and the companion is a client of it.
A second process would have meant a second Agent, and a second Agent is precisely
what Rules 10-16 forbid.

**Both UIs share one bridge and one Agent.** The edge companion is a second
*presentation* of the same backend, not a parallel control path. Selecting it at
runtime therefore builds one more view over the same
:class:`~ui.bridge.CoreEventBusBridge`, so there is still exactly one submitter,
exactly one canceller and exactly one event subscription set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.logging_setup import get_logger

from .bridge import CoreEventBusBridge
from .controller import UIController

log = get_logger("ui.launch")

__all__ = [
    "CompanionUnavailable",
    "CompanionRuntime",
    "EdgeCompanionRuntime",
    "build_companion_for_agent",
    "run_companion_for_agent",
    "build_edge_companion",
    "run_edge_companion",
]


class CompanionUnavailable(RuntimeError):
    """The companion cannot be shown, and the reason is not a UI bug.

    Raised for a missing Tk, a missing display, or a refused window creation. The
    caller should fall back to the console UI, which is always available.
    """

    def __init__(self, message: str, *, reason: str = "unavailable") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(slots=True)
class CompanionRuntime:
    """Everything the launch path owns, so shutdown cannot leak any of it."""

    controller: UIController
    bridge: CoreEventBusBridge
    app: Any
    _closed: bool = False

    def close(self, timeout: float = 1.0) -> None:
        """Shut down in the only safe order: worker first, bridge second.

        ``UIController.close`` cancels active jobs, stops the worker thread and,
        because this runtime passes its own bridge in, deliberately does not
        close it. Closing the bridge afterwards therefore has exactly one owner.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.controller.close(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - shutdown must not mask the cause
            log.warning("companion controller close failed: %s", exc)
        try:
            self.bridge.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("companion bridge close failed: %s", exc)

    def __enter__(self) -> "CompanionRuntime":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


def _submitter_for(agent: Any) -> Callable[..., Any]:
    """Wrap ``Agent.respond`` for the bridge worker thread.

    The bridge calls ``submit_message(text, task_id=...)``; ``Agent.respond``
    takes the text alone. The ``task_id`` the UI invented is deliberately not
    forwarded: task identity is owned by the Agent's TaskManager, and letting a
    UI-chosen id reach the agent would be a second source of identity.
    """

    def submit(text: str, task_id: str = "") -> str:
        return agent.respond(text)

    return submit


def _canceller_for(agent: Any) -> Callable[..., bool]:
    """Request cancellation of the agent's current turn, if the agent exposes it."""

    def cancel(task_id: str = "") -> bool:
        token = getattr(agent, "_active_cancellation_token", None)
        request = getattr(token, "request", None)
        if callable(request):
            request("cancelled by companion UI", source="ui")
            return True
        return False

    return cancel


def build_companion_for_agent(agent: Any, bus: Any, *, root: Any = None) -> CompanionRuntime:
    """Construct the companion against a real agent and event bus.

    The existing :func:`ui.companion.run_companion` takes an already-built
    bridge and is kept for that lower-level use; this is the production entry
    point that supplies the real bridge from a real agent, which is what was
    missing.

    Raises :class:`CompanionUnavailable` if Tk is unavailable, so a caller can
    fall back to the console UI rather than discovering the problem as a blank
    process.
    """
    try:
        from .companion import FloatingCompanionApp
    except Exception as exc:  # noqa: BLE001 - a Tk import failure is recoverable
        raise CompanionUnavailable(f"companion UI unavailable: {exc}", reason="import_failed") from exc

    bridge = CoreEventBusBridge(
        bus,
        submitter=_submitter_for(agent),
        canceler=_canceller_for(agent),
    )
    controller = UIController(bridge)
    try:
        if not controller.start():
            raise CompanionUnavailable("companion worker thread would not start", reason="worker_failed")
        app = FloatingCompanionApp(controller=controller, root=root)
    except CompanionUnavailable:
        controller.close()
        bridge.close()
        raise
    except Exception as exc:  # noqa: BLE001 - no display, TclError, bad widget
        controller.close()
        bridge.close()
        raise CompanionUnavailable(
            f"companion window could not be created: {exc}", reason="window_failed") from exc

    runtime = CompanionRuntime(controller=controller, bridge=bridge, app=app)
    log.info("companion UI launched (bridge=core-event-bus, worker started)")
    return runtime


def run_companion_for_agent(agent: Any, bus: Any, *, root: Any = None) -> int:
    """Run the companion until its window is closed.

    Blocking: Tk's ``mainloop`` owns the main thread until then. Returns a
    process exit code. Shutdown is guaranteed by the context manager, including
    on an exception out of the loop.
    """
    with build_companion_for_agent(agent, bus, root=root) as runtime:
        runtime.app.run()
    return 0


# ═════════════════════════════════════════════════════ M2.16.UI edge companion


@dataclass(slots=True)
class EdgeCompanionRuntime:
    """Owned resources for the edge companion, so shutdown cannot leak any of them.

    Deliberately does **not** own the bridge. The bridge is shared with any other
    companion view over the same Agent, so closing it here would tear down a
    backend feed another view is still using. The edge controller closes itself; the
    bridge belongs to whoever created it.
    """

    controller: Any
    app: Any
    bridge: CoreEventBusBridge
    created: bool = True
    _closed: bool = False

    def close(self) -> None:
        """Single shutdown path: controller, then the window.

        The controller is closed first so it unsubscribes from the bridge and asks
        the Agent to cancel anything still in flight before the window disappears.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.controller.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not mask the cause
            log.warning("edge companion controller close failed: %s", exc)
        try:
            self.app.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("edge companion window close failed: %s", exc)

    def __enter__(self) -> "EdgeCompanionRuntime":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


def build_edge_companion(
    agent: Any,
    bus: Any,
    *,
    bridge: CoreEventBusBridge | None = None,
    edge: str = "right",
    speak: bool = False,
    display_layer: Any = None,
    root: Any = None,
    goal_provider: Any = None,
) -> EdgeCompanionRuntime:
    """Construct the M2.16.UI edge companion against a real agent.

    The bridge is reused when supplied. That is the supported way to run the
    floating window and the edge companion over one Agent: both are views, and
    neither is allowed to own the backend.
    """
    from .companion_state import CompanionController
    from .edge_companion import EdgeCompanionUnavailable, launch_edge_companion

    shared = bridge is not None
    active_bridge = bridge or CoreEventBusBridge(
        bus,
        submitter=_submitter_for(agent),
        canceler=_canceller_for(agent),
    )

    controller = CompanionController(active_bridge)
    controller.attach()
    try:
        app, created = launch_edge_companion(
            controller,
            display_layer=display_layer,
            edge=edge,
            speak=speak,
            root=root,
            goal_provider=goal_provider,
        )
    except EdgeCompanionUnavailable:
        # Only tear down what this function created. A refused duplicate launch must
        # not close the bridge or the controller belonging to the live companion.
        controller.close()
        if not shared:
            active_bridge.close()
        raise
    except Exception:
        controller.close()
        if not shared:
            active_bridge.close()
        raise

    log.info(
        "edge companion %s (bridge=%s)",
        "created" if created else "already running, focused existing",
        "shared" if shared else "core-event-bus",
    )
    return EdgeCompanionRuntime(
        controller=controller, app=app, bridge=active_bridge, created=created)


def run_edge_companion(agent: Any, bus: Any, **kwargs: Any) -> int:
    """Run the edge companion until its window closes.

    Blocking, for the same reason :func:`run_companion_for_agent` is: Tk owns the
    main thread. The context manager guarantees teardown even if ``mainloop``
    returns through an exception.
    """
    owns_bridge = kwargs.get("bridge") is None
    with build_edge_companion(agent, bus, **kwargs) as runtime:
        try:
            runtime.app.run()
        finally:
            # The edge controller and window are already closed by the runtime. The
            # bridge is only closed here when this call created it, so a shared
            # bridge outlives the companion that used it.
            if owns_bridge:
                try:
                    runtime.bridge.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("edge companion bridge close failed: %s", exc)
    return 0
