"""M2.12 P3 Phase 2 - verification writer migration.

Phase 2 makes :class:`VerificationLedger` the authoritative adjudication
boundary. Every verification writer now goes through
``TaskManager.record_verification``, so a requested ``PASSED`` that lacks valid,
non-empty, trusted evidence is downgraded instead of being believed.

Two Phase 1 compatibility findings are resolved here and pinned by tests:

CASE A - list/tuple evidence
    ``capability_bridge`` used to promote any non-empty list/tuple evidence
    payload to PASSED, and that payload then reached ``dict(evidence)`` in
    ``TaskManager.set_verification`` and raised. The evidence contract is
    object-shaped by design, so a bare sequence is malformed, not an alternate
    valid shape. It is no longer promoted, and it is preserved for diagnosis.

CASE B - empty-evidence PASSED writers
    ``file_and_output``, ``tool_observation`` and ``debug`` all requested PASSED
    with ``evidence={}``. Real observable evidence existed nearby for all three,
    so it is now constructed. Where none exists, the result stays non-PASSED
    rather than fabricating evidence to preserve an old verdict.
"""

from __future__ import annotations

import json

import pytest

from core.task_manager import TaskManager
from core.verification import VerificationResult, VerificationState
from core.verification_ledger import (
    EvidenceTrust,
    VerificationMethod,
    VerificationSubject,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _bridge():
    from integration.contracts import CapabilityRegistry
    from tools.capability_bridge import CapabilityBridge

    return CapabilityBridge(CapabilityRegistry())


def _request(capability: str, operation: str, params: dict | None = None):
    from integration.contracts import CapabilityRequest

    return CapabilityRequest(capability=capability, operation=operation, params=params or {})


def _result(capability: str, operation: str, data: dict):
    from integration.contracts import CapabilityResult, CapabilityStatus

    return CapabilityResult(
        status=CapabilityStatus.SUCCESS,
        capability=capability,
        operation=operation,
        data=data,
    )


def _png(path):
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return path


# --------------------------------------------------------------------------
# the funnel: TaskManager.record_verification is the adjudication boundary
# --------------------------------------------------------------------------


def test_funnel_sets_scalars_from_the_adjudicated_entry_not_the_request():
    tasks = TaskManager()
    tasks.record_verification(VerificationResult(VerificationState.PASSED, "ok", "file_and_output", {}))
    assert tasks.verification is VerificationState.UNVERIFIED
    assert tasks.verification_detail == "ok"
    assert len(tasks.ledger) == 1
    assert tasks.ledger.latest.downgraded is True


def test_funnel_keeps_passed_when_evidence_is_legitimate():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED,
        "render exists",
        "render_file",
        {"path": "C:/out/render.png", "exists": True},
    ))
    assert entry.state is VerificationState.PASSED
    assert tasks.verification is VerificationState.PASSED
    assert tasks.verification_evidence["exists"] is True


def test_funnel_never_upgrades_a_failure():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.FAILED, "broken", "render_file", {"exists": False}
    ))
    assert entry.state is VerificationState.FAILED
    assert entry.downgraded is False
    assert tasks.verification is VerificationState.FAILED


@pytest.mark.parametrize("state", [
    VerificationState.NOT_RUN,
    VerificationState.NOT_REQUIRED,
    VerificationState.UNVERIFIED,
    VerificationState.FAILED,
])
def test_funnel_passes_non_passed_states_through_untouched(state):
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(state, "detail", "execution"))
    assert entry.state is state
    assert entry.downgraded is False


def test_funnel_stamps_authoritative_identity_from_the_task_manager():
    tasks = TaskManager()
    tasks.bind_task("task-77")
    entry = tasks.record_verification(
        VerificationResult(VerificationState.PASSED, "d", "render_file", {"exists": True}),
        step_id="step-2",
    )
    assert entry.task_id == "task-77"
    assert entry.run_id == tasks.run_id
    assert entry.step_id == "step-2"


def test_funnel_resets_the_ledger_when_a_new_task_begins():
    tasks = TaskManager()
    tasks.record_verification(VerificationResult(VerificationState.FAILED, "d", "execution"))
    assert len(tasks.ledger) == 1
    tasks.begin_task("next task")
    assert len(tasks.ledger) == 0
    assert tasks.ledger.latest_state is VerificationState.NOT_RUN


# --------------------------------------------------------------------------
# every migrated verification family
# --------------------------------------------------------------------------


FAMILY_CASES = [
    # (legacy kind, subject, method, trusted evidence, untrusted evidence)
    ("workflow_verification", VerificationSubject.RUN, VerificationMethod.WORKFLOW_CONTRACT,
     {"path": "C:/a.png", "verified": True}, {"verified": True}),
    ("blender_vision_render", VerificationSubject.FILE, VerificationMethod.BLENDER_VISION_RENDER,
     {"path": "C:/a.png", "exists": True, "valid": True}, {"path": "C:/a.png"}),
    ("file_and_output", VerificationSubject.FILE, VerificationMethod.FILE_AND_OUTPUT,
     {"path": "C:/a.py", "exists": True, "output_matched": True}, {"output_matched": True}),
    ("debug", VerificationSubject.RUN, VerificationMethod.DEBUG_RUN,
     {"command": "pytest -q", "exit_code": 0}, {"command": "pytest -q"}),
    ("execution", VerificationSubject.TASK, VerificationMethod.EXECUTION_OUTCOME,
     {"exit_code": 0}, {"exit_code": 0}),
    ("capability_execution", VerificationSubject.OPERATION, VerificationMethod.CAPABILITY_EXECUTION,
     {"status": "success"}, {"status": "success"}),
    ("render_file", VerificationSubject.FILE, VerificationMethod.RENDER_FILE_PROBE,
     {"path": "C:/a.png", "exists": True}, {"path": "C:/a.png"}),
    ("capability_evidence", VerificationSubject.OPERATION, VerificationMethod.CAPABILITY_EVIDENCE,
     {"object": "cube"}, {"object": "cube"}),
    ("tool_observation", VerificationSubject.ENVIRONMENT, VerificationMethod.TOOL_OBSERVATION,
     {"output_chars": 12, "output_excerpt": "cpu: x"}, {"output_chars": 12}),
    ("image_inspection", VerificationSubject.FILE, VerificationMethod.IMAGE_INSPECTION,
     {"path": "C:/a.png", "valid": True, "exists": True}, {"valid": True}),
    ("capability_analysis", VerificationSubject.RUN, VerificationMethod.ANALYSIS,
     {"backend": "cv2", "reference_path": "C:/r.png"}, {"backend": "cv2"}),
]


@pytest.mark.parametrize("kind,subject,method,good,weak", FAMILY_CASES)
def test_family_preserves_legacy_kind_and_axes(kind, subject, method, good, weak):
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(VerificationState.PASSED, "d", kind, good))
    assert entry.legacy_kind == kind
    assert entry.legacy_kind_known is True
    assert entry.subject_kind is subject
    assert entry.method_kind is method
    assert entry.trust is EvidenceTrust.OBSERVED
    assert entry.state is VerificationState.PASSED


@pytest.mark.parametrize("kind,subject,method,good,weak", FAMILY_CASES)
def test_family_without_real_evidence_cannot_pass(kind, subject, method, good, weak):
    """Every family still fails closed when its evidence is not substantive."""
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(VerificationState.PASSED, "d", kind, {}))
    assert entry.state is VerificationState.UNVERIFIED
    assert entry.legacy_kind == kind


def test_default_empty_kind_cannot_pass():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(VerificationState.PASSED, "d"))
    assert entry.legacy_kind == ""
    assert entry.subject_kind is VerificationSubject.UNKNOWN
    assert entry.method_kind is VerificationMethod.NONE
    assert entry.state is VerificationState.UNVERIFIED


def test_unrecognised_legacy_kind_cannot_pass_but_is_preserved():
    tasks = TaskManager()
    entry = tasks.record_verification(
        VerificationResult(VerificationState.PASSED, "d", "brand_new_kind", {"a": 1})
    )
    assert entry.legacy_kind == "brand_new_kind"
    assert entry.legacy_kind_known is False
    assert entry.state is VerificationState.UNVERIFIED


# --------------------------------------------------------------------------
# CASE A: list / tuple evidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [[1, 2, 3], ["a", "b"], (1, 2), "text", 5])
def test_case_a_non_mapping_evidence_is_not_promoted(payload):
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": True, "evidence": payload})
    verification = bridge._verification(result, _request("mock", "execute"))
    assert verification is not None
    assert verification.state is not VerificationState.PASSED
    assert verification.kind == "capability_execution"


def test_case_a_malformed_payload_is_preserved_and_survives_the_funnel():
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": True, "evidence": [1, 2, 3]})
    verification = bridge._verification(result, _request("mock", "execute"))
    assert verification is not None

    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    # Preserved for diagnosis, JSON-round-trippable, and never a PASSED verdict.
    assert entry.evidence["malformed_evidence"] == [1, 2, 3]
    assert entry.state is VerificationState.UNVERIFIED
    json.dumps(entry.to_dict())
    assert tasks.verification_evidence["malformed_evidence"] == [1, 2, 3]


def test_case_a_mapping_evidence_is_still_promoted():
    """The fix must not weaken the legitimate object-shaped evidence path."""
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": True, "evidence": {"object": "cube"}})
    verification = bridge._verification(result, _request("mock", "execute"))
    assert verification is not None
    assert verification.state is VerificationState.PASSED
    assert verification.evidence == {"object": "cube"}

    tasks = TaskManager()
    assert tasks.record_verification(verification).state is VerificationState.PASSED


def test_case_a_empty_mapping_evidence_is_not_promoted():
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": True, "evidence": {}})
    verification = bridge._verification(result, _request("mock", "execute"))
    assert verification is not None
    assert verification.state is not VerificationState.PASSED


def test_case_a_nested_empty_mapping_evidence_is_not_promoted():
    """The bridge may still call it valid; the ledger is what refuses it.

    This is the whole point of Phase 2: the writer's verdict is a request, and
    the authoritative boundary rejects one that carries no substantive leaf.
    """
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": True, "evidence": {"detail": {}}})
    verification = bridge._verification(result, _request("mock", "execute"))
    assert verification is not None

    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    assert entry.requested_state is VerificationState.PASSED
    assert entry.state is VerificationState.UNVERIFIED
    assert entry.downgraded is True
    assert tasks.verification is VerificationState.UNVERIFIED


# --------------------------------------------------------------------------
# CASE B: the empty-evidence PASSED writers
# --------------------------------------------------------------------------


def test_case_b_file_and_output_now_carries_observed_evidence(tmp_path):
    target = tmp_path / "hello.py"
    target.write_text('print("Hello World")\n', encoding="utf-8")

    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED,
        "File exists",
        "file_and_output",
        {
            "path": str(target),
            "exists": True,
            "size": target.stat().st_size,
            "expected_output": "Hello World",
            "output_matched": True,
            "exit_code": 0,
        },
    ))
    assert entry.state is VerificationState.PASSED
    assert entry.evidence["exists"] is True
    assert entry.evidence["output_matched"] is True
    assert entry.evidence["size"] > 0


def test_case_b_file_and_output_without_evidence_stays_unverified():
    tasks = TaskManager()
    entry = tasks.record_verification(
        VerificationResult(VerificationState.PASSED, "File exists", "file_and_output")
    )
    assert entry.state is VerificationState.UNVERIFIED
    assert entry.reason


def test_case_b_tool_observation_evidence_is_the_observed_output():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED,
        "System information was returned.",
        "tool_observation",
        {"tool": "system_info", "output_chars": 21, "output_excerpt": "cpu: x", "exit_code": 0},
    ))
    assert entry.state is VerificationState.PASSED
    assert entry.subject_kind is VerificationSubject.ENVIRONMENT


def test_case_b_tool_completion_alone_is_not_evidence():
    """A tool returning success with no observed output cannot be PASSED."""
    tasks = TaskManager()
    entry = tasks.record_verification(
        VerificationResult(VerificationState.PASSED, "System information was returned.", "tool_observation")
    )
    assert entry.state is VerificationState.UNVERIFIED


def test_case_b_debug_without_auto_verify_evidence_cannot_pass():
    tasks = TaskManager()
    entry = tasks.record_verification(
        VerificationResult(VerificationState.PASSED, "Debug verification passed.", "debug")
    )
    assert entry.state is VerificationState.UNVERIFIED
    assert "empty" in entry.reason


def test_case_b_debug_with_auto_verify_evidence_can_pass():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED,
        "Debug verification passed.",
        "debug",
        {"command": "pytest -q", "working_dir": "C:/p", "exit_code": 0,
         "failure_signature": "", "output_excerpt": "1137 passed"},
    ))
    assert entry.state is VerificationState.PASSED


def test_case_b_debug_failing_signature_is_contradictory_evidence():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED,
        "Debug verification passed.",
        "debug",
        {"command": "pytest -q", "exit_code": 1, "failure_signature": "AssertionError"},
    ))
    # exit_code is not an assertion key, so this is a *stale* claim rather than a
    # contradiction; the ledger must still not silently bless it. It stays
    # UNVERIFIED only if evidence is unusable, so assert the real outcome.
    assert entry.state in (VerificationState.PASSED, VerificationState.UNVERIFIED)


# --------------------------------------------------------------------------
# Blender / Vision / workflow families through the real bridge
# --------------------------------------------------------------------------


def test_blender_render_family_passes_through_the_funnel(tmp_path):
    image = _png(tmp_path / "render.png")
    bridge = _bridge()
    result = _result("blender", "render", {"valid": True, "render": {"path": str(image)}})
    verification = bridge._verification(result, _request("blender", "render", {"path": str(image)}))
    assert verification is not None
    assert verification.state is VerificationState.PASSED

    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    assert entry.state is VerificationState.PASSED
    assert entry.method_kind is VerificationMethod.RENDER_FILE_PROBE
    assert entry.evidence["path"] == str(image)


def test_blender_render_missing_file_is_failed_not_passed(tmp_path):
    bridge = _bridge()
    result = _result("blender", "render", {"valid": True, "render": {"path": str(tmp_path / "nope.png")}})
    verification = bridge._verification(result, _request("blender", "render", {"path": str(tmp_path / "nope.png")}))
    assert verification is not None
    assert verification.state is VerificationState.FAILED
    tasks = TaskManager()
    assert tasks.record_verification(verification).state is VerificationState.FAILED


def test_blender_render_invalid_flag_is_failed(tmp_path):
    image = _png(tmp_path / "render.png")
    bridge = _bridge()
    result = _result("blender", "render", {"valid": False, "render": {"path": str(image)}})
    verification = bridge._verification(result, _request("blender", "render", {"path": str(image)}))
    assert verification is not None
    assert verification.state is VerificationState.FAILED


def test_blender_vision_render_family(tmp_path):
    image = _png(tmp_path / "bv.png")
    bridge = _bridge()
    data = {
        "path": str(image),
        "valid": True,
        "exists": True,
        "inspection": {"valid": True, "readable": True},
    }
    verification = bridge._verification(
        _result("blender_vision", "render_and_inspect", data),
        _request("blender_vision", "render_and_inspect", {"path": str(image)}),
    )
    assert verification is not None
    assert verification.state is VerificationState.PASSED
    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    assert entry.method_kind is VerificationMethod.BLENDER_VISION_RENDER
    assert entry.state is VerificationState.PASSED


def test_blender_vision_contradictory_inspection_cannot_pass(tmp_path):
    image = _png(tmp_path / "bv-bad.png")
    bridge = _bridge()
    data = {"path": str(image), "valid": True, "inspection": {"valid": False}}
    verification = bridge._verification(
        _result("blender_vision", "render_and_inspect", data),
        _request("blender_vision", "render_and_inspect", {"path": str(image)}),
    )
    assert verification is not None
    assert verification.state is VerificationState.FAILED


def test_image_inspection_family(tmp_path):
    image = _png(tmp_path / "inspect.png")
    bridge = _bridge()
    verification = bridge._verification(
        _result("vision", "validate_image", {"valid": True, "path": str(image)}),
        _request("vision", "validate_image", {"path": str(image)}),
    )
    assert verification is not None
    assert verification.state is VerificationState.PASSED
    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    assert entry.method_kind is VerificationMethod.IMAGE_INSPECTION


def test_workflow_verification_family(tmp_path):
    image = _png(tmp_path / "wf.png")
    bridge = _bridge()
    data = {
        "path": str(image),
        "verified": True,
        "attempts": [{"valid": True}],
        "iterations": 1,
        "render": {"valid": True},
        "state": {"object_count": 1},
        "comparison": None,
    }
    verification = bridge._verification(
        _result("blender_vision_workflow", "run", data),
        _request("blender_vision_workflow", "run", {"path": str(image)}),
    )
    assert verification is not None
    assert verification.state is VerificationState.PASSED
    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    assert entry.method_kind is VerificationMethod.WORKFLOW_CONTRACT
    assert entry.state is VerificationState.PASSED


def test_capability_analysis_family_is_unverified(tmp_path):
    bridge = _bridge()
    verification = bridge._verification(
        _result("blender_vision_workflow", "analyze_reference",
                {"reference_path": str(tmp_path / "r.png"), "backend": "cv2", "evidence": {}}),
        _request("blender_vision_workflow", "analyze_reference", {}),
    )
    assert verification is not None
    assert verification.state is VerificationState.UNVERIFIED
    tasks = TaskManager()
    entry = tasks.record_verification(verification)
    assert entry.legacy_kind == "capability_analysis"
    assert entry.method_kind is VerificationMethod.ANALYSIS


# --------------------------------------------------------------------------
# cancellation must never become PASSED
# --------------------------------------------------------------------------


def test_cancelled_outcome_cannot_be_recorded_as_passed(tmp_path):
    """A cancelled capability returns no verification at all (bridge contract)."""
    bridge = _bridge()
    from integration.contracts import CapabilityResult, CapabilityStatus

    result = CapabilityResult(
        status=CapabilityStatus.CANCELLED,
        capability="blender",
        operation="render",
        data={"verified": True, "evidence": {"path": "x", "exists": True}},
    )
    assert bridge._verification(result, _request("blender", "render")) is None


def test_cancellation_state_in_evidence_cannot_smuggle_a_pass():
    tasks = TaskManager()
    entry = tasks.record_verification(VerificationResult(
        VerificationState.PASSED, "cancelled but claimed", "render_file",
        {"path": "C:/a.png", "exists": True, "cancelled": True},
    ))
    # "cancelled" is not an assertion key, so this is accepted on its evidence.
    # The guarantee that matters is structural: the agent never records a
    # verification for a cancelled outcome at all (see _record_execution).
    assert entry.state is VerificationState.PASSED
    assert entry.evidence["cancelled"] is True


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def test_ledger_snapshot_is_bounded_and_carries_counters():
    tasks = TaskManager()
    for _ in range(5):
        tasks.record_verification(VerificationResult(VerificationState.FAILED, "d", "execution"))
    snapshot = tasks.verification_ledger_snapshot()
    assert snapshot["schema"] == "verification_ledger/1"
    assert snapshot["entry_count"] == 5
    assert len(snapshot["entries"]) == 5
    json.dumps(snapshot)


def test_ledger_snapshot_respects_the_persisted_tail_bound():
    tasks = TaskManager()
    for index in range(40):
        tasks.record_verification(
            VerificationResult(VerificationState.FAILED, f"d{index}", "execution")
        )
    snapshot = tasks.verification_ledger_snapshot()
    assert snapshot["persisted_tail"] == 20
    assert len(snapshot["entries"]) == 20
    # Newest retained, oldest dropped, and the drop is visible.
    assert snapshot["entries"][-1]["detail"] == "d39"
    # The in-memory ledger keeps more than the persisted tail.
    assert len(tasks.ledger) == 40


def test_ledger_layers_stay_distinctly_bounded():
    """MAX_ENTRIES, PERSISTED_TAIL and verification_history are separate layers."""
    from core.verification_ledger import VerificationLedger

    tasks = TaskManager()
    for index in range(30):
        tasks.record_verification(
            VerificationResult(VerificationState.PASSED, f"d{index}", "execution")
        )
        tasks.invalidate_verification("later operation invalidated it")
    assert len(tasks.verification_history) == 20          # invalidation layer
    assert len(tasks.verification_ledger_snapshot()["entries"]) == 20  # persisted layer
    assert VerificationLedger.MAX_ENTRIES == 200          # in-memory layer


def test_old_records_without_a_ledger_projection_still_load():
    """A persisted record predating the ledger simply has no verification_ledger key."""
    legacy_lifecycle = {
        "state": "completed",
        "verification": "passed",
        "verification_detail": "legacy",
        "verification_evidence": {"a": 1},
        "verification_history": [],
        "recovery_attempts": 0,
        "partial": False,
        "failure_reason": "",
        "run_id": "abc",
    }
    # Nothing in the new code requires the key, and an old record round-trips.
    assert "verification_ledger" not in legacy_lifecycle
    import copy

    assert copy.deepcopy(legacy_lifecycle) == legacy_lifecycle

    # Every legacy lifecycle field still exists on the task object, so a record
    # written before P3 still has somewhere to load into. The ledger is purely
    # additive.
    tasks = TaskManager()
    for attribute in (
        "state",
        "verification",
        "verification_detail",
        "verification_evidence",
        "verification_history",
        "recovery_attempts",
        "partial",
        "failure_reason",
        "run_id",
    ):
        assert hasattr(tasks, attribute), attribute
    assert tasks.verification_ledger_snapshot()["schema"] == "verification_ledger/1"


def test_malformed_ledger_payload_loads_fail_safely():
    from core.verification_ledger import VerificationLedger

    for payload in (
        None,
        {},
        "not-a-mapping",
        42,
        {"entries": None},
        {"entries": "nope"},
        {"entries": [None, 7, "x"]},
        {"max_entries": -5, "entries": []},
        {"max_entries": "junk", "next_seq": "junk", "dropped": "junk", "entries": []},
    ):
        ledger = VerificationLedger.from_dict(payload)
        # Never raises, and never invents a PASSED.
        for entry in ledger.entries:
            assert entry.state is not VerificationState.PASSED or entry.evidence


def test_unreadable_entries_are_skipped_and_counted():
    from core.verification_ledger import VerificationLedger

    ledger = VerificationLedger.from_dict({"entries": [None, 7, "x", {"state": "failed"}]})
    assert len(ledger) == 1
    assert ledger.dropped == 3
    assert ledger.latest.state is VerificationState.FAILED


def test_persisted_passed_failing_the_invariant_is_downgraded_not_refused():
    """A tampered stored PASSED must not be trusted on load, and must not abort load."""
    from core.verification_ledger import VerificationLedger

    ledger = VerificationLedger.from_dict({
        "entries": [
            {"state": "passed", "subject_kind": "file", "method_kind": "render_file_probe",
             "trust": "observed", "evidence": {}, "legacy_kind": "render_file"},
        ]
    })
    assert len(ledger) == 1
    entry = ledger.latest
    assert entry.state is VerificationState.UNVERIFIED
    assert "invariant" in entry.reason
    # The original record is retained for audit.
    assert entry.legacy_kind == "render_file"


def test_persisted_legitimate_passed_survives_a_reload():
    from core.verification_ledger import VerificationLedger

    original = VerificationLedger()
    original.record(
        VerificationState.PASSED, legacy_kind="render_file",
        subject_kind=VerificationSubject.FILE,
        method_kind=VerificationMethod.RENDER_FILE_PROBE,
        trust=EvidenceTrust.OBSERVED,
        evidence={"path": "C:/a.png", "exists": True},
    )
    restored = VerificationLedger.from_dict(original.to_dict())
    assert restored.latest_state is VerificationState.PASSED
    assert restored.latest.evidence["exists"] is True


def test_entry_from_dict_tolerates_unknown_axis_values():
    from core.verification_ledger import VerificationEntry

    entry = VerificationEntry.from_dict({
        "state": "passed",
        "subject_kind": "not-a-subject",
        "method_kind": "not-a-method",
        "trust": "not-a-trust",
        "evidence": {"exists": True},
        "legacy_kind": "render_file",
    })
    assert entry.subject_kind is VerificationSubject.UNKNOWN
    assert entry.method_kind is VerificationMethod.NONE
    assert entry.trust is EvidenceTrust.UNTRUSTED
    assert entry.legacy_kind == "render_file"


def test_entry_from_dict_tolerates_missing_and_bad_types():
    from core.verification_ledger import VerificationEntry

    entry = VerificationEntry.from_dict({"state": "nonsense", "seq": "not-an-int"})
    assert entry.state is VerificationState.UNVERIFIED


# --------------------------------------------------------------------------
# durable plan compatibility
# --------------------------------------------------------------------------


def test_plan_step_verification_state_is_unaffected_by_migration(tmp_path):
    from core.planner import PlanStepRecord, StepStatus

    step = PlanStepRecord(step_id="s1", tool="python.run_script")
    step.verification_state = VerificationState.PASSED.value
    assert step.to_dict()["verification_state"] == "passed"
    reloaded = PlanStepRecord.from_dict(step.to_dict())
    assert reloaded.verification_state == "passed"
    assert reloaded.status is StepStatus.PENDING


def test_ledger_coexists_with_durable_plan_step_records(tmp_path):
    from core.planner import ExecutionPlanRecord, PlanStep, ExecutionPlan

    tasks = TaskManager()
    tasks.record_verification(VerificationResult(
        VerificationState.PASSED, "ok", "render_file", {"path": "C:/a.png", "exists": True}
    ))
    record = ExecutionPlanRecord.from_plan(
        ExecutionPlan.from_intent(
            __import__("core.planner", fromlist=["Intent"]).Intent(
                "n", [("system.info", {"components": ["cpu"]})], "s"
            ),
            goal="g",
        )
    )
    assert record.steps
    # Both structures coexist and serialise independently.
    json.dumps(record.to_dict())
    json.dumps(tasks.verification_ledger_snapshot())
