# M3.1 ARCHITECTURE MAP — DIABLO PROJECT

**Project Root:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Mission:** M3.1 — Unified Async Execution, Cancellation, Resource Safety & Planner Integration  
**Date:** 2026-10-25  
**Git Branch:** `backup/pre-m3-0`  
**Git HEAD:** `94556a1` (Backup: pre-M3.0 state with CI, resource bounds, docs)

---

## 1. HOW A USER REQUEST REACHES THE PLANNER

```
User Input (TerminalUI / EdgeCompanion / VoiceService)
         │
         ▼
Agent.respond(text)  [core/agent.py]
         │
         ├── _capture_plan_goal()  → extracts %plan directives
         ├── _select_model()       → ModelRouter chooses model
         ├── _build_system_prompt() → builds system prompt with context
         │
         ▼
Provider.chat(messages, tools)  [AIProvider → OllamaProvider]
         │
         ▼
AIResponse (text + ToolCalls)
         │
         ▼
Agent._execute_plan() / Agent._llm_loop()
         │
         ├── Planner.fast_intent()  → fast-path for known intents
         ├── Planner.coding_intent() → classifies coding requests
         │
         ▼
Planner creates ExecutionPlan (validated by Planner.validate())
         │
         ▼
ExecutionPlanRecord created (durable, persisted)
         │
         ▼
PlanStepRecord steps executed via Executor
```

**Key Files:**
- `main.py` → UI entry points, Agent creation
- `core/agent.py` → `Agent.respond()`, `_select_model()`, `_execute_plan()`, `_llm_loop()`
- `core/planner.py` → `Planner`, `ExecutionPlan`, `ExecutionPlanRecord`
- `ai/model_router.py` → `ModelRouter.choose()`
- `ai/ollama_provider.py` → `OllamaProvider.chat()`

---

## 2. HOW THE PLANNER CREATES AND SCHEDULES TASKS

```
Planner.fast_intent() / Planner.coding_intent()
         │
         ▼
Intent (name, steps[], summary)
         │
         ▼
ExecutionPlan.from_intent() → creates PlanStep[] with dependencies
         │
         ▼
Planner.validate(registry) → validates tools, args, dependencies, cycles
         │
         ▼
ExecutionPlanRecord.from_plan() → durable PlanStepRecord[]
         │
         ├── step_id, tool, args, depends_on, expected_result
         ├── status = PENDING, attempts=0
         ├── side_effect_key = fingerprint of side effect
         │
         ▼
Agent._active_plan = ExecutionPlanRecord
         │
         ▼
Agent._persist_plan() → TaskStore.update(task_id, plan=record)
```

**PlanStep Dependencies:**
- Linear by default: each step depends on previous (`depends_on=(previous,)`)
- `ExecutionPlanRecord.next_executable()` → dependency-gated runnable steps
- `PlanStepRecord.dependencies_satisfied()` / `dependencies_unsatisfiable()`

**Key Files:**
- `core/planner.py` → `Planner`, `ExecutionPlan`, `ExecutionPlanRecord`, `PlanStepRecord`
- `core/task_manager.py` → `StepStatus`, `TaskState`, state machines

---

## 3. HOW TASKS INVOKE TOOLS AND SUBPROCESSES

```
Agent._execute_plan() / Agent._llm_loop()
         │
         ▼
ExecutionPlanRecord.next_step() → next executable PlanStepRecord
         │
         ▼
Agent._execute_step(step)
         │
         ├── step.begin_attempt() → status=RUNNING, attempts++
         ├── _step_write_ahead() → persist RUNNING state
         │
         ▼
Executor.run(tool, args, cancellation_token)
         │
         ├── PermissionManager.evaluate() → PermissionDecision
         ├── Confirmer (if requires_confirmation)
         ├── cancellation_requested() checks at 4 points
         ├── _before_execute() hook (Agent._before_execute)
         │
         ▼
ToolSpec.fn(**call_args)  [tools/*.py]
         │
         ├── terminal.execute → run_command() [tools/terminal.py]
         │       └── subprocess.Popen(PS_PREFIX + [command], ...)
         │       └── _stop_process() → taskkill /T /PID (Windows)
         │
         ├── python.run_script → run_script() [tools/python.py]
         │       └── subprocess.Popen([sys.executable, script], ...)
         │       └── _stop_process() → terminate/kill
         │
         ├── blender.* → capability.execute → BlenderCapabilityLayer
         │
         └── other tools → direct function calls
```

**Subprocess Management:**
- `tools/terminal.py` → `run_command()` → `subprocess.Popen` + `taskkill /T /PID` (Windows)
- `tools/python.py` → `run_script()` → `subprocess.Popen` + `terminate()`/`kill()`
- Both use `_BoundedStream` for bounded output capture
- Both check `cancellation_requested()` at poll intervals

**Key Files:**
- `core/executor.py` → `Executor.run()`, timeout injection
- `tools/terminal.py` → `run_command()`, `_stop_process()` (Windows `taskkill /T`)
- `tools/python.py` → `run_script()`, `_stop_process()`
- `core/cancellation.py` → `CancellationToken`, `cancellation_requested()`

---

## 4. HOW RESULTS, PROGRESS, ERRORS, AND COMPLETION ARE REPORTED

```
Tool execution completes
         │
         ▼
ExecutionOutcome (tool, allowed, executed, success, cancelled, output, error, verification)
         │
         ▼
Executor._finish() → logs duration, status
         │
         ▼
Executor._emit_completed() → EventBus.emit("tool_completed", payload)
         │
         ▼
EventBus.emit_tool(tool, status, display)
         │
         ▼
Agent._record_execution(tool, args, outcome)
         │
         ├── TaskManager.add_step() / set_status()
         ├── ChangeTracker.step() → coding session tracking
         ├── TaskStore persistence
         │
         ▼
Agent._step_finalize() → PlanStepRecord.finish(status, detail, result_summary)
         │
         ▼
Verification (if outcome.success and outcome.verification)
         │
         ▼
TaskManager.record_verification() → VerificationLedger.adjudicate()
         │
         ▼
Task completion → TaskManager.final_status() → COMPLETED/FAILED/CANCELLED
         │
         ▼
Terminal verdict invariant: terminal_verdict(TaskState, VerificationState)
```

**Event Flow:**
- `EventBus.emit("tool_completed", payload)` → UI, logging
- `EventBus.emit_tool(tool, status, display)` → UI progress
- `Agent._emit_lifecycle()` → task lifecycle events

**Key Files:**
- `core/executor.py` → `ExecutionOutcome`, `Executor.run()`, `_emit_completed()`
- `core/events.py` → `EventBus.emit()`, `emit_tool()`
- `core/agent.py` → `_record_execution()`, `_record_verification()`
- `core/task_manager.py` → `TaskManager`, `VerificationLedger`
- `core/verification_ledger.py` → `VerificationLedger.adjudicate()`

---

## 5. HOW "DIABLO, STOP" CURRENTLY PROPAGATES

```
User: "STOP" / UI Stop Button / Voice STOP
         │
         ▼
VoiceService.cancel() / EdgeCompanionApp._on_stop() / TerminalUI.stop()
         │
         ▼
VoiceService.interruptions.request_cancel(task_id="", source="voice")
         │
         ▼
InterruptionManager.request_cancel(task_id="", source="voice", reason="user_interruption")
         │
         ├── token_for(task_id).request() → CancellationToken._event.set()
         ├── cancel_callback(task_id) → Agent.cancel_current_task()
         │       └── token.cancel() / token.request() / token.cancelled = True
         │
         ▼
CancellationToken._event.set() → CancellationToken.is_cancelled() = True
         │
         ▼
Executor.run() checks cancellation_requested() at 4 points:
         1. Pre-permission
         2. Post-permission / pre-confirmation
         2. Pre-execute (before dispatch)
         3. Post-execution
         │
         ▼
Tool checks cancellation_requested(cancel_token) in polling loops:
         │
         ├── terminal.execute → polling loop with 50ms interval
         ├── python.run_script → polling loop with 50ms interval
         │
         ▼
On cancellation/timed_out → _stop_process() → taskkill /T /PID (Windows) or terminate/kill
```

**Voice Command Router:**
- `VoiceCommandRouter` handles "stop", "cancel", "abort" → `InterruptionManager.request_cancel()`

**Key Files:**
- `voice/service.py` → `VoiceService.cancel()`, `InterruptionManager`
- `voice/cancellation.py` → `InterruptionManager`, `CancellationToken`
- `core/agent.py` → `Agent.cancel_current_task()`
- `core/executor.py` → `Executor.run()` checks at 4 points
- `tools/terminal.py`, `tools/python.py` → polling loops with `cancellation_requested()`

---

## 6. WHERE CANCELLATION OR CLEANUP CAN CURRENTLY FAIL

| Gap | Location | Impact |
|-----|----------|--------|
| **No async execution** | `Executor.run()` is synchronous | Blocks Agent thread; UI freezes during long tools |
| **No task queue** | `Executor.run()` runs directly | No bounded concurrency; no backpressure |
| **No streaming callbacks** | `Executor.run()` returns `ExecutionOutcome` at end | No progress updates during long tools |
| **Cancellation not in all tools** | Only terminal/python check `cancellation_requested()` | Git, GitHub, Blender, file ops ignore cancellation |
| **No process tree tracking** | `terminal.py` uses `taskkill /T`; `python.py` only kills direct child | Blender, nested processes may survive |
| **No async execution layer** | `Executor.run()` blocks | Agent thread blocked; UI freezes; autonomy stalls |
| **No streaming progress** | `Executor.run()` returns at end | UI cannot show progress during long operations |
| **Cancellation not in planner** | Planner doesn't check cancellation during planning | Long planning can't be interrupted |
| **No bounded task queue** | Direct execution | No backpressure; unlimited concurrent tools possible |
| **No unified cancellation hierarchy** | Voice → InterruptionManager → Agent → Executor → Tools | Gap between planner and executor cancellation |

**Known Limitation (from recovery.py):**
> "Some S2.8 workflow calls cannot interrupt an already-running Blender child because supervision is not bound directly to the task token."

---

## 7. EXISTING INTERFACES THAT MUST REMAIN COMPATIBLE

| Interface | Consumers | Must Preserve |
|-----------|-----------|---------------|
| `Agent.respond(text)` | TerminalUI, EdgeCompanion, VoiceService, AutonomyRuntime | Signature, return type, behavior |
| `Agent.cancel_current_task()` | VoiceService, EdgeCompanion, TerminalUI | Return bool, cancels current task |
| `Executor.run(tool, args, cancellation_token)` | Agent, tests | Signature, `ExecutionOutcome` return |
| `Executor.set_task_provider()` | Agent | Called during Agent init |
| `Executor.set_confirmer()` | Agent, UI | Called when UI attaches |
| `Planner.fast_intent()` / `coding_intent()` | Agent | Returns `Intent` or None |
| `Planner.validate(registry)` | Agent | Validates `ExecutionPlan` |
| `Executor.run()` timeout injection | `terminal.execute`, `python.run_script` | Injects `timeout` param if not present |
| `EventBus.emit()` / `emit_tool()` | UI, logging, tests | Signature, event types |
| `Agent.cancel_current_task()` | VoiceService, EdgeCompanion | Returns bool |
| `CancellationToken.is_cancelled()` | Tools, Executor | Protocol compliance |
| `InterruptionManager.request_cancel()` | VoiceService, VoiceCommandRouter | Returns `CancellationOutcome` |
| `CancellationToken.is_cancelled()` | Tools, Executor | Protocol |
| `Agent.cancel_current_task()` | VoiceService.cancel(), UI stop | Called via bridge |

---

## 8. ARCHITECTURE MAP SUMMARY

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER INTERFACES                                │
│  ┌─────────────┐  ┌─────────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ TerminalUI  │  │ EdgeCompanion   │  │ VoiceService │  │ Autonomy     │ │
│  └──────┬──────┘  └────────┬────────┘  └──────┬───────┘  └──────┬───────┘ │
└─────────┼───────────────────┼──────────────────┼────────────────┼─────────┘
          │                   │                  │                │
          ▼                   ▼                  ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  AGENT                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        Agent.respond()                                │   │
│  │  ┌──────────────┐  ┌─────────────┐  ┌─────────────┐  ┌───────────┐  │   │
│  │  │ ModelRouter  │  │   Planner   │  │  Executor   │  │ Verifier  │  │   │
│  │  └──────────────┘  └──────┬──────┘  └──────┬──────┘  └───────────┘  │   │
│  │                           │                │                          │   │
│  │                    ┌──────▼──────┐         │                          │   │
│  │                    │   Planner   │         │                          │   │
│  │                    │  Execution  │         │                          │   │
│  │                    │   Plan      │         │                          │   │
│  │                    └──────┬──────┘         │                          │   │
│  └──────────────────────────┼────────────────┼──────────────────────────┘   │
└────────────────────────────┼────────────────┼───────────────────────────────┘
                             │                │
              ┌──────────────┘                ▼
              │                    ┌─────────────────────┐
              │                    │    EXECUTOR         │
              │                    │  ┌───────────────┐  │
              │                    │  │ PermissionMgr │  │
              │                    │  └───────┬───────┘  │
              │                    │           │          │
              │                    ▼           ▼          │
              │         ┌─────────────────┐ ┌─────────┐  │
              │         │  Tool Registry  │ │Confirm  │  │
              │         └────────┬────────┘ └─────────┘  │
              │                  │                         │
              ▼                  ▼                         ▼
       ┌─────────────────────────────────────────────────────────────┐
       │                        TOOLS                                │
       │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
       │  │ terminal │ │ python   │ │ filesystem│ │ git, github, │  │
       │  │ .execute │ │ .run_    │ │ .read/   │ │ blender,     │  │
       │  │          │ │ script   │ │ write    │ │ vision, etc. │  │
       │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘  │
       └───────┼────────────┼────────────┼─────────────┼──────────┘
               │            │            │             │
               ▼            ▼            ▼             ▼
       ┌─────────────────────────────────────────────────────────────┐
       │                    SUBPROCESSES                             │
       │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
       │  │ PowerShell   │  │ Python       │  │ Blender, git,    │  │
       │  │ (taskkill)   │  │ (terminate)  │  │ gh, vision, etc. │  │
       │  └──────────────┘  └──────────────┘  └──────────────────┘  │
       └─────────────────────────────────────────────────────────────┘

                    ▲                    ▲              ▲
                    │                    │              │
                    └────────────────────┴──────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │    CANCELLATION HIERARCHY │
                    │  Voice → InterruptionMgr  │
                    │    → Agent.cancel()       │
                    │    → Executor (4 checks)  │
                    │    → Tools (polling)      │
                    │    → Subprocess (kill)    │
                    └───────────────────────────┘
```

---

## 9. KEY INTEGRATION POINTS FOR M3.1

| Integration Point | Current State | M3.1 Target |
|-------------------|---------------|-------------|
| **Executor → AsyncExecutor** | Synchronous `Executor.run()` | `AsyncExecutor` with `ThreadPoolExecutor` |
| **Agent → ExecutorAgent** | Direct `Executor` usage | `ExecutorAgent` wrapper |
| **Executor → AsyncExecutor** | Sync `run()` | Async `run_tool()` + streaming callbacks |
| **Cancellation** | 4 checkpoints in `Executor.run()` | Unified `CancellationToken` hierarchy |
| **Planner → Executor** | Direct `Executor.run()` calls | `ExecutorAgent.execute_plan()` |
| **Progress/Callbacks** | None (returns at end) | Streaming callbacks: queued/running/completed |
| **Cancellation Propagation** | 4 checkpoints in `Executor.run()` | Full hierarchy: Planner → ExecutorAgent → AsyncExecutor → Tools |
| **Subprocess Cleanup** | Per-tool `_stop_process()` | Unified `ProcessManager` with Windows job objects |
| **Planner/Executor Separation** | Tightly coupled in `Agent._llm_loop()` | Clear boundary: `PlannerAgent` ↔ `ExecutorAgent` |

---

## 10. IMPLEMENTATION PLAN (PHASE B-D)

### Phase B: Async Execution Foundation
1. **AsyncExecutor** class with `ThreadPoolExecutor`
   - `run_tool(tool, args, timeout)` → async, returns `ToolResult`
   - Bounded concurrency via `asyncio.Semaphore`
   - Streaming callbacks: `on_start`, `on_progress`, `on_complete`
2. **ExecutorAgent** class
   - Wraps `AsyncExecutor`
   - `execute_plan(plan, context)` → dispatches steps with dependency gating
   - Streaming callbacks for UI
3. Update `Agent` to use `ExecutorAgent` instead of direct `Executor`

### Phase C: Cancellation & Resource Safety
1. Unified `CancellationToken` hierarchy
   - `AsyncExecutor` creates child tokens for each task
   - `ExecutorAgent` propagates cancellation to `AsyncExecutor`
   - `PlannerAgent` checks cancellation during planning
2. Unified `ProcessManager` for subprocess cleanup
   - Windows Job Objects for process tree termination
   - Track all child processes per task
3. Resource bounds enforcement
   - Bounded concurrency in `AsyncExecutor`
   - Bounded output buffers (existing `_BoundedStream`)
   - Configurable timeouts (already in settings)

### Phase D: Planner Integration
1. **PlannerAgent** class
   - Extracts `Planner` logic from `Agent`
   - `create_plan(goal, context)` → `ExecutionPlan`
   - `replan(plan, failed_step, reason)` → revised `ExecutionPlan`
3. **Agent** delegates to `PlannerAgent` + `ExecutorAgent`
   - `Agent.respond()` → `PlannerAgent.create_plan()` → `ExecutorAgent.execute_plan()`
   - Streaming callbacks → UI
   - Cancellation propagates through all layers

---

**END OF ARCHITECTURE MAP**