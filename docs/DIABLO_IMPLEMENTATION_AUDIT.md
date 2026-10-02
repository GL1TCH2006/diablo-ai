# DIABLO IMPLEMENTATION AUDIT AND ROADMAP STATUS

M2.14.1 hardening, M2.15 Native Windows Process/System Foundation, M2.15.2
Trust, Interruption and Durability, and M2.16 Desktop Control — M2.16.1 window
identity, M2.16.2 pointer and mouse, M2.16.3 keyboard, M2.16.4 screen and display
foundation.

Baseline: `ef7c49a` (M2.14 Phase 10 freeze). Phase 10 history is unchanged; no
commit was rewritten, rebased or amended.

Source audit: `docs/DIABLO_COMPLETE_IMPLEMENTATION_AUDIT.txt` (31 sections,
134 KB, read-only). This document records what was done about it.

---

## 1. Verification summary

### M2.16.6

| Check | Result |
|---|---|
| Full suite, deterministic | **3163 passed, 197 skipped** |
| Full suite, real-Windows gates (`DIABLO_REAL_WINDOWS_TESTS=1`) | **3316 passed, 44 skipped** |
| M2.16.6 deterministic module (`tests/test_m2166_desktop_workflow.py`) | **83 passed** |
| M2.16.6 real-Windows module (`tests/test_m2166_desktop_workflow_real.py`) | **22 passed** |
| `python -m compileall` | clean |
| `pip check` | no broken requirements |
| Leaks from the M2.16.6 tests (processes / windows / apps) | 0 / 0 / 0 |

The M2.16.5 baseline was 3080 passed / 175 skipped deterministic and 3211 passed /
44 skipped real. **REAL-MODEL VALIDATION DEFERRED** — the Ollama-gated suites were
not rerun, so no claim is made that a model selects the new tools (§10). Details,
including three findings that presented as workflow bugs and were not, are in §7c.

### M2.16.5

| Check | Result |
|---|---|
| Full suite, deterministic | **3080 passed, 175 skipped** |
| Full suite, real-Windows gates (`DIABLO_REAL_WINDOWS_TESTS=1`) | **3211 passed, 44 skipped** |
| M2.16.5 deterministic module (`tests/test_windows_uia.py`) | **56 passed** |
| M2.16.5 real-Windows module (`tests/test_windows_uia_real.py`) | **28 passed** |
| `python -m compileall` | clean |
| `pip check` | no broken requirements |
| Process / window / GDI leaks from the new tests | none |

The M2.16.4 baseline was 2996 passed / 175 skipped deterministic and 3127 passed /
44 skipped real. M2.16.5 adds 84 tests and no test was weakened to reach green.
Details, including the two findings that presented as UIA provider bugs and were
not, are in §7b.

### M2.16.2 / M2.16.3 / M2.16.4

| Check | Result |
|---|---|
| Full suite, deterministic | **2996 passed, 175 skipped** |
| Full suite, real-Windows gates (`DIABLO_REAL_WINDOWS_TESTS=1`) | **3127 passed, 44 skipped** |
| Real display/capture module (`tests/test_m2164_display_real.py`) | **54 passed, 1 skipped**, three consecutive runs |
| M2.16.4 deterministic module | **100 passed** |
| Real input module, `tests/test_m2162_input_real.py` | **30 passed**, three consecutive runs |
| Real window + input modules together | **56 passed** |
| `python -m compileall` | clean |
| `pip check` | no broken requirements |

M2.16.2/.3 baseline was 2895 passed / 120 skipped deterministic. The suite grew by
101 tests and no test was weakened to reach green. No Blender gate was rerun:
M2.16 touches neither the Blender scene path nor the shared executor, so a rerun
would have measured nothing new.

The one environment-dependent skip is recorded in §7.9: this session's screen
surface does not present window content, so the pixel assertion against a
test-owned window is skipped with that reason rather than weakened.

### Earlier milestones

| Check | Result |
|---|---|
| M2.15.2 full suite, deterministic | **2593 passed, 80 skipped** |
| M2.15.2 full suite, real Windows + real Blender gates | **2633 passed, 40 skipped** |
| Real Windows layer (`DIABLO_REAL_WINDOWS_TESTS=1`) | **37 passed** |
| Real Blender (`DIABLO_REAL_BLENDER_TESTS=1`, `-k blender`) | **165 passed** |

Phase 10 baseline was 2212 passed / 42 skipped. Each commit was verified green in
isolation before the next was written.

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

## 7. M2.16 Desktop Control

### 7.1 M2.16.1 — window identity

The foundation the rest of M2.16 stands on: a window is addressed as
`(hwnd, process (pid, created_at), class, instance)` rather than by handle. HWND
alone is not an identity, because Windows recycles handles and a stale one names a
different window that happens to sit at the same address.

Activation failure is reported, never faked. Windows refuses
`SetForegroundWindow` from a process that does not own the foreground, and that
refusal is returned as "requested but not confirmed" rather than as success.

### 7.2 M2.16.2 — `core/windows/coordinates.py`

**A coordinate is not a target.** `click(x, y)` names a point on a screen, not a
thing: by the time input reaches Windows the window may have moved, closed, or
lost foreground, and the point may now lie over a different application. A caller
that supplied bare coordinates has no way to notice any of that.

So every coordinate carries the target it was meant for:

- `ScreenPoint` — absolute screen coordinates, explicitly *unanchored*. Accepted
  and inspectable, and refused for anything that changes the machine.
- `WindowPoint` — window-relative coordinates plus a `WindowIdentity`. Resolution
  re-reads the window and re-verifies the identity, so a stale rect cannot be
  applied to a recycled handle.
- `ResolvedPoint` — the result, carrying the provenance of the window it was
  computed from.

`mode` is declared explicitly and never inferred. Inferring it from which keys
happen to be present would let a caller smuggle a window identity in and have it
silently treated as absolute coordinates — discarding the anchoring exactly when
it was most wanted.

Bounds are half-open. `SM_CXVIRTUALSCREEN` is a width, so the last addressable
pixel is `left + width - 1`; `GetWindowRect` reports a right/bottom one past the
last covered pixel. Accepting `left + width` would admit a coordinate Windows
clamps back to the edge, so a caller would believe it had aimed at a position that
does not exist.

### 7.3 M2.16.2 / 7.4 M2.16.3 — `core/windows/input.py`

The only module that talks to Windows input. `SendInput` rather than
`mouse_event`/`keybd_event`: those are the 16-bit-era calls, they truncate
coordinates, and they cannot express Unicode. `KEYEVENTF_UNICODE` makes typed text
independent of the active keyboard layout.

**Every verb requires a target.** The keyboard verbs take a `point` even though no
coordinate is involved, because keystrokes go to whichever window holds
foreground, and without naming a target there is no way to notice that foreground
belongs to a different application than the caller meant.

**The target is re-read immediately before dispatch.** Resolution happens earlier;
the gap between resolving and dispatching is exactly where a window closes, gets
recycled, or loses foreground. Re-reading shrinks the race to the microseconds
inside `SendInput`, which is as close as a caller without an atomic kernel
primitive can get.

**A short `SendInput` return is an error, not a count.** A user-installed
`LLMHF_INJECT` block makes the call return zero, and a *partial* return means some
events of a modifier chord never went in — leaving the modifier down. Both are
raised from `_send_input` itself rather than left for each caller to check,
because the one that forgot would report input as delivered when it never reached
Windows.

**`release_key` consults neither the target nor the cancellation token.** This is
the one deliberate exception. A cancelled or half-finished press is exactly when a
key needs releasing, and refusing because the target changed or the request was
torn up would strand Ctrl down for the rest of the session. Every other verb is
cancelled at preflight and again before dispatch.

**Clicks and keystrokes are never verified.** Their effect lives inside an
application this code cannot observe. `dispatched` says Windows accepted the
events; it says nothing about whether a menu opened or a field changed. Only
pointer movement is verifiable, because the pointer is global state that can be
read back — within a 1px tolerance, since Windows clamps to the desktop edge.

### 7.5 Operations added

Twenty-two total through M2.16.3; M2.16.4 brings it to **twenty-eight, fifteen
mutating**. Pointer and keyboard verbs are all declared mutating, so all nine
reach the bridge already escalated to SENSITIVE; the reads are not.

A host without the window APIs does not advertise the input verbs. Each one
resolves and re-verifies a target through `WindowLayer`, so offering them where
that is impossible would advertise operations that can only refuse. `get_cursor_pos`
is the exception: it reads the pointer and the virtual screen, and needs no window
APIs. The M2.16.4 display operations are withheld on a host that cannot enumerate
displays, for the same reason.

### 7.6 Real-window testing

`tests/test_m2162_input_real.py` drives real `SendInput` at a Tk window the test
owns, with a single `Entry` and a click counter. Typed text and click counts are
asserted by reading the `Entry` back through Tk — the only way to observe a
keystroke's effect from outside, and the concrete demonstration of why clicks and
keys cannot be verified.

Two things about that file are worth stating plainly:

- **Every target is test-owned.** The foreground-race test uses a *second probe
  this module started*, not an unrelated application. Borrowing a user's window
  would mean changing something they are actually using.
- **`AttachThreadInput` is harness-only.** Windows refuses activation from a
  process that does not own the foreground, which a test harness never does, so
  the fixture uses the documented technique to raise its own window. The *product*
  does not: `focus_window` still honours Windows' refusal, which is M2.16.1
  behaviour with tests asserting it, and `test_no_native_input_api_is_referenced_outside_the_input_layer`
  keeps `SendInput` confined to the one module.

Teardown terminates the window's *owning* process as well as the direct child,
because a venv `python.exe` re-executes and the window belongs to a grandchild —
terminating only the direct child leaves it alive. Verified: zero probe windows
and no probe processes survive a run.

### 7.7 M2.16.4 — display topology, `core/windows/displays.py`

Enumerate through `EnumDisplayMonitors` + `GetMonitorInfoW`. **No `HMONITOR`
crosses the boundary**: the handle exists only for the duration of the enumeration
callback, and every value is copied out inside it. An `HMONITOR` is an address
Windows recycles, exactly the reason M2.16.1 refused to treat an `HWND` as an
identity.

**Display identity is the device name** (`\\.\DISPLAY1`), not a handle. There is no
second anchor available: a window can be pinned to a process creation time, a
monitor belongs to no process, so the device name is the whole identity. That is
why a target is re-resolved against live topology rather than trusted from
remembered bounds — and why a *reconfigured* display is reported as such instead of
silently presenting new bounds.

**The primary display comes from `MONITORINFOF_PRIMARY`** and never from ordering,
position or size. A primary that is not leftmost or not largest is completely
normal, and inferring it from geometry is how a machine with the primary on the
right gets the wrong answer. No primary reported, or two, is a structured failure.

#### Coordinates, and the two virtual desktops

`Rect` and `Size` live in `core/windows/coordinates.py` next to the existing
`VirtualScreen`, because they are the same subject. **Right and bottom are
exclusive**, so `width = right - left` and the last valid pixel is
`left + width - 1`. That is the off-by-one M2.16.2 already fixed for window bounds,
now stated once and obeyed by every containment test in the codebase.

Two things are deliberately *not* conflated, and this cost real thought:

- **Screen coordinates ≠ window-relative coordinates.** A `WindowPoint` resolved
  against a window is not a monitor point. Translating between spaces goes through
  the named `Rect.translated_from` / `Rect.offset` rather than arithmetic done
  somewhere convenient, because the M2.16.2 lesson is that provenance must not be
  laundered.
- **There are two legitimate "virtual desktop" rectangles.**
  `SM_CXVIRTUALSCREEN` spans any *gap* between monitors, because it describes where
  the pointer may be clamped. The union of the display rectangles covers only
  pixels that exist. So `core/windows/input.py` keeps using the system metric for
  pointer validation and `displays.py` uses the union for capture and topology.
  Unifying them would have meant either rejecting pointer positions Windows will
  accept, or letting a capture cover a gap with no pixels in it. `assert_consistent`
  logs if the two disagree with no gap to explain it.

#### DPI, and the trap in it

**`GetDpiForMonitor` does not always answer the question it appears to.** In a
DPI-*unaware* process Windows virtualises the desktop and reports an effective DPI
of 96 — on this machine that call returns exactly 96 while the panel is 2560×1600
running at 150%, and the monitor reports 1707×1067. All three are consistent: the
process is seeing a virtualised desktop.

Reporting that 96 as a measurement would be fabricating a number. So:

- `scale_percent` comes from `GetScaleFactorForMonitor`, which is truthful in every
  awareness mode, and is reported;
- `dpi_x` / `dpi_y` are reported **only when the process is actually per-monitor
  aware**, and are otherwise `None` with `dpi_virtualized` set to say why;
- `dpi_awareness()` publishes the context, so a reader can always tell which kind of
  number they are looking at.

**This layer reports DPI awareness; it never sets it.** Changing process DPI
awareness would alter how every other window call in the codebase is virtualised,
which is a far larger decision than a display foundation should take alone. A test
asserts no `SetProcessDpiAwareness*` call exists in the module.

### 7.8 M2.16.4 — bounded capture, `core/windows/capture.py`

`BitBlt` from the screen device context into a top-down 32-bit DIB section.
Negative `biHeight` requests a top-down DIB, so the image needs no vertical flip —
a positive value would yield an upside-down capture that still looks like plausible
image data, so the orientation is tested directly.

**Why not Desktop Duplication, Windows Graphics Capture, or `PrintWindow`:**

- *Desktop Duplication* is faster and is what a high-framerate capture wants, but it
  needs DXGI, a device and a lifecycle — a video-pipeline decision, not a
  foundation one.
- *Windows Graphics Capture* is the modern answer, but it is per-window,
  asynchronous and needs a package. It is the right choice for M2.16.5 when a
  specific *window's* contents are what is wanted.
- *`PrintWindow` is avoided entirely.* For many windows it returns a blank or stale
  image while reporting success, which is precisely the fake success this codebase
  refuses to produce.

Every GDI object is released in a `finally`, in reverse acquisition order — the DIB
must be deselected from the memory DC before it is deleted, the memory DC before
the screen DC is released. Getting that order wrong leaks a GDI object per capture
rather than failing loudly, so it is tested on success, on `BitBlt` refusal, on
mid-flight cancellation and on an exception raised between allocation and return.

#### What makes this capture and not surveillance

Screen capture is the first M2.16 operation that can expose something the user did
not intend to show, so the boundaries are explicit rather than incidental:

- **An explicit target is required.** No `capture()` with no argument, and no
  default meaning "the whole desktop". A caller names a display, or names a
  rectangle, or names the virtual desktop *as the virtual desktop*. There is no way
  to ask for a capture by not specifying one, because unspecified is exactly how an
  unbounded capture happens.
- **Nothing is persisted.** No file, no log, no memory store, no upload.
  `ScreenCapture.to_dict()` omits the image and the object excludes it from
  `__repr__`, so anything that gets logged or cached carries dimensions only.
- **Nothing runs by itself.** No timer, thread or poller; a capture happens when a
  caller asks. Asserted by an AST import scan, not a substring search.
- **The capability payload carries metadata only.** The pixels stay in the
  `ScreenCapture` object for an in-process caller. Handing screen contents to a
  model is a later milestone's decision to make explicitly.
- **`capture_screen` is classified mutating**, which in this codebase means SENSITIVE
  at the bridge. Screen capture is the only M2.16.4 operation that is; the five
  display queries are reads and stay SAFE.

#### Verification

A capture is genuinely verifiable: it reports the rectangle it covered and its own
pixel dimensions, and the exclusive-edge arithmetic ties those together, so a
capture at the wrong size **fails**. What is deliberately *not* claimed is anything
about the content — whether the pixels are what the user expected to see is not
observable from here, and the verifier's own text says so.

### 7.9 M2.16.4 — what this machine revealed

Testing capture here produced a finding worth recording rather than working around.
A test-owned Tk window filled with `#3366CC`:

- `PrintWindow` returns `(51, 102, 204)` — exactly the colour it was given;
- a screen `BitBlt` of the same rectangle returns `(38, 42, 49)`;
- DWM composition reports enabled;
- the capture is **byte-identical to Pillow's `ImageGrab`** for the same rectangle.

So the window paints correctly, the capture is faithful, and this session's screen
surface simply does not present window content. The pixel-content assertion is
therefore skipped with that explanation rather than weakened or deleted, and the
capture is validated against `ImageGrab` — an independent implementation of the same
idea, which is stronger evidence than comparing the output with itself.

`tests/test_m2164_display_real.py` asserts the whole screen is never captured as an
oracle, never written to disk, and that teardown leaves no window or process behind
(asserted, not attempted). It also caught a real leak during development: a venv
`python.exe` re-executes, so the window's owning process is a grandchild that
`Popen.terminate()` misses. Two processes per run were leaking until the fixture
resolved the owning pid from the window handle and terminated it too.

### 7.10 Three assertions that were wrong, not the code

Found while testing, and worth recording because in each case the *test* encoded
the weaker behaviour:

1. `test_contains_includes_the_edges` asserted `contains(100, 100)` on a 100×100
   screen was true. That is the bug, not the test: see §7.2.
2. `test_a_partially_injected_batch_is_also_a_refusal` replaced `_send_input`
   wholesale, which skipped the very check under test — a replacement can simply
   ignore the count it is handed. It now patches `user32.SendInput` so the real
   logic runs.
3. The Unicode test waited for "some text appeared" and then asserted the whole
   string, so it could catch a half-typed value. Waiting for the condition being
   *asserted* is the difference between a flaky test and a real one.

Two Tk-specific traps, also worth naming: returning `"break"` from a binding stops
the *default class binding*, which is what gives an `Entry` keyboard focus on
click; and a single physical click bound at three levels of the bind chain fires
all three handlers unless one returns `"break"`. Binding once on the toplevel and
returning `None` is what makes a click both count once and focus.

---

## 7b. M2.16.5 UI Automation and visual targeting

M2.16 could name a window and act on it by coordinate. It could not name *a thing
inside* a window. M2.16.5 adds that, and changes nothing above the capability
boundary.

### 7b.1 Why `comtypes`, and not `pywin32`

UI Automation is native-COM. `UIAutomationCore.dll` registers **no typelib**, so
there is nothing for `win32com` to generate early bindings from, and it does not
support `IDispatch`, so late binding fails with `E_NOINTERFACE`. `comtypes` loads
the interfaces from the DLL at runtime and drives them, so it is the smallest thing
that reaches UIA at all. This is now declared in `pyproject.toml` and
`requirements.txt` as a Windows-only dependency, separately from `pywin32`, with
the reason recorded where a future reader will find it.

`uia_available()` probes rather than assumes. Without it, the nine `uia_*`
operations are withheld and the agent is degraded, not broken.

### 7b.2 An element identity is a recipe, not a label

No single UIA field is a safe primary key, so a target is a recipe and `target_id`
is a deterministic digest of it:

| component            | stability                                                        |
| -------------------- | ---------------------------------------------------------------- |
| `window_key`         | M2.16.1 identity (hwnd, pid + creation time, class, instance). The anchor. |
| `runtime_id`         | Unique *within one running instance*. **Not** in the digest — baking it in would change every id on restart. |
| `automation_id`      | Stable for a well-behaved app, often the Win32 control id — but **not unique**. |
| `name`               | Human-readable and **not unique at all**.                          |
| `occurrence`         | Index among siblings matching the above. Without it, "the first button called OK" and "the second" are one target. |

An identity with no `automation_id`, no `name` and no `runtime_id` is **refused**.
Every field defaults, so such an identity is reachable by accident — and it matches
"the first element that has no name", which is how a click lands on the wrong
control.

`WindowIdentity.from_key()` was added, and is the inverse of `.key`. This is not
incidental: a stored element names its window *by key*, so without a way back from
a key the whole stale-target story cannot run. The first implementation split the
key on `":"`, which silently mis-parses it because `created_at` **contains colons**
(`1234:2026-10-02 19:14:52:MyClass:2a4:9876`). That produced a plausible identity
that then failed the live `matches` check and surfaced as a confusing *identity
mismatch* — pointing the investigation at the window layer instead of at the
parser. It is a regex now, with a test for the colons in the timestamp and for a
class name containing colons.

### 7b.3 Application text is confined to a labelled envelope

A button can be labelled `ignore previous instructions`. The first implementation
built the model payload by starting from the internal `to_dict()` — which
legitimately carries `name`, `automation_id` and `class_name` in the clear, because
that is what re-resolution needs — and bolting an untrusted envelope on top. That
published the same hostile string **twice**: once unlabelled, once labelled. A model
reading the unlabelled copy sees an instruction.

Every model-facing result is now built field by field. Application text appears
only inside `recipe`, which is labelled `UNTRUSTED_APPLICATION_DATA` and carries an
instruction to never follow it. The recipe still *travels* — it is confined, not
deleted, because an element whose name is stripped cannot be acted on again. Text
is truncated at 512 characters and the truncation says so, because an application
can put a megabyte in a control's name.

### 7b.4 Five actions, each with a postcondition

`uia_invoke`, `uia_set_value`, `uia_toggle`, `uia_select`, `uia_expand_collapse` —
all five declared mutating, all five SENSITIVE, all five cancellable. There is
deliberately **no** `execute(pattern, args)`: a generic entry point would accept any
pattern id with arbitrary arguments and no defined preconditions, which is a COM
escape hatch rather than an API.

Each re-reads the pattern's own state and compares. Two findings are worth
recording because both looked like provider bugs and were not:

- **Toggle.** The postcondition is "the state changed". An implementation that
  accepted any non-`None` reading as success exited its settle loop on the first
  poll — which returns the state from *before* the action — and reported
  `off → off / FAILED` for a checkbox that had flipped. Every working toggle was a
  failure until the predicate compared against `state_before`.
- **Invoke.** `Invoke` has no state of its own, so with no witness the result is
  honestly `UNVERIFIED`: Windows accepting the call is not evidence that anything
  happened. Naming a second element whose visible state should change turns it
  into a verifiable action. Without a settle poll the witness was read once,
  immediately, and returned the pre-press state.

UIA actions are **asynchronous**: they are delivered through the owning thread's
message loop. This layer does not pump another application's queue — sleeping
instead would report every real action as a failure. An optional `settle` callback
lets an in-process host run its own loop; a caller driving a real application
passes nothing and the application's own loop does the work.

### 7b.5 Pattern access, and a trap worth naming

`GetCurrentPattern` returns a bare `IUnknown`, and UIA has one method per pattern
and cannot choose a return type, so each pattern is `QueryInterface`-ed into its own
interface. That cast is also the honest availability test: an unsupported pattern
yields a pointer that will not convert, so "supported" means *usable*.

`GetCurrentPropertyValue` **must not be used**. It takes an overloaded parameter
whose generated binding does not dispatch correctly — it answers with the element's
control type and ignores the property id, which would have made every postcondition
compare control-type numbers.

Every pattern read is a COM acquisition, and the polling loops read one many times
a second. The bare `IUnknown` and the typed reference are both released on every
path. The test measures 300 read rounds and asserts the live COM pointer count does
not move — and asserts first that it *can* detect a leak by retaining 50 references
on purpose, so passing the check means something.

### 7b.6 A visual target is a claim, never a place to click

`VisualTarget` holds a rectangle, a declared source, a confidence and its
provenance — and no image, no model output and no native handle. Its revalidation
report says `actionable: False` unconditionally, and `assert_not_dispatchable()`
raises for it, so a visual claim cannot reach an input verb. Turning a claim into a
place to click goes through the existing anchored pointer path, which re-resolves
and re-checks foreground.

`geometry` and `uia` are installed. `future_vision` and `future_ocr` are **declared
and unavailable**: named so the shape is pinned early and explicitly not working
providers. A target from an uninstalled source reports `UNCORROBORATED`.

`visual_resolve_target` is *not* withheld when UIA is unavailable. Its `geometry`
source is plain rectangle arithmetic, so gating it on `comtypes` would mean a host
quietly lost a capability it can genuinely perform.

### 7b.7 Validation

| gate | result |
| ---- | ------ |
| M2.16.5 deterministic (`test_windows_uia.py`) | 56 passed |
| M2.16.5 real Windows (`test_windows_uia_real.py`) | 28 passed |
| Full deterministic | **3080 passed, 175 skipped** (was 2996 + 175) |
| Full real Windows (`DIABLO_REAL_WINDOWS_TESTS=1`) | **3211 passed, 44 skipped** (was 3127 + 44) |
| `compileall`, `pip check` | clean |
| Process / window / GDI leaks from the new tests | none (delta 0) |

Every action assertion has an independent witness outside UI Automation: a toggle
is cross-checked against `BM_GETCHECK`, a `set_value` against `GetWindowTextW`,
and an `Invoke` against a label the button's own `WM_COMMAND` handler changes. A
test that only asks UIA whether UIA worked proves nothing.

The test surface is native Win32 controls in-process. Tk was rejected: it exposes
generic unnamed panes with no usable patterns, so a Tk fixture would have tested
nothing.

### 7b.8 Known limitation: recoverable structured exceptions from `comtypes`

Running the real UIA module under pytest prints a number of `Windows fatal
exception: access violation` lines with a C stack trace. They are **recovered** —
Python's SEH handler continues, every test passes, and no reference is leaked — and
they appear **only** because pytest enables `faulthandler`. Running the same tests
with `-p no:faulthandler` produces zero messages and the same result.

Characterised, not guessed:

- It is **not** this layer's code. The count is unchanged whether `_release` calls
  COM `Release()` explicitly or lets `comtypes` release on collection, and
  unchanged whether the message-loop `argtypes` are declared. Both were A/B'd.
- It does not occur outside UIA interaction. A standalone create/pump/close cycle
  and a single-element `_describe` both print nothing.
- It scales with pattern probing, i.e. with the `QueryInterface` that
  `_patterns_of` performs for every pattern UIA might support — including the ones
  it does not, which is the point of that call (§7b.5).
- It originates inside `comtypes`' generated `UIAutomationCore` bindings.

It is reported rather than suppressed: silencing it would hide a genuine fault if
one ever appears. The results themselves are trustworthy for an independent reason
— every action is cross-checked against Win32 (§7b.7), not against UIA. If it is
ever diagnosed further, `-p no:faulthandler` makes the noise go away without
changing behaviour, and the harness would then silently stop reporting real
faults, so it is recorded here rather than configured away.

### 7b.9 Two findings that were not UIA's fault

- The fixture's `WM_COMMAND` handler read the **HIWORD** of `wParam` for the
  control id. `WM_COMMAND` packs the **LOWORD** as the id and the HIWORD as the
  notification, so the handler compared against `BN_CLICKED` (always 0) and never
  fired. An `Invoke` then "worked" and changed nothing, which reads exactly like a
  broken provider.
- `RegisterClassW` fails with `ERROR_CLASS_ALREADY_EXISTS` for every probe after
  the first, because the class is process-wide. Treating that as an error made the
  fixture usable once. Tolerating it without unregistering was worse: the class
  holds a raw pointer to the previous probe's `WNDPROC`, so Windows called into
  freed memory and took the interpreter down with
  `STATUS_FATAL_USER_CALLBACK_EXCEPTION`. The class is now unregistered on close,
  and every callback is retained deliberately.

## 7c. M2.16.6 Integrated Desktop Workflows

M2.16 could name a window and act on it by coordinate. M2.16.5 could name a control
inside it. Neither could *sequence* those steps - open an application, focus its
window, resolve a target by name, act on it, and prove the result. M2.16.6 adds
exactly that and nothing else.

### 7c.1 Composition, not capability

`integration/desktop_workflow.py` contains no native code. It cannot import
`pywin32`, `comtypes` or anything under `core.windows`, and a test asserts that it
does not. Every step is a `CapabilityRequest` against the already-registered
`windows` capability, dispatched through the existing `CapabilityRegistry`.

There is one Windows capability layer, one `Executor`, one `TaskManager`, one
`TaskStore` and one `VerificationLedger`; this milestone added none of them. A
workflow step projects onto the existing `PlanStepRecord` rather than introducing a
second persistent plan format.

### 7c.2 What a workflow context may hold

`DesktopContext` carries logical references only: process identity (pid **plus**
creation time), window key, an optional UIA target recipe, an optional display id.
`DesktopContext.from_dict` **refuses** any payload containing a handle key
(`hwnd`, `handle`, `HMonitor`, `PyHANDLE`, ...) - not because the key would be
dangerous alone, but because it means the boundary was already crossed and better
stopped at the edge.

The window layer learned to accept its own key string in this milestone
(`WindowLayer._coerce_identity`). It had a shape it emitted and could not consume,
which M2.16.6 hit immediately: every `get_window` call that passed a key back was
refused. A key that cannot be read back is not a durable identity.

### 7c.3 UIA-first, and what a fallback costs

`interact` takes an *intent* ("click the OK button") and chooses the pattern from a
table, so the caller never picks a COM interface. `targeting_mode` and
`fallback_reason` are recorded on every action.

Pointer fallback happens only when the caller allowed it **and** a UIA target point
exists - never a fabricated coordinate. Two distinctions the tests pin down:

- An **unavailable** pattern justifies a pointer click at the target's own point.
- A pattern that was **dispatched and did not meet its postcondition** does not. It
  may already have applied, so re-aiming at the same spot is a second attempt at an
  action of unknown outcome - which is how a form gets submitted twice.
- A pointer click has no pattern state to re-read, so it cannot carry the same
  `PASSED` claim a semantic invoke can. It reports `UNVERIFIED`.

### 7c.4 Verification is separate from success

A workflow that returned successfully has *not* thereby changed the desktop.
Status and verdict stay independent:

| what happened | status | verification |
|---|---|---|
| pressed a button, witness confirmed the change | success | PASSED |
| pressed a button, no witness | success | UNVERIFIED |
| dispatched, postcondition not met | failed | FAILED |
| cancelled | cancelled | UNVERIFIED |

The bridge verifier reads the verdict from the workflow's own `verification` field
and its per-step evidence, never from `result.success`. A step whose declared
`expect`ation did not hold is a **failed step**, not a clean completion - the
terminal-state invariant in `core.task_manager` does not admit a completed task
paired with a failed verdict, and it is right not to.

### 7c.5 Ambiguity and staleness are answers

`find_window` returning two candidates is `WINDOW_AMBIGUOUS` with the candidate
keys and titles attached and **no** choice made. `find_target` returning several
elements is `TARGET_AMBIGUOUS`, likewise.

This is not theoretical. Notepad genuinely owns a `GDI+ Hook Window Class` helper
whose title mentions it, so a title-only search for "Notepad" *is* ambiguous on this
machine, and the workflow refused until the test named the class. That is the
feature working.

Window failures and element failures are reported as `WINDOW_*` and `TARGET_*`
respectively, because the caller's recovery differs: re-enumerate the window, or
narrow the recipe.

### 7c.6 Permissions, twice, and why

A workflow is internal, but internal is not trusted. Each primitive is evaluated
against `PermissionManager` with the operation's own declared sensitivity, and the
tool-level decision the Executor already made remains the outer gate.

Two details were found the hard way and are worth recording:

- The inner gate treats `requires_confirmation` as *not* a denial and returns
  `true`. Confirmation is interactive and owned by the Executor, which already asked
  before the tool was dispatched; the inner gate is policy. Returning the raw
  decision made every sensitive desktop step impossible, because
  `CapabilityRegistry._authorized` reads `.allow`, which is false precisely when
  confirmation is pending.
- A sensitivity probe that *raises* widens to SENSITIVE. It never widens the other
  way, and the requirement is recorded in the result's evidence.

### 7c.7 Cancellation

Checked before expensive work, again immediately before every mutation, and between
plan steps. A cancelled plan does not run its remaining steps, and the test asserts
the fixture's click count and checkbox state are unchanged afterwards. Cancellation
propagates into the bound token, and is scoped per task.

One bug here is worth naming because it was silent: `run_plan` passed its internal
step wrapper to the event sink, which has no `to_dict`. The `AttributeError` was
swallowed as "sink is advisory", so a sink that requested cancellation never ran
and the workflow continued. A silently ignored cancellation is the worst version of
that bug.

### 7c.8 Replay

`DesktopStep.is_replayable` follows the planner's rule: read-only steps may be
repeated, side-effecting ones may not. The projection onto `PlanStepRecord` is
deliberately *more* conservative - every desktop step is non-replayable there,
because `tool_is_read_only` matches registered tool-name prefixes and a capability
operation is not one. Over-refusing a replay costs a re-observation;
under-refusing one duplicates a button press.

There is no retry loop anywhere in the workflow. Recovery re-anchors a *target*; it
never re-sends an *action*.

### 7c.9 The high-level tools

Five tools, each a thin schema over the `desktop_workflow` capability:

| tool | mutating | purpose |
|---|---|---|
| `desktop.open` | yes | launch an application and anchor to its window |
| `desktop.find` | no | resolve one window or one named target |
| `desktop.interact` | yes | act on a target, UIA-first, verified |
| `desktop.read` | no | read a target's current value or state |
| `desktop.verify` | no | confirm a window is still the same window |

`capability.execute` remains registered and reachable: these tools make hand-
composition optional, not impossible. The `desktop` tool-context signal now selects
them (16 tools for that signal) while the primitives stay available under
`capability`.

### 7c.10 Validation

| gate | result |
| ---- | ------ |
| M2.16.5 deterministic baseline | 3080 passed / 175 skipped |
| Full deterministic | **3163 passed, 197 skipped** |
| Full real Windows (`DIABLO_REAL_WINDOWS_TESTS=1`) | **3316 passed, 44 skipped** |
| M2.16.6 deterministic (`test_m2166_desktop_workflow.py`) | **83 passed** |
| M2.16.6 real Windows (`test_m2166_desktop_workflow_real.py`) | **22 passed** |
| `compileall`, `pip check` | clean |
| leaks from the M2.16.6 tests (processes / windows / apps) | 0 / 0 / 0 |

Every action assertion has a witness outside UI Automation - `BM_GETCHECK`,
`GetWindowTextW`, and a label the button's own `WM_COMMAND` handler writes.

### 7c.11 Three findings that were not the workflow's fault

- **A background pump cannot stand in for an application loop.** `PeekMessage`
  with a null hwnd pulls messages *system-wide*, so a background thread takes the
  target's messages and dispatches them on the wrong thread; the action never
  appears to take effect. Pumping has to happen on the thread that owns the window -
  which is why `DesktopWorkflow` takes a host `settle` hook, exactly as M2.16.5's
  `act` does.
- **A witness must be addressed by its stable fields.** Re-resolving a witness
  after the action means searching for the value it is *about to stop having* if its
  `name` is part of the recipe. `name` is stripped, and a partial witness supplied
  by a plan (which cannot know the window key yet) is resolved to a complete recipe
  first.
- **Notepad and Calculator are not scriptable the way this suite first assumed.**
  Notepad is genuinely ambiguous by title; Calculator's `ApplicationFrameWindow` is
  a protected shell window that M2.16.1 refuses to focus *by policy*. Both tests now
  assert the refusal instead of working around it.

### 7c.12 Limitations

1. **`123 x 456` is not asserted as a result.** Calculator's control ids differ
   between builds and the assertion would be a guess. What is asserted is that the
   window is found, anchored, verified, and that its buttons are reachable
   *semantically* - including that several match, which is why it refuses.
2. **Typing into Notepad is not asserted end-to-end.** Reading the typed text back
   needs a document UIA element whose availability varies by build, and fabricating
   a coordinate to type at is forbidden. The test asserts what holds everywhere: the
   window identity is stable across an input attempt, and unanchored typing is
   refused.
3. **No visual producer.** `future_vision` / `future_ocr` remain declared and
   unavailable (§7b.6). No model output is indexed as durable knowledge.
4. **Knowledge Graph integration is deliberately minimal.** A desktop workflow is
   not indexed. Task, application and window relationships belong to a later
   milestone, and indexing runtime state as durable truth is exactly what §7b.2
   warns against.
5. **`run_plan` is not exposed as a model tool.** `desktop_workflow.run_plan` exists
   for callers that have a plan, but the agent gets the five intent-level tools;
   exposing plans to the model would grow the surface without adding capability.

---

## 8. Still not done

1. **Knowledge Graph case-normalisation** (audit #9). Not addressed.
2. **23 skills of dead code** (audit #13). `coding/code_reader.py`,
   `coding/code_writer.py` and others are still on disk with no importers.
3. **Real-model runs.** `DIABLO_P9_TIMEOUT=300` gated suites
   (`test_knowledge_p9_real_model.py`, `test_recovery_p10_real_model.py`) were
   not rerun for M2.16; they need Ollama and ~50 minutes. Last verified at
   Phase 10. **Deferred, not claimed.**
4. **Packaged application launch** still costs a PowerShell process (§3.3).
5. **Window-pixel capture on this machine.** M2.16.4's capture is correct and
   validated against an independent implementation, but *this session* does not
   present window content into the screen device context (§7.9), so the one
   pixel-level assertion is environment-skipped. On a session that does, it runs.
6. **`uia_select` and `uia_expand_collapse` are unexercised against a real
   control** (§7b.4). Both are implemented, declared mutating and refuse cleanly on
   a control that lacks the pattern, but the fixture's combo box exposes only
   `Value`, and a list box's items came back upper-cased and in reverse insertion
   order through every message id tried. Rather than assert against a fixture
   element whose behaviour was not understood, the list box was removed. A real
   `ListView`/`TreeView`/`ComboBox` target is the obvious next fixture.
7. **No screenshot is a model input.** M2.16.5 keeps pixels out of every payload;
   M2.18 Vision Foundation is where image data reaches a model, and it must make
   that decision explicitly rather than inherit it. `future_vision` and
   `future_ocr` are declared and unavailable (§7b.6).
8. **No visual producer beyond `geometry`.** A vision or OCR target has no
   installer, and `VisualTarget` currently has no way to be created from a detector
   output — only validated once it exists.

Nothing here is claimed complete that is not.

---

## 9. Roadmap position

| Milestone | Status |
|---|---|
| M2.16.1 Window Foundation | **COMPLETE** |
| M2.16.2 Pointer Foundation | **COMPLETE** |
| M2.16.3 Keyboard Foundation | **COMPLETE** |
| M2.16.4 Screen / Display Foundation | **COMPLETE** |
| M2.16.5 UIA + Visual Targeting | **COMPLETE** (foundation; two actions unproven against a real control — §8.6) |
| M2.16.6 Integrated Desktop Workflows | **COMPLETE** (composition only; no autonomy, no vision) |
| M2.16.UI Edge Companion | not started |
| M2.17 Persistent Autonomy | not started |
| M2.18 Vision Foundation | not started |
| Voice / STT / VAD | not started |

**Accessibility tree:** UI Automation is now present, but only as the narrow set in
§7b — bounded discovery, five semantic actions, no arbitrary predicate query and no
generic pattern dispatch. It is a target mechanism, not a general screen reader.

OCR, vision, a VLM, visual element targeting beyond `geometry`, continuous
screenshot polling, screen recording and cloud image analysis are all **absent and
deliberately so**. `future_vision` and `future_ocr` are declared names in
`core/windows/visual.py` and explicitly report `installed: False` (§7b.6).
`tools/local_vlm.py` and the `capabilities.py` "analyze UI elements in a screenshot"
entry point are pre-existing placeholders from before M2.16; neither M2.16.4 nor
M2.16.5 wired, activated or tested them, and nothing in the new code path reaches
them.

---

## 10. Recommended next task

**M2.16.6 hardening, then M2.16.UI — the Edge Companion over a contract that is
already clean enough to consume.**

M2.16.6 is complete as composition, and the backend contract it leaves behind is
the thing worth building on: a workflow is a sequenced, permission-gated,
cancellation-aware, independently verified set of capability calls over an anchored
window identity, with no handles in its state.

Before any product surface, two gaps remain and both are about proof rather than
capability:

- `uia_select` and `uia_expand_collapse` still refuse cleanly but are unexercised
  against a real control (§8.6). A `ListView` or a genuine drop-down `ComboBox`
  would cover them.
- **REAL-MODEL VALIDATION DEFERRED.** The `DIABLO_P9_TIMEOUT=300` suites
  (`test_knowledge_p9_real_model.py`, `test_recovery_p10_real_model.py`) were not
  rerun; they need Ollama and ~50 minutes. Last verified at Phase 10. The desktop
  tools are therefore validated deterministically and against real Windows, but
  **no claim is made that a model has selected them**. Neither M2.16 nor M2.16.5
  nor M2.16.6 touches the agent turn path, so this is six milestones stale rather
  than urgent.

Boundaries any next milestone must respect, all established above:

- **No general GUI agent.** No autonomous screen exploration, no
  screenshot → model → click loop, no coordinate search, no unbounded retry. Every
  desktop action must still be a named, anchored, permission-gated capability call.
- **Uncertain mutations are never replayed.** Fresh observation or reconciliation,
  not a second attempt (§7c.8).
- **Ambiguity and staleness stay refusals**, not best-effort fallbacks (§7c.5).
- **Application text stays untrusted data**, including inside workflow evidence
  (§7b.3).
- **A screenshot is still not a model input.** M2.18 Vision Foundation is where that
  decision gets made explicitly rather than inherited.
- **Windows Graphics Capture** remains the better tool for a specific window's
  contents (§7.8).

The companion may consume `DesktopWorkflowResult` for display and must not reach
past it into capability internals — the result already carries the targeting mode,
the fallback reason, the observation and the verdict needed to render an honest
status.

Still deliberately out of scope: autonomous workflows that chain input without a
caller in the loop.