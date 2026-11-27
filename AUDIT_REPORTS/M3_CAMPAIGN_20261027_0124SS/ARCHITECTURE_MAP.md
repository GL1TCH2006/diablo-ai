# M3 Campaign Architecture Map

**Date**: 2026-10-27
**Branch**: `backup/pre-m3-0`

---

## High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           USER REQUEST                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            main.py                                          │
│  - Parses CLI args (--offline, --demo, etc.)                              │
│  - Creates Agent with provider, settings, registry, permissions, events  │
│  - Calls agent.respond()                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Agent.respond()                                   │
│  - BoundedRequestGate (max_concurrent_requests = 1)                        │
│  - _respond() / async _respond()                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Agent._respond()                                  │
│  - _begin_request_context() → new RequestContext                          │
│  - _active_cancellation_token = token                                     │
│  - _create_run_record()                                                    │
│  - _transition(TaskState.PLANNING)                                         │
│  - Fast intent checks:                                                     │
│      - _planner_agent.planner.matches_greeting() → greet_response()       │
│      - _planner_agent.planner.fast_intent() → _run_intent()               │
│      - _planner_agent.planner.coding_intent() == "debug" → _run_debug()   │
│      - else → _llm_loop()                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
           ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
           │ _run_intent()│ │ _run_debug() │ │ _llm_loop()  │
           └──────────────┘ └──────────────┘ └──────────────┘
                    │               │               │
                    └───────────────┼───────────────┘
                                    ▼
            ┌─────────────────────────────────────────────┐
            │           PlannerAgent                      │
            │  - create_plan(goal, context)               │
            │     - fast_intent() / coding_intent()       │
            │     - ExecutionPlan.from_intent()           │
            │     - _annotate_intent_expectations()       │
            │     - validate_plan()                       │
            │  - validate_plan(plan)                      │
            │  - replan(plan, failed_step, reason)        │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │        ExecutionPlanRecord                  │
            │  - goal, steps[], created_at, schema_ver   │
            │  - Durable, persisted via TaskStore         │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          ExecutorAgent                      │
            │  - execute_plan(plan, context)              │
            │    - Dependency-gated step execution        │
            │    - AsyncExecutor.run_tool() per step      │
            │    - Streaming callbacks (start/progress/end)│
            │    - Cancellation propagation               │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          AsyncExecutor                      │
            │  - max_concurrent (default 4)               │
            │  - ThreadPoolExecutor for blocking tools    │
            │  - run_tool(tool, args, cancellation_token) │
            │    - Permission check                       │
            │    - Confirmation if needed                 │
            │    - Tool execution in thread pool          │
            │    - Cancellation checks                    │
            │  - execute_plan() with dependency gating    │
            └─────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
           ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
           │ Tools        │ │ Tools        │ │ Tools        │
           │ (terminal,   │ │ (filesystem, │ │ (blender,    │
           │  python)     │ │  git, etc.)  │ │  vision)     │
           └──────────────┘ └──────────────┘ └──────────────┘
                    │               │               │
                    └───────────────┼───────────────┘
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          Subprocesses                       │
            │  - ProcessManager.register()                │
            │  - Windows Job Objects (process tree)       │
            │  - terminate_tree() on cancel/timeout       │
            │  - Fallback: terminate → kill               │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          Verification                       │
            │  - Verifier.check_file(), check_output()    │
            │  - VerificationLedger.adjudicate()          │
            │  - Evidence-based verdicts                  │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          Recovery (RecoveryPolicy)          │
            │  - FailureObservation from step outcome     │
            │  - decide() → RecoveryDecision              │
            │    - CANCELLED → NONE                       │
            │    - Exhausted → ABORT                      │
            │    - Non-retryable → ABORT                  │
            │    - Interrupted → ABORT                    │
            │    - Retryable + budget → RETRY_STEP        │
            │    - Verify fail + evidence → RERUN_VERIFY  │
            │    - Escalation budget → ESCALATE_MODEL     │
            │    - Skip allowed → SKIP                    │
            │    - Else → ABORT                           │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          Agent Loop (_llm_loop)             │
            │  - If RETRY_STEP: _begin_recovery(), retry  │
            │  - If ABORT/ESCALATE/SKIP: mark, finalize   │
            │  - Max turns, tool calls, turn budget       │
            │  - Turn budget exhausted with PASSED → OK   │
            └─────────────────────────────────────────────┘
                                    │
                                    ▼
            ┌─────────────────────────────────────────────┐
            │          Final Result                       │
            │  - COMPLETED / FAILED / CANCELLED           │
            │  - Verification verdict                     │
            │  - Recovery exhaustion state                │
            └─────────────────────────────────────────────┘
```

---

## Key Component Interfaces

### Agent ↔ PlannerAgent
```python
PlannerAgent.create_plan(goal: str, context: RequestContext) -> ExecutionPlan
PlannerAgent.validate_plan(plan: ExecutionPlan) -> ValidationResult
PlannerAgent.replan(plan, failed_step_id: str, reason: str, context) -> ExecutionPlan
```

### Agent ↔ ExecutorAgent
```python
ExecutorAgent.execute_plan(
    plan: ExecutionPlanRecord,
    context: RequestContext,
    on_step_start, on_step_progress, on_step_complete
) -> PlanExecutionResult
```

### ExecutorAgent ↔ AsyncExecutor
```python
AsyncExecutor.run_tool(
    tool: str,
    args: dict,
    cancellation_token,
    on_start, on_progress, on_complete
) -> ExecutionOutcome

AsyncExecutor.execute_plan(plan, context, callbacks) -> ExecutionResult
```

### AsyncExecutor ↔ Tools
```python
# Tools are registered in ToolRegistry
# ToolSpec.fn(**call_args) -> ToolResult
# ToolResult: success, output, error, cancelled, exit_code, data, verification
```

### Agent ↔ RecoveryPolicy
```python
RecoveryPolicy.decide(observation: FailureObservation) -> RecoveryDecision
RecoveryDecision.action ∈ {RETRY_STEP, RERUN_VERIFY, ESCALATE_MODEL, SKIP, ABORT, NONE}
```

### Task Lifecycle (TaskManager)
```
PENDING → RUNNING → DONE
              ↓
         FAILED → (recovery) → PENDING (retry)
              ↓
         CANCELLED
              ↓
         COMPLETED / FAILED
```

---

## State Management

### RequestContext (per-request)
- `task_id`, `run_id`
- `cancellation_token`
- `recovery_attempts`, `recovery_exhausted`, `recovery_termination`
- `max_turns`, `turn_count`, `tool_call_count`

### TaskRecord (durable)
- `task_id`, `title`, `goal`
- `steps[]` (PlanStepRecord)
- `state` (TaskState)
- `verification` (VerificationState)
- `recovery_attempts`, `recovery_exhausted`, `recovery_termination`
- `ledger` (VerificationLedger)
- `partial` flag

### PlanStepRecord
- `step_id`, `tool`, `args`, `depends_on`
- `status` (StepStatus)
- `detail`, `result_summary`
- `outcome` (ExecutionOutcome)
- `expected_result`, `recovery_hint`

---

## Cancellation Hierarchy

```
User: "Diablo, stop" / UI Stop / Voice STOP
         │
         ▼
VoiceService.cancel() / EdgeCompanionApp._on_stop() / TerminalUI.stop()
         │
         ▼
InterruptionManager.request_cancel()
         │
         ▼
CancellationToken.request() → _event.set()
         │
         ▼
Agent.cancel_current_task()
         │
         ▼
ExecutorAgent.cancel() / AsyncExecutor.cancel_all()
         │
         ▼
Task CancellationToken.request()
         │
         ▼
Tool cancellation_requested() checks (polling)
         │
         ▼
ProcessManager.terminate_tree() → Windows Job Object / terminate/kill
```

---

## Process Management (Windows)

```
ProcessManager
  ├── register(task_id, proc, operation, creationflags)
  │     ├── Creates Windows Job Object (if Win32 available)
  │     │     ├── CreateJobObjectW()
  │     │     ├── SetInformationJobObject(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
  │     │     └── AssignProcessToJobObject()
  │     └── Tracks: pid → ProcessInfo, task_id → [pids], task_id → job_handle
  │
  ├── terminate_tree(task_id, grace_period)
  │     ├── Phase 1: TerminateJobObject() (atomic tree kill)
  │     └── Phase 2: Fallback terminate/kill per process
  │
  ├── wait_bounded(task_id, timeout)
  ├── cleanup_all() (atexit)
  └── Context manager support
```

---

## Configuration Points

### Settings (config/settings.py)
- `tool_terminal_timeout` (default 120s)
- `tool_python_timeout` (default 300s)
- `tool_blender_timeout` (default 600s)
- `tool_generic_timeout` (default 60s)
- `max_result_chars` (default 8000)
- `max_recovery_attempts` (default 3)
- `max_concurrent_requests` (default 1)

### AsyncExecutor
- `max_concurrent` (default 4)
- `tool_*_timeout` passed from settings

### RecoveryPolicy
- `max_retries` (default 3)
- `max_rerun_verify` (default 1)
- `max_escalations` (default 2)
- `allow_skip` (default False)

---

## Known Issues (from Baseline)

1. **Recovery state pollution**: Old `recovery_exhausted=True` state incorrectly restored to new tasks
2. **Delete file not working**: `filesystem.delete_file` doesn't actually delete the file
3. **Recovery not triggering**: `recovery_attempts=0` despite failures, `recovery_exhausted=True` incorrectly set
4. **Verification not passing**: Even when work is verified, `VerificationState.FAILED` is set