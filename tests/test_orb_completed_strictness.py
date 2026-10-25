"""Batch 7 Task 2 - `completed` strictness, exhaustively.

The contract being pinned:

* ``completed`` requires an **explicit PASSED** verification outcome;
* that verification must appear **BEFORE** ``task_completed``;
* a ``verifying`` event - verification *in progress* - must **never** count as
  verified, in any order and any combination;
* ``unverified`` and ``failed`` stay distinct from each other and from
  ``completed``;
* everything else fails closed.

The projection is driven headlessly - no window is opened anywhere.
"""

from __future__ import annotations

import itertools
import time

import pytest

from ui.companion_state import CompanionEventKind, CompanionSnapshot, CompanionState
from ui.orb import OrbState
from ui.orb.state_map import map_snapshot, visual_state


def _clock(v: float = 0.0):
    return lambda: v


def _snapshot(state, events=()):
    return CompanionSnapshot(state=state, task_id="t1", events=tuple(events))


class _Passed:
    kind = "verification_passed"
    passed = True
    def __init__(self, tag=""): self.message = f"passed {tag}"; self.summary = ""; self.task_id = "t1"

class _Failed:
    kind = "verification_failed"
    passed = False
    def __init__(self, tag=""): self.message = f"failed {tag}"; self.summary = ""; self.task_id = "t1"

class _Verifying:
    """Verification IN PROGRESS. Must never license `completed`.

    Note the distinct spelling ``"verifying"``. The production enum has no such
    member - ``CompanionEventKind.VERIFICATION`` renders as ``"verification"`` -
    so this exercises the alternate spelling the mapper must treat identically.
    """
    kind = "verifying"
    def __init__(self, tag=""): self.message = f"verifying {tag}"; self.summary = ""; self.task_id = "t1"

class _Bare:
    """A verification event that states no outcome at all."""
    kind = CompanionEventKind.VERIFICATION
    def __init__(self, tag=""): self.message = f"verification {tag}"; self.summary = ""; self.task_id = "t1"

class _Completed:
    kind = CompanionEventKind.TASK_COMPLETED
    def __init__(self, tag=""): self.message = "task_completed"; self.summary = ""; self.task_id = "t1"

class _Cancelled:
    kind = CompanionEventKind.TASK_CANCELLED
    def __init__(self, tag=""): self.message = "task_cancelled"; self.summary = ""; self.task_id = "t1"


PASSED, FAILED, VERIFYING, BARE, COMPLETED = _Passed, _Failed, _Verifying, _Bare, _Completed
TOKENS = (PASSED, FAILED, VERIFYING, BARE)

#: ``_last_matching`` scans backwards for the most recent RECOGNISED kind. These are
#: the kinds it recognises. A trailing event outside this set is ignored; a trailing
#: event inside it hides a completion that came before it.
_RECOGNISED = {
    "task_completed", "task_cancelled", "task_failed", "error", "verification",
    "permission", "permission_required", "recovery", "step", "executing",
    "thinking", "task_accepted",
}


def _kind_value(token_cls) -> str:
    kind = getattr(token_cls, "kind", "")
    return str(getattr(kind, "value", kind) or "").strip().lower()


def _map(events):
    return map_snapshot(_snapshot(CompanionState.RETURNING, events), clock=_clock())


# ═══════════════════════════════════════ every ordering of the four tokens


def test_exhaustive_orderings_of_every_verification_token():
    """Every ordered pair of the four verification tokens plus ``task_completed``.

    ``completed`` may appear ONLY when the prefix before ``task_completed``
    contains a passed token and no failed token.
    """
    cases = []
    for combo in itertools.product(TOKENS, repeat=2):
        for order in itertools.permutations(combo + (_Completed,)):
            cases.append(order)
    for solo in TOKENS + (_Completed,):
        cases.append((solo,))

    completed_seen = 0
    for case in cases:
        events = [t() for t in case]
        result = _map(events)
        names = [t.__name__ for t in case]
        idx = next((i for i, t in enumerate(case) if t is _Completed), None)

        prefix = case[:idx] if idx is not None else case
        has_passed = any(t is _Passed for t in prefix)
        has_failed = any(t is _Failed for t in prefix)
        # Anything recognised after the completion hides it, by _last_matching.
        trailing = case[idx + 1:] if idx is not None else []
        hidden = any(_kind_value(t) in _RECOGNISED for t in trailing)

        if idx is None:
            assert result.visual_state != OrbState.COMPLETED, (
                f"{names}: completed with no task_completed event at all")
            continue

        if has_passed and not has_failed and not hidden:
            assert result.visual_state == OrbState.COMPLETED, (
                f"{names}: expected completed, got {result.visual_state} - "
                f"{result.reason}")
            assert result.verified is True
            completed_seen += 1
        else:
            assert result.visual_state != OrbState.COMPLETED, (
                f"{names}: CLAIMED COMPLETED without a preceding explicit pass "
                f"(failed={has_failed} hidden={hidden}) - {result.reason}")
            assert result.verified is False
            if has_failed and not hidden:
                assert result.visual_state == OrbState.FAILED, (
                    f"{names}: a failed verification must be FAILED, not "
                    f"{result.visual_state} - {result.reason}")
            else:
                assert result.visual_state == OrbState.UNVERIFIED, (
                    f"{names}: expected unverified, got {result.visual_state} - "
                    f"{result.reason}")
    assert completed_seen > 0, "no ordering produced completed; the matrix is broken"


@pytest.mark.parametrize("a", TOKENS + (_Completed,))
@pytest.mark.parametrize("b", TOKENS + (_Completed,))
def test_every_two_token_ordering(a, b):
    """Exhaustive 5x5 pair sweep, asserted token by token."""
    result = _map([a(), b()])
    idx = 0 if a is _Completed else (1 if b is _Completed else None)
    prefix = [] if idx is None else ([a] if idx == 1 else [])
    has_passed = any(t is _Passed for t in prefix)
    has_failed = any(t is _Failed for t in prefix)

    if idx is None:
        assert result.visual_state != OrbState.COMPLETED
        return
    if has_passed and not has_failed:
        assert result.visual_state == OrbState.COMPLETED, (
            f"[{a.__name__}, {b.__name__}] expected completed: {result.reason}")
    else:
        assert result.visual_state != OrbState.COMPLETED, (
            f"[{a.__name__}, {b.__name__}] licensed completed: {result.reason}")


def test_verifying_never_counts_as_verified_in_any_order():
    """The specific regression Batch 7 was asked to close."""
    for combo in itertools.permutations((_Verifying, _Bare, _Completed)):
        result = _map([t() for t in combo])
        assert result.visual_state != OrbState.COMPLETED, (
            f"{[t.__name__ for t in combo]}: a verifying/bare token licensed completed")


def test_a_passed_verification_AFTER_completion_does_not_count():
    result = _map([_Completed(), _Passed()])
    assert result.visual_state == OrbState.UNVERIFIED, (
        f"a verification arriving after the completion licensed completed: {result.reason}")


def test_failed_before_completion_is_failed_not_unverified():
    result = _map([_Failed(), _Completed()])
    assert result.visual_state == OrbState.FAILED, result.reason
    assert result.visual_state != OrbState.UNVERIFIED, (
        "a failed verification collapsed into unverified")


def test_a_pass_after_a_failure_before_completion_does_not_license_completed():
    result = _map([_Failed(), _Passed(), _Completed()])
    assert result.visual_state != OrbState.COMPLETED, (
        f"a pass after a failure licensed completed: {result.reason}")


def test_a_pass_before_a_failure_before_completion_is_failed():
    result = _map([_Passed(), _Failed(), _Completed()])
    assert result.visual_state == OrbState.FAILED, result.reason


class _AttrTrue:
    kind = CompanionEventKind.VERIFICATION
    passed = True
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"

class _OutcomePassed:
    kind = CompanionEventKind.VERIFICATION
    outcome = "passed"
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"

class _OutcomeVerified:
    kind = CompanionEventKind.VERIFICATION
    outcome = "verified"
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"

class _KindPassed:
    kind = "verification_passed"
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"

class _AttrFalse:
    kind = CompanionEventKind.VERIFICATION
    passed = False
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"

class _OutcomeFailed:
    kind = CompanionEventKind.VERIFICATION
    outcome = "failed"
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"

class _KindFailed:
    kind = "verification_failed"
    def __init__(self, tag=""): self.message = ""; self.summary = ""; self.task_id = "t1"


@pytest.mark.parametrize("cls", [_Passed, _AttrTrue, _OutcomePassed,
                                 _OutcomeVerified, _KindPassed])
def test_all_spellings_of_passed_agree(cls):
    result = _map([cls(), _Completed()])
    assert result.visual_state == OrbState.COMPLETED, (
        f"{cls.__name__} did not license completed: {result.reason}")


@pytest.mark.parametrize("cls", [_Failed, _AttrFalse, _OutcomeFailed, _KindFailed])
def test_all_spellings_of_failed_agree(cls):
    result = _map([cls(), _Completed()])
    assert result.visual_state == OrbState.FAILED, (
        f"{cls.__name__} was not treated as a failure: {result.reason}")


def test_a_bare_verification_event_is_not_completed():
    """The old rule. This must now fail closed."""
    result = _map([_Bare(), _Completed()])
    assert result.visual_state == OrbState.UNVERIFIED, (
        f"a bare verification event still licensed completed: {result.reason}")


def test_verified_is_true_only_for_completed():
    for events in ([_Passed(), _Completed()], [_Failed(), _Completed()],
                   [_Bare(), _Completed()], [_Verifying(), _Completed()],
                   [_Completed()]):
        r = _map(events)
        if r.visual_state == OrbState.COMPLETED:
            assert r.verified is True
        else:
            assert r.verified is False, (
                f"{r.visual_state} reported verified=True: {r.reason}")


def test_the_four_outcomes_stay_distinct():
    got = {
        "completed": _map([_Passed(), _Completed()]).visual_state,
        "failed": _map([_Failed(), _Completed()]).visual_state,
        "unverified": _map([_Completed()]).visual_state,
        "cancelling": _map([_Cancelled()]).visual_state,
    }
    assert len(set(got.values())) == 4, f"outcomes collapsed: {got}"
    assert got["completed"] == OrbState.COMPLETED
    assert got["failed"] == OrbState.FAILED
    assert got["unverified"] == OrbState.UNVERIFIED
    assert got["cancelling"] == OrbState.CANCELLING


# ═══════════════════════ end to end through the REAL projection (headless)


class _FakeClock:
    """A monotonic clock that does not move, so the test stays deterministic."""
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
    def __call__(self) -> float:
        return self.now
    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class _ScriptedBridge:
    """Emits exactly the backend event names the Agent emits."""

    def __init__(self, events):
        self._events = list(events)
        self.subscribers = []

    def subscribe_to_events(self, fn):
        self.subscribers.append(fn)
        return lambda: None

    def submit_message(self, text, task_id=""):
        # Nothing here: the scripted events are emitted by ``emit()`` after the
        # submission has settled, because the projection must be driven through
        # its real ingestion path in a realistic order.
        return {"success": True}

    def emit(self, kind):
        # The backend event shape the real Agent produces: a mapping with a
        # "type" field. A bare string would be dropped by ``map_backend_event``
        # because it has no displayable fields.
        #
        # No ``task_id``: CompanionController.handle_event drops any event whose
        # task_id does not match the current one, and the current one is generated
        # internally, so sending "t1" would be rejected as a stale task.
        payload = {"type": kind, "message": kind, "summary": kind}
        for sub in list(self.subscribers):
            sub(payload)


def _run_projection(events):
    from ui.companion_state import CompanionController
    bridge = _ScriptedBridge(events)
    clock = _FakeClock()
    controller = CompanionController(bridge, clock=clock)
    try:
        # attach() is what subscribes the controller to the bridge - without it
        # backend events go nowhere. The controller then starts asleep and refuses
        # a task until it is listening. Neither call opens a window.
        controller.attach()
        controller.activate()
        clock.advance(1.0)
        controller.tick()
        controller.submit_task("do the thing")
        if not controller.wait_idle(5.0):
            raise AssertionError("the submission never settled")
        # Now drive the projection through its real ingestion path, in the order a
        # genuine run produces: verification (if any) first, then the terminal.
        for kind in events:
            bridge.emit(kind)
            controller.tick()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if controller.snapshot().state is CompanionState.RETURNING:
                break
            controller.tick()
            time.sleep(0.01)
        snap = controller.snapshot()
        return snap, visual_state(snap, clock=_clock())
    finally:
        controller.close()


def test_end_to_end_a_verified_run_through_the_real_projection():
    """Drive the real controller and read the orb state off its own snapshot.

    Batch 7 Task 2 found that the projection collapses
    ``verification_completed`` and ``verification_started`` onto ONE
    ``CompanionEventKind.VERIFICATION`` and drops ``verification_failed``
    entirely, so the orb is never told the outcome. Under the strict rule a
    genuinely verified run therefore reports ``unverified`` rather than
    ``completed``. That is the safe direction and this test pins it, but it also
    records that ``completed`` is currently unreachable from production - see
    FINAL7.md, NEEDS HUMAN DECISION.
    """
    snap, state = _run_projection(["verification_completed", "task_completed"])
    assert snap.state is CompanionState.RETURNING, (
        f"the projection never reached a terminal state: {snap.state}")
    assert state == OrbState.UNVERIFIED, (
        f"expected unverified (the projection states no outcome), got {state}")
    assert state != OrbState.COMPLETED, (
        "the orb claimed completed on evidence the projection never gave it")


def test_end_to_end_an_unverified_run_through_the_real_projection():
    """No verification at all: must be unverified, never completed."""
    snap, state = _run_projection(["task_completed"])
    assert snap.state is CompanionState.RETURNING
    assert state == OrbState.UNVERIFIED
    assert state != OrbState.COMPLETED


def test_end_to_end_a_verifying_in_progress_run_never_completes():
    snap, state = _run_projection(["verification_started", "task_completed"])
    assert snap.state is CompanionState.RETURNING
    assert state != OrbState.COMPLETED, (
        f"verification-in-progress licensed completed: {state}")
