"""M2.17 - cross-restart continuation, proven across a real process boundary.

Each phase runs in its own interpreter. If any part of the resume path depended on
in-memory state, these tests could not pass - which is exactly the property §28
asks for ("Do not rely on in-memory state").

The central assertion is the negative one: a step left RUNNING by a process death,
whose tool has a side effect, is **not** replayed after the restart.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "tests" / "m217_restart_child.py"


def _run_phase(phase: str, sessions: Path, kind: str = "side_effect") -> dict:
    """Run one phase in a fresh interpreter and return its JSON result."""
    proc = subprocess.run(
        [sys.executable, str(CHILD), "--phase", phase,
         "--sessions", str(sessions), "--kind", kind],
        capture_output=True, text=True, timeout=300,
        cwd=str(REPO_ROOT), encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, (
        f"{phase} phase failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    marker = "@@RESULT@@"
    assert marker in proc.stdout, f"no result from {phase}: {proc.stdout[-2000:]}"
    return json.loads(proc.stdout.split(marker, 1)[1])


class TestCrossRestart:
    def test_uncertain_side_effect_is_never_replayed(self, tmp_path: Path) -> None:
        """The milestone's central safety rule, across a real restart.

        Seed leaves step s2 persisted as RUNNING with a write tool - so its effect
        may or may not have applied. The resuming process must refuse, not re-run.
        """
        sessions = tmp_path / "sessions"
        seeded = _run_phase("seed", sessions, kind="side_effect")
        assert seeded["task_id"] == "task-restart"

        result = _run_phase("resume", sessions, kind="side_effect")
        resume = result["resume"]

        assert resume["action"] == "blocked", (
            f"an uncertain side effect was replayed: {resume}")
        assert resume["refused_steps"] == ["s2"]
        assert "uncertain" in resume["reason"]

        # And the durable plan still says what it always said.
        steps = {s["step_id"]: s for s in result["plan_steps"]}
        assert steps["s1"]["status"] == "done"
        assert steps["s2"]["interrupted"] is True
        assert steps["s2"]["replayable"] is False, (
            "a side-effecting interrupted step reported itself replayable")

    def test_interrupted_read_only_step_is_resumable(self, tmp_path: Path) -> None:
        """The safe counterpart: re-reading a file cannot half-apply."""
        sessions = tmp_path / "sessions"
        _run_phase("seed", sessions, kind="read_only")
        result = _run_phase("resume", sessions, kind="read_only")
        resume = result["resume"]
        assert resume["action"] == "resumed", resume
        assert resume["resumable_steps"] == ["s2"]

    def test_the_resuming_process_learns_the_goal(self, tmp_path: Path) -> None:
        sessions = tmp_path / "sessions"
        _run_phase("seed", sessions, kind="side_effect")
        result = _run_phase("resume", sessions, kind="side_effect")
        # Reconciliation found the goal and made a decision about it.
        assert "goal-restart" in result["reconciliation"]["resumed"] or \
            "goal-restart" in result["reconciliation"]["paused"]
        assert result["goal_state"] is not None

    def test_a_completed_step_is_not_redone(self, tmp_path: Path) -> None:
        sessions = tmp_path / "sessions"
        _run_phase("seed", sessions, kind="read_only")
        result = _run_phase("resume", sessions, kind="read_only")
        steps = {s["step_id"]: s for s in result["plan_steps"]}
        assert steps["s1"]["status"] == "done"
        assert "s1" not in result["resume"]["resumable_steps"], (
            "resume re-offered an already-completed step")

    def test_resume_is_idempotent_across_repeated_restarts(self, tmp_path: Path) -> None:
        """Running the resuming phase twice must reach the same conclusion."""
        sessions = tmp_path / "sessions"
        _run_phase("seed", sessions, kind="side_effect")
        first = _run_phase("resume", sessions, kind="side_effect")["resume"]
        second = _run_phase("resume", sessions, kind="side_effect")["resume"]
        assert first["action"] == second["action"] == "blocked"
        assert first["refused_steps"] == second["refused_steps"]

    def test_no_execution_occurred_during_resume(self, tmp_path: Path) -> None:
        """The child's provider raises if used; reaching resume proves it was not."""
        sessions = tmp_path / "sessions"
        _run_phase("seed", sessions, kind="read_only")
        # If the provider had been called, the child would have exited non-zero.
        _run_phase("resume", sessions, kind="read_only")
        assert not (sessions / "out.txt").exists(), (
            "the resume path wrote to disk, so it executed something")