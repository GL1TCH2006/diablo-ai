"""M2.17 - cross-restart continuation, across a real interpreter.

Run as a script with ``--phase``. The parent test spawns one interpreter per phase,
so "restart" is a genuine process exit rather than a new object in the same
interpreter. Nothing is shared between phases except the files on disk, which is
the entire point: if any of this relied on in-memory state it could not pass.

Phases
------
``seed``    write a goal, a task and a durable plan, then exit
``resume``  build a fresh Agent, reconcile, and attempt to resume

The interesting case is ``seed`` leaving a step persisted as RUNNING with a
side-effecting tool. The resuming process must refuse to replay it. That refusal is
the milestone's central safety property (§27): *a RUNNING step at process shutdown
does not mean "execute it again"*.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _agent(settings: Any) -> Any:
    from config.permissions import PermissionManager
    from core.agent import Agent
    from core.events import EventBus
    from memory.conversation import ConversationStore
    from memory.preferences import PreferencesStore
    from memory.project_memory import ProjectMemory
    from tools import build_registry

    prefs = PreferencesStore(settings.memory_dir)
    return Agent(
        provider=_NoProvider(),
        settings=settings,
        registry=build_registry(settings),
        permissions=PermissionManager(settings, prefs),
        events=EventBus(),
        confirmer=None,
        project=None,
        memory_conversation=ConversationStore(
            settings.memory_dir, settings.max_conversation_turns),
        memory_preferences=prefs,
        memory_project=ProjectMemory(settings.memory_dir, settings.workspace_root),
        model_name="",
        model_catalog=[],
    )


class _NoProvider:
    """A provider that fails loudly if the resume path tries to use it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"resume must not call the provider ({name})")

    def list_models(self) -> list:
        return []


def _seed(sessions: Path, kind: str) -> dict[str, Any]:
    """Write durable state that looks like a process died mid-task."""
    from core.goals import GoalState, GoalStore, create_goal
    from core.planner import ExecutionPlanRecord, PlanStepRecord, side_effect_key
    from core.task_store import TaskRecord, TaskStore

    goals = GoalStore(sessions)
    tasks = TaskStore(sessions)

    goal = create_goal("Create the project and make all tests pass.",
                       goal_id="goal-restart", required_evidence=2)
    goals.create(goal)
    goal.transition(GoalState.ACTIVE)

    side_effecting = kind == "side_effect"
    done_tool = "filesystem.read_file"
    live_tool = "filesystem.write_file" if side_effecting else "filesystem.read_file"
    live_args = ({"path": "out.txt", "content": "x"} if side_effecting
                 else {"path": "in.txt"})

    plan = ExecutionPlanRecord(goal=goal.objective, created_at="20260101-000000")

    finished = PlanStepRecord(step_id="s1", tool=done_tool, args={"path": "in.txt"})
    finished.side_effect_key = side_effect_key(finished.tool, finished.args)
    finished.status = finished.status.DONE
    finished.finished_at = "20260101-000000"
    plan.steps.append(finished)

    live = PlanStepRecord(step_id="s2", tool=live_tool, args=live_args)
    live.side_effect_key = side_effect_key(live.tool, live.args)
    # Write-ahead: RUNNING reaches disk *before* dispatch, exactly as the Agent does.
    live.begin_attempt("20260101-000001")
    if side_effecting:
        # The effect may or may not have applied. The status stays ambiguous on
        # purpose; only ``interrupted`` records that the process died.
        live.mark_interrupted("20260101-000002")
    plan.steps.append(live)

    record = TaskRecord(
        task_id="task-restart",
        project_name="restart",
        project_root=str(sessions),
        title="part of the objective",
        goal=goal.objective,
        created_at="20260101-000000",
        started_at="20260101-000000",
        status="active",
        plan=plan,
    )
    tasks.create(record)
    record.metadata["lifecycle"] = {
        "state": "executing",
        "verification": "not_run",
        "recovery_attempts": 0,
        "recovery_exhausted": False,
    }
    tasks.update(record.task_id, metadata=record.metadata)

    goal.attach_task(record.task_id)
    goal.current_task_id = record.task_id
    goals.save(goal)
    return {"goal_id": goal.goal_id, "task_id": record.task_id, "kind": kind}


def _resume(sessions: Path) -> dict[str, Any]:
    """A fresh process reads only what is on disk and tries to continue."""
    from core.autonomy import reconcile_on_startup
    from core.goals import GoalStore
    from core.task_store import TaskStore

    goals = GoalStore(sessions)
    tasks = TaskStore(sessions)

    reconciliation = reconcile_on_startup(goals, tasks).to_dict()
    goal = goals.get("goal-restart")

    agent = _agent(_settings(sessions))
    agent._task_store = tasks
    report = agent.resume(goal_id="goal-restart", task_id="task-restart")

    return {
        "reconciliation": reconciliation,
        "goal_state": goal.state.value if goal else None,
        "resume": report.to_dict(),
        # The durable facts a resuming process must be able to see.
        "plan_steps": [
            {"step_id": s.step_id, "tool": s.tool, "status": s.status.value,
             "interrupted": s.interrupted, "replayable": s.is_replayable,
             "side_effect": bool(s.side_effect_key)}
            for s in (tasks.get("task-restart").plan.steps if tasks.get("task-restart") else [])
        ],
    }


def _settings(sessions: Path) -> Any:
    from config.settings import load_settings

    settings = load_settings()
    settings.memory_dir = sessions / "memory"
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("seed", "resume"))
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--kind", default="side_effect",
                        choices=("side_effect", "read_only"))
    args = parser.parse_args(argv)

    sessions = Path(args.sessions)
    sessions.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))

    if args.phase == "seed":
        result = _seed(sessions, args.kind)
    else:
        result = _resume(sessions)
    print("@@RESULT@@" + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())