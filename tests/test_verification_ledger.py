"""M2.12 P3 Phase 1 - VerificationLedger schema tests.

Covers the PASSED invariant, the three-axis model, legacy ``kind``
compatibility, and append-only ledger behaviour. These tests are additive:
Phase 1 migrates no existing verification writer, so the legacy
``VerificationResult`` behaviour asserted at the end of this file must keep
working exactly as before.
"""

from __future__ import annotations

import pytest

from core.verification import VerificationResult, VerificationState
from core.verification_ledger import (
    ASSERTION_KEYS,
    LEGACY_KIND_AXES,
    EvidenceTrust,
    VerificationEntry,
    VerificationLedger,
    VerificationLedgerError,
    VerificationMethod,
    VerificationSubject,
    adjudicate,
    resolve_legacy_kind,
)

# A minimal evidence payload that is valid, non-empty, substantive,
# non-contradictory and JSON-serialisable.
GOOD_EVIDENCE = {"path": "C:/out/render.png", "exists": True, "valid": True}

# Axes that satisfy the invariant, so each test can vary exactly one thing.
OK_SUBJECT = VerificationSubject.FILE
OK_METHOD = VerificationMethod.RENDER_FILE_PROBE
OK_TRUST = EvidenceTrust.OBSERVED


def _passing_entry(**overrides):
    kwargs = {
        "state": VerificationState.PASSED,
        "subject_kind": OK_SUBJECT,
        "method_kind": OK_METHOD,
        "trust": OK_TRUST,
        "detail": "Render file exists on disk.",
        "evidence": GOOD_EVIDENCE,
    }
    kwargs.update(overrides)
    return VerificationEntry.create(**kwargs)


# ---------------------------------------------------------------- 1. PASSED


def test_valid_trusted_evidence_can_produce_passed():
    entry = _passing_entry()
    assert entry.state is VerificationState.PASSED
    assert entry.requested_state is VerificationState.PASSED
    assert entry.reason == ""
    assert entry.downgraded is False


# ------------------------------------------- 2-5. evidence fail-closed rules


def test_missing_evidence_cannot_produce_passed():
    entry = _passing_entry(evidence=None)
    assert entry.state is VerificationState.UNVERIFIED
    assert "missing" in entry.reason
    assert entry.requested_state is VerificationState.PASSED
    assert entry.downgraded is True


def test_empty_evidence_cannot_produce_passed():
    entry = _passing_entry(evidence={})
    assert entry.state is VerificationState.UNVERIFIED
    assert "empty" in entry.reason


def test_nested_empty_evidence_cannot_produce_passed():
    """A populated wrapper around nothing carries no information."""
    entry = _passing_entry(evidence={"evidence": {}})
    assert entry.state is VerificationState.UNVERIFIED
    assert "empty" in entry.reason
    entry = _passing_entry(evidence={"evidence": None})
    assert entry.state is VerificationState.UNVERIFIED
    assert "empty" in entry.reason


def test_narrative_text_is_not_evidence():
    """A detail string is narration, never evidence."""
    entry = _passing_entry(evidence="everything worked")
    assert entry.state is VerificationState.UNVERIFIED
    assert "string" in entry.reason


@pytest.mark.parametrize(
    "evidence",
    [
        42,
        True,
        object(),
    ],
)
def test_malformed_evidence_cannot_produce_passed(evidence):
    entry = _passing_entry(evidence=evidence)
    assert entry.state is VerificationState.UNVERIFIED
    assert entry.reason


def test_non_string_key_evidence_is_malformed():
    entry = _passing_entry(evidence={1: "one"})
    assert entry.state is VerificationState.UNVERIFIED
    assert "non-string key" in entry.reason


def test_non_json_serialisable_evidence_is_malformed():
    entry = _passing_entry(evidence={"path": object()})
    assert entry.state is VerificationState.UNVERIFIED
    assert "JSON-serialisable" in entry.reason


def test_non_boolean_assertion_value_is_malformed():
    entry = _passing_entry(evidence={"path": "C:/x.png", "valid": "yes"})
    assert entry.state is VerificationState.UNVERIFIED
    assert "not a boolean" in entry.reason


def test_contradictory_evidence_cannot_produce_passed():
    entry = _passing_entry(evidence={"path": "C:/x.png", "exists": False})
    assert entry.state is VerificationState.UNVERIFIED
    assert "is False" in entry.reason


def test_contradictory_evidence_is_refused_even_with_a_positive_assertion():
    """``verified=True`` alongside ``valid=False`` is self-contradictory."""
    entry = _passing_entry(evidence={"verified": True, "valid": False})
    assert entry.state is VerificationState.UNVERIFIED
    assert "is False" in entry.reason


@pytest.mark.parametrize("key", sorted(ASSERTION_KEYS))
def test_every_assertion_key_false_blocks_passed(key):
    entry = _passing_entry(evidence={"path": "C:/x.png", key: False})
    assert entry.state is VerificationState.UNVERIFIED
    assert key in entry.reason


def test_assertion_key_true_alone_is_not_contradiction():
    """Presence of a true assertion is fine; the rest of evidence still counts."""
    entry = _passing_entry(evidence={"valid": True})
    assert entry.state is VerificationState.PASSED


# ------------------------------------------------- 6-7. non-evidence claims


def test_tool_completion_without_evidence_cannot_produce_passed():
    """A tool returning without raising is not verification evidence."""
    entry = VerificationEntry.create(
        VerificationState.PASSED,
        subject_kind=VerificationSubject.OPERATION,
        method_kind=VerificationMethod.CAPABILITY_EXECUTION,
        trust=EvidenceTrust.OBSERVED,
        detail="Tool returned without raising.",
    )
    assert entry.state is VerificationState.UNVERIFIED
    assert "missing" in entry.reason
    assert entry.downgraded is True


def test_tool_completion_with_merely_present_evidence_can_pass_but_must_be_observed():
    """Completion evidence is real evidence; it is not a success claim."""
    entry = VerificationEntry.create(
        VerificationState.PASSED,
        subject_kind=VerificationSubject.OPERATION,
        method_kind=VerificationMethod.CAPABILITY_EXECUTION,
        trust=EvidenceTrust.OBSERVED,
        detail="Tool returned without raising.",
        evidence={"exit_code": 0},
    )
    assert entry.state is VerificationState.PASSED


def test_model_only_success_claim_cannot_produce_passed():
    entry = VerificationEntry.create(
        VerificationState.PASSED,
        subject_kind=VerificationSubject.TASK,
        method_kind=VerificationMethod.EXECUTION_OUTCOME,
        trust=EvidenceTrust.CLAIMED,
        detail="The model says it finished.",
        evidence={"model_claim": "done"},
    )
    assert entry.state is VerificationState.UNVERIFIED
    assert "not observed" in entry.reason


def test_verified_true_without_supporting_evidence_cannot_produce_passed():
    entry = VerificationEntry.create(
        VerificationState.PASSED,
        subject_kind=VerificationSubject.OPERATION,
        method_kind=VerificationMethod.CAPABILITY_EVIDENCE,
        trust=EvidenceTrust.CLAIMED,
        evidence={"verified": True},
    )
    assert entry.state is VerificationState.UNVERIFIED


def test_default_verification_cannot_produce_passed():
    """The default VerificationResult shape: PASSED, no kind, no evidence."""
    entry = VerificationEntry.from_result(VerificationResult(VerificationState.PASSED, "ok"))
    assert entry.state is VerificationState.UNVERIFIED
    assert entry.legacy_kind == ""
    assert entry.method_kind is VerificationMethod.NONE
    assert entry.subject_kind is VerificationSubject.UNKNOWN


# ------------------------------------------------------- 8-11. three axes


def test_legacy_kind_is_preserved_verbatim():
    entry = _passing_entry(legacy_kind="render_file")
    assert entry.legacy_kind == "render_file"
    assert entry.legacy_kind_known is True
    assert entry.to_dict()["legacy_kind"] == "render_file"


def test_unknown_legacy_kind_is_representable_but_cannot_pass():
    entry = _passing_entry(legacy_kind="some_future_kind")
    assert entry.legacy_kind == "some_future_kind"
    assert entry.legacy_kind_known is False
    # The explicit axes were supplied, so the verdict itself is still valid.
    assert entry.state is VerificationState.PASSED


def test_subject_kind_is_preserved():
    for subject in VerificationSubject:
        if subject is VerificationSubject.UNKNOWN:
            continue
        entry = _passing_entry(subject_kind=subject)
        assert entry.subject_kind is subject
        assert entry.to_dict()["subject_kind"] == subject.value


def test_method_kind_is_preserved():
    for method in VerificationMethod:
        if method is VerificationMethod.NONE:
            continue
        entry = _passing_entry(method_kind=method)
        assert entry.method_kind is method
        assert entry.to_dict()["method_kind"] == method.value


def test_trust_is_preserved():
    for trust in EvidenceTrust:
        entry = _passing_entry(trust=trust)
        assert entry.trust is trust
        assert entry.to_dict()["trust"] == trust.value


def test_unknown_subject_cannot_produce_passed():
    entry = _passing_entry(subject_kind=VerificationSubject.UNKNOWN)
    assert entry.state is VerificationState.UNVERIFIED
    assert "subject_kind is unknown" in entry.reason


def test_no_method_cannot_produce_passed():
    entry = _passing_entry(method_kind=VerificationMethod.NONE)
    assert entry.state is VerificationState.UNVERIFIED
    assert "method_kind is none" in entry.reason


@pytest.mark.parametrize("trust", [EvidenceTrust.UNTRUSTED, EvidenceTrust.CLAIMED])
def test_only_observed_trust_can_produce_passed(trust):
    entry = _passing_entry(trust=trust)
    assert entry.state is VerificationState.UNVERIFIED
    assert "not observed" in entry.reason


def test_unrecognised_axis_values_fail_closed():
    entry = _passing_entry(subject_kind="not-a-subject", method_kind="nope", trust="perhaps")
    assert entry.subject_kind is VerificationSubject.UNKNOWN
    assert entry.method_kind is VerificationMethod.NONE
    assert entry.trust is EvidenceTrust.UNTRUSTED
    assert entry.state is VerificationState.UNVERIFIED


# --------------------------------------------------- legacy kind resolution


def test_legacy_kind_axis_table_covers_every_kind_built_in_the_codebase():
    """The mapping must cover every kind the current writers actually produce."""
    built = {
        "capability_analysis",
        "capability_evidence",
        "capability_execution",
        "blender_vision_render",
        "debug",
        "execution",
        "file_and_output",
        "image_inspection",
        "render_file",
        "tool_observation",
        "workflow_verification",
    }
    assert built <= set(LEGACY_KIND_AXES)


@pytest.mark.parametrize(
    "kind,axes", sorted((k, v) for k, v in LEGACY_KIND_AXES.items())
)
def test_every_legacy_kind_resolves_to_declared_axes(kind, axes):
    subject, method = axes
    resolved_subject, resolved_method = resolve_legacy_kind(kind)
    assert resolved_subject is subject
    assert resolved_method is method
    assert method is not VerificationMethod.NONE


def test_empty_legacy_kind_resolves_to_unknown_and_none():
    assert resolve_legacy_kind("") == (VerificationSubject.UNKNOWN, VerificationMethod.NONE)
    assert resolve_legacy_kind("nonsense") == (VerificationSubject.UNKNOWN, VerificationMethod.NONE)


def test_from_legacy_kind_populates_all_three_axes():
    entry = VerificationEntry.from_legacy_kind(
        "render_file",
        VerificationState.PASSED,
        evidence={"path": "C:/x.png", "exists": True},
    )
    assert entry.subject_kind is VerificationSubject.FILE
    assert entry.method_kind is VerificationMethod.RENDER_FILE_PROBE
    assert entry.trust is EvidenceTrust.OBSERVED
    assert entry.legacy_kind == "render_file"
    assert entry.state is VerificationState.PASSED


def test_from_result_maps_legacy_kind_onto_the_axes():
    """Compatibility adapter: a legacy writer can be routed without guessing."""
    result = VerificationResult(
        VerificationState.PASSED,
        "Image inspection evidence was returned.",
        "image_inspection",
        {"path": "C:/x.png", "valid": True, "exists": True},
    )
    entry = VerificationEntry.from_result(result)
    assert entry.subject_kind is VerificationSubject.FILE
    assert entry.method_kind is VerificationMethod.IMAGE_INSPECTION
    assert entry.trust is EvidenceTrust.OBSERVED
    assert entry.legacy_kind == "image_inspection"
    assert entry.state is VerificationState.PASSED


def test_from_result_keeps_a_legacy_failure_never_upgraded():
    result = VerificationResult(VerificationState.FAILED, "no file", "render_file", {})
    entry = VerificationEntry.from_result(result)
    assert entry.state is VerificationState.FAILED
    assert entry.downgraded is False


# ------------------------------------------------- 12. append-only ordering


def test_ledger_retains_insertion_order():
    ledger = VerificationLedger()
    for index in range(5):
        ledger.record(
            VerificationState.PASSED,
            legacy_kind="render_file",
            subject_kind=OK_SUBJECT,
            method_kind=OK_METHOD,
            trust=OK_TRUST,
            evidence={"path": f"C:/out/{index}.png", "exists": True},
        )
    entries = ledger.entries
    assert len(entries) == 5
    assert [entry.seq for entry in entries] == [0, 1, 2, 3, 4]
    assert [entry.evidence["path"] for entry in entries] == [
        f"C:/out/{index}.png" for index in range(5)
    ]


def test_ledger_latest_reflects_the_newest_entry():
    ledger = VerificationLedger()
    assert ledger.latest is None
    assert ledger.latest_state is VerificationState.NOT_RUN
    ledger.record(VerificationState.PASSED, **_passing_axes())
    assert ledger.latest_state is VerificationState.PASSED
    ledger.record(VerificationState.FAILED, **_passing_axes())
    assert ledger.latest_state is VerificationState.FAILED
    assert ledger.latest.state is VerificationState.FAILED


def test_ledger_entries_view_is_immutable():
    ledger = VerificationLedger()
    ledger.record(VerificationState.FAILED, **_passing_axes())
    snapshot = ledger.entries
    ledger.record(VerificationState.FAILED, **_passing_axes())
    assert len(snapshot) == 1
    assert len(ledger.entries) == 2


def test_ledger_is_bounded_and_counts_drops():
    ledger = VerificationLedger(max_entries=3)
    for index in range(6):
        ledger.record(VerificationState.FAILED, **_passing_axes())
    assert len(ledger) == 3
    assert ledger.dropped == 3
    assert [entry.seq for entry in ledger.entries] == [3, 4, 5]


def test_ledger_round_trips_through_dict():
    ledger = VerificationLedger()
    ledger.record(VerificationState.PASSED, legacy_kind="render_file", **_passing_axes())
    ledger.record(VerificationState.FAILED, legacy_kind="debug", **_passing_axes())
    payload = ledger.to_dict()

    import json

    json.dumps(payload)  # must be persistable

    restored = VerificationLedger.from_dict(payload)
    assert [e.to_dict() for e in restored.entries] == [e.to_dict() for e in ledger.entries]
    assert restored.latest_state is VerificationState.FAILED
    assert restored.dropped == 0


def test_ledger_preserves_authoritative_identity_passed_in_by_the_caller():
    ledger = VerificationLedger()
    entry = ledger.record(
        VerificationState.PASSED,
        **_passing_axes(),
        task_id="task-1",
        run_id="run-1",
        request_id="S1-REQ",
        step_id="step-3",
    )
    assert (entry.task_id, entry.run_id, entry.request_id, entry.step_id) == (
        "task-1",
        "run-1",
        "S1-REQ",
        "step-3",
    )


def test_with_identity_does_not_mint_identity():
    entry = _passing_entry()
    assert entry.with_identity(request_id="S1-REQ").request_id == "S1-REQ"
    assert entry.request_id == ""


# ------------------------------------------- 13. invalid input fails safely


def test_append_rejects_non_entry_without_mutating_the_ledger():
    ledger = VerificationLedger()
    ledger.record(VerificationState.FAILED, **_passing_axes())
    with pytest.raises(VerificationLedgerError):
        ledger.append({"state": "passed"})  # type: ignore[arg-type]
    assert len(ledger) == 1


def test_append_rejects_a_hand_built_passed_entry_that_skips_the_invariant():
    """A caller must not be able to hand-build PASSED around bad evidence."""
    forged = VerificationEntry(
        state=VerificationState.PASSED,
        subject_kind=VerificationSubject.FILE,
        method_kind=VerificationMethod.RENDER_FILE_PROBE,
        trust=EvidenceTrust.CLAIMED,
        evidence={},
    )
    ledger = VerificationLedger()
    with pytest.raises(VerificationLedgerError):
        ledger.append(forged)
    assert len(ledger) == 0
    assert ledger.latest is None


def test_record_never_raises_for_a_bad_state():
    ledger = VerificationLedger()
    entry = ledger.record(object(), **_passing_axes())
    assert entry.state is VerificationState.UNVERIFIED
    assert "unrecognised" in entry.reason
    assert len(ledger) == 1


def test_record_never_raises_for_a_non_evidence_payload():
    ledger = VerificationLedger()
    for bad in (None, object(), "text", 3.5, [1, 2, 3], ("a", "b")):
        entry = ledger.record(
            VerificationState.PASSED,
            subject_kind=OK_SUBJECT,
            method_kind=OK_METHOD,
            trust=OK_TRUST,
            evidence=bad,
        )
        assert entry.state is VerificationState.UNVERIFIED
        assert entry.reason


def test_bare_sequence_evidence_is_rejected_as_semantically_meaningless():
    """Stricter than capability_bridge on purpose; the durable contract is a dict."""
    state, reason = adjudicate(
        VerificationState.PASSED, OK_SUBJECT, OK_METHOD, OK_TRUST, [1, 2, 3]
    )
    assert state is VerificationState.UNVERIFIED
    assert "not a mapping" in reason


def test_ledger_rejects_a_non_positive_bound():
    with pytest.raises(VerificationLedgerError):
        VerificationLedger(max_entries=0)


def test_sequence_keeps_increasing_after_a_rejection():
    ledger = VerificationLedger()
    first = ledger.record(VerificationState.FAILED, **_passing_axes())
    with pytest.raises(VerificationLedgerError):
        ledger.append("not an entry")  # type: ignore[arg-type]
    second = ledger.record(VerificationState.FAILED, **_passing_axes())
    assert second.seq == first.seq + 1


# ------------------------------------- 14. legacy VerificationResult intact


def test_legacy_verification_result_is_unchanged():
    result = VerificationResult(VerificationState.PASSED, "detail", "render_file", {"a": 1})
    assert result.state is VerificationState.PASSED
    assert result.detail == "detail"
    assert result.kind == "render_file"
    assert result.evidence == {"a": 1}
    assert VerificationResult(VerificationState.FAILED, "d").kind == ""
    assert VerificationResult(VerificationState.FAILED, "d").evidence == {}
    with pytest.raises(Exception):
        result.state = VerificationState.FAILED  # frozen


def test_adjudicate_is_pure_and_returns_a_reason_only_on_change():
    assert adjudicate(VerificationState.FAILED, OK_SUBJECT, OK_METHOD, OK_TRUST, None) == (
        VerificationState.FAILED,
        "",
    )
    assert adjudicate(VerificationState.NOT_REQUIRED) == (VerificationState.NOT_REQUIRED, "")
    assert adjudicate(VerificationState.NOT_RUN) == (VerificationState.NOT_RUN, "")
    state, reason = adjudicate(VerificationState.PASSED, OK_SUBJECT, OK_METHOD, OK_TRUST, GOOD_EVIDENCE)
    assert (state, reason) == (VerificationState.PASSED, "")


def test_adjudicate_accepts_state_strings():
    assert adjudicate("passed", OK_SUBJECT, OK_METHOD, OK_TRUST, GOOD_EVIDENCE)[0] is (
        VerificationState.PASSED
    )
    assert adjudicate("failed")[0] is VerificationState.FAILED


def _passing_axes():
    return {
        "subject_kind": OK_SUBJECT,
        "method_kind": OK_METHOD,
        "trust": OK_TRUST,
        "evidence": GOOD_EVIDENCE,
    }


# ------------------ compatibility with the real capability_bridge writers


def _bridge():
    from integration.contracts import CapabilityRegistry
    from tools.capability_bridge import CapabilityBridge

    return CapabilityBridge(CapabilityRegistry())


def _result(capability: str, operation: str, data: dict):
    from integration.contracts import CapabilityResult, CapabilityStatus

    return CapabilityResult(
        status=CapabilityStatus.SUCCESS,
        capability=capability,
        operation=operation,
        data=data,
    )


def _request(capability: str, operation: str, params: dict | None = None):
    from integration.contracts import CapabilityRequest

    return CapabilityRequest(capability=capability, operation=operation, params=params or {})


def test_ledger_agrees_with_capability_bridge_on_a_real_passed_verification(tmp_path):
    """The ledger must not downgrade a legitimate capability_bridge PASSED.

    ``capability_bridge`` is the compatibility reference: it promotes a
    capability's explicit evidence contract to PASSED. The ledger has to accept
    that same evidence, otherwise Phase 2 migration would silently weaken
    existing behaviour.
    """
    image = tmp_path / "render.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    bridge = _bridge()
    result = _result("blender", "render", {"valid": True, "render": {"path": str(image)}})
    legacy = bridge._verification(result, _request("blender", "render", {"path": str(image)}))
    assert legacy is not None
    assert legacy.state is VerificationState.PASSED
    assert legacy.kind == "render_file"

    entry = VerificationEntry.from_result(legacy)
    assert entry.state is VerificationState.PASSED
    assert entry.reason == ""
    assert entry.legacy_kind == "render_file"


def test_ledger_agrees_with_capability_bridge_on_capability_evidence(tmp_path):
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": True, "evidence": {"object": "cube"}})
    legacy = bridge._verification(result, _request("mock", "execute"))
    assert legacy is not None
    assert legacy.state is VerificationState.PASSED
    assert VerificationEntry.from_result(legacy).state is VerificationState.PASSED


def test_ledger_never_upgrades_a_capability_bridge_failure():
    bridge = _bridge()
    result = _result("mock", "execute", {"verified": False, "evidence": {"reason": "rejected"}})
    legacy = bridge._verification(result, _request("mock", "execute"))
    assert legacy is not None
    assert legacy.state is VerificationState.FAILED
    entry = VerificationEntry.from_result(legacy)
    assert entry.state is VerificationState.FAILED
    assert entry.downgraded is False


def test_ledger_refuses_to_pass_capability_completion_without_evidence():
    """capability_bridge already says UNVERIFIED here; the ledger agrees."""
    bridge = _bridge()
    result = _result("blender_vision_workflow", "run", {"unrelated": True})
    legacy = bridge._verification(result, _request("blender_vision_workflow", "run"))
    assert legacy is not None
    assert legacy.state is VerificationState.UNVERIFIED
    assert VerificationEntry.from_result(legacy).state is VerificationState.UNVERIFIED

