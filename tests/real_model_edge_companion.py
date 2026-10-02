"""M2.16.UI - bounded real-model validation through the edge companion (Part 29).

Runs one real task through the *production* stack - real Ollama model, real
Agent, real PermissionManager, real capability runtime, real M2.16.6 desktop
workflow - with the companion as the only UI, and records what the model actually
did.

This is a script, not a test, and deliberately so. It needs Ollama, it takes
minutes, and it mutates the desktop. Its result belongs in the audit as an
honest observation, not as a pass/fail gate.

What it records: model, latency, every tool the agent selected, the workflow
result, the verification result, and the terminal state the companion reached.

Run it directly::

    python tests/real_model_edge_companion.py --task "Open Calculator." \
        --timeout 420 --model qwen3:8b

Exit codes: 0 the companion reached a terminal state; 1 it did not (timeout,
model refusal, or an exception). Either way the transcript is printed, because
"the model declined" is a result and must not be laundered into a pass.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="Open Calculator.",
                        help="the task to give the companion")
    parser.add_argument("--timeout", type=float, default=420.0,
                        help="seconds to wait for a terminal state")
    parser.add_argument("--model", default="", help="override the model name")
    parser.add_argument("--edge", default="right", help="which edge to dock to")
    parser.add_argument("--offline", action="store_true",
                        help="force the rule-based backend instead of Ollama")
    args = parser.parse_args(argv)

    from config.settings import load_settings

    settings = load_settings()
    if args.model:
        settings.ai_model = args.model

    from ai.model_manager import build_backend
    from core.events import EventBus
    from core.logging_setup import setup_logging

    setup_logging("WARNING", settings.logs_dir / "diablo.log")

    backend = build_backend(settings, force_offline=args.offline)
    print(f"backend        : {backend.note}")
    print(f"provider       : {backend.provider.name}")
    print(f"model          : {settings.ai_model or backend.model_name or '(default)'}")
    print(f"models detected: {len(backend.models)}")
    if not args.offline and not backend.models:
        print("no model available; aborting rather than reporting an empty run")
        return 1

    from config.permissions import PermissionManager
    from memory.conversation import ConversationStore
    from memory.preferences import PreferencesStore
    from memory.project_memory import ProjectMemory
    from tools import build_registry
    from main import build_capability_runtime

    bus = EventBus()
    preferences = PreferencesStore(settings.memory_dir)
    permissions = PermissionManager(settings, preferences)

    runtime = None
    try:
        (capability_registry, _integration, _blender, _workflow,
         _windows) = build_capability_runtime(settings, bus, permissions=permissions)
    except Exception as exc:  # noqa: BLE001
        print(f"capability runtime unavailable: {exc}")
        capability_registry = None

    registry = build_registry(settings, capability_registry=capability_registry,
                              capability_events=bus)
    conversation = ConversationStore(settings.memory_dir, settings.max_conversation_turns)
    project_memory = ProjectMemory(settings.memory_dir, settings.workspace_root)

    from core.agent import Agent

    agent = Agent(
        provider=backend.provider,
        settings=settings,
        registry=registry,
        permissions=permissions,
        events=bus,
        confirmer=None,          # non-interactive: rely on PermissionManager policy
        project=None,
        memory_conversation=conversation,
        memory_preferences=preferences,
        memory_project=project_memory,
        model_name=backend.model_name if settings.ai_model or
        backend.provider.name == "offline" else "",
        model_catalog=backend.models,
    )

    # The companion is the only UI in this run.
    from ui.bridge import CoreEventBusBridge
    from ui.companion_state import CompanionController, CompanionState
    from ui.launch import _canceller_for, _submitter_for

    bridge = CoreEventBusBridge(bus, submitter=_submitter_for(agent),
                                canceler=_canceller_for(agent))
    controller = CompanionController(bridge, dwell_seconds=3.0)
    controller.attach()

    tools_seen: list[str] = []
    raw_events: list[dict[str, Any]] = []

    def record(data: dict) -> None:
        tool = str(data.get("tool") or "")
        if tool:
            tools_seen.append(tool)
        raw_events.append(dict(data))

    for channel in ("tool", "task_done", "status"):
        bus.subscribe(channel, record)

    print(f"task           : {args.task!r}")
    print(f"timeout        : {args.timeout:.0f}s")
    print("-" * 68)

    started = time.monotonic()

    # A user clicks the sleeping presence before it will accept anything, and this
    # run has no window, so the same path is driven directly. Task submission from
    # SLEEPING is refused by design.
    controller.activate()
    while controller.state is CompanionState.AWAKENING:
        controller.tick()
        time.sleep(0.05)
    print(f"companion state : {controller.state.value}")

    job = controller.submit_task(args.task)
    print(f"companion task : {job}")

    terminal: list[Any] = []
    original_handle = controller.handle_event

    def watch(event: Any) -> None:
        original_handle(event)
        if event.kind.value in ("task_completed", "task_failed", "task_cancelled"):
            terminal.append((time.monotonic() - started, event.kind.value,
                             event.message))

    controller.handle_event = watch  # type: ignore[method-assign]

    # The controller normally advances on the UI thread's timer. This run has no
    # window, so a plain loop drives it, which is exactly what pump_once does.
    deadline = time.monotonic() + args.timeout
    last_state = None
    while time.monotonic() < deadline and not terminal:
        controller.tick()
        state = controller.state
        if state is not last_state:
            print(f"  [{time.monotonic() - started:7.1f}s] {state.value}")
            last_state = state
        if state is CompanionState.SLEEPING and time.monotonic() - started > 3:
            break
        time.sleep(0.1)

    elapsed = time.monotonic() - started
    snapshot = controller.snapshot()

    print("-" * 68)
    print(f"elapsed            : {elapsed:.1f}s")
    print(f"terminal events    : {len(terminal)}")
    if terminal:
        for at, kind, message in terminal:
            print(f"  - {kind} at {at:.1f}s: {message}")
    else:
        print("  - NONE: no terminal event was observed")
    print(f"final state        : {controller.state.value}")
    print(f"companion message  : {snapshot.terminal_message!r}")
    print(f"tools selected     : {tools_seen or '(none observed)'}")
    print(f"progress shown     : {list(snapshot.progress)}")

    verification = [data for data in raw_events
                    if "verif" in str(data.get("tool", "")).lower()
                    or "verif" in str(data.get("detail", "")).lower()]
    print(f"verification hints : {len(verification)}")

    controller.close()
    try:
        bus.close()
    except Exception:  # noqa: BLE001
        pass
    if capability_registry is not None:
        try:
            from main import close_registered_desktop_workflow

            close_registered_desktop_workflow(capability_registry)
        except Exception:  # noqa: BLE001
            pass
    _ = runtime

    print("-" * 68)
    if terminal and terminal[0][1] == "task_completed":
        print("REAL-MODEL RESULT: task_completed observed through the companion")
        return 0
    print("REAL-MODEL RESULT: no successful completion observed")
    return 1


if __name__ == "__main__":
    sys.exit(main())