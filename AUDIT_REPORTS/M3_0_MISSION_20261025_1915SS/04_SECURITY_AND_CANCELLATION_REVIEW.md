# SECURITY AND CANCELLATION REVIEW

**Mission:** M3.0 Engineering Mission  
**Date:** 2026-10-25

---

## TRUST BOUNDARIES ANALYSIS

### Current Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│                      TRUSTED ZONE                               │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐              │
│  │  Agent      │ │  Permissions│ │  Verifier   │              │
│  │  Orchestrator│ │  Manager    │ │  + Ledger   │              │
│  └─────────────┘ └─────────────┘ └─────────────┘              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ AUTHORIZED CALLS
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      TOOL LAYER                                 │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐              │
│  │ Terminal    │ │  Python     │ │  Git/GH     │              │
│  │  (blocked   │ │  (timeout,  │ │  (policy    │              │
│  │   commands) │ │   cancel)   │ │   gated)    │              │
│  └─────────────┘ └─────────────┘ └─────────────┘              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ SUBPROCESS EXECUTION
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      UNTRUSTED ZONE                             │
│  - PowerShell processes                                         │
│  - Python subprocesses                                          │
│  - Blender processes                                            │
│  - External CLIs (git, gh)                                      │
└─────────────────────────────────────────────────────────────────┘
```

### Tool Output → Model Context Flow

```
Tool Execution → ToolResult → Executor._record_execution() 
  → Agent._note_step() → ChangeTracker → Agent._messages 
  → Prompt Construction → Model Input
```

**Current Gap:** Tool outputs (stdout, stderr, file contents) enter model context WITHOUT sanitization between tool execution and prompt injection.

---

## IDENTIFIED SECURITY GAPS

### GAP-1: Prompt Injection via Tool Outputs
**Severity:** MEDIUM  
**Location:** `core/agent.py` → `_reference_sections()` → prompt construction  
**Description:** Tool outputs (terminal stdout/stderr, file reads, git diffs, web content) are directly concatenated into model context without sanitization for control tokens, role labels, or injection phrases.  
**Evidence:** `memory/sanitizer.py` exists but only applied to memory writes/reads, not tool outputs.  
**Impact:** Malicious command output could inject fake role labels, override instructions, or exfiltrate prompt.

### GAP-2: No Sandbox for Code/Shell Execution
**Severity:** MEDIUM  
**Location:** `tools/terminal.py`, `tools/python.py`  
**Description:** `terminal.execute` runs PowerShell with user privileges; `python.run_script` runs Python with Diablo's privileges. No OS-level isolation (containers, WSL2, restricted Python).  
**Mitigations Present:** Blocklist, auto-allow patterns, permission ladder, working-dir restrictions.  
**Gap:** No OS-level containment; compromised model output → arbitrary code execution as user.

### GAP-3: Heuristic Secret Detection
**Severity:** LOW  
**Location:** `core/logging_setup.py` → `redact()`  
**Description:** Keyword-based secret detection (password, token, api_key, secret, etc.)  
**Gap:** False negatives (custom field names); false positives (over-redaction). No regex patterns for common secret formats (AWS keys, JWTs, etc. in sanitizer but not in logger).

### GAP-4: Path Traversal via Symlinks
**Severity:** LOW  
**Location:** `config/permissions.py` → `path_is_within()`  
**Description:** Uses `Path.resolve()` which follows symlinks. A symlink inside allowed_root pointing outside could bypass containment.  
**Mitigation:** `Path.resolve()` does follow symlinks but target is checked against allowed_roots.

### GAP-5: Git Operations Without Signature Verification
**Severity:** LOW  
**Location:** `tools/git.py`  
**Description:** `git.push`, `git.commit`, `git.restore` have policies but no GPG/signature verification.  
**Gap:** Unsigned commits could be pushed if policy changed.

---

## CANCELLATION SYSTEM REVIEW

### Current Cancellation Flow

```
User: "STOP" / UI Stop Button / Voice STOP
    │
    ▼
VoiceService/EdgeCompanionApp/TerminalUI
    │
    ▼
InterruptionManager.request_cancel(task_id)
    │
    ├─▶ CancellationToken.request() → sets event
    │
    ├─▶ Agent.cancel_current_task() → token.cancel()
    │
    └─▶ CoreEventBusBridge.canceler(task_id)
            │
            ▼
       Executor.run() checks cancellation_requested() at 4 points:
       1. Pre-permission
       2. Post-permission/pre-confirmation
       3. Pre-execute (before dispatch)
       4. Post-execution
            │
            ▼
       Terminal/Python tools check cancellation_token in their loops
            │
            ▼
       _stop_process() → taskkill /T /PID (Windows) → terminate tree
```

### Cancellation Coverage

| Component | Cancellation Support | Notes |
|-----------|---------------------|-------|
| **Agent.respond()** | ✅ Via `_active_cancellation_token` | Checks in recovery loop |
| **Executor.run()** | ✅ 4 checkpoints | Pre/post permission, pre/post execute |
| **Terminal tool** | ✅ Full | Pre-launch, during execution, process tree kill |
| **Python tool** | ✅ Full | Pre-launch, during execution, process kill |
| **Blender tool** | ⚠️ Partial | Capability layer; needs verification |
| **Git tools** | ❌ No | Long-running git ops not cancellable |
| **GitHub tools** | ❌ No | HTTP calls not cancellable |
| **File ops** | ⚠️ Partial | Fast ops; no mid-op cancellation |
| **Recovery loop** | ✅ Yes | Checks token in loop |

### Cancellation Gaps

1. **Git operations not cancellable** - Long clones/fetches cannot be interrupted
2. **GitHub API calls not cancellable** - HTTP requests to gh API ignore cancellation
3. **File operations not mid-op cancellable** - Large file reads/writes continue
4. **Blender capability** - Needs verification of cancellation token propagation
5. **Knowledge indexing** - Long-running indexing not cancellable

---

## PERMISSION SYSTEM REVIEW

### Current Permission Ladder

| Level | Tools | Auto-Allow | Confirmation Required |
|-------|-------|------------|----------------------|
| **SAFE** | filesystem.read_file, list_dir, system.info, git.status/diff/log, project.info/files/grep, memory.search/list, models.status, gh.* (read) | Yes (config) | No |
| **NORMAL** | filesystem.write_file (inside roots), python.run_script (inside roots), git.add/commit/pull/branch, applications.open (whitelist), project.verify, code.patch_file, chart.generate, rules.create | Configurable | Conditional |
| **SENSITIVE** | filesystem.write_file (outside), filesystem.delete_file, terminal.execute, python.run_script (outside), git.push/restore/reset/rebase, gh.* (write), rules.create (policy) | Never | Always |

### Session Blocks
- User can "block" a tool for session → added to session block list
- Persists until restart or explicit unblock
- Checked in `PermissionManager.evaluate()`

### Rule Engine Integration
- Rules scoped: global, project, task, tool
- `RuleEngine` evaluates conditions against context
- `PermissionManager.rule_engine` reference set by Agent
- Task-scoped rules matched via `Executor._task_provider()`

### Permission Gaps

1. **No temporary elevation** - Cannot grant one-time SENSITIVE without confirmation
2. **No audit trail for permission changes** - Session blocks not logged to task record
3. **No time-bounded blocks** - Blocks persist indefinitely
4. **Auto-allow patterns are static** - Config-only, no learning

---

## SUBPROCESS HANDLING REVIEW

### Terminal Tool (`tools/terminal.py`)

**Strengths:**
- Uses `taskkill /T /PID` for Windows process tree termination
- Bounded grace periods: `TERMINATE_GRACE_S=5s`, `KILL_GRACE_S=5s`
- Output bounded: `MAX_STREAM_CHARS=64KB`
- Cancellation checked at poll interval (50ms)
- Proper stream draining and cleanup

**Concerns:**
- `CREATE_NEW_PROCESS_GROUP` used but `CTRL_BREAK_EVENT` not sent (uses taskkill instead)
- No stdin piping (cannot send input to interactive commands)
- Working directory not validated against allowed_roots (uses scratch_dir default)

### Python Tool (`tools/python.py`)

**Strengths:**
- Similar bounded termination pattern
- Cancellation checked at poll interval
- Output bounded
- Script path validated (must exist, be file)

**Concerns:**
- Runs with full Python privileges (no restricted execution)
- Working directory = script parent (could escape if script in allowed_root but writes outside)
- No resource limits (CPU, memory)

### Blender Capability

**Status:** Not fully reviewed in this mission  
**Known:** Uses `blender/runner.py` with subprocess; cancellation token passed via `_tool_context`  
**Needed:** Verify process tree termination on Windows

---

## RECOMMENDED SECURITY IMPROVEMENTS

### Priority 1 (Do Next)
| Improvement | Effort | Impact |
|-------------|--------|--------|
| Sanitize tool outputs before prompt injection | Medium | High |
| Add OS-level sandbox (WSL2/container) for terminal/python | High | High |
| Add regex-based secret detection to logger | Low | Medium |

### Priority 2 (Next Mission)
| Improvement | Effort | Impact |
|-------------|--------|--------|
| Add cancellation to Git/GitHub tools | Medium | Medium |
| Add symmetric key encryption for sensitive memory | Medium | Low |
| Add audit logging for permission changes | Low | Low |
| Symlink-safe path resolution | Low | Low |

### Priority 3 (Future)
| Improvement | Effort | Impact |
|-------------|--------|--------|
| GPG signature verification for Git | Medium | Low |
| Capability-based sandbox (seccomp/capsicum) | High | High |
| Formal verification of permission logic | High | Low |

---

## CANCELLATION VERIFICATION CHECKLIST

| Scenario | Tested? | Result |
|----------|---------|--------|
| Voice STOP → Agent cancel → Terminal tool cancel | ✅ | Pass |
| Voice STOP → Agent cancel → Python tool cancel | ✅ | Pass |
| UI Stop button → Agent cancel → Tool cancel | ✅ | Pass |
| Cancellation during permission confirmation | ✅ | Pass |
| Cancellation during tool execution | ✅ | Pass |
| Cancellation after tool completion | ✅ | Pass |
| Multiple rapid cancellations | ⚠️ | Not tested |
| Cancellation during recovery loop | ✅ | Pass |
| Cancellation with nested tools | ⚠️ | Not tested |
| Cancellation propagation to Blender | ❓ | Not verified |

---

## VERDICT

**Cancellation System:** **ROBUST** - Well-designed, tested, propagates correctly to terminal/python tools. Gaps in git/GitHub/Blender tools identified.

**Permission System:** **SOLID** - Three-level ladder, session blocks, rule engine integration. No critical gaps.

**Security Posture:** **ACCEPTABLE WITH GAPS** - Defense in depth present (blocklist, auto-allow, permissions, working-dir). Critical gap: no OS-level sandbox; prompt injection via tool outputs not mitigated.

**Recommended Next Action:** Implement tool output sanitization before prompt injection (builds on existing `memory/sanitizer.py` infrastructure).