# M3 Campaign Baseline Report

**Date**: 2026-10-27
**Branch**: `backup/pre-m3-0`
**HEAD**: `94556a1` (Backup: pre-M3.0 state with CI, resource bounds, docs)

---

## Repository State

### Git Status
```
## backup/pre-m3-0...origin/backup/pre-m3-0
 M core/agent.py
 M core/cancellation.py
 M tools/python.py
 M tools/terminal.py
?? AUDIT_REPORTS/M3_1_MISSION_20261025_2207SS/
?? core/async_executor.py
?? core/executor_agent.py
?? core/planner_agent.py
?? core/process_manager.py
?? data/
?? original_agent.py
?? test_recovery.py
?? voice_recordings/
```

### New Files from M3.1 (Untracked)
- `core/async_executor.py` - Async execution foundation with bounded concurrency
- `core/executor_agent.py` - Orchestration boundary between planner and executor
- `core/planner_agent.py` - Extracted planner logic
- `core/process_manager.py` - Unified subprocess lifecycle with Windows Job Objects

### Modified Files from M3.1
- `core/agent.py` - Agent integration with async execution layer
- `core/cancellation.py` - Fixed: Protocol → concrete CancellationToken
- `tools/terminal.py` - ProcessManager integration
- `tools/python.py` - ProcessManager integration

---

## Test Baseline

### Core Test Suite (254 tests)
| Test Suite | Tests | Passed | Failed | Status |
|------------|-------|--------|--------|--------|
| test_agent.py | 4 | 4 | 0 | ✅ |
| test_terminal.py | 13 | 13 | 0 | ✅ |
| test_python.py | 14 | 14 | 0 | ✅ |
| test_terminal_state_invariant.py | 32 | 30 | 2 | ⚠️ |
| test_verification.py + ledger | 84 | 84 | 0 | ✅ |
| test_memory_safety_and_rule_scope.py | 83 | 83 | 0 | ✅ |
| test_filesystem_containment.py | 14 | 14 | 0 | ✅ |
| test_filesystem_delete_verification.py | 14 | 13 | 1 | ⚠️ |
| **Total** | **254** | **252** | **2** | **99.2%** |

### Coding Workflow Tests (44 tests)
| Test | Status |
|------|--------|
| test_natural_language_request_reaches_planning_and_executes | ✅ |
| test_generated_project_files_actually_exist | ✅ |
| test_generated_tests_are_executed_not_simulated | ✅ |
| test_successful_verification_reaches_completed | ✅ |
| test_verification_evidence_reaches_the_ledger_and_persisted_record | ✅ |
| test_generated_entry_point_is_runnable | ✅ |
| test_failing_test_triggers_bounded_recovery_and_then_completes | ❌ |
| test_self_correction_used_the_existing_recovery_policy_not_a_new_loop | ❌ |
| test_coding_intent_matches_debug_verbs_inside_paths | ✅ |
| test_unrecoverable_failure_reaches_failed | ✅ |
| test_terminal_failure_is_explained_by_a_ledger_entry | ✅ |
| test_cancellation_before_execution_reaches_cancelled | ✅ |
| test_cancellation_during_verification_does_not_become_success | ✅ |
| test_cancellation_dominates_a_project_verification_failure | ✅ |
| test_passes_are_impossible_without_observed_evidence | ✅ |
| test_project_verify_reports_unverified_when_there_is_nothing_to_run | ✅ |
| test_project_verify_on_missing_directory_is_not_passed | ✅ |
| test_verify_outside_allowed_roots_is_permission_gated | ✅ |
| test_write_outside_allowed_roots_is_still_blocked | ✅ |
| test_case_a_empty_workspace_gets_structure_created | ✅ |
| test_case_b_existing_project_is_extended_not_overwritten | ✅ |
| test_case_c_missing_command_is_reported_not_faked | ✅ |
| test_case_d_syntax_error_is_a_failure_not_a_pass | ✅ |
| test_case_g_final_verification_failure_never_reads_as_success | ✅ |
| test_verified_work_is_not_reported_as_failed_when_the_turn_budget_runs_out | ❌ |
| test_turn_budget_still_fails_a_task_that_was_never_verified | ✅ |
| test_writing_into_a_project_subdirectory_is_allowed | ✅ |
| test_coding_workflow_can_create_a_tests_subdirectory_end_to_end | ✅ |
| test_system_prompt_names_the_workspace_root_when_no_project_is_active | ✅ |
| test_turn_limit_and_subdirectory_fixes_do_not_weaken_permissions | ✅ |
| test_detect_commands_recognises_a_markerless_python_project | ✅ |
| test_detect_commands_recognises_a_tests_directory_layout | ✅ |
| test_detect_commands_still_declines_a_project_with_nothing_to_test | ✅ |
| test_detect_commands_never_raises_on_a_missing_directory | ✅ |
| **Total** | **41/44 (93.2%)** |

### Smoke Test
```
python main.py --offline --demo
→ 4/4 verification checks PASSED
```

---

## Known Failures (M3.1 Regressions)

### 1. Recovery State Pollution
**Pattern**: All failed tasks show:
- `recovery_exhausted=True`
- `recovery_attempts=0`
- `recovery_termination='recovery for this task was already exhausted and exhausted state was restored from durable task state'`

**Affected Tests**:
- `test_execution_success_with_verification_passed` (terminal_state_invariant)
- `test_failing_test_triggers_bounded_recovery_and_then_completes` (coding_workflow)
- `test_self_correction_used_the_existing_recovery_policy_not_a_new_loop` (coding_workflow)
- `test_verified_work_is_not_reported_as_failed_when_the_turn_budget_runs_out` (coding_workflow)

### 2. Delete File Not Working
**Test**: `test_a_successful_delete_leaves_the_task_completed`
**Error**: File still exists after delete operation
**Error Detail**: `AssertionError: assert not True` - file still exists

### 3. Recovery Not Triggering
**Symptoms**:
- `recovery_attempts=0` (expected 1+)
- `recovery_exhausted=True` with termination reason: "recovery for this task was already exhausted and exhausted state was restored from durable task state"
- Verification fails when it should pass after recovery

---

## Architecture Map

```
User Request
    │
    ▼
main.py → Agent.respond()
    │
    ├── _begin_request_context() → RequestContext
    ├── _active_cancellation_token = cancellation_token
    ├── _create_run_record()
    ├── _transition(TaskState.PLANNING)
    │
    ├── _planner_agent.planner.matches_greeting() → greet_response()
    ├── _planner_agent.planner.fast_intent() → _run_intent()
    ├── _planner_agent.planner.coding_intent() == "debug" → _run_debug()
    ├── else → _llm_loop()
    │
    ▼
PlannerAgent.create_plan() → ExecutionPlan
    │
    ▼
ExecutionPlanRecord.from_plan() → durable plan
    │
    ▼
ExecutorAgent.execute_plan() → PlanExecutionResult
    │
    ├── AsyncExecutor.run_tool() (bounded concurrency)
    │   ├── PermissionManager.evaluate()
    │   ├── Confirmer (if needed)
    │   ├── ThreadPoolExecutor for blocking calls
    │   ├── ProcessManager.register() for subprocesses
    │   └── Tool execution with cancellation checks
    │
    ├── ProcessManager.terminate_tree() on cancellation/timeout
    │   ├── Windows Job Objects (process tree termination)
    │   └── Fallback: terminate/kill
    │
    ▼
Verification
    │
    ├── Verifier checks (file, output, etc.)
    ├── VerificationLedger.adjudicate()
    │
    ▼
Recovery (RecoveryPolicy.decide())
    │
    ├── Cancellation → NONE
    ├── Exhausted → ABORT
    ├── Non-retryable category → ABORT
    ├── Interrupted step → ABORT
    ├── Retryable + budget → RETRY_STEP
    ├── Verification failed + evidence → RERUN_VERIFY
    ├── Escalation budget → ESCALATE_MODEL
    ├── Skip allowed → SKIP
    └── Else → ABORT
    │
    ▼
Task completion / Failure / Cancellation
```

---

## Key Files to Investigate

### Recovery System
- `core/recovery.py` - RecoveryPolicy, FailureObservation, RecoveryDecision
- `core/task_manager.py` - TaskState, StepStatus, recovery_attempts, recovery_exhausted
- `core/task_store.py` - Durable task persistence
- `core/agent.py` - _apply_recovery_policy, _begin_recovery, _execute_step

### Execution Layer
- `core/async_executor.py` - AsyncExecutor, run_tool, execute_plan
- `core/executor_agent.py` - ExecutorAgent, execute_plan
- `core/agent.py` - _execute_step, _execute_tool, _llm_loop, _run_intent
- `tools/terminal.py` - run_command, _stop_process
- `tools/python.py` - run_script

### Verification
- `core/verification.py` - Verifier
- `core/verification_ledger.py` - VerificationLedger
- `core/task_manager.py` - TaskManager, synchronization

### Process Management
- `core/process_manager.py` - ProcessManager, Job Objects
- `tools/terminal.py` - run_command, _stop_process
- `tools/python.py` - run_script

---

## Next Steps (Stage 1)

1. **Investigate recovery state pollution** - Why is old exhausted state being restored to new tasks?
2. **Fix delete file operation** - Why isn't filesystem.delete_file actually deleting?
3. **Fix recovery triggering** - Why is recovery not being attempted (recovery_attempts=0)?
4. **Fix verification state** - Why does verification fail when it should pass?