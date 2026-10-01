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
    "build_companion_for_agent",
    "run_companion_for_agent",
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
