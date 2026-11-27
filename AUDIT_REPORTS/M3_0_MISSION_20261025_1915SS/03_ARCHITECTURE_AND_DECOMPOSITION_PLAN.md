# ARCHITECTURE AND DECOMPOSITION PLAN

**Mission:** M3.0 Engineering Mission  
**Date:** 2026-10-25  
**Based on:** Audit reports + implemented fixes

---

## CURRENT ARCHITECTURE (Post-Fixes)

### Core Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         main.py                                  │
│  (Entry point, CLI, orchestration, UI launchers)               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Agent                                     │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌───────────┐ │
│  │  Planner    │ │  Executor   │ │  Verifier   │ │  Router   │ │
│  └─────────────┘ └─────────────┘ └─────────────┘ └───────────┘ │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌───────────┐ │
│  │  TaskMgr    │ │  TaskStore  │ │  VerifLedger│ │  Memory   │ │
│  └─────────────┘ └─────────────┘ └─────────────┘ └───────────┘ │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌───────────┐ │
│  │  Rules      │ │  Knowledge  │ │  Recovery   │ │  Cancel   │ │
│  └─────────────┘ └─────────────┘ └─────────────┘ └───────────┘ │
└──────────────────────────┬──────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌───────────┐    ┌───────────┐    ┌───────────┐
    │ Terminal  │    │   Edge    │    │  Voice    │
    │    UI     │    │ Companion │    │  Service  │
    └───────────┘    └───────────┘    └───────────┘
```

---

## COMPONENT RESPONSIBILITIES (Current)

| Component | Lines | Responsibilities |
|-----------|-------|------------------|
| **Agent** | 2,226 | Central orchestrator: planning, execution, verification, recovery, memory, routing, cancellation, task reconciliation, knowledge projection |
| **Planner** | ~400 | Plan creation, validation, dependency resolution, ExecutionPlanRecord |
| **Executor** | ~350 | Permission eval, confirmation, tool dispatch, outcome recording, **timeout injection** |
| **Verifier** | ~300 | File/output/exit_code/grep/python verification |
| **VerificationLedger** | ~250 | Adjudication authority, evidence validation, trust levels |
| **TaskManager** | ~600 | Task state machine, step tracking, terminal verdict invariant |
| **TaskStore** | ~500 | Persistent TaskRecord storage (JSONL) |
| **ModelRouter** | ~370 | Adaptive qwen3 8B/14B selection, escalation, project prefs |
| **Memory** | ~500 | Conversation, key-value, rules, preferences, projects |
| **KnowledgeGraph** | ~1,200 | SQLite per-project, nodes/edges, staleness, canonical keys |
| **VoiceService** | ~300 | STT/TTS/VAD/wake-word/pipeline/cancellation |
| **EdgeCompanionApp** | ~980 | Tk UI, animation, docking, personas |
| **Tools** | 50+ | Filesystem, terminal, python, git, github, coding, data, chart, memory, rules, models, tasks, knowledge, desktop, blender, vision |

---

## PROPOSED DECOMPOSITION (M3.0 Target)

### Phase 1: Extract Core Agents (Week 1-2)

```
┌─────────────────────────────────────────────────────────────────┐
│                    AgentOrchestrator (NEW)                      │
│  - Receives user input                                          │
│  - Coordinates PlannerAgent → ExecutorAgent → VerifierAgent    │
│  - Manages RequestContext, cancellation, lifecycle events       │
│  - Public API unchanged (respond(), cancel_current_task())      │
└─────────────────────────────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│  PlannerAgent   │ │ ExecutorAgent   │ │ VerifierAgent   │
│  (NEW class)    │ │ (NEW class)     │ │ (NEW class)     │
│                 │ │                 │ │                 │
│ - create_plan() │ │ - execute_plan()│ │ - verify_step() │
│ - validate()    │ │ - dispatch()    │ │ - adjudicate()  │
│ - dependencies  │ │ - timeouts      │ │ - ledger entry  │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

### Proposed Interfaces

#### PlannerAgent
```python
class PlannerAgent:
    def __init__(self, settings: Settings, router: ModelRouter):
        pass
    
    def create_plan(self, goal: str, context: RequestContext) -> ExecutionPlan:
        """Create validated execution plan from goal."""
        pass
    
    def validate_plan(self, plan: ExecutionPlan) -> ValidationResult:
        """Check for cycles, missing deps, resource feasibility."""
        pass
    
    def replan(self, plan: ExecutionPlan, failed_step: Step, reason: str) -> ExecutionPlan:
        """Generate revised plan after failure."""
        pass
```

#### ExecutorAgent
```python
class ExecutorAgent:
    def __init__(self, registry: ToolRegistry, permissions: PermissionManager,
                 events: EventBus, settings: Settings):
        pass
    
    def execute_plan(self, plan: ExecutionPlan, context: RequestContext) -> ExecutionResult:
        """Execute plan with permission checks, timeouts, cancellation."""
        pass
    
    def dispatch_step(self, step: PlanStep, context: RequestContext) -> StepResult:
        """Execute single step with timeout and cancellation."""
        pass
    
    def cancel(self, task_id: str) -> bool:
        """Cancel active execution and propagate to tools."""
        pass
```

#### VerifierAgent
```python
class VerifierAgent:
    def __init__(self, verifier: Verifier, ledger: VerificationLedger, settings: Settings):
        pass
    
    def verify_step(self, step: PlanStep, outcome: ExecutionOutcome, context: RequestContext) -> VerificationResult:
        """Verify step outcome against expected results."""
        pass
    
    def verify_task(self, task: TaskRecord) -> VerificationResult:
        """Verify entire task completion."""
        pass
    
    def adjudicate(self, result: VerificationResult) -> VerificationEntry:
        """Record verdict in ledger."""
        pass
```

---

## DEPENDENCY ORDERING (Critical Path)

```
FOUNDATION (Must complete first)
├── 1. AsyncExecutor infrastructure (thread pool + asyncio bridge)
├── 2. RequestContext propagation (already exists in M2.12 P1)
├── 3. Cancellation propagation (already works via CancellationToken)
├── 4. EventBus prioritization (separate channels for tool/task/verification)

AGENT EXTRACTION (Can proceed in parallel after Foundation)
├── 5. PlannerAgent extraction (depends on: Planner, ModelRouter, Settings)
├── 6. VerifierAgent extraction (depends on: Verifier, Ledger, Settings)
├── 7. ExecutorAgent extraction (depends on: Executor, Permissions, ToolRegistry)

ORCHESTRATION (Requires all three agents)
├── 8. AgentOrchestrator (replaces Agent.respond())
├── 9. Streaming callbacks (token/step → UI)
├── 10. RequestContext lifecycle management

UI INTEGRATION (Parallel with Orchestration)
├── 11. TerminalUI streaming support
├── 12. EdgeCompanionApp async updates
├── 13. VoiceService async pipeline
```

---

## COMPATIBILITY CONSTRAINTS

| Constraint | Current State | Required for Decomposition |
|------------|---------------|---------------------------|
| **Public API** | `Agent.respond()`, `Agent.cancel_current_task()` | Must remain unchanged |
| **Permission checks** | In Executor.run() | Must remain in ExecutorAgent |
| **Verification ledger** | Single authority | Must remain in VerifierAgent |
| **Task persistence** | TaskStore JSONL | Shared across agents |
| **Cancellation** | CancellationToken | Shared across agents |
| **Model routing** | ModelRouter in Agent | Available to PlannerAgent |
| **Memory/knowledge** | Stores in Agent | Shared read access |

---

## EXTRACTION STRATEGY (Low Risk)

### Step 1: Create Agent Classes (No Behavior Change)
```python
# core/planner_agent.py
class PlannerAgent:
    def __init__(self, settings, router, events):
        self._planner = Planner(settings)
        self._router = router
        self._events = events
    
    # Wrap existing Planner methods
```

### Step 2: Update Agent to Delegate
```python
# In Agent.__init__
self._planner_agent = PlannerAgent(settings, self._router, self._events)
self._executor_agent = ExecutorAgent(...)
self._verifier_agent = VerifierAgent(...)

# In Agent.respond()
plan = self._planner_agent.create_plan(goal, context)
result = self._executor_agent.execute_plan(plan, context)
verdict = self._verifier_agent.verify_task(task)
```

### Step 3: Extract Methods (One at a Time)
- Move `Planner` logic → `PlannerAgent`
- Move `Executor` logic → `ExecutorAgent`  
- Move `Verifier`/`Ledger` logic → `VerifierAgent`

### Step 3: Verify Compatibility
- All existing tests pass
- Public API unchanged
- No behavior changes

---

## ASYNC EXECUTION DESIGN

### Current (Synchronous)
```
Agent.respond() 
  → Executor.run() [BLOCKS]
    → Tool.fn() [BLOCKS]
      → subprocess.run() [BLOCKS]
```

### Target (Async)
```
Agent.respond() 
  → asyncio.create_task(ExecutorAgent.execute_plan())
    → asyncio.to_thread(Tool.fn()) [for blocking tools]
      → subprocess.run() [in thread pool]
    → UI receives streaming callbacks
```

### Thread Pool Strategy
```python
class AsyncExecutor:
    def __init__(self, max_workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._loop = asyncio.get_event_loop()
    
    async def run_tool(self, tool: str, args: dict, timeout: float) -> ToolResult:
        # For blocking tools (terminal, python, blender)
        return await self._loop.run_in_executor(self._pool, _run_tool_sync, tool, args, timeout)
    
    async def run_tool_async(self, tool: str, args: dict) -> ToolResult:
        # For async-native tools (future)
        pass
```

---

## RISK MITIGATION

| Risk | Mitigation |
|------|------------|
| Breaking public API | Keep `Agent.respond()` signature; delegate internally |
| Breaking permission checks | Keep in ExecutorAgent; don't move to orchestrator |
| Breaking verification | Keep Ledger as single authority; VerifierAgent writes to it |
| Breaking cancellation | CancellationToken shared; agents check it |
| Breaking tests | Extract incrementally; run full suite after each step |
| Tk UI blocking | Move agent communication to background thread; UI callbacks via `root.after()` |

---

## NEXT MILESTONE DEFINITION

### M3.1: Foundation + PlannerAgent (2 weeks)
- [ ] AsyncExecutor with thread pool
- [ ] PlannerAgent extracted and tested
- [ ] Agent delegates planning to PlannerAgent
- [ ] All tests pass

### M3.2: ExecutorAgent + VerifierAgent (2 weeks)
- [ ] ExecutorAgent extracted with async execution
- [ ] VerifierAgent extracted with ledger integration
- [ ] Agent delegates execution and verification
- [ ] Streaming callbacks to UI implemented

### M3.3: Orchestration + UI Integration (2 weeks)
- [ ] AgentOrchestrator replaces Agent.respond()
- [ ] Streaming callbacks to TerminalUI/EdgeCompanionApp
- [ ] VoiceService async pipeline
- [ ] All tests pass; performance baseline established

---

**STATUS:** Architecture mapped, interfaces defined, dependency ordering established. Ready for incremental extraction starting with PlannerAgent.