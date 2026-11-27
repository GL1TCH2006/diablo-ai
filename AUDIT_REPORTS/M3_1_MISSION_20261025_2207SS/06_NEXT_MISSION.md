# NEXT MISSION: M3.1 FOUNDATION + PLANNER INTEGRATION

**Follow-up to:** M3.0 Engineering Mission  
**Date:** 2026-10-25  
**Estimated Duration:** 2 weeks (2-3 hour sessions)

---

## MISSION OBJECTIVE

Build the async execution foundation and extract the first agent (PlannerAgent) as the stepping stone toward full Agent decomposition.

The completed audit reports identify these major architectural and reliability concerns:

- `core/agent.py` is approximately 2,226 lines and combines several responsibilities.
- Tool execution is synchronous in important paths.
- The Tk-based companion UI can become unresponsive during long-running operations.
- Resource limits are configured incompletely and are not reliably enforced.
- There is no established CI/CD quality gate.
- Untrusted tool output can enter model context without sufficient isolation.
- Arbitrary code and shell execution lack a comprehensive sandbox boundary.
- Existing tests may be slow, flaky, or capable of hanging.
- Agent decomposition, asynchronous execution, and streaming are important foundations for the long-term roadmap.

**Preserve the working foundation. Improve it incrementally. Do not replace working systems merely to produce a cleaner architecture.**

This mission has already completed the foundation work:
- ✅ AsyncExecutor with bounded concurrency
- ✅ ExecutorAgent with plan execution and streaming callbacks
- ✅ ProcessManager for unified subprocess management
- ✅ Concrete CancellationToken implementation
- ✅ PlannerAgent extraction
- ✅ Agent integration with async execution
- ✅ CancellationToken concrete implementation

---

## ORDERED TASKS

### Week 1: Planner Integration & Streaming

| Task | Description | Acceptance Criteria | Effort |
|------|-------------|---------------------|--------|
| **1.1** | Complete PlannerAgent integration | `Agent.respond()` delegates to `PlannerAgent.create_plan()` and `ExecutorAgent.execute_plan()` | 4h |
| **1.2** | Implement `_run_intent` using PlannerAgent | Replace direct Planner usage with PlannerAgent | 3h |
| **1.3** | Implement streaming callbacks | `on_step_start`, `on_step_progress`, `on_step_complete` fire correctly | 3h |
| **1.4** | Add cancellation during planning | PlannerAgent checks cancellation token | 2h |
| **1.5** | Unit tests for PlannerAgent | 10+ tests covering plan creation, validation, replanning | 3h |

**Week 1 Deliverable:** PlannerAgent fully integrated; Agent delegates planning; streaming callbacks work; all existing tests pass.

---

### Week 2: Replanning & Bounded Replanning

| Task | Description | Acceptance Criteria | Effort |
|------|-------------|---------------------|--------|
| **2.1** | Implement `PlannerAgent.replan()` | Generates revised plan after step failure | 4h |
| **2.2** | Bounded replanning limits | Max planning iterations, max replanning per request, max retries per task | 3h |
| **2.3** | Duplicate-task prevention | Stable task IDs, track queued/running/completed/failed/cancelled | 3h |
| **2.4** | Integration tests | End-to-end: goal → plan → execute → verify with new PlannerAgent | 4h |
| **2.5** | Regression test suite | All 400+ tests pass; no behavior change | 2h |

**Week 2 Deliverable:** PlannerAgent extracted and tested; Agent delegates planning; replanning works; all tests pass.

---

## DEPENDENCIES

| Task | Depends On |
|------|------------|
| 1.1 | PlannerAgent exists (✅ done) |
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
| After 1.1 | Revert `PlannerAgent` creation; `Agent` uses original `Planner` |
| After 1.2 | Revert delegation; `Agent` uses `self._planner` directly |
| After 1.3 | Remove streaming callbacks; no behavior change |
| After 1.4 | Remove cancellation checks; keep sync execution |
| After 2.1 | Revert `PlannerAgent.replan()`; `Agent` uses original `Planner` |
| After 2.2 | Revert replanning limits; keep sync execution |
| After 2.3 | Revert duplicate-task prevention; no behavior change |

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
git status  # Shows M3.1 changes
python -m pytest tests/test_agent.py -v  # Baseline
```

**First Command for M3.1 Week 2:**
```bash
# Implement PlannerAgent.replan() with bounded retries
# Update Agent._run_intent() to use PlannerAgent.replan() on failure
```

---

**Ready to begin M3.1 Week 2 when authorized.**