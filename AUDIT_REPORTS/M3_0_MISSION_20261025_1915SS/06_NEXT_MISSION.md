# NEXT MISSION: M3.1 FOUNDATION + PLANNER AGENT

**Follow-up to:** M3.0 Engineering Mission  
**Date:** 2026-10-25  
**Estimated Duration:** 2 weeks (2-3 hour sessions)

---

## MISSION OBJECTIVE

Build the async execution foundation and extract the first agent (PlannerAgent) as the stepping stone toward full Agent decomposition.

---

## ORDERED TASKS

### Week 1: Async Execution Foundation

| Task | Description | Acceptance Criteria | Effort |
|------|-------------|---------------------|--------|
| **1.1** | Create `AsyncExecutor` class with `ThreadPoolExecutor` | `AsyncExecutor.run_tool()` executes blocking tools in thread pool; returns `ToolResult` | 4h |
| **1.2** | Add async execution to `ExecutorAgent` (new class) | `ExecutorAgent.execute_plan()` uses `AsyncExecutor`; returns `ExecutionResult` | 4h |
| **1.3** | Add streaming callback infrastructure | `ExecutorAgent` emits `step_started`/`step_completed` events; UI can subscribe | 4h |
| **1.4** | Add cancellation to async execution | `AsyncExecutor.cancel_all()` stops pending/running tasks; propagates to tools | 2h |
| **1.5** | Unit tests for AsyncExecutor | 10+ tests covering: dispatch, timeout, cancellation, error handling | 3h |

**Week 1 Deliverable:** `AsyncExecutor` and `ExecutorAgent` working with async execution; all existing tests pass.

---

### Week 2: PlannerAgent Extraction

| Task | Description | Acceptance Criteria | Effort |
|------|-------------|---------------------|--------|
| **2.1** | Create `PlannerAgent` class | Extracts `Planner` logic; `create_plan()`, `validate_plan()`, `replan()` methods | 4h |
| **2.2** | Update `Agent` to delegate planning | `Agent._planner_agent` replaces direct `Planner` usage; `Agent.respond()` unchanged | 3h |
| **2.3** | Add replanning support | `PlannerAgent.replan(plan, failed_step, reason)` generates revised plan | 3h |
| **2.4** | Integration tests | End-to-end: goal → plan → execute → verify with new PlannerAgent | 4h |
| **2.5** | Regression test suite | All 400+ tests pass; no behavior change | 2h |

**Week 2 Deliverable:** `PlannerAgent` extracted and tested; `Agent` delegates planning; all tests pass.

---

## DEPENDENCIES

| Task | Depends On |
|------|------------|
| 1.1 | None (foundation) |
| 1.2 | 1.1 |
| 1.3 | 1.2 |
| 1.4 | 1.1 |
| 1.5 | 1.1, 1.4 |
| 2.1 | 1.1 (uses AsyncExecutor for planning steps?) |
| 2.2 | 2.1 |
| 2.3 | 2.1 |
| 2.4 | 2.2, 1.2 |
| 2.5 | All above |

---

## ROLLBACK REQUIREMENTS

| Checkpoint | Rollback Action |
|------------|-----------------|
| After 1.1 | Revert `AsyncExecutor` creation; keep `Executor` unchanged |
| After 1.2 | Revert `ExecutorAgent` creation; `Agent` uses original `Executor` |
| After 1.3 | Remove streaming callbacks; no behavior change |
| After 1.4 | Remove cancellation logic; keep sync execution |
| After 2.1 | Revert `PlannerAgent`; `Agent` uses `Planner` directly |
| After 2.2 | Revert delegation; `Agent` uses `self._planner` directly |

**Rollback Triggers:**
- Any existing test fails after change
- Performance regression >10%
- Behavioral change in smoke test
- Cancellation stops working

---

## ACCEPTANCE CRITERIA (M3.1 Complete)

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
| Thread pool deadlock | Medium | Use bounded queue; timeout on submit; separate pool for I/O vs CPU |
| Async/sync boundary bugs | High | Clear separation: only `ExecutorAgent` is async; `Agent` stays sync |
| Cancellation race conditions | Medium | Single `CancellationToken` shared; atomic checks |
| UI blocking on async callbacks | Low | Use `root.after()` for Tk callbacks; never block main thread |
| Test flakiness | Medium | Deterministic fake tools; no real subprocesses in unit tests |

---

## RESOURCE REQUIREMENTS

| Resource | Requirement |
|----------|-------------|
| Python | 3.11+ (existing) |
| Thread pool | 4 workers (configurable) |
| Memory | +50 MB for thread pool |
| CPU | Low overhead (<5% idle) |
| Test time | +30s for async tests |

---

## SUCCESS METRICS

| Metric | Baseline (M3.0) | Target (M3.1) |
|--------|-----------------|---------------|
| Concurrent tool execution | 0 (sequential) | 4+ parallel |
| Avg task latency (multi-tool) | 30s | <10s |
| UI responsiveness during execution | Frozen | Responsive |
| Test pass rate | 100% | 100% |
| Smoke test pass rate | 4/4 | 4/4 |

---

## NEXT AFTER M3.1

| Next Mission | Focus |
|--------------|-------|
| **M3.2** | VerifierAgent extraction + Ledger integration |
| **M3.3** | AgentOrchestrator + Streaming UI + Voice async |
| **M3.4** | Sandbox research + Prompt injection defense |

---

## STARTING POINT

```bash
# Current state
cd C:\PROJECTS\jarvis like ai for my computer\diablo-ai
git status  # Shows M3.0 changes
python -m pytest tests/test_agent.py -v  # Baseline
```

**First Command for M3.1:**
```bash
# Create AsyncExecutor foundation
mkdir -p core/async_executor
# Create core/async_executor/__init__.py with AsyncExecutor class
```

---

**Ready to begin M3.1 when authorized.**