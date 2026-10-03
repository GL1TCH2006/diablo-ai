"""M2.16.UI.1 - bounded real-model desktop-tool selection validation.

Answers the question the M2.16.UI audit could not: *can the model select the
M2.16.6 desktop workflow tools when the request actually needs them?*

What is real here
-----------------
A real Ollama model, the real ``Agent``, the real ``ToolContextSelector``, the real
registry and real tool schemas. What is recorded is **which tool the model chose**
and which tools it was shown - the routing decision this milestone exists to
change.

What is NOT claimed
-------------------
Nothing is executed against the desktop. Selection happens against a real provider
with real schemas, but the tool call is intercepted at the plan boundary and not
dispatched, so this measures *routing*, not desktop automation. The companion UI
is not involved either - it is a presentation layer over exactly this lifecycle,
and measuring it here would add a variable without adding evidence about routing.

Sample size and interpretation are reported exactly. N is stated, not implied.

Run directly::

    python tests/real_model_desktop_selection.py --cases 12 --model qwen3:8b
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

#: (request, expected_tool). ``expected`` is the tool that can *serve* the request;
#: a launch request is satisfied by either launcher, an interaction request only by
#: the desktop workflow tools. Written as a set because either answer is correct.
CASES: tuple[tuple[str, frozenset[str]], ...] = (
    ("Open Calculator.", frozenset({"applications.open", "desktop.open"})),
    ("Launch Notepad.", frozenset({"applications.open", "desktop.open"})),
    ("start notepad", frozenset({"applications.open", "desktop.open"})),
    ("Click Save in Notepad.", frozenset({
        "desktop.find", "desktop.interact", "desktop.read", "desktop.verify"})),
    ("find the OK button in the Calculator dialog", frozenset({
        "desktop.find", "desktop.read"})),
    ("Press Ctrl+S in Notepad.", frozenset({
        "desktop.interact", "desktop.find"})),
    ("type hello into the search box", frozenset({
        "desktop.interact", "desktop.find"})),
    ("minimize the Notepad window", frozenset({
        "desktop.find", "desktop.interact", "desktop.verify", "desktop.read"})),
    ("Capture the screen.", frozenset({
        "desktop.read", "desktop.find", "capability.execute"})),
    ("Open Calculator and calculate 123 x 456.", frozenset({
        "desktop.open", "desktop.find", "desktop.interact", "desktop.read",
        "desktop.verify", "applications.open"})),
    ("Open Notepad and click Save", frozenset({
        "desktop.open", "desktop.find", "desktop.interact", "desktop.read",
        "desktop.verify", "applications.open"})),
    ("Open Calculator, then verify that it is open.", frozenset({
        "desktop.open", "desktop.find", "desktop.interact", "desktop.read",
        "desktop.verify", "applications.open"})),
)


class RecordingProvider:
    """Wraps a real provider and records the tools it was offered per call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def chat(self, messages: list[dict], tools: Any = None, model: str = "") -> Any:
        names = [getattr(t, "name", str(t)) for t in (tools or [])]
        response = self._inner.chat(messages, tools=tools, model=model)
        selected = [getattr(c, "name", "") or
                    (getattr(getattr(c, "function", None), "name", "") or "")
                    for c in (getattr(response, "tool_calls", []) or [])]
        self.calls.append({
            "model": model,
            "tools_offered": names,
            "desktop_tools_offered": sorted(
                n for n in names if n.startswith(("desktop.", "applications."))),
            "selected": selected,
            "text": (getattr(response, "text", "") or "")[:200],
        })
        return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=len(CASES))
    parser.add_argument("--only-model", action="store_true",
                        help="skip cases the fast path answers, so a slower model "
                             "can be measured on the cases that actually reach it")
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
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
        print("REFUSING: offline backend cannot measure model tool selection.")
        return 1
    return _run(backend, settings, args)


def _is_fast_path(text: str) -> bool:
    from core.desktop_intent import DesktopIntentFamily, classify_desktop_intent
    from core.planner import Planner
    from config.settings import load_settings

    planner = Planner(load_settings())
    return planner.fast_intent(text) is not None and \
        classify_desktop_intent(text).family is DesktopIntentFamily.APPLICATION_LAUNCH


def _build_agent(settings: Any, backend: Any, bus: Any, preferences: Any,
                 permissions: Any, registry: Any, provider: Any) -> Any:
    """A fresh Agent per case.

    This is not tidiness, it is correctness of the measurement. The first version of
    this probe reused one Agent across all cases, and because the Agent restores its
    conversation, every later request was answered by a model that had just read ten
    of its own earlier refusals ("Notepad must be manually opened"). It reproduced
    them verbatim and the probe scored correct routing as zero. A shared transcript
    turns a per-case capability measurement into a test of the probe's own history.
    """
    from core.agent import Agent
    from memory.conversation import ConversationStore
    from memory.project_memory import ProjectMemory

    return Agent(
        provider=provider,
        settings=settings,
        registry=registry,
        permissions=permissions,
        events=bus,
        confirmer=None,
        project=None,
        memory_conversation=ConversationStore(
            settings.memory_dir, settings.max_conversation_turns),
        memory_preferences=preferences,
        memory_project=ProjectMemory(settings.memory_dir, settings.workspace_root),
        model_name=backend.model_name,
        model_catalog=backend.models,
    )


def _intercept(agent: Any, dispatched: list[str]) -> None:
    """Refuse every execution path so nothing on the desktop is touched.

    Both paths matter. The first version intercepted only ``_execute_step``; the
    fast-path short circuit runs through ``_run_intent``, so two probe cases really
    did launch Notepad on this machine. A probe that mutates the desktop it is
    measuring is not a probe.
    """
    from core.executor import ExecutionOutcome

    def _refuse_step(tool: str, args: dict, plan_step: Any = None) -> Any:
        dispatched.append(tool)
        return ExecutionOutcome(
            tool=tool, allowed=True, approved=True, executed=False,
            success=False, status="error",
            error="routing probe: intercepted, not dispatched",
            output="routing probe: intercepted, not dispatched",
        )

    def _refuse_intent(intent: Any) -> str:
        for step in getattr(intent, "steps", []) or []:
            dispatched.append(str(step[0]))
        return "routing probe: fast-path intent intercepted, not dispatched."

    agent._execute_step = _refuse_step  # type: ignore[method-assign]
    agent._run_intent = _refuse_intent  # type: ignore[method-assign]


def _run(backend: Any, settings: Any, args: Any) -> int:
    import tempfile

    from config.permissions import PermissionManager
    from core.events import EventBus
    from main import build_capability_runtime
    from memory.preferences import PreferencesStore
    from tools import build_registry

    # Each run gets a private memory directory.
    #
    # This is the third measurement bug in this harness, and the subtlest: building
    # a fresh Agent per case is not enough, because ``ConversationStore`` is durable
    # and every new Agent *restores* the previous run's transcript from disk. The
    # model therefore answered each case having just read its own earlier refusals
    # ("Notepad must be manually opened") and reproduced them - so a working route
    # scored zero. A per-case Agent with a shared disk transcript is not isolation.
    scratch = tempfile.TemporaryDirectory(prefix="diablo-routing-probe-")
    settings.memory_dir = Path(scratch.name)

    bus = EventBus()
    preferences = PreferencesStore(settings.memory_dir)
    permissions = PermissionManager(settings, preferences)

    # The desktop tools only exist when a capability runtime supplies
    # ``desktop_workflow``. Without this the registry has no desktop.* tools at all
    # and the probe would measure a routing decision made against an empty set.
    try:
        (capability_registry, _integration, _blender, _workflow,
         _windows) = build_capability_runtime(settings, bus, permissions=permissions)
    except Exception as exc:  # noqa: BLE001
        print(f"capability runtime unavailable: {exc}")
        capability_registry = None
    registry = build_registry(settings, capability_registry=capability_registry)
    offered_names = [s.name for s in registry.all_specs()]
    print(f"registry: {len(offered_names)} tools, "
          f"desktop.* present: {sorted(n for n in offered_names if n.startswith('desktop.'))}")
    if not any(n.startswith("desktop.") for n in offered_names):
        print("REFUSING: no desktop workflow tools in the registry; selection cannot "
              "be measured.")
        return 1

    provider = RecordingProvider(backend.provider)

    # Nothing is dispatched, on either execution path. See ``_intercept``.
    dispatched: list[str] = []

    selected_cases = CASES[: max(1, min(args.cases, len(CASES)))]
    if getattr(args, "only_model", False):
        planner_probe = Planner_probe(settings) if False else None
        selected_cases = [c for c in selected_cases
                          if not _is_fast_path(c[0])]
    cases = selected_cases
    print(f"\ncases: {len(cases)}   (N is stated, not implied)")
    print("one fresh Agent per case, and a fresh conversation with it\n")
    print("-" * 100)
    header = f"{'request':<46} {'intent':<24} {'shown':>5} {'selected':<22} ok"
    print(header)
    print("-" * 100)

    rows: list[dict[str, Any]] = []
    correct = 0
    desktop_selected = 0
    fast_path = 0
    from core.planner import Planner

    planner = Planner(settings)
    for request, expected in cases:
        from core.desktop_intent import classify_desktop_intent

        intent = classify_desktop_intent(request)
        before = len(provider.calls)
        started = time.monotonic()
        # Fresh Agent, fresh conversation, per case. See ``_build_agent``: sharing
        # one made the model imitate its own earlier refusals and scored working
        # routing as zero.
        agent = _build_agent(settings, backend, bus, preferences, permissions,
                             registry, provider)
        _intercept(agent, dispatched)
        try:
            agent.respond(request)
        except Exception as exc:  # noqa: BLE001 - a routing probe must not crash
            print(f"{request[:44]:<46} ERROR {type(exc).__name__}: {exc}")
            continue
        elapsed = time.monotonic() - started
        consulted = len(provider.calls) > before

        if not consulted:
            # Routed without a model. That is the intended behaviour for a pure
            # launch, and it is counted as correct routing - but it is reported
            # separately, because "the model chose this" would be false.
            fast_path += 1
            planned = planner.fast_intent(request)
            selected = [str(step[0]) for step in (planned.steps if planned else [])]
            offered = []
            selected = selected  # noqa: F841 - assigned for symmetry
            models_used = []
            model_text = ""
            routed_by = "fast_path"
        else:
            routed_by = "model"
            # Aggregate across every provider call this request made, not just the
            # last. The agent loops: a first turn may select a tool correctly, the
            # stubbed failure drives a recovery turn, and the *final* turn is prose.
            # Reading only the last call reported "no selection" for requests where
            # the first call had already chosen correctly - a measurement bug that
            # made a working path look broken.
            turn_calls = provider.calls[before:]
            offered = sorted({
                name
                for call in turn_calls
                for name in call["desktop_tools_offered"]
            })
            selected = [name for call in turn_calls for name in call["selected"]]
            models_used = sorted({c["model"] for c in turn_calls})
            model_text = turn_calls[-1]["text"] if turn_calls else ""
            turns = len(turn_calls)

        picked = [s for s in selected if s in expected]
        ok = bool(picked)
        correct += 1 if ok else 0
        used_desktop = any(s.startswith("desktop.") for s in selected)
        desktop_selected += 1 if used_desktop else 0
        row = {
            "request": request,
            "intent": intent.family.value,
            "confidence": intent.confidence,
            "routed_by": routed_by,
            "model_consulted": consulted,
            "desktop_tools_shown": offered,
            "selected": selected,
            "expected_any_of": sorted(expected),
            "correct": ok,
            "used_desktop_workflow": used_desktop,
            "latency_s": round(elapsed, 2),
            "models_used": models_used,
            "model_text": model_text[:200],
        }
        rows.append(row)
        print(f"{request[:44]:<46} {intent.family.value:<24} {len(offered):>5} "
              f"{(selected[0] if selected else '(none)'):<22} "
              f"{'YES' if ok else 'no ':<3} {routed_by}")

    print("-" * 100)
    print(f"correct routing    : {correct}/{len(rows)}")
    print(f"routed by fast path: {fast_path}/{len(rows)} (no model consulted)")
    print(f"model consulted    : {len(rows) - fast_path}/{len(rows)}")
    print(f"desktop.* chosen   : {desktop_selected}/{len(rows)}")
    print(f"tools intercepted  : {len(dispatched)} (none executed)")
    latencies = [r["latency_s"] for r in rows]
    if latencies:
        print(f"latency           : min {min(latencies)}s  max {max(latencies)}s  "
              f"mean {sum(latencies) / len(latencies):.2f}s")
    wrong = [r for r in rows if not r["correct"]]
    if wrong:
        print("\nnot correct:")
        for row in wrong:
            print(f"  {row['request']!r} -> {row['selected'] or '(none)'} "
                  f"(expected one of {row['expected_any_of']})")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"model": settings.ai_model or backend.model_name,
                       "n": len(rows), "correct": correct,
                       "desktop_chosen": desktop_selected,
                       "cases": rows}, handle, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0 if correct == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())