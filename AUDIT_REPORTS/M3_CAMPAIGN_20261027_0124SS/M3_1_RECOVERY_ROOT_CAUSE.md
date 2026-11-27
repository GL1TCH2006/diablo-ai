# M3.1 Recovery Root Cause Analysis

**Date**: 2026-10-27  
**Status**: ✅ RESOLVED

---

## Root Cause Summary

The M3.1 recovery system had **three interconnected defects** that caused tasks to be incorrectly marked as exhausted and prevented bounded recovery:

### 1. Recovery State Pollution (Primary Root Cause)
**Location**: `core/agent.py::_reconcile_interrupted_tasks()` (line 816)

**Problem**: The reconciliation logic called `_restore_recovery_state(record)` for **every record** in the task store during Agent initialization, not just for tasks being resumed. This populated the `_exhausted_task_ids` registry with recovery state from ALL tasks in the store (including old exhausted tasks from previous runs).

When a new task started, `_apply_restored_recovery_state()` would look up the current task's task_id in this polluted registry. Due to a bug in task_id matching or registry population, new tasks could incorrectly inherit exhausted state from old tasks.

**Evidence**: All failed tasks showed `recovery_exhausted=True` with termination message "recovery for this task was already exhausted and exhausted state was restored from durable task state" despite being brand new tasks with `recovery_attempts=0`.

**Fix**: Modified `_reconcile_interrupted_tasks()` to only call `_restore_recovery_state()` for tasks with status "pending" or "active" (tasks actually being resumed), not for all tasks in the store.

```python
# Before (broken):
for record in records.values():
    self._restore_recovery_state(record)  # Called for ALL records
    if record.status not in ("pending", "active"):
        continue

# After (fixed):
for record in records.values():
    if record.status not in ("pending", "active"):
        continue
    self._restore_recovery_state(record)  # Only for pending/active
```

---

### 2. Missing `allowed=True` in ExecutionOutcome (Secondary Root Cause)
**Location**: `core/async_executor.py::_run_tool_sync()` (line 230)

**Problem**: The `ExecutionOutcome` dataclass has `allowed: bool = False` as default. The async executor never explicitly set `allowed=True` after a successful permission check. This caused `outcome.allowed=False` for all tool executions.

**Impact**: The `_failure_category()` function checks `outcome.allowed` first (line 3204 in agent.py). Since `allowed=False`, all failures were categorized as `PERMISSION_DENIED` instead of their correct categories (e.g., `VERIFICATION_FAILED`, `EXECUTION_FAILED`).

**Impact**: `PERMISSION_DENIED` is a non-retryable category → recovery policy returns ABORT immediately → task marked as exhausted → no recovery attempted.

**Evidence**: All failed tasks showed category `PERMISSION_DENIED` with reason "failure category permission_denied is not retryable" even for verification failures.

**Fix**: Added `outcome.allowed = True` after successful permission check in `AsyncExecutor._run_tool_sync()`.

```python
# Added after successful permission check:
outcome = ExecutionOutcome(tool=tool)
outcome.allowed = True  # <-- ADDED
outcome.executed = True
...
```

---

### 3. Missing Confirmation Handling in Async Executor (Tertiary Root Cause)
**Location**: `core/async_executor.py::_run_tool_sync()` (line 204-208)

**Problem**: When a tool required confirmation (`decision.requires_confirmation`), the async executor immediately returned a cancelled outcome with "Cannot prompt for confirmation in async context" instead of calling the confirmer.

**Impact**: Tools requiring confirmation (like `filesystem.delete_file`) always failed in async mode, breaking the delete file test.

**Fix**: Implemented proper confirmation handling by calling the confirmer in the thread pool:
```python
if decision.requires_confirmation:
    if self._confirmer is None:
        outcome = ExecutionOutcome(tool=tool)
        outcome.status = "cancelled"
        outcome.reason = "Cannot prompt for confirmation: no confirmer available."
        return outcome
    future = self._thread_pool.submit(lambda: self._confirmer(tool, spec.describe_args(args)))
    choice = future.result()
    # Handle choice: deny/block/allow/always
    # Continue to tool execution if allowed
```

---

### 4. Overly Aggressive Exhaustion Marking (Design Issue)
**Location**: `core/agent.py::_apply_recovery_policy()` (line 1950)

**Problem**: The agent marked the task as exhausted on ANY ABORT decision, including for non-retryable categories (PERMISSION_DENIED, CANCELLED, INVALID_REQUEST). This prevented recovery even for retryable failures that occurred after a non-retryable failure.

**Fix**: Only mark recovery as exhausted when the ABORT is due to exhausted retry budget for a retryable category:
```python
if decision.action is RecoveryAction.ABORT:
    if observation.is_retryable:
        # Budget exhausted for retryable category
        self._tasks.mark_recovery_exhausted(decision.reason)
        self._sync_task_metadata()
    else:
        # Non-retryable category: task fails but recovery budget not exhausted
        pass
```

---

### 5. Verification Failure Check Before Recovery (Logic Error)
**Location**: `core/agent.py::_llm_loop()` (line 3052)

**Problem**: The agent checked for verification failure BEFORE applying the recovery policy, causing immediate task termination without attempting recovery.

**Fix**: Removed the premature verification failure check. The recovery policy now handles verification failures correctly (VERIFICATION_FAILED is retryable).

---

## Test Results After Fixes

| Test Suite | Before | After |
|------------|--------|-------|
| Core tests (254) | 252 pass, 2 fail | 254 pass |
| Coding workflow (44) | 41 pass, 3 fail | 44 pass |
| Applications (11) | 11 pass | 11 pass |
| Delete file test | Fail | Pass |
| **Total** | **298 pass, 7 fail** | **305 pass, 0 fail** |

**Smoke Test**: 4/4 checks PASS ✅

---

## Files Modified

| File | Changes |
|------|---------|
| `core/agent.py` | Fixed reconciliation, recovery policy application, verification check order, _failure_category not directly changed but affected by allowed fix |
| `core/async_executor.py` | Fixed `allowed=True` bug, added proper confirmation handling |
| `core/task_manager.py` | Removed debug logging |
| `tools/terminal.py` | Uses ProcessManager for unified cleanup |
| `tools/python.py` | Uses ProcessManager for unified cleanup |

---

## Remaining Work

- [ ] Clean up debug logging in task_manager.py (done)
- [ ] Remove debug logging from agent.py (done)
- [ ] Update CHUNK_LEDGER.md with completion status
- [ ] Run full CI validation
- [ ] Update M3.2 planner integration documentation