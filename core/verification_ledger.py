"""M2.12 P3 Phase 1 - Verification Ledger and schema.

This module adds a *structured, append-oriented* place to record verification
evidence. It is deliberately additive: nothing in the existing verification
pipeline is routed through it yet. See "Migration status" below.

What a :class:`VerificationEntry` represents
--------------------------------------------
One immutable adjudication of "was this thing actually verified, and by what
evidence?". An entry is the atomic unit a durable ledger stores. It records

* what was being judged (``subject_kind``),
* how it was judged (``method_kind``),
* who is asserting the evidence and how much that assertion is worth
  (``trust``),
* the evidence itself, verbatim,
* the state that was *requested* by the writer, and the state that was
  *adjudicated* after the invariant below was applied,
* why a request was downgraded (``reason``),
* correlation fields (``task_id`` / ``run_id`` / ``request_id`` / ``step_id``).

The three axes
--------------
The legacy :class:`~core.verification.VerificationResult` carries a single
opaque ``kind`` string. That string conflates *what was verified* with *how it
was verified*, so it cannot answer "is this evidence strong enough?". The ledger
splits it into three explicit axes:

``subject_kind``
    What the claim is about (:class:`VerificationSubject`). ``UNKNOWN`` means
    the writer did not say what it verified, which by itself can never support
    ``PASSED``.

``method_kind``
    How the claim was established (:class:`VerificationMethod`). ``NONE`` means
    no method was declared, which by itself can never support ``PASSED``.

``trust``
    Whether the source is strong enough to *support* ``PASSED``
    (:class:`EvidenceTrust`). Only :attr:`EvidenceTrust.OBSERVED` can.

Legacy ``kind`` compatibility
-----------------------------
Legacy kind information is never discarded. Every entry keeps the writer's
``legacy_kind`` verbatim, and :data:`LEGACY_KIND_AXES` gives the explicit
``kind -> (subject_kind, method_kind)`` mapping for every kind observed in the
current codebase. :func:`resolve_legacy_kind` performs that lookup, returning
``(UNKNOWN, NONE)`` for an unrecognised or empty kind rather than guessing. An
unrecognised kind stays representable and still round-trips, it simply cannot
support ``PASSED``.

Evidence requirements
---------------------
Evidence is a JSON-serialisable, non-empty mapping that contains at least one
substantive leaf value. Following
``tools/capability_bridge.CapabilityBridge._valid_evidence`` as the
compatibility reference, a bare container is not enough: ``{"evidence": {}}``
and ``{"evidence": None}`` carry no information and are rejected. A ``detail``
string is narration, not evidence, and never counts.

The ledger is deliberately *stricter* than ``_valid_evidence`` in one place: it
requires a mapping, not a bare list or tuple. A bare sequence has no defined
evidence semantics, and the durable contract (``VerificationResult.evidence``
and ``TaskManager.set_verification``, which does ``dict(evidence)``) is a
mapping. Being stricter cannot weaken existing behaviour.

The ledger is fail-closed and rejects or downgrades:

* missing evidence (``None`` / absent),
* empty evidence (``{}``, ``[]``, ``""``),
* evidence with no substantive leaf (``{"evidence": {}}``),
* malformed evidence (not a mapping, a non-string key, a non-JSON-serialisable
  value, or a boolean assertion key carrying a non-boolean),
* contradictory evidence (a boolean assertion key present and ``False``),
* untrusted evidence (``trust`` is not ``OBSERVED``).

The PASSED invariant
--------------------
``PASSED`` requires valid, non-empty evidence from a trusted source, about a
declared subject, by a declared method. Explicitly **not** evidence:

* a tool returning without raising an exception,
* a model claiming success,
* ``verified=True`` with no supporting evidence,
* empty or default verification,
* arbitrary text with no defined evidence semantics.

A requested ``PASSED`` that fails any of these is *downgraded* to
:attr:`~core.verification.VerificationState.UNVERIFIED`; the original request is
retained in ``requested_state`` and the cause in ``reason``. Downgrade is
one-way: a requested ``FAILED`` is never upgraded, and non-``PASSED`` states
pass through untouched.

Migration status
----------------
Phase 1 is greenfield. The 27 existing ``VerificationResult`` construction
sites (``core/agent.py`` and ``tools/capability_bridge.py``) are **not**
migrated, and ``Agent._record_verification`` / ``TaskManager.set_verification``
/ ``DebugLoop`` / ``_llm_loop`` recovery are untouched. Phase 2 routes those
writers through :class:`VerificationLedger` and then into the existing durable
verification state. The Phase 2 route is:

    Agent._record_verification -> VerificationLedger -> TaskManager.set_verification

:mod:`core.verification` is intentionally left exactly as it is.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from core.verification import VerificationResult, VerificationState

__all__ = [
    "EvidenceTrust",
    "VerificationSubject",
    "VerificationMethod",
    "VerificationEntry",
    "VerificationLedger",
    "VerificationLedgerError",
    "LEGACY_KIND_AXES",
    "adjudicate",
    "resolve_legacy_kind",
]


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class VerificationLedgerError(ValueError):
    """Raised when input cannot be admitted to the ledger at all.

    Raised only by the strict entry point
    :meth:`VerificationLedger.append`, which is atomic: a rejected entry never
    mutates the ledger. The forgiving entry point
    :meth:`VerificationLedger.record` never raises for a bad *value*; it
    downgrades to a safe state instead.
    """


class VerificationSubject(str, Enum):
    """What a verification claim is about."""

    TASK = "task"
    STEP = "step"
    OPERATION = "operation"
    FILE = "file"
    ENVIRONMENT = "environment"
    RUN = "run"
    UNKNOWN = "unknown"


class VerificationMethod(str, Enum):
    """How a verification claim was established.

    ``NONE`` is the default and can never support ``PASSED``.
    """

    NONE = "none"
    EXECUTION_OUTCOME = "execution_outcome"
    FILE_AND_OUTPUT = "file_and_output"
    RENDER_FILE_PROBE = "render_file_probe"
    IMAGE_INSPECTION = "image_inspection"
    BLENDER_VISION_RENDER = "blender_vision_render"
    TOOL_OBSERVATION = "tool_observation"
    CAPABILITY_EVIDENCE = "capability_evidence"
    CAPABILITY_EXECUTION = "capability_execution"
    WORKFLOW_CONTRACT = "workflow_contract"
    ANALYSIS = "analysis"
    DEBUG_RUN = "debug_run"


class EvidenceTrust(str, Enum):
    """How much a source is allowed to prove.

    The Phase 1 model only needs to answer one question: is this source
    trustworthy enough to support ``PASSED``? Three levels are enough, and only
    :attr:`OBSERVED` can.

    ``UNTRUSTED``
        No defined evidence semantics: empty/default verification, free text, a
        bare narration string, or a source the ledger cannot place.
    ``CLAIMED``
        Self-reported success the ledger cannot independently confirm: a model
        asserting it finished, or any assertion whose supporting evidence is
        itself only asserted. A claim may be recorded, never believed.
    ``OBSERVED``
        Established by an independent observation the ledger can name and check:
        a filesystem probe, a process exit code, an image decode, or a declared
        evidence contract that is object-shaped and substantive (the
        ``capability_evidence`` contract). This is the only level that may
        support ``PASSED``.

    Note the distinction from Phase 1's first draft, which listed "a capability
    reporting ``verified=True`` about itself" under ``CLAIMED``. Phase 2 settled
    this against the real architecture: a capability evidence contract that
    carries a substantive, object-shaped, non-contradictory payload is a
    *declared contract* and is ``OBSERVED``, while a bare or malformed payload
    has no defined semantics and is rejected outright rather than believed.
    """

    UNTRUSTED = "untrusted"
    CLAIMED = "claimed"
    OBSERVED = "observed"


#: Evidence keys whose value is an assertion about the subject. The ledger
#: requires them to be strictly boolean when present, and refuses ``PASSED``
#: when one is ``False``. This encodes the existing
#: ``capability_bridge`` rule (``data.get("valid") is False`` -> ``FAILED``)
#: generically, by key name, without embedding any tool's semantics.
ASSERTION_KEYS = frozenset(
    {"valid", "exists", "verified", "ok", "success", "passed", "readable", "complete"}
)

#: Explicit ``legacy kind -> (subject_kind, method_kind)`` mapping. Covers every
#: kind currently constructed in the codebase. Phase 2 migrates writers by
#: looking here rather than by guessing.
LEGACY_KIND_AXES: dict[str, tuple[VerificationSubject, VerificationMethod]] = {
    "capability_analysis": (VerificationSubject.RUN, VerificationMethod.ANALYSIS),
    "capability_evidence": (VerificationSubject.OPERATION, VerificationMethod.CAPABILITY_EVIDENCE),
    "capability_execution": (VerificationSubject.OPERATION, VerificationMethod.CAPABILITY_EXECUTION),
    "blender_vision_render": (VerificationSubject.FILE, VerificationMethod.BLENDER_VISION_RENDER),
    "debug": (VerificationSubject.RUN, VerificationMethod.DEBUG_RUN),
    "execution": (VerificationSubject.TASK, VerificationMethod.EXECUTION_OUTCOME),
    "file_and_output": (VerificationSubject.FILE, VerificationMethod.FILE_AND_OUTPUT),
    "image_inspection": (VerificationSubject.FILE, VerificationMethod.IMAGE_INSPECTION),
    "render_file": (VerificationSubject.FILE, VerificationMethod.RENDER_FILE_PROBE),
    "tool_observation": (VerificationSubject.ENVIRONMENT, VerificationMethod.TOOL_OBSERVATION),
    "workflow_verification": (VerificationSubject.RUN, VerificationMethod.WORKFLOW_CONTRACT),
}


def resolve_legacy_kind(kind: str) -> tuple[VerificationSubject, VerificationMethod]:
    """Resolve a legacy ``kind`` to its three-axis subject/method.

    An empty or unrecognised kind resolves to ``(UNKNOWN, NONE)``. The legacy
    string itself is always preserved separately on the entry, so an
    unrecognised kind loses no information - it simply cannot support PASSED.
    """
    return LEGACY_KIND_AXES.get(str(kind or ""), (VerificationSubject.UNKNOWN, VerificationMethod.NONE))


def _coerce_ex(value: Any, kind: type[Enum], fallback: Enum) -> tuple[Any, bool]:
    """Coerce to an enum member, reporting whether the input was recognised.

    ``isinstance`` must be checked first: these enums subclass ``str``, so
    ``str(member)`` yields ``"VerificationState.PASSED"`` rather than
    ``"passed"`` and re-lookup by ``str()`` would fail.
    """
    if isinstance(value, kind):
        return value, True
    if isinstance(value, str):
        try:
            return kind(value), True
        except ValueError:
            return fallback, False
    return fallback, False


def _coerce(value: Any, kind: type[Enum], fallback: Enum) -> Any:
    """Best-effort coercion to an enum member. Never raises."""
    return _coerce_ex(value, kind, fallback)[0]


def _safe_int(value: Any, fallback: int) -> int:
    """Coerce to int, tolerating malformed persisted data."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _is_substantive(value: Any) -> bool:
    """True when ``value`` carries real information.

    A container is substantive only when at least one leaf is. This is what
    makes ``{"evidence": {}}`` and ``{"evidence": None}`` empty.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        # 0 is substantive: ``{"exit_code": 0}`` is a real observation.
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_is_substantive(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_is_substantive(item) for item in value)
    return bool(value)


def _validate_evidence(evidence: Any) -> str:
    """Return "" when the evidence can support PASSED, else the failure reason."""
    if evidence is None:
        return "evidence is missing"
    if isinstance(evidence, (str, bytes, bytearray)):
        # Narration is not evidence.
        return "evidence is a string, not a structured evidence container"
    if not isinstance(evidence, Mapping):
        # Deliberately stricter than ``capability_bridge._valid_evidence``, which
        # also accepts a bare list/tuple. A bare sequence carries no defined
        # evidence semantics, and the durable contract
        # (``VerificationResult.evidence`` / ``TaskManager.set_verification``,
        # which does ``dict(evidence)``) is a mapping. Stricter, never weaker.
        return f"evidence is {type(evidence).__name__}, not a mapping"
    for key in evidence.keys():
        if not isinstance(key, str):
            return f"evidence has a non-string key {key!r}"
    for key in ASSERTION_KEYS:
        if key not in evidence:
            continue
        value = evidence[key]
        if not isinstance(value, bool):
            return f"evidence assertion {key!r} is {value!r}, not a boolean"
        if value is False:
            return f"evidence assertion {key!r} is False"
    if not _is_substantive(evidence):
        return "evidence is empty"
    try:
        json.dumps(evidence)
    except (TypeError, ValueError) as exc:
        return f"evidence is not JSON-serialisable ({exc.__class__.__name__})"
    return ""


def adjudicate(
    state: Any,
    subject_kind: Any = VerificationSubject.UNKNOWN,
    method_kind: Any = VerificationMethod.NONE,
    trust: Any = EvidenceTrust.UNTRUSTED,
    evidence: Any = None,
) -> tuple[VerificationState, str]:
    """Apply the PASSED invariant.

    Returns the adjudicated ``(state, reason)``. ``reason`` is "" when nothing
    was changed. Only a requested ``PASSED`` can be downgraded, and it is
    downgraded to ``UNVERIFIED`` - never silently kept.
    """
    resolved, recognised = _coerce_ex(state, VerificationState, VerificationState.UNVERIFIED)
    if not recognised:
        # Fail closed: an unreadable state is never treated as a verdict.
        return VerificationState.UNVERIFIED, f"unrecognised verification state {state!r}"
    if resolved is not VerificationState.PASSED:
        # Never upgrade a writer's own non-PASSED verdict.
        return resolved, ""

    subject = _coerce(subject_kind, VerificationSubject, VerificationSubject.UNKNOWN)
    method = _coerce(method_kind, VerificationMethod, VerificationMethod.NONE)
    source = _coerce(trust, EvidenceTrust, EvidenceTrust.UNTRUSTED)

    if subject is VerificationSubject.UNKNOWN:
        return VerificationState.UNVERIFIED, "subject_kind is unknown"
    if method is VerificationMethod.NONE:
        return VerificationState.UNVERIFIED, "method_kind is none"
    if source is not EvidenceTrust.OBSERVED:
        return VerificationState.UNVERIFIED, f"trust is {source.value}, not observed"

    reason = _validate_evidence(evidence)
    if reason:
        return VerificationState.UNVERIFIED, reason
    return VerificationState.PASSED, ""


@dataclass(frozen=True)
class VerificationEntry:
    """One immutable, adjudicated verification record.

    ``state`` is the adjudicated state. ``requested_state`` is what the writer
    asked for, retained so a downgrade stays auditable. ``reason`` explains a
    downgrade and is "" otherwise. ``legacy_kind`` is the writer's original
    opaque ``kind``, verbatim.
    """

    state: VerificationState
    subject_kind: VerificationSubject = VerificationSubject.UNKNOWN
    method_kind: VerificationMethod = VerificationMethod.NONE
    trust: EvidenceTrust = EvidenceTrust.UNTRUSTED
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    legacy_kind: str = ""
    requested_state: VerificationState | None = None
    reason: str = ""
    task_id: str = ""
    run_id: str = ""
    request_id: str = ""
    step_id: str = ""
    seq: int = -1
    recorded_at: str = ""

    # ------------------------------------------------------------- factories

    @classmethod
    def create(
        cls,
        state: Any,
        *,
        subject_kind: Any = VerificationSubject.UNKNOWN,
        method_kind: Any = VerificationMethod.NONE,
        trust: Any = EvidenceTrust.UNTRUSTED,
        detail: str = "",
        evidence: Any = None,
        legacy_kind: str = "",
        task_id: str = "",
        run_id: str = "",
        request_id: str = "",
        step_id: str = "",
        recorded_at: str = "",
    ) -> "VerificationEntry":
        """Build an entry, applying the PASSED invariant.

        Never raises. ``evidence`` is normalised to a mapping so the entry stays
        JSON-round-trippable; a rejected list/tuple is retained under ``items``
        so the record of what was claimed is never lost.
        """
        requested = _coerce(state, VerificationState, VerificationState.UNVERIFIED)
        # The raw ``state`` is handed to adjudicate so an unrecognised value is
        # reported rather than silently pre-coerced into a real verdict.
        resolved, reason = adjudicate(state, subject_kind, method_kind, trust, evidence)
        if isinstance(evidence, Mapping):
            safe_evidence: dict[str, Any] = dict(evidence)
        elif isinstance(evidence, (list, tuple)):
            safe_evidence = {"items": list(evidence)}
        else:
            safe_evidence = {}
        return cls(
            state=resolved,
            subject_kind=_coerce(subject_kind, VerificationSubject, VerificationSubject.UNKNOWN),
            method_kind=_coerce(method_kind, VerificationMethod, VerificationMethod.NONE),
            trust=_coerce(trust, EvidenceTrust, EvidenceTrust.UNTRUSTED),
            detail=str(detail or ""),
            evidence=safe_evidence,
            legacy_kind=str(legacy_kind or ""),
            requested_state=requested,
            reason=reason,
            task_id=str(task_id or ""),
            run_id=str(run_id or ""),
            request_id=str(request_id or ""),
            step_id=str(step_id or ""),
            recorded_at=str(recorded_at or _now()),
        )

    @classmethod
    def from_legacy_kind(cls, legacy_kind: str, state: Any, **kwargs: Any) -> "VerificationEntry":
        """Build an entry from a legacy ``kind``, resolving the three axes.

        Explicit subject/method/trust passed by the caller win; anything left
        unset is resolved from :data:`LEGACY_KIND_AXES`.
        """
        subject, method = resolve_legacy_kind(legacy_kind)
        trust = EvidenceTrust.OBSERVED if method is not VerificationMethod.NONE else EvidenceTrust.UNTRUSTED
        kwargs.setdefault("subject_kind", subject)
        kwargs.setdefault("method_kind", method)
        kwargs.setdefault("trust", trust)
        return cls.create(state, legacy_kind=legacy_kind, **kwargs)

    @classmethod
    def from_result(
        cls,
        result: VerificationResult,
        *,
        subject_kind: Any = None,
        method_kind: Any = None,
        trust: Any = None,
        legacy_kind: str | None = None,
        **kwargs: Any,
    ) -> "VerificationEntry":
        """Compatibility adapter from :class:`VerificationResult`.

        Defaults ``legacy_kind`` to ``result.kind`` and resolves the three axes
        from it, so an unmigrated writer can be routed through the ledger
        without losing its legacy vocabulary.
        """
        kind = result.kind if legacy_kind is None else legacy_kind
        subject, method = resolve_legacy_kind(kind)
        return cls.create(
            result.state,
            subject_kind=subject if subject_kind is None else subject_kind,
            method_kind=method if method_kind is None else method_kind,
            trust=(EvidenceTrust.OBSERVED if method is not VerificationMethod.NONE else EvidenceTrust.UNTRUSTED)
            if trust is None
            else trust,
            detail=result.detail,
            evidence=result.evidence,
            legacy_kind=kind,
            **kwargs,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationEntry":
        """Rebuild an entry from :meth:`to_dict` output, preserving adjudication.

        Total by design: persisted data may be truncated, hand-edited or written
        by an older schema, so every field degrades to its safe default instead
        of raising. A malformed record must never abort loading a task, and must
        never be able to manufacture a PASSED.
        """
        if not isinstance(data, Mapping):
            return cls(
                state=VerificationState.UNVERIFIED,
                reason=f"unreadable verification entry of type {type(data).__name__}",
            )
        evidence = data.get("evidence")
        requested = data.get("requested_state")
        return cls(
            state=_coerce(data.get("state"), VerificationState, VerificationState.UNVERIFIED),
            subject_kind=_coerce(data.get("subject_kind"), VerificationSubject, VerificationSubject.UNKNOWN),
            method_kind=_coerce(data.get("method_kind"), VerificationMethod, VerificationMethod.NONE),
            trust=_coerce(data.get("trust"), EvidenceTrust, EvidenceTrust.UNTRUSTED),
            detail=str(data.get("detail") or ""),
            evidence=dict(evidence) if isinstance(evidence, Mapping) else {},
            legacy_kind=str(data.get("legacy_kind") or ""),
            requested_state=_coerce(requested, VerificationState, VerificationState.UNVERIFIED)
            if requested
            else None,
            reason=str(data.get("reason") or ""),
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            request_id=str(data.get("request_id") or ""),
            step_id=str(data.get("step_id") or ""),
            seq=_safe_int(data.get("seq"), -1),
            recorded_at=str(data.get("recorded_at") or ""),
        )

    # ------------------------------------------------------------ inspection

    @property
    def downgraded(self) -> bool:
        """True when a requested PASSED was refused by the invariant."""
        return self.requested_state is VerificationState.PASSED and self.state is not VerificationState.PASSED

    @property
    def legacy_kind_known(self) -> bool:
        """True when the legacy kind is present in :data:`LEGACY_KIND_AXES`."""
        return self.legacy_kind in LEGACY_KIND_AXES

    def with_identity(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        request_id: str = "",
        step_id: str = "",
    ) -> "VerificationEntry":
        """Return a copy stamped with authoritative correlation identity.

        The ledger never mints these; the caller passes the S1-authoritative
        values down.
        """
        return replace(
            self,
            task_id=task_id or self.task_id,
            run_id=run_id or self.run_id,
            request_id=request_id or self.request_id,
            step_id=step_id or self.step_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, suitable for later durable persistence."""
        return {
            "seq": self.seq,
            "state": self.state.value,
            "requested_state": self.requested_state.value if self.requested_state else None,
            "subject_kind": self.subject_kind.value,
            "method_kind": self.method_kind.value,
            "trust": self.trust.value,
            "detail": self.detail,
            "evidence": dict(self.evidence),
            "legacy_kind": self.legacy_kind,
            "reason": self.reason,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "step_id": self.step_id,
            "recorded_at": self.recorded_at,
        }


class VerificationLedger:
    """Append-only store of :class:`VerificationEntry` records.

    Ordering is insertion order and is never rewritten. ``seq`` is assigned on
    append. The store is bounded by :attr:`MAX_ENTRIES`; older entries are
    dropped oldest-first and counted in :attr:`dropped` so truncation is never
    silent.

    Two entry points:

    * :meth:`record` - forgiving. Applies the PASSED invariant and always
      yields a safe entry. Never raises for a bad value.
    * :meth:`append` - strict. Raises :class:`VerificationLedgerError` for a
      structurally invalid entry and leaves the ledger untouched.

    Phase 1 does not persist this. The ``to_dict`` / :meth:`from_dicts` pair
    exists so Phase 2 can store it without redesigning the schema.
    """

    #: Upper bound on retained entries. Mirrors the bounded-history habit in
    #: ``TaskManager.invalidate_verification`` (``[-20:]``) at a larger scale.
    MAX_ENTRIES = 200

    #: Upper bound on the slice written into a persisted task record.
    #:
    #: This is a *different layer* from :attr:`MAX_ENTRIES` and from
    #: ``TaskManager.verification_history``, and the two are deliberately not
    #: merged:
    #:
    #: * :attr:`MAX_ENTRIES` bounds the **in-memory adjudication ledger** - every
    #:   verdict ever requested for this task, kept so recovery can ask "what
    #:   did the last verification actually establish?".
    #: * :attr:`PERSISTED_TAIL` bounds the **persisted projection** written into
    #:   ``TaskRecord.metadata["lifecycle"]``. Records are re-serialised on every
    #:   ``_sync_task_metadata`` call, so this stays small on purpose.
    #: * ``TaskManager.verification_history`` is a third thing again: it records
    #:   *invalidations* ("what was true before a later operation invalidated
    #:   it?"), not adjudications.
    #:
    #: All three are bounded, so no layer can grow without limit.
    PERSISTED_TAIL = 20

    def __init__(self, max_entries: int | None = None) -> None:
        limit = self.MAX_ENTRIES if max_entries is None else int(max_entries)
        if limit <= 0:
            raise VerificationLedgerError(f"max_entries must be positive, got {max_entries!r}")
        self._max_entries = limit
        self._entries: list[VerificationEntry] = []
        self._next_seq = 0
        self.dropped = 0

    # -------------------------------------------------------------- writing

    def record(
        self,
        state: Any = None,
        *,
        result: VerificationResult | None = None,
        legacy_kind: str | None = None,
        subject_kind: Any = None,
        method_kind: Any = None,
        trust: Any = None,
        detail: str = "",
        evidence: Any = None,
        task_id: str = "",
        run_id: str = "",
        request_id: str = "",
        step_id: str = "",
    ) -> VerificationEntry:
        """Adjudicate and append one entry. Never raises.

        Accepts either a raw ``state`` or an existing
        :class:`VerificationResult` via ``result=``. Unset axes are resolved
        from ``legacy_kind`` (defaulting to ``result.kind``).
        """
        if result is not None:
            entry = VerificationEntry.from_result(
                result,
                subject_kind=subject_kind,
                method_kind=method_kind,
                trust=trust,
                legacy_kind=legacy_kind,
                task_id=task_id,
                run_id=run_id,
                request_id=request_id,
                step_id=step_id,
            )
        else:
            kind_str = str(legacy_kind or "")
            fallback_subject, fallback_method = resolve_legacy_kind(kind_str)
            method = method_kind if method_kind is not None else fallback_method
            entry = VerificationEntry.create(
                state if state is not None else VerificationState.UNVERIFIED,
                subject_kind=subject_kind if subject_kind is not None else fallback_subject,
                method_kind=method,
                trust=trust
                if trust is not None
                else (
                    EvidenceTrust.OBSERVED
                    if method is not VerificationMethod.NONE
                    else EvidenceTrust.UNTRUSTED
                ),
                detail=detail,
                evidence=evidence,
                legacy_kind=kind_str,
                task_id=task_id,
                run_id=run_id,
                request_id=request_id,
                step_id=step_id,
            )
        return self.append(entry)

    def append(self, entry: VerificationEntry) -> VerificationEntry:
        """Strictly append a pre-built entry. Atomic: rejects without mutating.

        Raises :class:`VerificationLedgerError` when ``entry`` is not a
        :class:`VerificationEntry`, or when it is internally inconsistent
        (``state`` is PASSED without the invariant holding).
        """
        if not isinstance(entry, VerificationEntry):
            raise VerificationLedgerError(f"expected VerificationEntry, got {type(entry).__name__}")
        if entry.state is VerificationState.PASSED:
            expected, reason = adjudicate(
                VerificationState.PASSED,
                entry.subject_kind,
                entry.method_kind,
                entry.trust,
                entry.evidence,
            )
            if expected is not VerificationState.PASSED:
                raise VerificationLedgerError(f"PASSED entry violates the invariant: {reason}")
        stamped = replace(entry, seq=self._next_seq, recorded_at=entry.recorded_at or _now())
        self._next_seq += 1
        self._entries.append(stamped)
        while len(self._entries) > self._max_entries:
            self._entries.pop(0)
            self.dropped += 1
        return stamped

    # -------------------------------------------------------------- reading

    @property
    def entries(self) -> tuple[VerificationEntry, ...]:
        """Retained entries in insertion order."""
        return tuple(self._entries)

    @property
    def latest(self) -> VerificationEntry | None:
        return self._entries[-1] if self._entries else None

    @property
    def latest_state(self) -> VerificationState:
        """Adjudicated state of the newest entry, or ``NOT_RUN`` when empty."""
        latest = self.latest
        return latest.state if latest is not None else VerificationState.NOT_RUN

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(tuple(self._entries))

    # ----------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot, suitable for later durable persistence."""
        return {
            "schema": "verification_ledger/1",
            "max_entries": self._max_entries,
            "next_seq": self._next_seq,
            "dropped": self.dropped,
            "entries": [entry.to_dict() for entry in self._entries],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationLedger":
        """Rebuild a ledger from :meth:`to_dict` output.

        Total by design. Anything unreadable is skipped rather than raised, so a
        corrupt or older persisted projection cannot stop a task from loading.
        Skipped entries are not silently lost: the reconstructed ledger reports
        them through :attr:`dropped`.
        """
        if not isinstance(data, Mapping):
            return cls()
        limit = _safe_int(data.get("max_entries"), cls.MAX_ENTRIES)
        ledger = cls(max_entries=limit if limit > 0 else cls.MAX_ENTRIES)
        raw_entries = data.get("entries")
        if isinstance(raw_entries, Mapping) or not isinstance(raw_entries, (list, tuple)):
            raw_entries = ()
        skipped = 0
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                skipped += 1
                continue
            entry = VerificationEntry.from_dict(raw)
            try:
                ledger.append(entry)
            except VerificationLedgerError as exc:
                # A persisted PASSED that no longer satisfies the invariant must
                # not be trusted on load. Keep the record for audit, but never as
                # a PASSED: fail closed rather than refuse to load the task.
                ledger.append(
                    replace(
                        entry,
                        state=VerificationState.UNVERIFIED,
                        reason=f"persisted entry failed the PASSED invariant on load: {exc}",
                    )
                )
        ledger.dropped = _safe_int(data.get("dropped"), 0) + skipped
        ledger._next_seq = max(ledger._next_seq, _safe_int(data.get("next_seq"), 0))
        return ledger

    def to_persisted_dict(self, limit: int | None = None) -> dict[str, Any]:
        """Bounded projection for storage inside an existing task record.

        Carries only the newest ``limit`` (default :attr:`PERSISTED_TAIL`)
        entries plus the counters needed to show that truncation happened. The
        oldest entries stay in memory; this is a projection, not a second store.
        """
        keep = self.PERSISTED_TAIL if limit is None else max(0, int(limit))
        tail = self._entries[-keep:] if keep else []
        return {
            "schema": "verification_ledger/1",
            "max_entries": self._max_entries,
            "persisted_tail": keep,
            "next_seq": self._next_seq,
            "dropped": self.dropped,
            "entry_count": len(self._entries),
            "entries": [entry.to_dict() for entry in tail],
        }
