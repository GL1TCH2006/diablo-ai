# DIABLO — DEBUG DISPATCH AND CANCELLATION BUG FIX REPORT

**Repository:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Fix date:** 2026-11-01  
**Branch:** `backup/pre-m3-0` (HEAD `94556a1`, in sync with `origin/backup/pre-m3-0`)  
**Constraint:** No commit, push, stash, reset, clean, checkout, or branch switch. All pre-existing modified/untracked files preserved. No unrelated architecture changed.

---

## Root cause

Two defects in `core/agent.py`, both introduced by the incomplete async conversion of the Agent request path during M3.1:

### Defect 1 — Erroneous `await` on a synchronous method (Phase 1)

`_respond()` (made `async`) calls the debug path at line 2497:

```python
reply = await self._run_debug(user_input)
```

but `_run_debug()` (line 3101) is a **synchronous** `def` that returns a `str`. `await <str>` raises:

```
TypeError: 'str' object can't be awaited
```

The sibling `_run_intent()` was correctly converted to `async def`; `_run_debug()` was missed. `DebugLoop.diagnose()` (called inside `_run_debug`) is also synchronous, so no async wrapper is needed — keeping `_run_debug` synchronous is correct (confirmed by the existing direct-sync test `test_debug_failures_project_into_task_status` in `tests/test_phase3_verification.py`).

### Defect 2 — Non-cancellation-safe failure handling (Phase 2)

In the failing security tests, the confirmer denies an out-of-roots write, so `DebugLoop` cancels the report and `_run_debug()` transitions the task to `TaskState.CANCELLED` (line 3117) **then returns a `str` normally**. The erroneous `await` then raises `TypeError`, which is caught by `_respond`'s `except Exception` handler, which calls `_mark_failure()` → `_transition(TaskState.FAILED)`:

```
ValueError: invalid task transition: cancelled -> failed
```

`CANCELLED` is terminal; `_mark_failure()` unconditionally transitioned to `FAILED`, violating the task-state invariant and crashing the handler on an already-terminal cancelled task.

---

## Files changed

| File | Change | Rationale |
|---|---|---|
| `core/agent.py` | `await self._run_debug(user_input)` → `self._run_debug(user_input)` (line 2497) | Phase 1: remove the invalid `await` on the synchronous `_run_debug`. No async wrapper introduced. |
| `core/agent.py` | `_mark_failure()` returns early when `self._tasks.state == TaskState.CANCELLED` (no `FAILED` transition, no metadata overwrite) | Phase 2: cancellation is terminal; a cancelled task can never be relabelled `FAILED`. |
| `core/agent.py` | `except Exception` handler: when state is `CANCELLED`, sets `reply = "Task cancelled."` instead of calling `_mark_failure` / `reply = "Task failed: ..."` | Phase 2: the outer handler does not overwrite an already-terminal `CANCELLED` state. Genuine non-cancellation exceptions still produce `FAILED`. |
| `tests/test_phase3_verification.py` | Added `TaskState` import + 2 targeted tests | Phase 3: unit coverage for the cancellation-safe `_mark_failure` guard. |

No other files touched. `core/cancellation.py`, `tools/python.py`, `tools/terminal.py` remain as they were (pre-existing M3 modifications, preserved).

---

## Explanation of each change

### 1. `core/agent.py` line 2497 — dispatch correction

```python
# before
reply = await self._run_debug(user_input)
# after
reply = self._run_debug(user_input)
```

`_run_debug()` builds a `DebugLoop`, calls the synchronous `loop.diagnose()`, applies the report (cancellation → `CANCELLED`, exhaustion/failure → `_mark_failure`, verified pass → verification ledger), and returns a `str` reply. It contains no `await`, so it must not be awaited. Keeping it synchronous preserves the existing direct-invocation contract and avoids introducing unnecessary async wrappers or thread execution, per the mission.

### 2. `core/agent.py` `_mark_failure()` — cancellation guard

```python
def _mark_failure(self, detail: str) -> None:
    # Cancellation is terminal: a cancelled task must never be relabelled as
    # FAILED, and its metadata must not be overwritten by a later failure.
    if self._tasks.state == TaskState.CANCELLED:
        return
    ...
    self._transition(TaskState.FAILED)
    self._sync_task_metadata()
```

When already `CANCELLED`, it returns without touching state or `failure_reason`. `CANCELLED` stays terminal; the task is not misreported as failed. `TASK_TRANSITIONS` / terminal-state definitions are unchanged.

### 3. `core/agent.py` `except Exception` handler — cancellation-aware reply

```python
except Exception as exc:
    log.error("Agent request failed: %s", exc, exc_info=True)
    if self._tasks.state == TaskState.CANCELLED:
        # Cancellation dominates: do not relabel a cancelled task as FAILED.
        reply = "Task cancelled."
    else:
        self._mark_failure(f"{type(exc).__name__}: {exc}")
        reply = f"Task failed: {exc}"
```

A genuine non-cancellation exception still transitions to `FAILED` (via `_mark_failure`) and reports `Task failed: ...`. When the task is already cancelled, the handler reports cancellation and never transitions to `FAILED`. Cancellation is not silently reported as successful completion, and logging/diagnostics are preserved.

### 4. `tests/test_phase3_verification.py` — targeted tests

- `test_mark_failure_does_not_relabel_cancelled_task_as_failed`: state stays `CANCELLED` after `_mark_failure`.
- `test_mark_failure_still_transitions_genuine_failure_to_failed`: a genuine failure transitions `PLANNING → FAILED` with an accurate `failure_reason`.

---

## Tests executed and exact results

All runs: `.venv\Scripts\python.exe -m pytest ...` on Windows, Python 3.14.6, pytest 9.1.1. Tests use `tmp_path`-based settings (temp dirs only); no tracked files modified, no real external actions, no paid APIs, no network model calls.

| # | Command | Result | Exit code | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `pytest tests/test_recovery_p10_repair.py -q` | PASS | 0 | 52 | 0 | 0 | Includes the 4 previously failing `TestRepairCannotEscapeItsProject` security tests |
| 2 | `pytest tests/test_phase3_verification.py -q` | 13 pass + 1 pre-existing fail | 1 | 13 | 1 | 0 | The 1 failure (`test_debug_failures_project_into_task_status`, `AttributeError: 'Agent' object has no attribute '_executor_agent'`) is **pre-existing and unrelated** — `_bare_agent` never sets an executor, and baseline `HEAD` had the same defect (`_run_debug` referenced `self._executor`). My 2 new tests pass. |
| 3 | `pytest tests/test_agent.py tests/test_terminal.py tests/test_python.py tests/test_terminal_state_invariant.py tests/test_verification.py tests/test_verification_ledger.py tests/test_memory_safety_and_rule_scope.py tests/test_filesystem_containment.py tests/test_filesystem_delete_verification.py tests/test_applications.py -q` | PASS | 0 | 264 | 0 | 0 | Bounded regression suite |
| 4 | `pytest tests/test_m213_coding_workflow.py -q` | PASS | 0 | 34 | 0 | 0 | Exercises debug/intent paths through `respond` |

**Totals across runs: 363 passed, 1 failed (pre-existing, unrelated), 0 skipped.**

The four tests that were failing before this fix now pass:
- `test_a_traversing_write_path_is_refused`
- `test_a_sibling_outside_the_roots_is_not_writable`
- `test_a_denial_outside_the_roots_ends_cancelled`
- `test_a_write_inside_the_project_is_allowed`

**Not run:** `python main.py --offline --demo` smoke test (may invoke real external/Ollama connections; prohibited by the bounded, no-external-action rule) and the full `tests/` suite (Blender, Windows UIA, real-model, GUI/display tests that could invoke external actions or hang).

---

## Remaining risks

1. **Pre-existing broken test** `tests/test_phase3_verification.py::test_debug_failures_project_into_task_status`: fails with `AttributeError: 'Agent' object has no attribute '_executor_agent'` because `_bare_agent` never sets up an executor. This defect exists at baseline `HEAD` too (baseline `_run_debug` referenced `self._executor`, equally absent from `_bare_agent`). It is orthogonal to this fix and was not introduced here. Fixing it would require test-infrastructure changes outside this mission's scope.
2. **Smoke test unverified** — `python main.py --offline --demo` claimed 4/4 PASS in prior reports but was not independently executed here (external-action risk).
3. **Uncommitted work-in-progress** — the 4 new `core/` modules, `data/`, `voice_recordings/`, and the audit reports remain untracked; a destructive git operation would lose them. No git state was changed during this fix.
4. **Duplicate planner/executor instances** — `core/agent.py` still instantiates both legacy (`self._planner`, `self._executor`) and new (`self._planner_agent`, `self._executor_agent`) components; only the new path drives execution. Left for a future consolidation task (out of scope; no architecture change here).

---

## Final Git status

Verbatim `git status --short --branch` (no git state changed during this fix):

```
## backup/pre-m3-0...origin/backup/pre-m3-0
 M core/agent.py
 M core/cancellation.py
 M tests/test_phase3_verification.py
 M tools/python.py
 M tools/terminal.py
?? AUDIT_REPORTS/M3_1_MISSION_20261025_2207SS/
?? AUDIT_REPORTS/M3_CAMPAIGN_20261027_0124SS/
?? AUDIT_REPORTS/M3_RESUMPTION_AUDIT_20261029/
?? core/async_executor.py
?? core/executor_agent.py
?? core/planner_agent.py
?? core/process_manager.py
?? data/
?? voice_recordings/
```

HEAD: `94556a1` (in sync with `origin/backup/pre-m3-0`). No commits, pushes, stashes, resets, or branch switches performed.

---

**Stop.** The fix is complete and verified; no further implementation was undertaken beyond this mission's scope.