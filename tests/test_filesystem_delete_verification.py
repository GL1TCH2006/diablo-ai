"""M2.17.x Task 3 - deletion verification: a successful delete must verify PASSED.

Task 6 of Batch 1 found that ``tools/filesystem.py`` verified a delete with a
**presence** assertion::

    Verifier().observe_path(target, expected_type="file", allowed_roots=roots)

``observe_path`` answers "is a file here?". After a successful ``unlink()`` the
honest answer is no, so ``core/verification.py`` returned ``FAILED`` - and
``core/agent.py`` turns a ``FAILED`` verdict into ``TaskState.FAILED``. Measured at
the Agent boundary before this fix::

    file removed from disk : True
    task state             : failed
    task verification      : failed
    reply                  : Task stopped because verification failed.

The user was told their delete had failed when it had succeeded.

The contract these tests pin:

======================================  ==========  =========================
situation                               verdict     why
======================================  ==========  =========================
deleted, and it is gone                 PASSED      absence observed
delete did not take effect, still there FAILED      presence observed
path outside the allowed roots          UNVERIFIED  containment unproven
observation could not be made           UNVERIFIED  no observation, no verdict
``write_file`` left nothing behind      FAILED      a presence assertion
======================================  ==========  =========================

The last row matters: absence is only correct for an assertion that *wants*
absence. A write must still fail when the file is not there, or the fix would have
bought correctness for deletes by breaking writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.permissions import PermissionManager
from config.settings import Settings
from core.events import EventBus
from core.verification import Verifier, VerificationState
from memory.preferences import PreferencesStore
from tools import filesystem


def _roots(tmp_path: Path):
    return [tmp_path]


# ══════════════════════════════════════════════ the verifier, in isolation


def test_absence_of_a_deleted_path_is_passed(tmp_path: Path):
    target = tmp_path / "gone.txt"
    target.write_text("bye\n", encoding="utf-8")
    target.unlink()

    result = Verifier().observe_absence(target, allowed_roots=_roots(tmp_path))

    assert result.state is VerificationState.PASSED, (
        f"a genuinely absent path was not verified absent: {result.detail}")
    assert result.evidence["absent"] is True
    assert result.evidence["observed_type"] == "missing"
    assert result.evidence["observed_independently"] is True


def test_a_path_that_still_exists_is_failed(tmp_path: Path):
    """The negative case: the delete did not take effect."""
    target = tmp_path / "still-here.txt"
    target.write_text("bye\n", encoding="utf-8")

    result = Verifier().observe_absence(target, allowed_roots=_roots(tmp_path))

    assert result.state is VerificationState.FAILED, (
        f"a surviving path was reported as deleted: {result.detail}")
    assert result.evidence["absent"] is False


def test_absence_outside_the_allowed_roots_is_unverified(tmp_path: Path):
    """Containment is checked before observation and refuses rather than guesses."""
    outside = tmp_path.parent / "definitely-outside-the-roots"
    outside.mkdir(exist_ok=True)
    target = outside / "x.txt"
    if not target.exists():
        target.write_text("x\n", encoding="utf-8")

    result = Verifier().observe_absence(target, allowed_roots=_roots(tmp_path))

    assert result.state is VerificationState.UNVERIFIED, (
        f"a path outside the allowed roots produced a hard verdict: {result.detail}")
    assert result.evidence["contained"] is False


def test_absence_with_no_allowed_roots_is_unverified(tmp_path: Path):
    """No containment set means no way to prove the probe was about an allowed path."""
    target = tmp_path / "y.txt"
    target.write_text("y\n", encoding="utf-8")

    result = Verifier().observe_absence(target, allowed_roots=None)

    assert result.state is VerificationState.UNVERIFIED, (
        f"absence was asserted with no containment set: {result.detail}")


def test_absence_evidence_never_carries_content(tmp_path: Path):
    """A secret must not reach evidence through the deletion path either."""
    target = tmp_path / "creds.txt"
    target.write_text("sk-live-TOPSECRETVALUE999\n", encoding="utf-8")
    target.unlink()

    result = Verifier().observe_absence(target, allowed_roots=_roots(tmp_path))
    blob = repr(result.evidence) + result.detail

    assert "TOPSECRET" not in blob, "raw file content leaked into absence evidence"
    assert result.state is VerificationState.PASSED


def test_write_verification_still_fails_when_nothing_was_written(tmp_path: Path):
    """Absence must stay a failure for a *presence* assertion.

    Guards the fix: if absence were universally PASSED, a write that produced
    nothing would silently pass.
    """
    target = tmp_path / "never-written.txt"

    result = Verifier().observe_file_content(
        target, expected_content="payload\n", allowed_roots=_roots(tmp_path))

    assert result.state is VerificationState.FAILED, (
        f"a write that produced no file was not failed: {result.detail}")
    assert result.evidence["exists"] is False


# ══════════════════════════════════════════════ the delete tool


def test_delete_file_verifies_passed(tmp_path: Path):
    settings = Settings(allowed_roots=_roots(tmp_path), backups_dir=tmp_path / "backups")
    target = tmp_path / "doomed.txt"
    target.write_text("bye\n", encoding="utf-8")

    result = filesystem.delete_file(str(target), settings)

    assert result.success is True
    assert not target.exists()
    assert result.verification is not None, (
        "delete_file attached no verification at all")
    assert result.verification.state is VerificationState.PASSED, (
        f"a successful delete did not verify: {result.verification.state.value} - "
        f"{result.verification.detail}")


def test_delete_file_that_did_not_take_effect_fails(tmp_path: Path, monkeypatch):
    """Simulated failed delete: the file survives, so the verdict must be FAILED."""
    settings = Settings(allowed_roots=_roots(tmp_path), backups_dir=tmp_path / "backups")
    target = tmp_path / "survivor.txt"
    target.write_text("still here\n", encoding="utf-8")

    # The mutation is neutered, exactly as a permission denial or a locked file
    # would behave. Everything downstream must still see the truth.
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **kw: None)

    result = filesystem.delete_file(str(target), settings)

    assert result.success is True, "the stub made unlink a no-op, not a failure"
    assert target.exists()
    assert result.verification.state is VerificationState.FAILED, (
        f"a delete that did not happen verified PASSED: {result.verification.detail}")


def test_delete_file_on_a_nonexistent_file_fails_clearly(tmp_path: Path):
    """D1 (decided): deleting what is not there is a FAILURE, not a no-op success.

    Pinned here so the decision cannot drift silently. The reason a reader should
    see is "nothing was deleted", not a traceback and not a generic failure: the
    caller asked for a mutation and none happened, and the tool output has to say
    so. It also keeps "operation failed" distinct from "assertion failed" - there
    is no assertion here, so there is nothing to adjudicate.
    """
    missing = tmp_path / "never-existed.txt"

    result = filesystem.delete_file(str(missing), Settings(
        allowed_roots=_roots(tmp_path), backups_dir=tmp_path / "backups"))

    assert result.success is False, (
        "deleting a file that never existed reported success")
    message = result.error.lower()
    assert "not found" in message or "no such file" in message, (
        f"the failure does not say the file was not found: {result.error!r}")
    # Nothing was deleted, so there is no absence to verify and no verdict.
    assert result.verification is None, (
        "a delete that never ran still produced a verification verdict")
    assert not missing.exists()
    # And the tool did not raise.
    assert isinstance(result.error, str) and result.error


def test_delete_file_outside_the_allowed_roots_is_unverified(tmp_path: Path):
    settings = Settings(allowed_roots=[tmp_path / "allowed"], backups_dir=tmp_path / "backups")
    outside = tmp_path / "outside.txt"
    outside.write_text("bye\n", encoding="utf-8")

    result = filesystem.delete_file(str(outside), settings)

    assert result.verification.state is VerificationState.UNVERIFIED, (
        f"an out-of-root delete produced a hard verdict: "
        f"{result.verification.state.value}")


def test_delete_file_evidence_carries_no_content(tmp_path: Path):
    settings = Settings(allowed_roots=_roots(tmp_path), backups_dir=tmp_path / "backups")
    target = tmp_path / "with-secret.txt"
    target.write_text("api_key=TOPSECRETXYZ\n", encoding="utf-8")

    result = filesystem.delete_file(str(target), settings)
    blob = repr(result.verification.evidence) + result.verification.detail

    assert "TOPSECRET" not in blob, "raw file content leaked into delete evidence"


# ══════════════════════════════════════════════ the Agent, end to end


class _ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)
        self.model_label = "probe"
        self.name = "probe"

    def chat(self, messages, tools=None, model=None):
        return self.script.pop(0) if self.script else _response()


class _Call:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments
        self.id = "c1"


def _response(*calls, text=""):
    from ai.provider import AIResponse

    return AIResponse(text=text, tool_calls=list(calls))


def _agent(tmp_path: Path, script):
    from core.agent import Agent

    settings = Settings(
        base_dir=tmp_path,
        allowed_roots=[tmp_path],
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "data" / "memory",
        logs_dir=tmp_path / "data" / "logs",
        backups_dir=tmp_path / "data" / "backups",
        scratch_dir=tmp_path / "workspace",
        sessions_dir=tmp_path / "data" / "sessions",
        max_conversation_turns=20,
    )
    for directory in (settings.data_dir, settings.memory_dir, settings.logs_dir,
                      settings.backups_dir, settings.scratch_dir):
        directory.mkdir(parents=True, exist_ok=True)
    prefs = PreferencesStore(settings.memory_dir)
    return Agent(
        provider=_ScriptedProvider(script), settings=settings,
        registry=_registry(settings),
        permissions=PermissionManager(settings, prefs), events=EventBus(),
        confirmer=lambda tool, description: "allow", project=None,
        memory_preferences=prefs, model_name=""), settings


def _registry(settings):
    from tools import build_registry

    return build_registry(settings)


def test_a_successful_delete_leaves_the_task_completed(tmp_path: Path):
    """The end-to-end property from Task 6 S1.

    Before the fix this ended ``TaskState.FAILED`` with the reply "Task stopped
    because verification failed." on a delete that had succeeded.
    """
    from core.task_manager import TaskState

    target = tmp_path / "goodbye.txt"
    target.write_text("bye\n", encoding="utf-8")

    agent, settings = _agent(tmp_path, [
        _response(_Call("filesystem.delete_file", {"path": str(target)})),
        _response(text="Deleted it."),
    ])

    reply = agent.respond("delete goodbye.txt")

    assert not target.exists()
    assert agent._tasks.state is TaskState.COMPLETED, (
        f"a successful delete ended the task {agent._tasks.state.value}: {reply}")
    assert agent._tasks.verification is VerificationState.PASSED, (
        f"a successful delete verified {agent._tasks.verification.value}")
    assert agent._tasks.terminal_violation() == ""
    assert agent._tasks.task_succeeded() is True