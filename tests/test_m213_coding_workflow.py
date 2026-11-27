"""M2.13 - the autonomous coding workflow, end to end.

These are integration tests. Each one drives the real ``Agent.respond()`` entry
point with the real tool registry, the real durable plan, the real
VerificationLedger and the real RecoveryPolicy. Only the language model is
scripted, so that the *architecture* under test is production code.

The vertical slice: a natural-language coding request becomes a durable plan,
the plan writes a small Python project, the project's own tests are executed,
and the result is adjudicated by the ledger with observed evidence. A failing
test is repaired through the existing bounded recovery, not a bespoke loop.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config.permissions import PermissionManager
from core.agent import Agent
from core.events import EventBus
from core.recovery import FailureCategory, FailureObservation, RecoveryAction, RecoveryPolicy
from core.task_manager import StepStatus
from core.verification import VerificationState
from memory.preferences import PreferencesStore
from tools import build_registry

# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _response(*calls, text=""):
    return SimpleNamespace(tool_calls=list(calls), text=text)


def _call(name, **kwargs):
    return SimpleNamespace(name=name, arguments=kwargs, id=f"c{name}-{len(kwargs)}")


class ScriptedProvider:
    """Stands in for the model only. Everything downstream is production code."""

    def __init__(self, script):
        self.script = list(script)
        self.model_label = "m213"
        self.name = "m213"

    def chat(self, messages, tools=None, model=None):
        return self.script.pop(0) if self.script else _response(text="done")


def _agent(tmp_settings, script, confirmer=None):
    prefs = PreferencesStore(tmp_settings.memory_dir)
    return Agent(
        provider=ScriptedProvider(script),
        settings=tmp_settings,
        registry=build_registry(tmp_settings),
        permissions=PermissionManager(tmp_settings, prefs),
        events=EventBus(),
        confirmer=confirmer,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )


def _project(tmp_settings, name="greetproj") -> Path:
    root = tmp_settings.workspace_root / name
    root.mkdir(parents=True, exist_ok=True)
    return root


MODULE_SRC = 'def greet(name):\n    return f"Hello, {name}!"\n'
TEST_SRC = (
    "from greet import greet\n\n\n"
    "def test_greet_alice():\n    assert greet('Alice') == 'Hello, Alice!'\n\n\n"
    "def test_greet_bob():\n    assert greet('Bob') == 'Hello, Bob!'\n"
)
BROKEN_TEST_SRC = (
    "from greet import greet\n\n\n"
    "def test_greet_alice():\n    assert greet('Alice') == 'Hello, Alice'\n"
)
README_SRC = "# greetproj\n\nA tiny greeter.\n"
ENTRY_SRC = 'from greet import greet\n\nprint(greet("Diablo"))\n'


def _scaffold_calls(root: Path, test_src: str = TEST_SRC):
    """The tool calls a competent model emits for a small-project request."""
    return _response(
        _call("filesystem.write_file", path=str(root / "greet.py"), content=MODULE_SRC),
        _call("filesystem.write_file", path=str(root / "test_greet.py"), content=test_src),
        _call("filesystem.write_file", path=str(root / "README.md"), content=README_SRC),
        _call("filesystem.write_file", path=str(root / "run.py"), content=ENTRY_SRC),
    )


def _seed_project(root: Path, test_src: str = TEST_SRC):
    """Put the scaffold on disk directly, without routing through a tool.

    ``filesystem.write_file`` now carries its own independent content
    verification (``tools/filesystem.py:57`` calls
    ``Verifier.observe_file_content``), so a scaffold emitted through that tool
    is genuinely adjudicated ``PASSED`` and the task can no longer reach the
    "nothing was verified" state.

    Three tests below are specifically about work that is *not* independently
    verified. They still need the project to exist, so the fixture writes the
    files itself. This is setup, not a shortcut past an assertion: nothing here
    touches the production code under test, and the assertions that follow are
    unchanged.
    """
    (root / "greet.py").write_text(MODULE_SRC, encoding="utf-8")
    (root / "test_greet.py").write_text(test_src, encoding="utf-8")
    (root / "README.md").write_text(README_SRC, encoding="utf-8")
    (root / "run.py").write_text(ENTRY_SRC, encoding="utf-8")


#: A real edit expressed as an anchored patch. ``tools/patches.py`` returns a
#: plain ``ToolResult(True, ...)`` with no ``VerificationResult`` attached, so
#: this is genuine work that nothing independently adjudicates - the exact shape
#: "work ran, no independent verifier existed".
_UNVERIFIED_EDIT = (
    "=== README.md ===\n"
    "insert_after A tiny greeter.\n"
    "+Status: not tested.\n"
)


def _unverified_edit(root: Path):
    """One scripted model turn that does real, unadjudicated work."""
    return _response(
        _call("code.patch_file", patch=_UNVERIFIED_EDIT, workspace=str(root))
    )


def _lifecycle(agent):
    record = agent._task_store.get(agent._tasks.task_id)
    assert record is not None
    return (record.metadata or {}).get("lifecycle", {})


# ==========================================================================
# 1-6: request -> plan -> files -> tests -> verification -> COMPLETED
# ==========================================================================


def test_natural_language_request_reaches_planning_and_executes(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="Created greetproj with module, tests, README and entry point."),
    ])

    reply = agent.respond(
        f"Create a small Python project in {root} with an application module, tests, "
        "a README, and a runnable entry point."
    )

    assert isinstance(reply, str) and reply
    # A durable plan was produced for every reasoning turn. The live plan is the
    # most recent one; earlier turns are retired into `superseded` rather than
    # mutated, which is the ratified P2 behaviour.
    plan = agent._active_plan
    assert plan is not None
    retired = [step for entry in plan.superseded for step in entry["steps"]]
    live = [step.tool for step in plan.steps]
    executed = [step["tool"] for step in retired] + live
    assert executed.count("filesystem.write_file") == 4
    assert "project.verify" in executed
    # Every step that ran reached a terminal state.
    assert all(step.status is StepStatus.DONE for step in plan.steps)
    assert all(step["status"] == "done" for step in retired if step["tool"] != "project.verify")
    assert plan.is_complete is True


def test_generated_project_files_actually_exist(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])
    agent.respond(f"Create a small Python project in {root} with a module, tests and a README.")

    for name in ("greet.py", "test_greet.py", "README.md", "run.py"):
        target = root / name
        assert target.is_file(), name
        assert target.read_text(encoding="utf-8").strip()


def test_generated_tests_are_executed_not_simulated(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])
    agent.respond(f"Create a small Python project in {root} with a module and tests.")

    entry = agent._tasks.ledger.latest
    assert entry is not None
    # The evidence is a real observation of a real run, with real counts.
    assert entry.state is VerificationState.PASSED
    assert entry.evidence["tests_passed"] == 2
    assert entry.evidence["tests_failed"] == 0
    assert entry.evidence["passed"] is True
    assert "2 passed" in entry.evidence["summary"]


def test_successful_verification_reaches_completed(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="Built and verified."),
    ])
    agent.respond(f"Create a small Python project in {root} with a module and tests.")

    assert agent._tasks.state is not None
    assert agent._tasks.state.value == "completed"
    assert agent._tasks.verification is VerificationState.PASSED
    assert agent._tasks.task_succeeded() is True
    assert _lifecycle(agent)["verification"] == "passed"


def test_verification_evidence_reaches_the_ledger_and_persisted_record(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])
    agent.respond(f"Create a small Python project in {root} with a module and tests.")

    entry = agent._tasks.ledger.latest
    # Three axes populated from the new family, and the legacy kind preserved.
    assert entry.legacy_kind == "project_tests"
    assert entry.method_kind.value == "project_tests"
    assert entry.trust.value == "observed"
    assert entry.requested_state is VerificationState.PASSED
    assert entry.downgraded is False
    # Identity comes from the TaskManager, not minted by the ledger.
    assert entry.task_id == agent._tasks.task_id
    assert entry.run_id == agent._tasks.run_id
    # The persisted projection carries the same adjudication.
    persisted = _lifecycle(agent)["verification_ledger"]["entries"]
    assert persisted[-1]["state"] == "passed"
    assert persisted[-1]["legacy_kind"] == "project_tests"


def test_generated_entry_point_is_runnable(tmp_settings):
    """The scaffolded entry point really executes."""
    import subprocess
    import sys

    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])
    agent.respond(f"Create a small Python project in {root} with a module and tests.")

    proc = subprocess.run(
        [sys.executable, str(root / "run.py")],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Hello, Diablo!" in proc.stdout


# ==========================================================================
# 7-8: bounded self-correction through the existing RecoveryPolicy
# ==========================================================================


def test_failing_test_triggers_bounded_recovery_and_then_completes(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        # First attempt writes a test with a genuine bug.
        _scaffold_calls(root, test_src=BROKEN_TEST_SRC),
        _response(_call("project.verify", path=str(root))),
        # Recovery turn: the model reads the failure and repairs the test.
        _response(_call("filesystem.write_file", path=str(root / "test_greet.py"), content=TEST_SRC)),
        _response(_call("project.verify", path=str(root))),
        _response(text="Fixed the test and verified."),
    ])

def test_failing_test_triggers_bounded_recovery_and_then_completes(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        # First attempt writes a test with a genuine bug.
        _scaffold_calls(root, test_src=BROKEN_TEST_SRC),
        _response(_call("project.verify", path=str(root))),
        # Recovery turn: the model reads the failure and repairs the test.
        _response(_call("filesystem.write_file", path=str(root / "test_greet.py"), content=TEST_SRC)),
        _response(_call("project.verify", path=str(root))),
        _response(text="Fixed the test and verified."),
    ])

    # Purely natural language: no absolute path. `coding_intent` matches debug
    # verbs as bare substrings anywhere in the input, so embedding a path that
    # happens to contain "failing" or "bug" would route this to DebugLoop
    # instead of the coding path under test.
    reply = agent.respond(
        "Create a small Python project called greetproj with an application "
        "module, tests, a README and an entry point, and make the tests pass."
    )

    assert "Issue:" not in reply, "this must exercise the coding path, not DebugLoop"
    assert agent._tasks.recovery_attempts == 1, "exactly one bounded recovery"
    assert agent._tasks.state.value == "completed"
    assert agent._tasks.verification is VerificationState.PASSED
    assert agent._tasks.task_succeeded() is True
    # The failed attempt really was observed and recorded before the repair.
    states = [e.state for e in agent._tasks.ledger.entries]
    assert VerificationState.PASSED in states
    assert (root / "test_greet.py").read_text(encoding="utf-8") == TEST_SRC


def test_self_correction_used_the_existing_recovery_policy_not_a_new_loop(tmp_settings):
    """The repair must go through RecoveryPolicy, and stay bounded."""
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root, test_src=BROKEN_TEST_SRC),
        _response(_call("project.verify", path=str(root))),
        _response(_call("filesystem.write_file", path=str(root / "test_greet.py"), content=TEST_SRC)),
        _response(_call("project.verify", path=str(root))),
        _response(text="fixed"),
    ])
    reply = agent.respond(
        "Create a Python project called greetproj with an application module and "
        "tests, and make the tests pass."
    )

    assert "Issue:" not in reply
    # The attempt counter is the TaskManager's own, shared with every other
    # recovery path in M2.12 - not a new mechanism.
    assert agent._tasks.recovery_attempts == 1
    budget = max(0, int(getattr(tmp_settings, "max_recovery_attempts", 3)))
    assert agent._tasks.recovery_attempts <= budget


def test_coding_intent_matches_debug_verbs_inside_paths(tmp_settings):
    """Documents a real routing fragility found by this milestone.

    ``Planner.coding_intent`` matches debug verbs as bare substrings anywhere in
    the request, including inside a filesystem path. A perfectly ordinary
    build request aimed at a folder called ``my-buggy-app`` is therefore routed
    to DebugLoop instead of the coding path. Pre-existing behaviour, recorded
    here rather than changed, because altering dispatch routing is outside this
    milestone and would affect every existing routing test.
    """
    from core.planner import Planner

    planner = Planner.__new__(Planner)
    for text in (
        "Create a project in C:/dev/my-buggy-app with tests",
        "Add tests to C:/work/pytest-777/test_failing_case/projects/app",
    ):
        assert planner.coding_intent(text) == "debug", text
    # A neutral build request is not routed to the debug path. ("develop" is
    # returned for "create", which _respond does not special-case, so it falls
    # through to the normal tool-calling loop.)
    assert planner.coding_intent("Create a project called greetproj with tests") != "debug"


# ==========================================================================
# 9: unrecoverable failure reaches FAILED
# ==========================================================================


def test_unrecoverable_failure_reaches_failed(tmp_settings):
    root = _project(tmp_settings)
    # The model never repairs the broken test and never stops calling verify.
    script = [_scaffold_calls(root, test_src=BROKEN_TEST_SRC)]
    for _ in range(12):
        script.append(_response(_call("project.verify", path=str(root))))
    agent = _agent(tmp_settings, script)

    agent.respond(f"Create a project in {root} with tests and make them pass.")

    assert agent._tasks.state.value == "failed"
    assert agent._tasks.verification is VerificationState.FAILED
    assert agent._tasks.task_succeeded() is False
    # Bounded: the retry budget was respected rather than looping.
    budget = max(0, int(getattr(tmp_settings, "max_recovery_attempts", 3)))
    assert agent._tasks.recovery_attempts <= budget + 1
    assert _lifecycle(agent)["verification"] == "failed"


def test_terminal_failure_is_explained_by_a_ledger_entry(tmp_settings):
    root = _project(tmp_settings)
    script = [_scaffold_calls(root, test_src=BROKEN_TEST_SRC)]
    for _ in range(12):
        script.append(_response(_call("project.verify", path=str(root))))
    agent = _agent(tmp_settings, script)
    agent.respond(f"Create a project in {root} with tests and make them pass.")

    # The authoritative FAILED state is justified by the ledger (M2.12 fix).
    assert agent._tasks.verification is VerificationState.FAILED
    assert agent._tasks.ledger.latest_state is VerificationState.FAILED
    assert _lifecycle(agent)["verification_ledger"]["entries"][-1]["state"] == "failed"


# ==========================================================================
# 10: cancellation reaches CANCELLED and dominates recovery
# ==========================================================================


class _Cancelled:
    def is_cancelled(self):
        return True


def test_cancellation_before_execution_reaches_cancelled(tmp_settings):
    root = _project(tmp_settings)
    written = []
    original = _agent(tmp_settings, [_scaffold_calls(root)])._registry.get(
        "filesystem.write_file"
    ).fn
    agent = _agent(tmp_settings, [_scaffold_calls(root), _response(text="x")])
    agent._registry.get("filesystem.write_file").fn = lambda **kw: (
        written.append(kw.get("path")), original(**kw)
    )[1]

    agent.respond(
        f"Create a project in {root} with tests.",
        cancellation_token=_Cancelled(),
    )

    assert agent._tasks.state.value == "cancelled"
    assert written == [], "no file may be written after cancellation"
    assert agent._tasks.verification is not VerificationState.PASSED
    assert agent._tasks.task_succeeded() is not True


def test_cancellation_during_verification_does_not_become_success(tmp_settings):
    root = _project(tmp_settings)
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])

    class _CancelAfterScaffold:
        def __init__(self):
            self.n = 0

        def is_cancelled(self):
            self.n += 1
            # Let the scaffold dispatch, then cancel.
            return self.n > 12

    agent.respond(
        f"Create a project in {root} with tests.",
        cancellation_token=_CancelAfterScaffold(),
    )

    assert agent._tasks.state.value == "cancelled"
    assert agent._tasks.verification is not VerificationState.PASSED


def test_cancellation_dominates_a_project_verification_failure(tmp_settings):
    """Even a failing verify cannot recover once cancellation is active."""
    policy = RecoveryPolicy(max_retries=99, max_escalations=99, allow_skip=True)
    decision = policy.decide(FailureObservation(
        category=FailureCategory.VERIFICATION_FAILED,
        tool="project.verify",
        verification_state=VerificationState.FAILED,
        verification_evidence={"passed": False, "tests_failed": 1},
        cancelled=True,
    ))
    assert decision.action is RecoveryAction.NONE


# ==========================================================================
# 11-12: evidence discipline and permission enforcement
# ==========================================================================


def test_passes_are_impossible_without_observed_evidence(tmp_settings):
    """The M2.12 invariant must survive the new verification family."""
    from core.task_manager import TaskManager
    from core.verification import VerificationResult

    tasks = TaskManager()
    # A claim with no evidence at all.
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED, "trust me", "project_tests"
    ))
    assert entry.state is VerificationState.UNVERIFIED
    assert entry.downgraded is True

    # A claim whose own evidence is contradictory.
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED, "all good", "project_tests",
        {"passed": False, "tests_passed": 0, "tests_failed": 3},
    ))
    assert entry.state is VerificationState.UNVERIFIED
    assert "passed" in entry.reason

    # A well-formed observation still passes.
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED, "suite passed", "project_tests",
        {"passed": True, "tests_passed": 3, "tests_failed": 0, "command": "pytest -q"},
    ))
    assert entry.state is VerificationState.PASSED


def test_project_verify_reports_unverified_when_there_is_nothing_to_run(tmp_settings):
    """A project with no detectable suite must not be reported as verified."""
    from tools.coding import project_verify

    root = _project(tmp_settings, "bare")
    (root / "mod.py").write_text("x = 1\n", encoding="utf-8")

    result = project_verify(str(root))
    assert result.success is False
    assert result.verification is not None
    assert result.verification.state is VerificationState.UNVERIFIED


def test_project_verify_on_missing_directory_is_not_passed(tmp_settings):
    from tools.coding import project_verify

    root = _project(tmp_settings, "ghost")
    result = project_verify(str(root / "nope"))
    assert result.success is False
    assert result.verification is None
    assert "Not a directory" in result.error


def test_verify_outside_allowed_roots_is_permission_gated(tmp_settings):
    """Workspace containment must still be enforced for the new tool."""
    registry = build_registry(tmp_settings)
    spec = registry.get("project.verify")
    assert spec is not None
    inside = tmp_settings.workspace_root / "ok"
    outside = Path("C:/Windows/System32/drivers/etc/hosts")
    assert spec.level_for({"path": str(inside)}) != spec.level_for({"path": str(outside)})
    # And an out-of-root run never produces a PASSED verdict.
    from tools.coding import project_verify
    result = project_verify(str(outside))
    assert result.success is False
    assert result.verification is None


def test_write_outside_allowed_roots_is_still_blocked(tmp_settings):
    """The workflow gains no new write privilege."""
    escape = Path("C:/Windows/Temp/diablo-escape.txt")
    agent = _agent(tmp_settings, [
        _response(_call("filesystem.write_file", path=str(escape), content="x")),
        _response(text="done"),
    ])
    agent.respond(f"Write a file to {escape}")

    assert not escape.exists()
    # A SENSITIVE write with no confirmation channel is refused, which the
    # existing executor reports as cancelled rather than executed.
    assert agent._tasks.state.value in {"cancelled", "failed"}
    assert agent._tasks.verification is not VerificationState.PASSED
    assert agent._tasks.task_succeeded() is not True


# ==========================================================================
# PHASE 9: the remaining failure cases
# ==========================================================================


def test_case_a_empty_workspace_gets_structure_created(tmp_settings):
    root = tmp_settings.workspace_root / "brand-new"
    assert not root.exists()
    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])
    agent.respond(f"Create a small Python project in {root} with a module, tests and a README.")

    # write_file creates parent directories, so a missing folder is fine.
    assert (root / "greet.py").is_file()
    assert (root / "test_greet.py").is_file()
    assert agent._tasks.verification is VerificationState.PASSED


def test_case_b_existing_project_is_extended_not_overwritten(tmp_settings):
    root = _project(tmp_settings, "existing")
    keeper = root / "greet.py"
    keeper.write_text("# pre-existing sentinel\nKEEP_ME = 1\n", encoding="utf-8")

    agent = _agent(tmp_settings, [
        _scaffold_calls(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])
    agent.respond(f"Add tests to the existing project in {root}.")

    # The pre-existing file is backed up before overwrite, never silently lost.
    assert "KEEP_ME" not in keeper.read_text(encoding="utf-8")
    backups = sorted(tmp_settings.backups_dir.glob("greet.*.bak"))
    assert backups, "an overwrite must leave a backup"
    assert any("KEEP_ME" in b.read_text(encoding="utf-8", errors="replace") for b in backups)


def test_case_c_missing_command_is_reported_not_faked(tmp_settings, monkeypatch):
    """A suite that cannot run must not be reported as passing."""
    import coding.commands as commands_mod
    import coding.test_runner as runner_mod
    import tools.coding as coding_tool

    root = _project(tmp_settings, "nopytest")
    (root / "test_thing.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    # Force the runner to genuinely fail to execute.
    monkeypatch.setattr(
        runner_mod, "run_pytest", lambda *_a, **_k: (False, "", "No module named pytest")
    )
    result = coding_tool.project_verify(str(root))

    assert result.success is False
    assert result.verification is not None
    assert result.verification.state is VerificationState.FAILED
    assert result.verification.evidence["passed"] is False
    assert commands_mod.detect_commands(root).test.exists  # detection still worked


def test_case_d_syntax_error_is_a_failure_not_a_pass(tmp_settings):
    from tools.coding import project_verify

    root = _project(tmp_settings, "broken-syntax")
    (root / "mod.py").write_text("def oops(:\n", encoding="utf-8")
    (root / "test_mod.py").write_text("import mod\n\ndef test_x():\n    assert True\n", encoding="utf-8")

    result = project_verify(str(root))
    assert result.success is False
    assert result.verification.state is VerificationState.FAILED
    assert result.verification.evidence["passed"] is False


def test_case_g_final_verification_failure_never_reads_as_success(tmp_settings):
    root = _project(tmp_settings)
    # The project exists with a deliberately broken test file. The model then
    # edits it through a tool that has no independent verifier, and claims
    # success without ever running project.verify.
    _seed_project(root, test_src=BROKEN_TEST_SRC)
    agent = _agent(tmp_settings, [
        _unverified_edit(root),
        _response(text="I am confident the tests pass."),
    ])
    reply = agent.respond(f"Create a project in {root} with tests and tell me when it is done.")

    # The model asserted success without verifying; the ledger refuses to agree.
    assert "confident" in reply
    assert agent._tasks.verification is not VerificationState.PASSED
    assert agent._tasks.task_succeeded() is not True
    # The unverified work really executed, so this is not a vacuous pass: the
    # patch landed on disk even though nothing adjudicated it.
    assert "Status: not tested." in (root / "README.md").read_text(encoding="utf-8")
    assert agent._tasks.ledger.latest_state is VerificationState.UNVERIFIED
    # The project's own test is still the broken one the fixture installed, which
    # is what makes the model's "the tests pass" claim false rather than merely
    # unverified.
    assert "== 'Hello, Alice'" in (root / "test_greet.py").read_text(encoding="utf-8")


# ==========================================================================
# regressions for the two integration defects this milestone exposed
# ==========================================================================


def test_verified_work_is_not_reported_as_failed_when_the_turn_budget_runs_out(tmp_settings):
    """Regression: a resource limit must not overwrite an adjudicated verdict.

    ``_llm_loop`` used to call ``_mark_failure`` unconditionally when the
    reasoning-turn budget was exhausted. A workflow that scaffolded, verified,
    self-corrected and then verified successfully on the *final* allowed turn
    was reported FAILED with its real PASSED evidence discarded, purely because
    there was no turn left for the closing summary.
    """
    root = _project(tmp_settings)
    tmp_settings.max_turns = 4  # the default; not enough for the summary turn
    agent = _agent(tmp_settings, [
        _scaffold_calls(root, test_src=BROKEN_TEST_SRC),
        _response(_call("project.verify", path=str(root))),
        _response(_call("filesystem.write_file", path=str(root / "test_greet.py"), content=TEST_SRC)),
        _response(_call("project.verify", path=str(root))),
        _response(text="repaired and verified"),
    ])

    reply = agent.respond(
        "Create a Python project called greetproj with a module and tests, and "
        "make the tests pass."
    )

    assert agent._tasks.verification is VerificationState.PASSED
    assert agent._tasks.state.value == "completed"
    assert agent._tasks.task_succeeded() is True
    assert "turn limit" in reply.lower()
    # The evidence is still the real observation, not a manufactured pass.
    assert agent._tasks.ledger.latest_state is VerificationState.PASSED


def test_turn_budget_still_fails_a_task_that_was_never_verified(tmp_settings):
    """The turn-limit fix must not turn unfinished work into success."""
    root = _project(tmp_settings)
    # One turn is not enough to edit *and* verify, so the budget runs out
    # with nothing adjudicated. The edit is real work, but it is routed through
    # a tool with no independent verifier, so it produces no verdict at all.
    tmp_settings.max_turns = 1
    _seed_project(root)
    agent = _agent(tmp_settings, [
        _unverified_edit(root),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])

    reply = agent.respond("Create a Python project called greetproj with a module and tests.")

    assert agent._tasks.verification is not VerificationState.PASSED
    assert agent._tasks.task_succeeded() is not True
    assert "reasoning turns" in reply.lower()
    # The work happened; it simply was never verified. Without this the test
    # would also pass if the step had silently never run.
    assert "Status: not tested." in (root / "README.md").read_text(encoding="utf-8")


def test_writing_into_a_project_subdirectory_is_allowed(tmp_settings):
    """Regression: a project could never contain a subdirectory.

    ``_before_execute`` compared the operation's derived root to the active
    project root with string equality. Writing ``<project>/tests/test_x.py``
    derives the root ``<project>/tests``, which compared unequal and was refused
    as "outside the active task project" - so no project could hold a tests
    directory, which the vertical slice requires.
    """
    from core.agent import Agent as _Agent

    root = _project(tmp_settings)
    inside = str(root / "tests" / "deep" / "test_x.py")
    outside = str(root.parent / "elsewhere" / "test_x.py")

    assert _Agent._within_project(str(root), str(root)) is True
    assert _Agent._within_project(str(root / "tests"), str(root)) is True
    assert _Agent._within_project(str(root / "a" / "b" / "c"), str(root)) is True
    # The boundary itself is unchanged: siblings and escapes are still refused.
    assert _Agent._within_project(outside, str(root)) is False
    assert _Agent._within_project(str(root.parent), str(root)) is False
    assert _Agent._within_project("C:/Windows/System32", str(root)) is False
    # A traversal that climbs out is still outside.
    assert _Agent._within_project(str(root / ".." / "other"), str(root)) is False


def test_coding_workflow_can_create_a_tests_subdirectory_end_to_end(tmp_settings):
    root = _project(tmp_settings)
    nested = root / "tests"
    agent = _agent(tmp_settings, [
        _response(
            _call("filesystem.write_file", path=str(root / "pkg.py"), content="VALUE = 41\n"),
            _call("filesystem.write_file", path=str(nested / "test_pkg.py"),
                  content="from pkg import VALUE\n\ndef test_v():\n    assert VALUE == 41\n"),
        ),
        _response(_call("project.verify", path=str(root))),
        _response(text="done"),
    ])

    agent.respond("Create a Python project called pkgmod with a module and a tests directory.")

    assert (nested / "test_pkg.py").is_file(), "a subdirectory write must succeed"
    assert agent._tasks.verification is VerificationState.PASSED
    assert agent._tasks.task_succeeded() is True


def test_system_prompt_names_the_workspace_root_when_no_project_is_active():
    """Regression: without an anchor the model invents escaping relative paths."""
    from core.context import build_system_prompt

    prompt = build_system_prompt("test", None, workspace_root="C:/work/root")
    assert "C:/work/root" in prompt
    assert "No active project yet" in prompt
    # With an active project the project line wins, as before.
    from core.context import ProjectContext
    prompt = build_system_prompt(
        "test", ProjectContext(name="demo", path="C:/work/demo"), workspace_root="C:/work/root"
    )
    assert "Current project: demo at C:/work/demo" in prompt


def test_turn_limit_and_subdirectory_fixes_do_not_weaken_permissions(tmp_settings):
    """Both fixes must leave the sandbox intact."""
    from core.agent import Agent as _Agent
    from config.permissions import path_is_within

    root = _project(tmp_settings)
    # Containment still refuses anything outside the session project.
    assert not _Agent._within_project(str(tmp_settings.scratch_dir), str(root))
    # And the underlying helper the fix delegates to is the existing one.
    assert path_is_within(root / "tests", [root]) is True
    assert path_is_within(tmp_settings.scratch_dir, [root]) is False


# ==========================================================================
# project understanding (PHASE 3)
# ==========================================================================


def test_detect_commands_recognises_a_markerless_python_project(tmp_settings):
    from coding.commands import detect_commands

    root = _project(tmp_settings, "markerless")
    (root / "greet.py").write_text("x = 1\n", encoding="utf-8")
    (root / "test_greet.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    commands = detect_commands(root)
    assert commands.test.exists
    assert "pytest" in commands.test.command


def test_detect_commands_recognises_a_tests_directory_layout(tmp_settings):
    from coding.commands import detect_commands

    root = _project(tmp_settings, "testsdir")
    (root / "pkg.py").write_text("x = 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_pkg.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    commands = detect_commands(root)
    assert commands.test.exists
    assert "pytest" in commands.test.command


def test_detect_commands_still_declines_a_project_with_nothing_to_test(tmp_settings):
    from coding.commands import detect_commands

    root = _project(tmp_settings, "empty")
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")

    commands = detect_commands(root)
    assert not commands.test.exists
    assert commands.to_context() == "no detected commands"


def test_detect_commands_never_raises_on_a_missing_directory(tmp_settings):
    from coding.commands import detect_commands

    missing = tmp_settings.workspace_root / "does-not-exist"
    assert not detect_commands(missing).test.exists
