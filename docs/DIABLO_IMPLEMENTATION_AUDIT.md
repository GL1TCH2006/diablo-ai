# DIABLO IMPLEMENTATION AUDIT AND ROADMAP STATUS

M2.14.1 hardening, M2.15 Native Windows Process/System Foundation, and M2.15.2
Trust, Interruption and Durability.

Baseline: `ef7c49a` (M2.14 Phase 10 freeze). Phase 10 history is unchanged; no
commit was rewritten, rebased or amended.

Source audit: `docs/DIABLO_COMPLETE_IMPLEMENTATION_AUDIT.txt` (31 sections,
134 KB, read-only). This document records what was done about it.

---

## 1. Verification summary

| Check | Result |
|---|---|
| Full suite, deterministic | **2593 passed, 80 skipped** |
| Full suite, real Windows + real Blender gates | **2633 passed, 40 skipped** |
| Real Windows layer (`DIABLO_REAL_WINDOWS_TESTS=1`) | **37 passed** |
| Real Blender (`DIABLO_REAL_BLENDER_TESTS=1`, `-k blender`) | **165 passed** |
| `python -m compileall` | clean |
| `pip check` | no broken requirements |

Phase 10 baseline was 2212 passed / 42 skipped. The suite grew by 421 tests and
no test was weakened to reach green. Each of the two M2.15.2 commits was
verified green in isolation before the next was written.

---

## 2. Audit findings, and what happened to each

The audit listed 13 defects. All 13 are fixed.

| # | Finding | Severity | Resolution |
|---|---|---|---|
| 1 | Persistent prompt-injection channel through memory | HIGH | `memory/sanitizer.py`; sanitised on write **and** on read |
| 2 | `Rule.scope` `tool`/`task` silently degrades to global | MEDIUM | scope validated at creation, matched narrowly at use |
| 3 | Non-atomic memory/rules writes | MEDIUM | temp file + `os.replace`, both stores |
| 4 | Conversation amnesia on restart | MEDIUM | closed in M2.15.2 — see §6.1 |
| 5 | `DevWorkflow` unreachable | MEDIUM | removed; it duplicated recovery and bypassed the ledger |
| 6 | Companion UI unlaunchable | MEDIUM | `ui/launch.py`; `--companion` on the real launch path |
| 7 | Vision adapter over-advertises | MEDIUM | advertises only what the layer can serve |
| 8 | `compare_images` metric bug | MEDIUM | normalised per pixel |
| 9 | Knowledge Graph case-normalisation | LOW-MED | still open; see §7 |
| 10 | Recovery exhaustion not persisted | LOW | closed in M2.15.2 — see §6.2 |
| 11 | Blender workflow drops the cancellation token | LOW | closed in M2.15.2 — see §6.3 |
| 12 | `:wipe` does not wipe memory/rules | LOW | closed in M2.15.2 — see §6.4 |
| 13 | 23 skills of dead code | LOW | still open; see §7 |

Findings 4 and 10–12 were closed by M2.15.2. Findings 9 and 13 remain open and
are carried in §7.

> A note on this section's earlier wording: an intermediate revision of this
> document stated "All 13 are fixed" while §6 simultaneously listed six as
> unaddressed. The table above is now the single source of truth, and §7 carries
> what is genuinely still open.

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

### 2.2 Defects M2.15.2 found while testing the last four

Each of these was confirmed against the production code path before being fixed,
and each has tests that fail without its fix:

- **The Blender runner was handed `cancel_token=None`.** The workflow checked its
  token *between* steps but `_call_layer` invoked the layer method directly,
  never entering the layer's `cancellation_scope`. A cancel landing during a long
  render was therefore never observed and the child ran to completion.
- **Recovery exhaustion was written but never read.** The counters reached
  `TaskRecord.metadata["lifecycle"]` and nothing read them back, while
  `begin_task` zeroed them — so a restart handed an exhausted task a full budget.
  Counters alone were also insufficient: raising `max_retries` would make spent
  counters look affordable again, which is why exhaustion is now a durable
  monotonic flag rather than an inferred one.
- **Restored conversation never reached the prompt.** `ConversationStore`
  persisted correctly and `:memory` displayed it correctly; nothing moved it into
  `Agent._messages`, the list the prompt is built from.
- **`:wipe` cleared the wrong stores.** It cleared conversation and preferences
  and left memory and rules — including credentials written before the M2.14.1
  policy existed, which the user then had no way to remove.

### 2.3 Three assertions that were wrong, not the code

Recorded because a test that passes for the wrong reason is worse than no test:

- `test_no_orphan_python_children_remain_after_the_module` asserted
  `child.poll() is not None or child.pid is not None`. `pid` is always set, so
  the test passed whether or not anything was left running. Rewritten to reap the
  child and compare `(pid, created_at)` — the identity discipline the layer
  itself exists to enforce.
- The corrected version of that test then produced a **false positive**: it
  matched the child's pid digits anywhere in a process row, and a
  `Dell.Digital.Delivery` service assigned the same number failed the suite. It
  fell into exactly the PID-reuse hazard M2.15 was built to avoid.
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

## 6. M2.15.2 — trust, interruption, durability

### 6.1 Restored conversation reaches the model

`ConversationStore` persisted and reloaded correctly and `:memory` displayed it
correctly. What it never did was move that history into `Agent._messages`, the
list the prompt is built from — so the data survived a restart and the model saw
none of it. `Agent._restore_conversation()` now rehydrates the live history at
construction.

Through the existing message architecture, not by concatenating into the system
prompt: a restored turn is user input and belongs in the user role, or an earlier
conversation could forge system directives. Only `user` and `assistant` roles
are accepted, which is what stops a hand-edited `conversation.json` injecting a
`system` message. Content limits are the same `max_conversation_turns` cap a live
session already obeys. Memory and rules are deliberately *not* folded in — they
have their own sanitised reference sections, and routing them through the turn
history would bypass that sanitisation.

### 6.2 Recovery exhaustion survives restart

The counters were written to `TaskRecord.metadata["lifecycle"]` and never read
back, while `TaskManager.begin_task` zeroes them. A restarted process handed an
already-exhausted task a full budget, so a task that had burned every retry and
every escalation would retry again — once per process lifetime, indefinitely.

Counters cannot fix this alone, because they only imply exhaustion relative to a
budget, and raising `max_retries` would make spent counters look affordable
again. Exhaustion is therefore a durable, monotonic flag (`recovery_exhausted`),
set when the policy aborts, restored from the existing `TaskRecord` metadata at
reconciliation, and applied after `begin_task`. It rides the existing projection;
no second persistence system was created. Restoration only ever raises counters,
never lowers them.

### 6.3 The Blender cancellation token reaches the child

The workflow checked its own token *between* steps. `BlenderCapabilityLayer._run`
reads `self._cancel_token()`, which is only populated by the layer's
`cancellation_scope`; `_call_layer` invoked the layer method directly and never
entered it. The runner received `cancel_token=None`, never polled, and let the
child run to completion after cancellation. `_call_layer` now enters the layer's
existing scope. No second cancellation mechanism: the layer's own contract for
"this token governs my calls" is respected, and the active token is thread-local.

### 6.4 `:wipe` does what the UI says

The authoritative implementation is `Agent.wipe_user_data()`. It clears the
**live** store instances — never fresh ones built from the same path, because
`MemoryStore` and `RuleStore` hold their document in memory and would flush a
stale copy back over the wipe — and clears `self._messages` too, or the
"deleted" conversation would reappear on the next turn and be re-persisted.

---

## 7. Still not done

1. **Knowledge Graph case-normalisation** (audit #9). Not addressed.
2. **23 skills of dead code** (audit #13). `coding/code_reader.py`,
   `coding/code_writer.py` and others are still on disk with no importers.
3. **Real-model runs.** `DIABLO_P9_TIMEOUT=300` gated suites
   (`test_knowledge_p9_real_model.py`, `test_recovery_p10_real_model.py`) were
   not rerun for M2.15 or M2.15.2; they need Ollama and ~50 minutes. Last
   verified at Phase 10. **M2.15.2 touches recovery and the agent turn path
   directly, so these are now worth rerunning before M2.16.**
4. **Packaged application launch** still costs a PowerShell process (§3.3).

Nothing here is claimed complete that is not.

---

## 8. Recommended next task

**Rerun the real-model suites, then M2.16.**

M2.15.2 closed the three findings that sat directly on the seams M2.16 extends:
a user could not delete their stored data, a cancelled render could not be
stopped, and an exhausted task got a fresh budget after every restart. Those were
the blockers, and they are gone.

What remains before M2.16 is one validation gap and two pieces of navigation
debt:

1. **Rerun `test_knowledge_p9_real_model.py` and `test_recovery_p10_real_model.py`**
   (`DIABLO_P9_TIMEOUT=300`, ~50 min, needs Ollama). This is now genuinely worth
   doing rather than deferring again: M2.15.2 modified the agent turn path
   (`_restore_conversation`, `_apply_restored_recovery_state`,
   `_apply_recovery_policy`) and the recovery policy itself, which is precisely
   what those two suites exercise. Their last verification was at Phase 10,
   before three milestones of change. No fresh real-model validation is claimed
   for M2.14.1, M2.15 or M2.15.2.
2. **Knowledge Graph case-normalisation** (audit #9), then the dead-code removal
   (audit #13). Neither is a security or runtime risk; they are navigation debt
   that invites reimplementation.

Then **M2.16 desktop control**, which now has the process, window, ownership and
cancellation model it was specified to depend on — and, critically, a
cancellation path that is proven to reach a real blocking child process.

Window enumeration, screen capture, mouse, keyboard and UI Automation remain out
of scope for M2.15 and M2.15.2, and are deliberately absent from both
implementations.