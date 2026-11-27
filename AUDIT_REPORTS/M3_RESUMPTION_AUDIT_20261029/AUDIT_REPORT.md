# DIABLO PRODUCTION RESUMPTION — READ-ONLY RECOVERY AUDIT

**Repository:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Audit date:** 2026-10-29  
**Auditor:** Read-only recovery audit (no implementation, no file modifications)  
**Constraint:** Strictly read-only. No git state changes. No new frameworks. No API charges.

---

## 1. EXECUTIVE SUMMARY

The Diablo project is on branch `backup/pre-m3-0` at HEAD `94556a1` ("Backup: pre-M3.0 state with CI, resource bounds, docs"), in sync with upstream `origin/backup/pre-m3-0`.

The working tree contains an in-progress M3.1 async-execution/cancellation/planner-integration effort: **4 modified tracked files** and **4 new untracked `core/` modules** (`async_executor.py`, `executor_agent.py`, `planner_agent.py`, `process_manager.py`). The M3.1 mission (10-25) and M3 campaign (10-27) reports claim the work is complete with 298/298 tests passing and zero regressions.

**That claim is not supported by the current working tree.** My actual test execution found **4 real, verifiable failures** in security-critical path-containment tests, all caused by a single incomplete async conversion in `core/agent.py`: `_run_debug()` is `await`ed at line 2493 but is defined as a synchronous `def` at line 3093 (it returns a `str`, not a coroutine). The sibling `_run_intent()` was correctly converted to `async def`, but `_run_debug()` was not.

The M3 campaign reports (10-27) describe three fixes that **are** present in the working tree (reconciliation restricted to pending/active tasks; `outcome.allowed = True` after permission check; exhaustion marked only for retryable categories), and those fixes work — the recovery and coding-workflow tests that were reported as regressions now pass. However, the `_run_debug` await bug was not caught, and it breaks 4 security tests.

**Recommended next milestone:** Fix the incomplete async conversion (`_run_debug` → `async def`) and re-verify the full test suite, before considering M3.1/M3 campaign complete or starting M3.2. Do **not** restart, re-architect, or integrate CrewAI.

---

## 2. REPOSITORY STATE (ESTABLISHED)

### Git state (read-only, verified)

| Property | Value |
|---|---|
| Current branch | `backup/pre-m3-0` |
| HEAD commit | `94556a1a8215af2b1c97ba8983275cf969a478b0` |
| HEAD subject | `Backup: pre-M3.0 state with CI, resource bounds, docs` |
| Upstream | `origin/backup/pre-m3-0` (same commit `94556a1`; branch in sync, no ahead/behind) |
| Other local branches | `main`, `reliability-batch-1` |
| Remote branches | `remotes/backup/HEAD -> backup/main`, `remotes/backup/main`, `remotes/origin/backup/pre-m3-0` |

### `git status --short --branch` (verbatim)

```
## backup/pre-m3-0...origin/backup/pre-m3-0
 M core/agent.py
 M core/cancellation.py
 M tools/python.py
 M tools/terminal.py
?? AUDIT_REPORTS/M3_1_MISSION_20261025_2207SS/
?? AUDIT_REPORTS/M3_CAMPAIGN_20261027_0124SS/
?? core/async_executor.py
?? core/executor_agent.py
?? core/planner_agent.py
?? core/process_manager.py
?? data/
?? voice_recordings/
```

### Recent commit history (`git log --oneline -15`)

```
94556a1 Backup: pre-M3.0 state with CI, resource bounds, docs
a74b53c docs: M2.17.x Task 1 verification pipeline map
a4649ea docs: M2.17.x Task 0 baseline and skip inventory
989aaa5 M2.17: wire the companion projection, single-gate limits, real-model execution
f975625 docs: correct the audited commit reference in the combined audit
cbfdf38 M2.16.UI + M2.17: complete audit TXT and roadmap reconciliation
df2f33d M2.17: production autonomy runtime, CLI wiring and redaction fixes
622e7e2 M2.17: persistent goals, durable resume, bounded scheduler, startup reconciliation
a3f826b M2.16.UI.1: desktop intent routing, adaptive tool context, wrong-tool recovery
6841fd6 M2.16.UI: complete audit TXT and roadmap reconciliation
```

### Tracked diff summary (`git diff --stat`)

```
 core/agent.py        | 129 +++++++++++++++++++++++++++++++++------------------
 core/cancellation.py | 98 +++++++++++++++++++++++++++++++++++---
 tools/python.py      | 38 +++++++--------
 tools/terminal.py    | 59 ++++++++---------------
4 files changed, 215 insertions(+), 109 deletions(-)
```

### Baseline commit and limitations

- **Baseline:** `94556a1` is the baseline (HEAD == upstream; no committed divergence).
- **Untracked-file limitation:** The 4 untracked `core/` modules, `data/`, and `voice_recordings/` are not in git. Their contents can be read but cannot be verified against any committed baseline; there is no diff against HEAD for them, and no commit history to audit. They represent work-in-progress outside version control.

---

## 3. TRACKED MODIFICATIONS AND UNTRACKED-FILE INVENTORY

### Modified tracked files (4)

**`core/agent.py`** (+129/-67 approx): Incomplete async conversion of the Agent request path.
- Imports `core.executor_agent.ExecutorAgent` and `core.async_executor.AsyncExecutor` in addition to `core.executor.Executor`.
- Instantiates `self._async_executor`, `self._executor_agent`, and `self._planner_agent` alongside the pre-existing `self._planner` and `self._executor` (the latter kept as backward-compat alias to `self._executor_agent._async_executor`).
- `_execute_step`, `_execute_resume_step`, `respond`, `_respond`, `_run_intent`, `_run_debug` (call site only), `_llm_loop` converted to `async`; `respond` uses `asyncio.run(self._respond(...))`.
- Recovery reconciliation restricted to pending/active tasks (fixes recovery-state pollution).
- Recovery exhaustion marked only for retryable categories (fixes over-aggressive exhaustion).
- **Bug:** `await self._run_debug(user_input)` at line 2493, but `_run_debug` is `def` (sync) at line 3093 → `TypeError: 'str' object can't be awaited`.

**`core/cancellation.py`** (+98/-14): `CancellationToken` converted from a `Protocol` (unusable) to a concrete thread-safe class with `threading.Event`, `RLock`, `CancellationRequest` dataclass, `request/is_cancelled/wait/raise_if_cancelled/reset`, plus `CancellationRequest.to_dict()`. Compatible with existing `cancellation_requested()`.

**`tools/python.py`** (+14/-24): Replaced per-tool `_stop_process()` terminate/kill logic with `ProcessManager.register()` + `ProcessManager.terminate_tree()`. Registers process at script start and in the cancelled/timed-out path.

**`tools/terminal.py`** (-29/+30): Replaced Windows `taskkill /T /F /PID` tree-kill with `ProcessManager.register()` + `ProcessManager.terminate_tree()`. Registers process at command start and in the cancelled/timed-out path.

### Untracked inventory

| Item | Type | Notes |
|---|---|---|
| `core/async_executor.py` | New file (~803 lines) | `AsyncExecutor`: bounded concurrency (`asyncio.Semaphore` + `ThreadPoolExecutor`), streaming callbacks, per-task cancellation tokens, timeout injection, sync `run()` wrapper, `execute_plan()` dependency gating. |
| `core/executor_agent.py` | New file (~389 lines) | `ExecutorAgent`: orchestration boundary between planner and execution engine; `execute_plan()`, `cancel()`/`cancel_all()`, plan status tracking, streaming callbacks. |
| `core/planner_agent.py` | New file (~291 lines) | `PlannerAgent`: extracted planner logic; `create_plan()`, `validate_plan()`, `replan()` (stub), intent expectation annotation. |
| `core/process_manager.py` | New file (~414 lines) | `ProcessManager`: unified subprocess lifecycle with Windows Job Objects (`KILL_ON_JOB_CLOSE`), per-task PID tracking, bounded terminate→kill escalation, `atexit` cleanup, context manager. |
| `data/` | New directory | Diablo operational data: `backups`, `blender`, `knowledge`, `logs`, `memory`, `models`, `orb_preview`, `run_backups`, `run_reports`, `sessions`. **User data — must be preserved.** |
| `voice_recordings/` | New directory | Contains `test_recording.wav` (110,636 bytes). **User data — must be preserved.** |
| `AUDIT_REPORTS/M3_1_MISSION_20261025_2207SS/` | New directory | M3.1 mission reports (7 files). |
| `AUDIT_REPORTS/M3_CAMPAIGN_20261027_0124SS/` | New directory | M3 campaign reports (6 files). |

Note: The M3.1 post-mission verification report (10-26) lists `?? original_agent.py` and `?? test_recovery.py` as untracked, but neither appears in the current `git status`. Those files are no longer present (report describes a different point-in-time state).

---

## 4. M3.1 AND M3 CAMPAIGN FINDINGS WITH EVIDENCE

### M3.1 mission report (`AUDIT_REPORTS/M3_1_MISSION_20261025_2207SS/`, dated 2026-10-25)

Claims:
- Phases B (async execution) and C (cancellation/resource safety) complete; PlannerAgent implemented; Agent integration complete.
- `~400+` tests pass, `0` regressions.
- Smoke test `python main.py --offline --demo`: 4/4 verification checks PASSED.

**Verification against code:** The components it claims exist do exist as untracked files. The fixes it implies (reconciliation restricted to pending/active; `allowed=True`; retryable-only exhaustion) **are present** in the working tree and are working (recovery tests now pass). The "0 regressions" claim is **partially correct** for the suites I ran — most pass — but it missed the `_run_debug` bug.

### M3.1 post-mission verification report (dated 2026-10-26, one day after the mission report)

Claims 7 REGRESSIONS:
- `test_execution_success_with_verification_passed`, `test_a_successful_delete_leaves_the_task_completed`, `test_failing_test_triggers_bounded_recovery_and_then_completes`, `test_self_correction_used_the_existing_recovery_policy_not_a_new_loop`, `test_verified_work_is_not_reported_as_failed_when_the_turn_budget_runs_out` — all FAIL.
- Root cause: recovery state pollution, `allowed=False` bug, overly aggressive exhaustion.
- Verdict: **PARTIALLY VERIFIED**; BLOCKER for M3.2.

**Verification against code:** These 5 tests **now PASS** in the current working tree. The fixes described (which match the agent.py diff) resolved them. The post-mission report captured a point-in-time state before the campaign fixes were complete.

### M3 campaign reports (`AUDIT_REPORTS/M3_CAMPAIGN_20261027_0124SS/`, dated 2026-10-27)

Claims (Final Report):
- M3.1 Recovery & Reliability, M3.1 Async Execution & Cancellation, M3.2 Planner Integration & Replanning: all **COMPLETE**.
- **298/298 tests PASS (100%)**, 0 regressions, smoke test 4/4 PASS.
- Fixes: recovery-state pollution fix, `outcome.allowed = True`, retryable-only exhaustion, delete-file confirmation handling.

**Verification against code:** The described fixes are present and working. **However**, the claim of "298/298 tests pass / 0 regressions" is **FALSE** for the current working tree: my test run found **4 failures** in `test_recovery_p10_repair.py::TestRepairCannotEscapeItsProject` (path-containment/security tests), all caused by the `_run_debug` await bug.

### Cross-check summary

| Report claim | Evidence status |
|---|---|
| Recovery fixed (no false exhaustion) | ✅ CONFIRMED — recovery tests pass |
| `allowed=True` after permission check | ✅ CONFIRMED — present in `async_executor.py` |
| Exhaustion only for retryable categories | ✅ CONFIRMED — present in `agent.py` diff |
| Delete file fixed | ✅ CONFIRMED — `test_a_successful_delete_leaves_the_task_completed` PASSES |
| 298/298 tests pass, 0 regressions | ❌ FALSE — 4 security tests FAIL (see Section 5) |
| All M3 objectives complete | ❌ NOT SUPPORTED — 4 failures remain |

---

## 5. ARCHITECTURE AND SECURITY FINDINGS (RANKED BY SEVERITY)

### Severity HIGH — Incomplete async conversion breaks path-containment/security tests

**File:** `core/agent.py`  
**Location:** Line 2493 `reply = await self._run_debug(user_input)` vs line 3093 `def _run_debug(self, user_input: str) -> str:` (sync, returns `str`).

**Effect:** `TypeError: 'str' object can't be awaited` when any request is classified as a debug intent. The error propagates through `_respond` → `_mark_failure` → `_transition(TaskState.FAILED)`, which then raises `ValueError: invalid task transition: cancelled -> failed` because the task is already cancelled.

**Failing tests (all path containment / security):**
1. `test_a_traversing_write_path_is_refused` — FAIL
2. `test_a_sibling_outside_the_roots_is_not_writable` — FAIL
3. `test_a_denial_outside_the_roots_ends_cancelled` — FAIL
4. `test_a_write_inside_the_project_is_allowed` — FAIL

**Fix (not implemented, per read-only constraint):** Change line 3093 from `def _run_debug` to `async def _run_debug`. `_run_intent` (line 2544) was correctly converted to `async def`; `_run_debug` was missed.

### Severity MEDIUM — Duplicate / potentially competing authoritative components

`core/agent.py` now holds **both** legacy and new components simultaneously:

| Component | Legacy (still instantiated) | New (active path) |
|---|---|---|
| Planner | `self._planner = Planner(settings)` | `self._planner_agent = PlannerAgent(...)` |
| Executor | `self._executor = self._executor_agent._async_executor` (alias) | `self._executor_agent = ExecutorAgent(...)` |

The active request path (`_respond` → `_run_intent`/`_llm_loop`) uses `self._planner_agent` and `self._executor_agent`. The legacy `self._planner` and `self._executor` are retained for backward compatibility (tests access `agent._executor` directly; comment in code confirms this). They are not currently driving execution, so there is **no active competing pipeline** — but there is duplicate instantiation and two conceptual planners/executors in memory.

AGENTS.md requires "One authoritative Agent, Executor, TaskManager/TaskStore, PermissionManager, VerificationLedger, and capability registry. No competing pipelines." The current state satisfies this at the execution-path level (one active path) but violates it at the object-graph level (two planners, two executors instantiated). `core/executor.py` (the original `Executor` class) is still imported but not instantiated by the Agent — it is dead code in this path.

### Severity MEDIUM — No dedicated tests for the four new untracked modules

The M3.1 implementation plan lists `tests/test_async_executor.py`, `test_executor_agent.py`, `test_planner_agent.py`, `test_process_manager.py`, `test_cancellation_hierarchy.py`, `test_async_integration.py`, `test_replanning.py`. **None of these exist** in the `tests/` directory. The new modules are exercised only indirectly through existing agent/terminal/python/recovery tests. There is no direct coverage of `AsyncExecutor.execute_plan()`, `ExecutorAgent.cancel_all()`, `ProcessManager.terminate_tree()`, or `PlannerAgent.create_plan()` in isolation.

### Severity LOW — `CancellationToken` concrete class is protocol-compatible

`CancellationToken` was a `Protocol` (unusable at runtime) and is now a concrete class. `cancellation_requested()` uses duck-typing (`getattr(token, "is_cancelled", None)`), so it works with both old and new token shapes. No test failures attributable to this change.

---

## 6. TEST RESULTS WITH COMMANDS AND EXIT CODES

All runs used `.venv\Scripts\python.exe -m pytest` on Windows, Python 3.14.6, pytest 9.1.1. Tests use `tmp_path`-based settings (temp directories), so they do not modify tracked files. No real external actions, no paid APIs, no network model calls were invoked.

| # | Command | Result | Exit code | Passed | Failed | Skipped |
|---|---|---|---|---|---|---|
| 1 | `pytest tests/test_agent.py tests/test_terminal.py tests/test_python.py -v --tb=short` | PASS | 0 | 27 | 0 | 0 |
| 2 | `pytest tests/test_terminal_state_invariant.py tests/test_verification.py tests/test_verification_ledger.py tests/test_memory_safety_and_rule_scope.py -v --tb=short` | PASS | 0 | 199 | 0 | 0 |
| 3 | `pytest tests/test_filesystem_containment.py tests/test_filesystem_delete_verification.py tests/test_applications.py -v --tb=short` | PASS | 0 | 38 | 0 | 0 |
| 4 | `pytest tests/test_m213_coding_workflow.py -v --tb=short` | PASS | 0 | 34 | 0 | 0 |
| 5 | `pytest tests/test_recovery_p10_repair.py -v --tb=short` | FAIL | 1 | 48 | 4 | 0 |

**Totals: 351 passed, 4 failed, 0 skipped.**

The 4 failures (run 5) are all in `TestRepairCannotEscapeItsProject`:
- `test_a_traversing_write_path_is_refused` — `ValueError: invalid task transition: cancelled -> failed` (root: `TypeError: 'str' object can't be awaited` on `_run_debug`)
- `test_a_sibling_outside_the_roots_is_not_writable` — same
- `test_a_denial_outside_the_roots_ends_cancelled` — same
- `test_a_write_inside_the_project_is_allowed` — same

**Not run (and why):**
- `python main.py --offline --demo` smoke test: Not run. It may attempt real external/model connections (Ollama at `localhost:11434`) and could run long; the read-only constraint prohibits invoking real external actions. The reports claim 4/4 PASS but this was not independently verified in this audit.
- `test_terminal.py::test_terminate_to_kill_escalation` and `test_recovery_p10_repair.py::test_the_graph_knows_the_task_and_its_verification`: Both passed in my runs (no 120s timeout observed), contrary to reports labeling them "pre-existing timeouts."
- Full `tests/` suite: Not run in full. Many test files require Blender, Windows UIA, real models, or GUI/display (e.g. `test_blender_*`, `test_windows_uia_*`, `test_real_model_*`, `test_m216_*`, `test_m217_*`); running them could invoke real external actions or hang. They were excluded as out of scope for this bounded audit.

---

## 7. COMPLETED, INCOMPLETE, BLOCKED, AND UNVERIFIED WORK

### Completed (verified by evidence)
- AsyncExecutor with bounded concurrency (semaphore + thread pool) — implemented, exercised by tests.
- ExecutorAgent orchestration with dependency gating — implemented.
- ProcessManager with Windows Job Objects — implemented.
- Concrete CancellationToken — implemented; all cancellation tests pass.
- PlannerAgent extraction — implemented; coding-workflow tests pass (34/34).
- Agent delegation to PlannerAgent/ExecutorAgent — implemented; agent tests pass (4/4).
- Recovery-state pollution fix (reconciliation restricted to pending/active) — verified (recovery tests pass).
- `outcome.allowed = True` after permission check — verified (present in `async_executor.py`).
- Retryable-only recovery exhaustion — verified (present in `agent.py` diff).

### Incomplete (the blocker)
- Async conversion of `_run_debug`: call site `await`s it, but method is sync `def`. **This is the single blocking defect.** It breaks 4 path-containment/security tests.

### Blocked
- M3.2 / M3.3 planner integration and subsequent milestones cannot be considered started while 4 security tests fail. The M3 campaign report's "complete" verdict is not yet earned.

### Unverified
- Smoke test `python main.py --offline --demo` (not run — external-action risk).
- Dedicated unit tests for the 4 new modules (do not exist).
- Blender, Windows UIA, real-model, GUI, and voice test suites (not run — external/display dependencies).
- `data/` and `voice_recordings/` contents are user data; not audited for correctness (read-only, preserved as-is).

---

## 8. RECOMMENDED NEXT MILESTONE

**Milestone:** "M3.1 Completion — Fix async conversion and re-verify"

**Description:** Complete the incomplete async conversion in `core/agent.py` so the debug path is consistent with the intent path, then re-run the full bounded test suite to confirm zero failures. Do not restart, re-architect, or integrate CrewAI. Preserve all settled decisions (reconciliation fix, `allowed=True`, retryable-only exhaustion, ProcessManager integration, CancellationToken concrete class).

**Dependencies:**
- None external. Single-file change in `core/agent.py`.
- Requires `async def _run_debug(...)` and ensuring the method body is await-compatible (it calls `loop.diagnose(...)` — verify that is sync or await it appropriately).

**Risks:**
- `_run_debug` body calls `DebugLoop(...).diagnose(...)` — confirm `diagnose` is synchronous (returns a report object, not a coroutine). If `diagnose` is async, it must be awaited inside `_run_debug`.
- `_run_debug` references `self._executor_agent._async_executor` (already updated in the diff) — consistent.
- Any other sync methods `await`ed elsewhere in `agent.py` should be audited (grep found only `_run_intent` and `_run_debug` as `await self._run_*`; `_run_intent` is correctly async).

**Acceptance criteria (measurable):**
1. `python -m pytest tests/test_recovery_p10_repair.py -v` → 52/52 pass, exit code 0 (currently 48 pass / 4 fail / exit 1).
2. `python -m pytest tests/test_agent.py tests/test_terminal.py tests/test_python.py tests/test_terminal_state_invariant.py tests/test_verification.py tests/test_verification_ledger.py tests/test_memory_safety_and_rule_scope.py tests/test_filesystem_containment.py tests/test_filesystem_delete_verification.py tests/test_applications.py tests/test_m213_coding_workflow.py -v` → all pass, exit code 0 (currently pass).
3. Combined: 355/355 pass, 0 failed, exit code 0 across all bounded suites.
4. `git status --short --branch` unchanged except for the intentional `core/agent.py` edit (the only file that should change).

**Why this takes priority:** The 4 failing tests are security/path-containment tests (`TestRepairCannotEscapeItsProject`). A crashing debug path (`TypeError` → `ValueError` on task transition) means untrusted debug intents are not handled through the normal containment flow. AGENTS.md safety rules prohibit weakening path containment; this defect effectively bypasses it by throwing before containment logic runs. Fixing it is the gating item before any M3.2 work.

---

## 9. REMAINING RISKS

1. **`_run_debug` async bug** (HIGH) — blocks M3.1 completion; security tests fail. Addressed by recommended milestone.
2. **No dedicated tests for new modules** (MEDIUM) — `async_executor`, `executor_agent`, `planner_agent`, `process_manager` lack direct unit tests. Should be added in a follow-up task.
3. **Duplicate planner/executor instances** (MEDIUM) — `self._planner` and `self._executor` coexist with `self._planner_agent`/`self._executor_agent`. Not active but violates "one authoritative component" at the object-graph level. Consider consolidating in a follow-up.
4. **Untracked work not in git** — the 4 new `core/` modules and 4 modified files are uncommitted. A reset or checkout would lose them. They must be committed intentionally when authorized.
5. **Smoke test unverified** — `python main.py --offline --demo` claimed 4/4 PASS in reports but not independently verified in this audit (external-action risk).
6. **`original_agent.py` / `test_recovery.py` discrepancy** — the 10-26 post-mission report lists them as untracked, but they are absent from current `git status`. Possibly deleted since; no impact on current code path.

---

## 10. EXACT FINAL GIT STATUS

Verbatim `git status --short --branch` (read-only, no changes made during this audit):

```
## backup/pre-m3-0...origin/backup/pre-m3-0
 M core/agent.py
 M core/cancellation.py
 M tools/python.py
 M tools/terminal.py
?? AUDIT_REPORTS/M3_1_MISSION_20261025_2207SS/
?? AUDIT_REPORTS/M3_CAMPAIGN_20261027_0124SS/
?? core/async_executor.py
?? core/executor_agent.py
?? core/planner_agent.py
?? core/process_manager.py
?? data/
?? voice_recordings/
```

HEAD: `94556a1` (in sync with `origin/backup/pre-m3-0`). No git state was changed during this audit. No files were created, modified, deleted, or renamed except this audit report itself (written to a new directory under `AUDIT_REPORTS/` per the report-creation allowance).

---

**End of audit report.** Implementation is paused at the recommended milestone; no implementation was started during this mission.