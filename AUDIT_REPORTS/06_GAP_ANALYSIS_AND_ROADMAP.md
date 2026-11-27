# DIABLO GAP ANALYSIS AND ROADMAP

**Project Root:** `C:\PROJECTS\jarvis like ai for my computer\diablo-ai`  
**Audit Date:** 2026-10-25  
**Git Branch:** `reliability-batch-1`  
**Git HEAD:** `a74b53c`

---

## 1. VISION STATEMENT (FROM AGENTS.md)

> Diablo's long-term goal is to become a **personal AI partner** that:
> - **Reasons, calculates, evaluates pros and cons**
> - **Plans, researches, uses computer tools**
> - **Executes approved tasks**
> - **Remembers relevant context**
> - **Verifies results**

---

## 2. CURRENT STATE vs VISION — GAP MATRIX

| Vision Capability | Current State | Gap Classification | Evidence |
|-------------------|---------------|-------------------|----------|
| **Reasons, calculates, evaluates pros/cons** | PARTIAL | Single-turn planning; no multi-criteria decision analysis | `core/planner.py` creates linear plans; no evaluation framework |
| **Plans** | IMPLEMENTED | ExecutionPlan with dependencies; but no replanning on failure | `core/planner.py`, `core/agent.py:1557-1604` |
| **Researches** | PARTIAL | Tools exist (project.grep, knowledge.query, web search missing) | No web search tool; knowledge graph local only |
| **Uses computer tools** | IMPLEMENTED | 50+ tools across filesystem, terminal, git, coding, data, desktop | `tools/__init__.py` — comprehensive tool registry |
| **Executes approved tasks** | IMPLEMENTED | Permission ladder, confirmations, session blocks, rules | `config/permissions.py`, `core/executor.py` |
| **Remembers relevant context** | IMPLEMENTED | Conversation, memory, rules, project memory, knowledge graph | `memory/`, `core/knowledge_graph.py` |
| **Verifies results** | IMPLEMENTED | Verifier + VerificationLedger with terminal verdict invariant | `core/verification.py`, `core/verification_ledger.py` |

---

## 3. DETAILED GAP ANALYSIS BY CAPABILITY

### 3.1 Reasoning & Decision Making

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Multi-turn reasoning | Single-turn Agent.respond() | Persistent reasoning chains | No chain-of-thought persistence | HIGH |
| Pros/cons evaluation | None | Structured decision framework | Missing entirely | HIGH |
| Uncertainty quantification | None | Confidence scores on decisions | Missing | MEDIUM |
| Counterfactual reasoning | None | "What if" analysis | Missing | LOW |
| Learning from outcomes | Recovery policy only | Preference/weight updates | Minimal | MEDIUM |

### 3.2 Planning

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Hierarchical planning | Flat ExecutionPlan | Multi-level (strategic→tactical) | Missing | HIGH |
| Conditional branching | Linear dependencies | If/then/else in plans | Missing | HIGH |
| Resource-aware planning | None | Time/memory/cost estimates | Missing | HIGH |
| Replanning on failure | RecoveryPolicy retries | Full plan revision | Partial (step retry only) | HIGH |
| Multi-agent coordination | Single Agent | Planner/Executor/Verifier separation | Monolith Agent | HIGH |

### 3.3 Research & Information Gathering

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Web search | Missing | Search API integration | Missing entirely | HIGH |
| Document retrieval | Local knowledge graph only | RAG over arbitrary docs | Missing | HIGH |
| API integration | GitHub only | Generic REST/GraphQL | Missing | MEDIUM |
| Citation tracking | None | Source attribution | Missing | MEDIUM |
| Fact verification | Code/output verification only | External fact-checking | Missing | LOW |

### 3.4 Tool Use & Computer Control

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Filesystem | ✅ Full | ✅ | None | — |
| Terminal/Shell | ✅ Full (PowerShell) | ✅ Cross-platform | Windows-only | MEDIUM |
| Code editing | ✅ patch_file | ✅ LSP integration | No semantic editing | MEDIUM |
| Desktop automation | ✅ Windows (UIA) | ✅ Cross-platform | Windows-only | HIGH |
| Browser automation | Missing | Playwright/Selenium | Missing entirely | HIGH |
| Application scripting | Blender only | Generic COM/DBus/AppleScript | Very limited | MEDIUM |
| Container/VM control | Missing | Docker/Podman/Vagrant | Missing | LOW |

### 3.5 Execution & Approval

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Permission ladder | ✅ SAFE/NORMAL/SENSITIVE | ✅ Fine-grained | None | — |
| Confirmation UX | Console + UI buttons | ✅ Rich (diff preview, risk summary) | Basic | LOW |
| Audit trail | ✅ TaskRecord + VerificationLedger | ✅ Tamper-proof | Good | — |
| Rollback/Undo | Git snapshots + checkpoints | ✅ One-click revert | Partial (coding only) | MEDIUM |
| Dry-run/Simulation | code.patch_file dry_run | ✅ All tools | Missing for most tools | MEDIUM |

### 3.6 Memory & Context

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Conversation history | ✅ 20 turns | ✅ Unlimited + summarization | Fixed window | MEDIUM |
| Long-term memory | ✅ Key-value + categories | ✅ Vector + semantic search | Linear scan only | HIGH |
| Project context | ✅ ProjectMemory + knowledge graph | ✅ Cross-project | Per-project isolation | MEDIUM |
| Rule learning | ✅ Explicit rules only | ✅ Implicit preference learning | No learning | MEDIUM |
| Context relevance | Fixed char limits | ✅ Dynamic relevance ranking | Heuristic only | HIGH |
| Memory consolidation | None | ✅ Automatic summarization | Missing | MEDIUM |

### 3.7 Verification & Quality Assurance

| Sub-capability | Current | Target | Gap | Priority |
|----------------|---------|--------|-----|----------|
| Code verification | ✅ project.verify (pytest) | ✅ Multi-language | Python only | MEDIUM |
| Output verification | ✅ Verifier (file/exit/grep) | ✅ Semantic verification | Pattern-based only | MEDIUM |
| Self-critique | RecoveryPolicy only | ✅ Automatic critique | Single policy | HIGH |
| External audit | None | ✅ Third-party verification | Missing | LOW |
| Regression detection | Test suite only | ✅ Continuous monitoring | Manual only | MEDIUM |

---

## 4. ARCHITECTURAL GAPS

| Gap | Current | Target | Impact |
|-----|---------|--------|--------|
| **Agent Monolith** | Single 2226-line class | Decomposed services | Maintainability, testability, deployment |
| **Synchronous Execution** | Blocking tool calls | Async/parallel execution | Responsiveness, throughput |
| **Single Event Bus** | All events on one bus | Prioritized channels | Performance, debugging |
| **Per-Project Knowledge** | Isolated SQLite DBs | Federated workspace graph | Cross-project insights |
| **No Streaming** | Turn-based request/response | Token/step streaming | UX, long operations |
| **No Plugin System** | Hardcoded tool registry | Dynamic plugin loading | Extensibility |
| **Windows-Centric Desktop** | UIA/COM only | Cross-platform abstraction | Platform lock-in |

---

## 5. DEPENDENCY ORDERING (CRITICAL PATH)

```
FOUNDATION (Do First)
├── 1. Agent Decomposition → PlannerAgent, ExecutorAgent, VerificationAgent
├── 2. Async Execution Framework → asyncio + thread pool for tools
├── 3. CI/CD Pipeline → GitHub Actions (test, lint, type-check)
├── 4. Resource Bounds → CPU/memory/time limits on tools
└── 5. Streaming Infrastructure → Token/step streaming to UI

CORE INTELLIGENCE (After Foundation)
├── 6. Multi-turn Reasoning → Chain-of-thought persistence
├── 7. Decision Framework → Pros/cons, uncertainty, counterfactuals
├── 8. Hierarchical Planning → Strategic + tactical plans
├── 9. Replanning Engine → Full plan revision on failure
└── 10. Self-Critique → Automatic quality assessment

EXPANDED CAPABILITIES (After Core)
├── 11. Web Search + RAG → External knowledge
├── 12. Browser Automation → Playwright integration
├── 13. Cross-platform Desktop → Abstract UI automation layer
├── 14. Vector Memory → Semantic search + consolidation
├── 15. Federated Knowledge Graph → Workspace-wide queries

POLISH & PRODUCTION (Ongoing)
├── 16. Plugin System → Dynamic tool loading
├── 17. Advanced UI → Streaming, rich confirmations, dashboards
├── 18. Observability → Metrics, tracing, alerting
├── 19. Security Hardening → Sandboxing, audit logging
└── 20. Distribution → Installer, auto-update, config management
```

---

## 6. RECOMMENDED NEXT MILESTONE

### MILESTONE: **Agent Decomposition + Async Execution** (M3.0)

**Why this milestone:**
1. **Unblocks everything** — Current monolith prevents parallel work, streaming, and independent scaling
2. **Highest risk reduction** — Single point of failure eliminated
3. **Enables future milestones** — Planner/Executor separation required for hierarchical planning
4. **Measurable** — Clear before/after: response latency, concurrent tool support, test isolation

**Scope (6-8 weeks):**
| Week | Deliverable |
|------|-------------|
| 1-2 | Extract Planner → PlannerAgent (plan creation, validation, dependency resolution) |
| 2-3 | Extract Executor → ExecutorAgent (permission, dispatch, outcome recording) |
| 3-4 | Extract Verification → VerificationAgent (Verifier + Ledger adjudication) |
| 4-5 | Implement AsyncExecutor → ThreadPoolExecutor + asyncio bridge |
| 5-6 | Add Streaming → Token/step callbacks to UI |
| 6-7 | Integration Tests → End-to-end with decomposed agents |
| 7-8 | Performance Baseline → Latency, throughput, resource usage |

**Success Criteria:**
- [ ] Agent class < 500 lines (orchestration only)
- [ ] Tools execute in parallel when dependencies allow
- [ ] UI receives streaming updates (not turn-based)
- [ ] All existing tests pass without modification
- [ ] New unit tests for each decomposed agent >80% coverage

---

## 7. UNRESOLVED QUESTIONS

| Question | Impact | Research Needed |
|----------|--------|-----------------|
| **Sandboxing strategy** — WSL2 vs containers vs restricted Python? | Security architecture | Prototype each; measure overhead |
| **Cross-platform desktop automation** — uiautomator2 (Android), AT-SPI (Linux), AppleScript (macOS)? | Platform parity | Spike implementations |
| **Vector DB choice** — Chroma, Qdrant, SQLite-vec, FAISS? | Memory scaling | Benchmark with Diablo data sizes |
| **Plugin API design** — Python entry points, WASM, subprocess? | Extensibility | Define plugin contract; test with 3 plugins |
| **Streaming protocol** — Server-sent events, WebSocket, gRPC? | UI/UX | Prototype with EdgeCompanionApp |
| **Multi-model routing beyond 8B/14B** — Dynamic model selection from catalog? | Cost/performance | Extend ModelRouter with capability matrix |

---

## 8. ESTIMATED TIMELINE (ROUGH)

| Phase | Duration | Focus |
|-------|----------|-------|
| **M3.0 Foundation** | 6-8 weeks | Agent decomposition, async, CI/CD |
| **M3.1 Core Intelligence** | 8-10 weeks | Reasoning, planning, self-critique |
| **M3.2 Expanded Capabilities** | 10-12 weeks | Web, browser, vector memory, federated KG |
| **M3.3 Production** | 6-8 weeks | Plugins, observability, distribution |
| **Total to "Personal AI Partner"** | ~30-38 weeks | Incremental releases each milestone |

---

## 9. SUCCESS METRICS FOR VISION

| Metric | Current | Target (M3.3) |
|--------|---------|---------------|
| **Task Success Rate** | ~70% (estimated) | >95% |
| **Avg Task Latency** | 30-120s | <10s (parallel) |
| **Concurrent Operations** | 1 | 5+ |
| **Memory Recall Accuracy** | Keyword only | Semantic >90% |
| **Cross-Project Insights** | None | Daily |
| **Self-Correction Rate** | Recovery only | Proactive >50% |
| **Platform Coverage** | Windows only | Win/Linux/macOS |
| **Extensibility** | Code changes | Plugin install |

---

**END OF GAP ANALYSIS AND ROADMAP**