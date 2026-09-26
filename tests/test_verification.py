from config.permissions import Decision, PermissionLevel
from core.task_manager import TaskManager
from core.verification import VerificationState, Verifier


def test_check_file(tmp_path):
    verifier = Verifier()
    ok, _ = verifier.check_file(tmp_path / "missing.py")
    assert ok is False
    target = tmp_path / "x.py"
    target.write_text("print(1)\n", encoding="utf-8")
    ok, msg = verifier.check_file(target)
    assert ok is True
    assert "bytes" in msg


def test_check_output():
    verifier = Verifier()
    assert verifier.check_output("some text HELLO World here", "hello world") is True
    assert verifier.check_output("nothing here", "hello world") is False


def test_check_run_success():
    verifier = Verifier()
    decision = Decision(PermissionLevel.NORMAL, allow=True, requires_confirmation=False)
    ok, _ = verifier.check_run(decision, 0, "Hello World", "hello world")
    assert ok is True


def test_check_run_failures():
    verifier = Verifier()
    decision = Decision(PermissionLevel.NORMAL, allow=True, requires_confirmation=False)
    ok, msg = verifier.check_run(decision, 1, "", None)
    assert ok is False and "code 1" in msg
    ok, msg = verifier.check_run(decision, 0, "different", "hello")
    assert ok is False and "expected" in msg


# --- verification evidence boundary -------------------------------------
#
# `verification_evidence` is a mapping by contract and is persisted into the
# task record. A capability that reports a bare list/tuple/string evidence
# payload used to reach dict() here and raise, aborting task persistence.


def test_set_verification_stores_mapping_evidence():
    tasks = TaskManager()
    tasks.set_verification(VerificationState.PASSED, "ok", {"path": "C:/x.png"})
    assert tasks.verification_evidence == {"path": "C:/x.png"}


def test_set_verification_rejects_non_mapping_evidence_without_raising():
    tasks = TaskManager()
    for payload in ([1, 2, 3], ["a", "b"], ("a", "b"), "text", 7):
        tasks.set_verification(VerificationState.PASSED, "ok", payload)
        assert tasks.verification_evidence == {}
        assert tasks.verification is VerificationState.PASSED


def test_capability_list_evidence_crash_is_reachable_and_now_survivable():
    """Regression: the real bridge used to emit list evidence that crashed here."""
    from integration.contracts import (
        CapabilityRegistry,
        CapabilityRequest,
        CapabilityResult,
        CapabilityStatus,
    )
    from tools.capability_bridge import CapabilityBridge

    bridge = CapabilityBridge(CapabilityRegistry())
    result = CapabilityResult(
        status=CapabilityStatus.SUCCESS,
        capability="mock",
        operation="execute",
        data={"verified": True, "evidence": [1, 2, 3]},
    )
    verification = bridge._verification(
        result, CapabilityRequest(capability="mock", operation="execute")
    )
    assert verification is not None
    tasks = TaskManager()
    # Must not raise: this is the exact path that aborted persistence.
    tasks.set_verification(verification.state, verification.detail, verification.evidence)
    assert tasks.verification_evidence == {}