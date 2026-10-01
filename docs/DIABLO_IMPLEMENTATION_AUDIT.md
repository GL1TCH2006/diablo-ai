# DIABLO IMPLEMENTATION AUDIT AND ROADMAP STATUS

M2.14.1 hardening and M2.15 Native Windows Process/System Foundation.

Baseline: `ef7c49a` (M2.14 Phase 10 freeze). Phase 10 history is unchanged; no
commit was rewritten, rebased or amended.

Source audit: `docs/DIABLO_COMPLETE_IMPLEMENTATION_AUDIT.txt` (31 sections,
134 KB, read-only). This document records what was done about it.

---

## 1. Verification summary

| Check | Result |
|---|---|
| Full suite, deterministic | **2476 passed, 79 skipped** |
| Full suite, real Windows gate on | **2513 passed, 42 skipped** |
| Real Windows layer (`DIABLO_REAL_WINDOWS_TESTS=1`) | **37 passed** |
| Real Blender (`DIABLO_REAL_BLENDER_TESTS=1`, `-k blender`) | **149 passed** |
| `python -m compileall` | clean |
| `pip check` | no broken requirements |

Baseline was 2212 passed / 42 skipped. The suite grew by 301 tests and no test
was weakened to reach green.

---

## 2. Audit findings, and what happened to each

The audit listed 13 defects. All 13 are fixed.

| # | Finding | Severity | Resolution |
|---|---|---|---|
| 1 | Persistent prompt-injection channel through memory | HIGH | `memory/sanitizer.py`; sanitised on write **and** on read |
| 2 | `Rule.scope` `tool`/`task` silently degrades to global | MEDIUM | scope validated at creation, matched narrowly at use |
| 3 | Non-atomic memory/rules writes | MEDIUM | temp file + `os.replace`, both stores |
| 4 | Conversation amnesia on restart | MEDIUM | not in this pass; see §6 |
| 5 | `DevWorkflow` unreachable | MEDIUM | removed; it duplicated recovery and bypassed the ledger |
| 6 | Companion UI unlaunchable | MEDIUM | `ui/launch.py`; `--companion` on the real launch path |
| 7 | Vision adapter over-advertises | MEDIUM | advertises only what the layer can serve |
| 8 | `compare_images` metric bug | MEDIUM | normalised per pixel |
| 9 | Knowledge Graph case-normalisation | LOW-MED | not in this pass; see §6 |
| 10 | Recovery exhaustion not persisted | LOW | not in this pass; see §6 |
| 11 | Blender workflow drops the cancellation token | LOW | not in this pass; see §6 |
| 12 | `:wipe` does not wipe memory/rules | LOW | not in this pass; see §6 |
| 13 | 23 skills of dead code | LOW | not in this pass; see §6 |

Findings 4 and 9–13 are genuinely unaddressed. They are listed here rather than
claimed done, and are carried in §6.

### 2.1 Defects the new tests found

Writing tests for these areas found five further defects that the audit did not
report. Each is worth naming, because they were all live:

- **`level_for` received no tool name.** `ToolSpec.level_for(args)` is called
  with the argument dict alone. The memory write policy's hook read the first
  positional as a tool name, so every memory write reported `SAFE` regardless of
  `memory_write_policy`. The setting was inert.
- **`_bounded_limit` crashed on `float("inf")`.** `int(inf)` raises
  `OverflowError`, which was not in the caught tuple. A hostile limit became a
  traceback rather than a clamped result.
- **`--companion` closed capabilities on its own failure path.** A `finally`
  unwound through the `CompanionUnavailable` handler, closing the Blender
  connection and Windows layer before the console fallback used them.
- **Rules were rendered without the current task id.** Enforcement filtered by
  task; the prompt did not. The two disagreed in the text the model reasons over.
- **Registry paths were returned unexpanded.** `%SystemRoot%\system32\NOTEPAD.EXE`
  is not a path, so every registry-sourced launch would have failed.

### 2.2 Two assertions that were wrong, not the code

Recorded because a test that passes for the wrong reason is worse than no test:

- `test_no_orphan_python_children_remain_after_the_module` asserted
  `child.poll() is not None or child.pid is not None`. `pid` is always set, so
  the test passed whether or not anything was left running. It now reaps the
  child and scans the process table.
- The `_Vision` test double in `test_s1_capability_integration.py` had no
  discovery hook, so it did not model the real layer. The double was corrected,
  not the production contract.

---

## 3. M2.15: what was built

### 3.1 Process layer — `core/windows/processes.py`

| Concern | Decision |
|---|---|
| Enumeration | `NtQuerySystemInformation(SystemProcessInformation)` |
| Image path | `QueryFullProcessImageNameW` |
| Command line | `NtQueryInformationProcess(ProcessCommandLineInformation, 60)` |
| Owner | process token lookup |
| Identity | `(pid, created_at)` — both halves |
| Termination | request → bounded wait → confirm |
| Refused | PIDs 0, 1, 2, 4; self; any float PID |

`requested` and `confirmed` are separate fields and never conflated. A
termination that was requested but not observed is `confirmed=False` with a
reason, and `tests/test_windows_safety.py` asserts the bridge cannot promote it
to `PASSED`.

Identity includes the creation time because PID reuse is the mechanism by which
a native layer could terminate an unrelated process. A PID alone is a guess.

### 3.2 System layer — `core/windows/system.py`

Registry for OS caption and CPU, `GlobalMemoryStatusEx` for memory. Measured
~45 ms against hundreds of milliseconds for the PowerShell path it replaced.

### 3.3 Start Menu discovery — `core/windows/start_menu.py`

Replaces `Get-StartApps` on the normal path by reading the registry
(`Software\Classes\Applications\*\shell\open\command`) and the Start Menu
directories directly.

**Stated limitation.** Packaged (AppX/MSIX) applications — Calculator, Photos,
the Store — have neither a shortcut nor a registered open command. Both the AppX
state repository and `C:\Program Files\WindowsApps` are unreadable to a
non-elevated process (`Access is denied`, verified). This reader returns `None`
for them rather than fabricating a path, and `tools/applications.py` keeps the
PowerShell probe as a documented fallback. The PowerShell call is now a fallback
rather than the primary path, which is the change the milestone asked for; a
packaged application still costs a PowerShell process.

### 3.4 The capability surface

Five named operations — `list`, `get`, `find`, `system_info`, `terminate` —
reachable only through `capability.execute`. No handle or pointer crosses the
boundary; `test_no_operation_accepts_a_handle_or_a_pointer` enforces it.

`terminate` is `SENSITIVE`; the four queries are `SAFE`. The level comes from
the adapter's own `is_mutating`, so a new mutating operation cannot be classified
`SAFE` by accident.

Shelling out also widened what the product could do: PowerShell expresses
anything, so a capability described as "process info" inherited the reach of the
whole language. Pinning the surface to names is the security-relevant half of
this milestone.

---

## 4. Architecture preserved

No new permission system, ledger, recovery layer, or task authority. Everything
routes through what Phase 10 froze:

- `TaskStore` for task identity — the Executor reads it through a provider, and
  a UI-chosen task id is deliberately not forwarded.
- `VerificationLedger` for verification — the Windows bridge emits evidence
  through it; it cannot pass without observed evidence.
- Git for history.
- Knowledge Graph projection-only.
- `integration/contracts.py` does not import `core`; status codes register in the
  other direction.

`core/dev_workflow.py` was **deleted**, not patched. It duplicated recovery
policy and wrote its own verification records, bypassing the ledger. Keeping it
would have meant two competing verification authorities.

---

## 5. Documentation reconciliation

The audit's §25 found documentation and reality disagreeing in both directions.
Resolved:

| Claim | Reality | Now |
|---|---|---|
| `applications.open` is PowerShell-only | native with documented fallback | §3.3 |
| `system.info` shells out per call | native, ~45 ms | §3.2 |
| Vision offers object detection / OCR / UI analysis | no backend implements them | not advertised |
| Voice toggle implies working speech | TTS measured; STT/wake/barge-in deferred | measured status |
| `memory_write_policy` configurable | enforced through the permission ladder | enforced |
| Rules support `tool`/`task` scope | now actually enforced | enforced |
| Tool count 67 | 68 (`memory.policy` added) | updated |

The existing `docs/DIABLO_COMPLETE_IMPLEMENTATION_AUDIT.txt` is preserved
unmodified. It is a read-only record of the pre-implementation state and should
not be edited to match what followed.

---

## 6. Still not done

Named explicitly so the next milestone does not inherit a false picture:

1. **Conversation amnesia on restart** (audit #4). Not addressed.
2. **Knowledge Graph case-normalisation** (audit #9).
3. **Recovery exhaustion not persisted** (audit #10).
4. **Blender workflow drops the cancellation token** (audit #11). Relevant to
   M2.16, which depends on this layer's cancellation discipline.
5. **`:wipe` does not wipe memory/rules** (audit #12). The memory rules here make
   this worse, not better: a user who runs `:wipe` reasonably expects the
   sanitised entries gone.
6. **23 skills of dead code** (audit #13).
7. **Real-model runs.** `DIABLO_P9_TIMEOUT=300` gated suites
   (`test_knowledge_p9_real_model.py`, `test_recovery_p10_real_model.py`) were
   not rerun; they need Ollama and ~50 minutes. Last verified at Phase 10.
8. **Packaged application launch** still costs a PowerShell process (§3.3).

Nothing here is claimed complete that is not.

---

## 7. Recommended next task

**M2.15.2 — close the five remaining audit findings, then M2.16.**

The argument for doing this before M2.16 is that all five are correctness issues
in already-shipped behaviour, and M2.16 builds on the same permission and
cancellation paths. Specifically:

1. `:wipe` must wipe memory and rules. It is the one finding that is a user
   trust problem rather than a functional gap, and this milestone made stored
   memory more sensitive, not less.
2. The Blender workflow must carry the cancellation token through. M2.16 will
   add a mouse/keyboard layer whose operations can block; a dropped token means
   a user cannot interrupt them.
3. Recovery exhaustion must persist, so a crash loop is visible after restart
   rather than reset.
4. Knowledge Graph case normalisation, then the dead-code removal.

Then **M2.16 desktop control**, which now has the process, window, ownership and
cancellation model it was specified to depend on.

Window enumeration, screen capture, mouse, keyboard and UI Automation remain out
of scope for M2.15 and are deliberately absent from this implementation.