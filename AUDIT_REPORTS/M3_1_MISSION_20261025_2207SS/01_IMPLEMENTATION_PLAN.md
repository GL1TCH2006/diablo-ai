# M3.1 IMPLEMENTATION PLAN

**Mission:** M3.1 — Unified Async Execution, Cancellation, Resource Safety & Planner Integration  
**Date:** 2026-10-25  
**Branch:** `backup/pre-m3-0` (HEAD: `94556a1`)

---

## PHASE A: PREFLIGHT COMPLETE ✅

Architecture map created at `00_ARCHITECTURE_MAP.md`

---

## PHASE B: ASYNC EXECUTION FOUNDATION

### B1. AsyncExecutor Class

**File:** `core/async_executor.py` (NEW)

```python
class AsyncExecutor:
    """Async executor with bounded concurrency, streaming callbacks, and cancellation."""
    
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionManager,
        events: EventBus,
        confirmer=None,
        max_result_chars: int = 8000,
        max_concurrent: int = 4,
        before_execute=None,
        state_listener=None,
        event_context=None,
        tool_terminal_timeout: float = 120.0,
        tool_python_timeout: float = 300.0,
        tool_blender_timeout: float = 600.0,
        tool_generic_timeout: float = 60.0,
    ):
        # ... implementation
    
    async def run_tool(
        self,
        tool: str,
        args: dict,
        cancellation_token: CancellationToken | None = None,
        on_start=None,
        on_progress=None,
        on_complete=None,
    ) -> ExecutionOutcome:
        # ... implementation
    
    async def execute_plan(
        self,
        plan: ExecutionPlanRecord,
        context: RequestContext,
        on_step_start=None,
        on_step_progress=None,
        on_step_complete=None,
    ) -> ExecutionResult:
        # ... implementation
    
    def cancel_all(self) -> None:
        # ... implementation
```

**Key Features:**
- `ThreadPoolExecutor` for blocking tool execution
- `asyncio.Semaphore` for bounded concurrency (default: 4)
- Streaming callbacks: `on_start`, `on_progress`, `on_complete`
- Per-task `CancellationToken` hierarchy
- Timeout injection (reuses existing `_get_tool_timeout()`)
- Structured `ExecutionOutcome` return (compatible with existing `Executor.run()`)

### B2. ExecutorAgent Class

**File:** `core/executor_agent.py` (NEW)

```python
class ExecutorAgent:
    """Orchestration boundary between planner and execution engine."""
    
    def __init__(
        self,
        async_executor: AsyncExecutor,
        registry: ToolRegistry,
        permissions: PermissionManager,
        events: EventBus,
        confirmer=None,
    ):
        # ... implementation
    
    async def execute_plan(
        self,
        plan: ExecutionPlanRecord,
        context: RequestContext,
        on_step_start=None,
        on_step_progress=None,
        on_step_complete=None,
    ) -> ExecutionResult:
        # ... implementation
    
    def cancel(self, task_id: str) -> bool:
        # ... implementation
```

**Responsibilities:**
- Accepts validated `ExecutionPlanRecord` from `PlannerAgent`
- Dispatches steps via `AsyncExecutor` with dependency gating
- Enforces permissions and confirmations
- Tracks execution state and task ownership
- Forwards progress events via callbacks
- Registers tasks with shared cancellation mechanism

### B3. Streaming Callbacks

**Event Types:**
- `task_accepted` — plan accepted, queued
- `task_queued` — waiting for concurrency slot
- `task_started` — step execution begun
- `task_progress` — partial output, tool activity
- `task_completed` — step succeeded
- `task_failed` — step failed
- `task_timed_out` — step timed out
- `cancellation_requested` — stop requested
- `cancellation_completed` — stop confirmed

**Callback Contract:**
```python
# All callbacks are non-blocking; exceptions isolated
on_start(task_id: str, step: PlanStepRecord) -> None
on_progress(task_id: str, step: PlanStepRecord, progress: dict) -> None
on_complete(task_id: str, step: PlanStepRecord, outcome: ExecutionOutcome) -> None
```

---

## PHASE C: UNIFIED CANCELLATION & RESOURCE SAFETY

### C1. Unified Cancellation Hierarchy

```
User Request → VoiceService/UI → InterruptionManager
                    │
                    ▼
            Agent.cancel_current_task()
                    │
                    ▼
         CancellationToken.request() → _event.set()
                    │
                    ▼
         ExecutorAgent.cancel(task_id)
                    │
                    ▼
         AsyncExecutor.cancel_task(task_id)
                    │
                    ▼
         Task CancellationToken.request()
                    │
                    ▼
         Tool cancellation_requested() checks
                    │
                    ▼
         _stop_process() → ProcessManager.terminate_tree()
```

### C2. Unified ProcessManager

**File:** `core/process_manager.py` (NEW)

```python
class ProcessManager:
    """Unified subprocess lifecycle management with Windows Job Objects."""
    
    def __init__(self):
        self._processes: dict[int, subprocess.Popen] = {}
        self._task_processes: dict[str, list[int]] = defaultdict(list)
        self._lock = threading.RLock()
    
    def register(self, task_id: str, proc: subprocess.Popen) -> None:
        # ... track process by task
    
    def terminate_tree(self, task_id: str) -> bool:
        # Windows: Job Objects for process tree
        # Unix: process group kill
    
    def wait_bounded(self, task_id: str, timeout: float) -> bool:
        # ... bounded wait with escalation
    
    def cleanup_all(self) -> None:
        # ... shutdown cleanup
```

**Windows Job Object Integration:**
```python
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
CreateJobObject = kernel32.CreateJobObjectW
AssignProcessToJobObject = kernel32.AssignProcessToJobObject
TerminateJobObject = kernel32.TerminateJobObject
```

### C3. Resource Bounds Enforcement

- `AsyncExecutor`: `asyncio.Semaphore(max_concurrent)` for concurrency
- `AsyncExecutor`: `asyncio.wait_for()` for per-task timeouts
- Existing `_BoundedStream` for output bounding (preserved)
- Configurable timeouts from settings (already implemented)
- `ProcessManager` tracks and bounds child processes

---

## PHASE D: PLANNER INTEGRATION

### D1. PlannerAgent Class

**File:** `core/planner_agent.py` (NEW)

```python
class PlannerAgent:
    """Extracted planner logic with clear execution boundary."""
    
    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        events: EventBus,
        registry: ToolRegistry,
    ):
        self._planner = Planner(settings)
        self._router = router
        self._events = events
        self._registry = registry
    
    def create_plan(self, goal: str, context: RequestContext) -> ExecutionPlan:
        # ... extract from Agent._llm_loop()
    
    def validate_plan(self, plan: ExecutionPlan) -> ValidationResult:
        # ... uses Planner.validate()
    
    def replan(
        self,
        plan: ExecutionPlanRecord,
        failed_step: PlanStepRecord,
        reason: str,
    ) -> ExecutionPlan:
        # Bounded replanning with retry limits
```

### D2. Agent Delegation

Update `Agent.respond()` to delegate:
```python
def respond(self, text: str) -> str:
    # 1. Create RequestContext
    # 2. PlannerAgent.create_plan(goal, context) → ExecutionPlan
    # 3. ExecutionPlanRecord.from_plan() → durable plan
    # 4. ExecutorAgent.execute_plan(plan, context) → ExecutionResult
    # 5. Handle result, verification, completion
```

### D3. Bounded Replanning

- Max planning iterations: `settings.max_recovery_attempts` (default 3)
- Max replanning per request: `settings.max_recovery_attempts`
- Max retries per task: `settings.max_recovery_attempts`
- Planning iteration timeout: configurable

---

## PHASE E: UNIFIED TESTING

### Test Files to Create/Extend

| Test File | Coverage |
|-----------|----------|
| `tests/test_async_executor.py` | AsyncExecutor core |
| `tests/test_executor_agent.py` | ExecutorAgent orchestration |
| `tests/test_process_manager.py` | ProcessManager cleanup |
| `tests/test_planner_agent.py` | PlannerAgent extraction |
| `tests/test_cancellation_hierarchy.py` | Cancellation propagation |
| `tests/test_async_integration.py` | End-to-end async flow |
| `tests/test_replanning.py` | Bounded replanning |

### Test Categories

| Category | Tests | Approach |
|----------|-------|----------|
| Unit | 50+ | Mocks, deterministic |
| Integration | 20+ | Real tools, temp dirs |
| Cancellation | 15+ | Real subprocesses |
| Regression | 100+ | Existing suite |

---

## IMPLEMENTATION ORDER

### Week 1: Foundation (Days 1-3)
1. **Day 1:** `AsyncExecutor` + `ExecutorAgent` skeleton
2. **Day 2:** `AsyncExecutor.run_tool()` with streaming callbacks
3. **Day 3:** `ExecutorAgent.execute_plan()` with dependency gating

### Week 1: Integration (Days 4-5)
4. **Day 4:** `ProcessManager` for unified subprocess cleanup
5. **Day 5:** Integration with `Agent` — replace `Executor` with `ExecutorAgent`

### Week 2: Cancellation & Planner (Days 6-8)
6. **Day 6:** Unified cancellation hierarchy + `ProcessManager` integration
7. **Day 7:** `PlannerAgent` extraction + `Agent` delegation
8. **Day 8:** Bounded replanning + duplicate-task prevention

### Week 2: Testing & Polish (Days 9-10)
9. **Day 9:** Test suite execution + regression fixes
10. **Day 10:** Final diff review, documentation, report

---

## ROLLBACK POINTS

| Checkpoint | Rollback Action |
|------------|-----------------|
| After B1 | Revert `AsyncExecutor` creation; keep `Executor` |
| After B2 | Revert `ExecutorAgent`; `Agent` uses original `Executor` |
| After B3 | Remove streaming callbacks; no behavior change |
| After C1 | Revert cancellation hierarchy; keep sync execution |
| After C2 | Revert `ProcessManager`; keep per-tool `_stop_process()` |
| After D1 | Revert `PlannerAgent`; `Agent` uses `Planner` directly |
| After D2 | Revert delegation; `Agent` uses `self._planner` directly |

**Rollback Triggers:**
- Any existing test fails
- Performance regression >10%
- Behavioral change in smoke test
- Cancellation stops working

---

## SUCCESS CRITERIA

| Criterion | Target |
|-----------|--------|
| All 400+ existing tests pass | 100% |
| AsyncExecutor handles 4+ concurrent tools | Verified |
| ExecutorAgent executes plans asynchronously | Verified |
| PlannerAgent creates valid plans | Verified |
| Agent delegates planning to PlannerAgent | Verified |
| Replanning works after step failure | Verified |
| Streaming callbacks fire for UI | Verified |
| Cancellation works in async mode | Verified |
| No performance regression | <10% |
| Smoke test passes | 4/4 verification checks |

---

## RISK MITIGATION

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Thread pool deadlock | Medium | Bounded queue; timeout on submit; separate pool for I/O vs CPU |
| Async/sync boundary bugs | High | Clear separation: only `ExecutorAgent` is async; `Agent` stays sync |
| Cancellation race conditions | Medium | Single `CancellationToken` shared; atomic checks |
| UI blocking on async callbacks | Low | Use `root.after()` for Tk callbacks; never block main thread |
| Test flakiness | Medium | Deterministic fake tools; no real subprocesses in unit tests |

---

**Ready to begin implementation. Starting with Phase B1: AsyncExecutor.**