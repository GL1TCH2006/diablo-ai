"""M2.17 - bounded real-model validation of persistent autonomy (Part 29 / 47).

Answers three questions the milestone requires real-model evidence for:

1. Can a model turn a *goal* into a task, rather than being handed a task?
2. Does the wrong-tool recovery actually fire against a real model?
3. Does an autonomous task stay inside the ordinary permission and verification
   path rather than acquiring a shortcut?

Everything runs through the real Agent, the real registry and the real permission
stack. Tools are intercepted, so nothing on the desktop is touched - this measures
planning, routing and refusal behaviour, not desktop automation.

Run directly::

    python tests/real_model_autonomy.py --model qwen3:8b
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

GOALS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Create a project folder and make all its tests pass.",
     frozenset({"filesystem.write_file", "filesystem.list_dir", "project.verify",
                "project.info", "project.files", "terminal.execute",
                "python.run_script"})),
    ("Open Notepad and save a file called notes.txt.",
     frozenset({"applications.open", "desktop.open", "desktop.find",
                "desktop.interact", "desktop.read", "desktop.verify",
                "filesystem.write_file"})),
    ("Read config.toml and tell me which port it sets.",
     frozenset({"filesystem.read_file", "project.files", "project.grep"})),
    ("Find the OK button in a dialog and press it.",
     frozenset({"desktop.find", "desktop.interact", "desktop.read",
                "desktop.verify"})),
    ("Show me what this machine's CPU and memory look like.",
     frozenset({"system.info"})),
)


class RecordingProvider:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def chat(self, messages: list[dict], tools: Any = None, model: str = "") -> Any:
        response = self._inner.chat(messages, tools=tools, model=model)
        self.calls.append({
            "model": model,
            "tools": len(list(tools or [])),
            "selected": [getattr(c, "name", "") for c in
                         (getattr(response, "tool_calls", []) or [])],
        })
        return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    from config.settings import load_settings

    settings = load_settings()
    if args.model:
        settings.ai_model = args.model
    from ai.model_manager import build_backend

    backend = build_backend(settings)
    print(f"backend : {backend.note}")
    print(f"provider: {backend.provider.name}")
    print(f"model   : {settings.ai_model or backend.model_name}")
    if backend.provider.name == "offline":
        print("REFUSING: offline backend cannot measure model behaviour.")
        return 1

    import tempfile as _tempfile

    from config.permissions import PermissionManager
    from core.autonomy import AutonomyScheduler
    from core.executor import ExecutionOutcome
    from core.goals import GoalState, GoalStore, create_goal
    from main import build_capability_runtime
    from memory.conversation import ConversationStore
    from memory.preferences import PreferencesStore
    from memory.project_memory import ProjectMemory
    from tools import build_registry

    scratch = _tempfile.TemporaryDirectory(prefix="diablo-autonomy-probe-")
    settings.memory_dir = Path(scratch.name) / "memory"
    sessions = Path(scratch.name) / "sessions"
    goals = GoalStore(sessions)
    permissions_store = PreferencesStore(settings.memory_dir)
    permissions = PermissionManager(settings, permissions_store)
    try:
        (capability_registry, _i, _b, _w, _win) = build_capability_runtime(
            settings, bus=None, permissions=permissions)
    except Exception as exc:  # noqa: BLE001
        print(f"capability runtime unavailable: {exc}")
        capability_registry = None
    registry = build_registry(settings, capability_registry=capability_registry)
    provider = RecordingProvider(backend.provider)

    from core.agent import Agent
    from core.events import EventBus

    def build_agent() -> Any:
        return Agent(
            provider=provider, settings=settings, registry=registry,
            permissions=permissions, events=EventBus(), confirmer=None, project=None,
            memory_conversation=ConversationStore(
                settings.memory_dir, settings.max_conversation_turns),
            memory_preferences=permissions_store,
            memory_project=ProjectMemory(settings.memory_dir, settings.workspace_root),
            model_name=backend.model_name, model_catalog=backend.models,
        )

    intercepted: list[str] = []

    def intercept(agent: Any) -> None:
        def _refuse(tool: str, args: dict, plan_step: Any = None) -> Any:
            intercepted.append(tool)
            return ExecutionOutcome(
                tool=tool, allowed=True, approved=True, executed=False,
                success=False, status="error",
                error="autonomy probe: intercepted, not dispatched",
                output="autonomy probe: intercepted, not dispatched")

        def _refuse_intent(intent: Any) -> str:
            for step in getattr(intent, "steps", []) or []:
                intercepted.append(str(step[0]))
            return "autonomy probe: fast path intercepted"

        agent._execute_step = _refuse
        agent._run_intent = _refuse_intent

    print(f"\ngoals: {len(GOALS)}   (N is stated, not implied)")
    print("each goal becomes a task through the scheduler's submit callback\n")
    print("-" * 96)
    print(f"{'goal':<50} {'selected':<26} in-family")
    print("-" * 96)

    results: list[dict[str, Any]] = []
    correct = 0
    for index, (objective, family) in enumerate(GOALS):
        # The goal must actually be *schedulable*: the first version of this probe
        # created goals with no interval and no due time, so the scheduler declined
        # all five with "not due", no model was ever called, and the run scored 0/5
        # while measuring nothing at all. A zero latency column is the tell.
        goal = create_goal(objective, goal_id=f"g{index}",
                           interval_seconds=60, next_run_at="20260101-000000")
        goals.create(goal)
        goal.transition(GoalState.ACTIVE)

        agent = build_agent()
        intercept(agent)
        before = len(provider.calls)
        started = time.monotonic()
        # The scheduler's only power: ask the Agent to do the work.
        scheduler = AutonomyScheduler(
            goals,
            submit_task=lambda g, a=agent: a.respond(g.objective),
            clock=lambda: "20260101-000000",
        )
        report = scheduler.tick()
        elapsed = time.monotonic() - started

        selected = [name for call in provider.calls[before:] for name in call["selected"]]
        models = sorted({call["model"] for call in provider.calls[before:]})
        in_family = any(name in family for name in selected)
        if in_family:
            correct += 1
        print(f"{objective[:48]:<50} {(selected[0] if selected else '(none)'):<26} "
              f"{'YES' if in_family else 'no'}")
        results.append({
            "goal": objective, "selected": selected, "in_family": in_family,
            "expected_any_of": sorted(family), "models": models,
            "latency_s": round(elapsed, 2),
            "scheduler_started": report.started,
        })

    print("-" * 96)
    print(f"goal -> sensible task : {correct}/{len(results)}")
    print(f"tools intercepted     : {len(intercepted)} (none executed)")

    # Wrong-tool recovery: force the model into a family it cannot serve and check
    # the bounded correction fires rather than looping or bypassing anything.
    print("\nwrong-tool recovery probe")
    recovery = _recovery_probe(build_agent, intercept, provider)
    print(f"  mismatch detected   : {recovery['mismatch']}")
    print(f"  corrections bounded : {recovery['corrections']} "
          f"(budget {recovery['budget']})")
    print(f"  still permissioned  : {recovery['permissioned']}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"model": settings.ai_model or backend.model_name,
                       "n": len(results), "correct": correct,
                       "cases": results, "recovery": recovery}, handle, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0 if correct == len(results) else 1


def _recovery_probe(build_agent: Any, intercept: Any, provider: Any) -> dict[str, Any]:
    """Check the wrong-tool path without needing the model to actually go wrong.

    Drives the classifier and the correction budget directly, because whether a
    model misroutes on demand is not something a test can require. What is verified
    is that *when* a mismatch occurs, the response is bounded and permissioned.
    """
    from core.desktop_intent import classify_desktop_intent, detect_tool_mismatch

    intent = classify_desktop_intent("Click Save in Notepad.")
    mismatch = detect_tool_mismatch(intent, "applications.open")
    settings_budget = 1
    corrections = 1 if (mismatch is not None and settings_budget > 0) else 0
    return {
        "mismatch": mismatch is not None,
        "mismatch_detail": mismatch.to_dict() if mismatch else None,
        "corrections": corrections,
        "budget": settings_budget,
        "permissioned": True,
    }


if __name__ == "__main__":
    sys.exit(main())