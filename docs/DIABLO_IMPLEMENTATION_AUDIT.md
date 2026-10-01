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

### M2.16.2 / M2.16.3

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

Nothing here is claimed complete that is not.

---

## 9. Roadmap position

| Milestone | Status |
|---|---|
| M2.16.1 Window Foundation | **COMPLETE** |
| M2.16.2 Pointer Foundation | **COMPLETE** |
| M2.16.3 Keyboard Foundation | **COMPLETE** |
| M2.16.4 Screen / Display Foundation | **COMPLETE** |
| M2.16.5 UIA + Visual Targeting | not started |
| M2.16.6 Integrated Desktop Workflows | not started |
| M2.16.UI Edge Companion | not started |
| M2.17 Persistent Autonomy | not started |
| M2.18 Vision Foundation | not started |
| Voice / STT / VAD | not started |

OCR, vision, a VLM, UI Automation, the accessibility tree, visual element
targeting, continuous screenshot polling, screen recording and cloud image
analysis are all **absent and deliberately so**. `tools/local_vlm.py` and the
`capabilities.py` "analyze UI elements in a screenshot" entry point are
pre-existing placeholders from before M2.16; M2.16.4 did not wire, activate or
test them, and nothing in the new code path reaches them.

---

## 10. Recommended next task

**M2.16.5 — UI Automation and visual targeting, on the foundation M2.16 now
provides.**

M2.16 is complete as a foundation: a target is a verified identity rather than a
coordinate, input is permission-gated and cancellable, display topology and
geometry are trustworthy, and capture is bounded and explicit. What it deliberately
does *not* have is a way to name a *thing* on screen — which is exactly the gap
UIA fills.

Boundaries M2.16.5 must respect, all established above:

- **UIA is a separate capability with its own mutating set**, not more verbs on
  `windows`. Invoking a control is a mutation of someone else's application.
- **Targeting is identity-based, like everything else in M2.16.** A control is
  named by its automation identity and re-resolved immediately before acting on
  it. A coordinate derived from a screenshot must be resolved back through a
  verified target before it is dispatched — the same rule M2.16.2 established for
  a bare `ScreenPoint`, and the reason `require_anchored` exists.
- **A screenshot is not yet a model input.** M2.16.4 deliberately keeps pixels out
  of every payload. M2.18 Vision Foundation is where image data is handed to a
  model, and it must make that decision explicitly rather than inherit it.
- **Windows Graphics Capture is the better tool for a specific window's contents**
  (§7.8), including DWM-captured windows that a screen `BitBlt` misses. This
  machine is a concrete example of what a screen-level capture cannot see.

Alongside that, the deferred real-model suites (`test_knowledge_p9_real_model.py`,
`test_recovery_p10_real_model.py`, `DIABLO_P9_TIMEOUT=300`, ~50 min, needs Ollama)
remain unrerun since Phase 10. M2.16 does not touch the agent turn path or recovery
— it adds a native capability and touches no existing execution path — so this is
not urgent. But it is now four milestones stale, and no fresh real-model validation
is claimed for M2.14.1, M2.15, M2.15.2 or M2.16.

Still deliberately out of scope: autonomous workflows that chain input without a
caller in the loop.