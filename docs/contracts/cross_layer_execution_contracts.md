# Ratified Cross-Layer Execution Contracts (S1 -> S2)

Status: **RATIFIED** at M2.12 (commit `e8fbfd7` lineage).
Scope: S1 (`core/`, `tools/`, `config/`, `ai/`) and the S1-owned boundary in
`integration/contracts.py`. S2.9 (`blender/`, `vision/`) is the first consumer
obligated by these contracts.

These three contracts are normative. S2.9 MUST NOT invent a competing policy.
Where S1 already implements a rule, the rule is locked by
`tests/test_cross_layer_contracts.py`.

---

## Contract 1 — Request ID Ownership

### Ratified

1. The authoritative `request_id` is **owned by S1**. It is minted by the S1
   request execution path (registry / request construction) and is never
   derived from model output, layer output, or backend output.
2. `CapabilityResult.request_id == trusted S1 request_id` on **every** path,
   including success, failure, cancellation, and unavailable.
3. `run_id` and `task_id` remain authoritative S1 / `TaskManager` identities.
   `CapabilityResult.task_id` is likewise the S1 task id.
4. S2 MUST NOT overwrite the trusted S1 `request_id` or `task_id`. This is
   structurally guaranteed in S1: `CapabilityResult.from_value` assigns
   `request_id=request.request_id` and `task_id=request.task_id` and **ignores
   any `request_id`/`task_id` present in a layer-supplied payload**.
5. Model-supplied `request_id` / `run_id` / `task_id` / `source` remain
   untrusted and continue to be stripped by the capability bridge
   (`UNTRUSTED_PROVENANCE_KEYS`).
6. `RequestContext` (M2.12 P1) introduces **no second user-request identity**.
   It holds exactly nine request-scoped fields and exposes `run_id`/`task_id`
   as read-only proxies onto `TaskManager`. It has no `request_id` field.
   `DEFAULT_MAX_CONCURRENT_REQUESTS = 1`; single-flight is retained.

### S2 internal correlation identifiers

If S2 needs an internal correlation id it MUST be named
`s2_internal_request_id` and MUST remain distinct from `request_id`. The two
identifiers MUST NOT share a field name, because `request_id` is
S1-authoritative end to end.

### Known S2 gap that S2.9 MUST close — **CLOSED (envelope correlation)**

`blender/protocol.py` and `vision/protocol.py` used to mint their **own**
`request_id` when the caller supplied none, and the S2 layer adapters
(`integration/contracts.py::_LayerAdapter._dispatch`) call the layer operation
with `params` only — they did not forward the trusted S1 `request_id`.

Consequence, verified empirically at ratification (state **before** the fix):

| Identifier | Value | Owner |
| --- | --- | --- |
| `CapabilityRequest.request_id` | `S1-AUTHORITATIVE-REQ` | S1 |
| `CapabilityResult.request_id` | `S1-AUTHORITATIVE-REQ` | S1 (correct) |
| `request_id` on the Blender wire envelope | `6ce5924d0667484ba15a2ab5e22c8f50` | S2, **divergent** |

The ratified invariant (`CapabilityResult.request_id == trusted S1 request_id`)
therefore **already holds**. The defect was narrower: the trusted request id was
not *visible to the S2 backend* for direct `blender.*` / `vision.*` operations,
so backend-side correlation used a divergent id.

#### Status: closed

The forwarding obligation is implemented. `request_id` on the Blender and Vision
wire envelopes is now the S1-authoritative id on every path — success, failure,
cancellation and unavailable. The mechanism:

* `BlenderCapabilityLayer` and `VisionCapabilityLayer` expose a re-entrant,
  thread-local `request_correlation(request_id, context)` scope, and pass the
  trusted id into `build_request(request_id=...)`. `VisionCapabilityLayer`
  additionally falls back to `context["request_id"]`, since its public
  operations already take a `context` payload.
* `_LayerAdapter.execute` and `BlenderVisionCapabilityAdapter.execute` enter that
  scope around the layer call, so the adapter contract itself carries the id
  without changing any operation params.
* A layer with no `request_correlation` hook is unaffected.
* `blender/protocol.py::build_request` and `vision/protocol.py::build_request`
  keep minting an id when no trusted id is in scope. That fallback is retained as
  an internal diagnostic for direct (non-S1) callers; it is never authoritative,
  because `CapabilityResult.from_value` assigns identity from the request.

The same field name is used on the wire, which is intentional: `request_id` is
S1-authoritative end to end. The `s2_internal_request_id` rule above continues
to govern any *separate* S2-internal correlation id.

Verified by `tests/test_s2_wire_correlation.py` (behavioural, at the real
protocol/backend boundary via a spy runner and a spy vision backend) and against
a real `blender.exe` background process, which echoes the trusted id back.

This closes the envelope-correlation obligation only. The remaining S2.9
obligations — subprocess hardening, cancellation supervision, output limits, temp
cleanup and error redesign — are **NOT STARTED**.

---

## Contract 2 — Long-Running Cancellation

### Ratified

1. When cancellation is triggered while an interruptible Blender/S2 subprocess
   is actively running, S2 MUST stop waiting for normal completion and MUST
   attempt to terminate the active child process.
2. If the child does not terminate within a bounded grace period, a bounded
   kill MUST be attempted.
3. When cancellation caused the termination, the operation MUST report
   `CANCELLED`, never generic `FAILURE`.
4. Cancellation MUST NOT silently become a successful result.
5. Cancellation MUST NOT leave an active child process running indefinitely.
6. The S2 session/workspace MUST NOT be reused blindly after forced
   termination; it MUST be revalidated or recovered before subsequent reuse.
7. The S2 session/workspace reuse and recovery rules are S2-owned. S1 does not
   modify Blender concurrency or session handling under this contract.

### Status vocabulary is closed and CANCELLED is distinct

`CapabilityStatus` is exactly:

```
success | failure | cancelled | permission_required | unavailable | invalid_request
```

`CANCELLED` is a distinct member from both `SUCCESS` and `FAILURE`, and
`CapabilityStatus.cancelled` is a distinct predicate from `.success`.

### How "cancellation pending / not interruptible" MUST be reported

The status vocabulary is **closed**. There is no `cancel_pending` member, and
one MUST NOT be invented: `CapabilityResult.from_value` resolves an unknown
status string to `SUCCESS`/`FAILURE`, so an invented `cancel_pending` silently
degrades a caller-requested cancellation into a generic `FAILURE` — which
Contract 2 point 3 forbids. The accompanying `metadata` *does* survive, so the
not-terminated signal is not lost, but the status S1 branches on is wrong and
indistinguishable from a real operational failure. This exact degradation is
locked by
`test_invented_cancel_pending_status_degrades_to_false_failure`.

A non-interruptible operation that could not be terminated MUST be reported as:

* `status = cancelled`, plus
* `error.error = "cancellation_pending"` (stable machine-readable code), plus
* `metadata.cancellation_terminated = false` and
  `metadata.interruptible = false`.

This channel is verified to survive the full S1 -> S2 -> `CapabilityResult`
path. S2 MUST NOT claim successful termination it did not achieve, and MUST
NOT report `SUCCESS`.

### Required distinction

Every long-running S2 operation MUST be attributable to exactly one of:

| Outcome | Status |
| --- | --- |
| caller-requested cancellation | `cancelled` |
| normal operation failure | `failure` |
| infrastructure / process-launch failure | `unavailable` or `failure` |
| timeout | `failure` (with a timeout-identifiable `error.error` code) |
| successful completion | `success` |

### Grace period

The exact grace timeout is an **S2 implementation detail**. S1 configuration
defines no grace/termination timeout today (verified: no grace/timeout knob in
`config/settings.py`), so nothing on the S1 side needs to change. If S1 later
defines one, this contract is the integration point.

#### Status: implemented (S2.9)

Points 1-6 are implemented in `blender/runner.py` and `blender/capabilities.py`:

* the child is started with `subprocess.Popen`, so the runner keeps a handle and
  a supervision loop polls the cancellation token and the deadline;
* on cancellation (or timeout) the runner stops waiting, calls `terminate()`,
  then escalates to `kill()` after a bounded grace, and reaps the child;
* the outcome is reported as `cancelled` with error code `blender_cancelled`;
* termination is **confirmed, never assumed**: when the child cannot be
  confirmed dead, the code is `cancellation_pending` and
  `metadata.cancellation_terminated` is `false`, as required above. S2 does not
  claim a termination it did not achieve;
* `metadata.interruptible` reflects the operation's class from the authoritative
  inventory (`blender/operations.py`): read-only operations are interruptible;
  session-mutating operations are not, and force session revalidation;
* after any forced termination of a session-mutating operation the session is
  revalidated and discarded when it is not provably intact, so a torn `.blend`
  is never reused blindly.

`terminate()`/`kill()` are used rather than `send_signal` because Windows has no
`SIGKILL`; a missing signal constant must never turn a kill escalation into an
`AttributeError`.

Verified behaviourally in `tests/test_s2_9_hardening.py` (real child processes
for termination, kill ordering, orphan checks and output bounding) and against a
real `blender.exe`, which is terminated mid-render with no surviving process.

**Limitation.** Supervision is bound through the capability boundary
(`BlenderCapabilityLayer.cancellation_scope`, entered by the adapters). An S2.8
workflow that calls the layer directly does not bind a per-task token, so a
workflow step still checks for cancellation only before and after the call and
cannot interrupt a child already running. Closing that would mean threading the
workflow's per-task token into its layer-call funnel, i.e. an S2.8 redesign,
which is out of scope here.

---

## Contract 3 — Error Vocabulary / Status Assignment

### Ratified

`INVALID_REQUEST` is reserved for errors **unambiguously attributable to the
caller or the request itself**.

Permitted uses: malformed request; unsupported operation requested by the
caller; missing required caller parameter; invalid caller-provided parameter
value; structurally invalid capability request.

The following MUST NOT be classified as `INVALID_REQUEST`:

* Blender executable missing
* subprocess launch failure
* `PermissionError` raised by infrastructure
* `OSError` during process execution
* backend crash
* timeout
* session corruption
* temporary-directory cleanup failure
* internal exception
* unknown internal operation dispatch failure

Infrastructure and runtime failures MUST use the appropriate
failure/infrastructure status.

### Classification MUST NOT be inferred from message wording

An error MUST NOT be classified as `INVALID_REQUEST` merely because its
message or code contains a word such as `invalid`. Status assignment MUST be
driven by the raised exception type or an explicit structured error code, not
by substring matching on human-readable text.

Verified S1 behaviour at ratification (no S1 change required):

| Raised condition | Resulting status |
| --- | --- |
| `FileNotFoundError` (blender missing) | `unavailable` |
| `OSError` (subprocess launch failure) | `failure` |
| `PermissionError` (infrastructure) | `permission_required` |
| `RuntimeError` (backend crash) | `failure` |
| `TimeoutError` | `failure` |
| `BlenderVisionError` (session corrupted) | `failure` |
| `ValueError` (internal, message contains "invalid") | `failure` |
| unknown dispatch `Exception` | `failure` |

No infrastructure condition produced `invalid_request`. S1 already satisfies
this; S2.9 removes the last S2-side text inference so the guarantee is structural
rather than incidental.

#### Status: implemented (S2.9)

S2 no longer infers a status from a message, or from a substring of an error
code. `_LayerAdapter._status_for_exception` matches an **exact** stable code
against a declared table, then falls back to exact exception types, and defaults
to `failure`:

| Blender error code | Status |
| --- | --- |
| `blender_cancelled`, `cancellation_pending` | `cancelled` |
| `blender_not_found`, `blender_spawn_error` | `unavailable` |
| `blender_permission_denied` | `permission_required` |
| `blender_timeout` | `failure` |
| `blender_process_error`, `blender_script_error`, `blender_protocol_error` | `failure` |
| `blender_closed` | `failure` |
| `blender_error` (unclassified) | `failure` |

A subprocess that cannot be created is now a typed error rather than a raw
`OSError`: `FileNotFoundError` -> `blender_not_found`, `PermissionError` ->
`blender_permission_denied`, any other `OSError` -> `blender_spawn_error`.
`blender_closed` (an operation attempted on a closed layer) is a lifecycle fault
and is deliberately a `failure`, not caller-attributable invalid input.

`INVALID_REQUEST` remains reachable only for a structurally invalid request or an
unsupported operation requested by the caller. No entry in the table can produce
it. The closed code set lives in `blender.errors.ERROR_CODES` and is pinned by
`tests/test_s2_9_hardening.py`.

### Operation inventory

`blender/operations.py` is the single authoritative inventory. It classifies each
operation as subprocess or local, and as session-mutating or read-only, which is
what drives the cancellation-safety distinction above. The wire protocol
(`SUPPORTED_OPERATIONS`) and the capability adapter (`_operations`) derive from
it; the Blender-side dispatch script keeps its own read-only literal because it
runs standalone inside Blender's interpreter, and a test asserts the two agree.
this contract.

`INVALID_REQUEST` is currently assigned only for: missing
`capability`/`operation`, and unsupported `operation`
(`integration/contracts.py`, `integration/workflows.py`).

### Residual ambiguity recorded for follow-up (not ratified as correct)

`CapabilityRegistry._status_for_exception` contains
`isinstance(exc, PermissionError) or "permission" in code`. The
`"permission" in code` branch is substring sniffing on a structured code and
is the same anti-pattern this contract rejects for `invalid`. It can map an
infrastructure code that merely contains `permission` to
`permission_required`, which would invite S1 to prompt the user for a
permission that is not the problem. It does **not** violate the ratified
`INVALID_REQUEST` rule, so S1 leaves it unchanged; it is recorded here so S2.9
does not rely on it and a later S1 task can tighten it.

### Determinism requirement

The mapping above is the whole classification policy. S2.9 MUST use it and MUST
NOT introduce a second classification layer of its own.

---

## M2.12 P3 Phase 1 — Verification Ledger (schema only)

Status: **ADDITIVE, NOT YET WIRED**. Introduced in `core/verification_ledger.py`.
No existing verification writer is migrated by this phase.

This section records the Phase 1 contract so later phases do not have to
re-derive it. The normative detail lives in the module docstring; this is the
summary.

### What a `VerificationEntry` represents

One immutable adjudication of "was this thing verified, and by what evidence?" —
the atomic unit a durable ledger stores. It carries the three axes below, the
evidence verbatim, both the *requested* and *adjudicated* state, the reason for
any downgrade, and the caller-supplied correlation identity (`task_id`,
`run_id`, `request_id`, `step_id`).

### The three axes

The legacy `VerificationResult.kind` is a single opaque string that conflates
*what* was verified with *how*. The ledger splits it explicitly:

| Axis | Question | Values |
| --- | --- | --- |
| `subject_kind` | What is the claim about? | `task`, `step`, `operation`, `file`, `environment`, `run`, `unknown` |
| `method_kind` | How was it established? | `none`, `execution_outcome`, `file_and_output`, `render_file_probe`, `image_inspection`, `blender_vision_render`, `tool_observation`, `capability_evidence`, `capability_execution`, `workflow_contract`, `analysis`, `debug_run` |
| `trust` | Is the source strong enough to support `PASSED`? | `untrusted`, `claimed`, `observed` |

`unknown` subject and `none` method can never support `PASSED`.

### Trust semantics

`EvidenceTrust` has exactly one job in Phase 1: decide whether a source may
support `PASSED`. There is no pre-existing trust vocabulary in the codebase to
reuse, so these three levels are new and deliberately minimal.

* `untrusted` — no defined evidence semantics: empty/default verification, free
  text, a source the ledger cannot place. Can never support `PASSED`.
* `claimed` — self-reported success the ledger cannot independently confirm: a
  model asserting it finished, or a capability reporting `verified=True` about
  itself. Recorded, never believed.
* `observed` — established by an independent observation the ledger can name
  and check: a filesystem probe, a process exit code, an image decode, a
  capability evidence contract. **The only level that may support `PASSED`.**

### Evidence requirements and the PASSED invariant

**`PASSED` requires valid, non-empty evidence from a trusted source, about a
declared subject, by a declared method.**

Evidence must be a JSON-serialisable, non-empty mapping with at least one
substantive leaf. A `detail` string is narration, not evidence. The ledger
rejects or downgrades missing, empty, malformed, contradictory and untrusted
evidence.

Explicitly **not** evidence:

* a tool returning without raising an exception,
* a model claiming success,
* `verified=True` without supporting evidence,
* empty or default verification,
* arbitrary text with no defined evidence semantics.

Contradiction is detected generically: `ASSERTION_KEYS` (`valid`, `exists`,
`verified`, `ok`, `success`, `passed`, `readable`, `complete`) must be strictly
boolean when present, and a `False` value blocks `PASSED`. This encodes the
existing `capability_bridge` rule (`data.get("valid") is False` -> `FAILED`) by
key name, without embedding any tool's semantics.

A requested `PASSED` failing any check is **downgraded** to `UNVERIFIED`; the
original request is kept in `requested_state` and the cause in `reason`.
Downgrade is one-way — a requested `FAILED` is never upgraded.

Compatibility reference: `tools/capability_bridge.CapabilityBridge._valid_evidence`.
The ledger is deliberately *stricter* in requiring a mapping rather than a bare
list/tuple; being stricter cannot weaken existing behaviour. Cross-checks against
the real bridge live in `tests/test_verification_ledger.py`.

### Legacy `kind` compatibility

Legacy kind information is never discarded. Every entry keeps `legacy_kind`
verbatim, and `LEGACY_KIND_AXES` gives the explicit
`kind -> (subject_kind, method_kind)` mapping for every kind the current writers
produce. `resolve_legacy_kind` returns `(UNKNOWN, NONE)` for an unrecognised or
empty kind rather than guessing. An unrecognised kind still round-trips
losslessly; it simply cannot support `PASSED`.

### Migration status

Phase 2 routes every verification writer through the ledger:

```
Agent._record_verification -> TaskManager.record_verification -> VerificationLedger
                             -> TaskManager.set_verification (adjudicated state)
```

`TaskManager.record_verification` is the single funnel. The ledger adjudicates;
the scalar `verification` / `verification_evidence` fields are set from the
*adjudicated* entry, never from the writer's request, so a writer can no longer
assert `PASSED` without valid, non-empty, trusted evidence. A downgrade is
logged and emitted as a `verification_downgraded` lifecycle event.

### Phase 2 compatibility findings, resolved

**CASE A — list/tuple evidence.** `capability_bridge` used to promote any
non-empty list/tuple evidence payload to `PASSED`, and that payload then reached
`dict(evidence)` in `TaskManager.set_verification` and **raised**, aborting task
persistence. The evidence contract is object-shaped by design:
`integration/workflows.py` always builds evidence dicts and explicitly rejects a
verification callback that supplies evidence without a boolean verdict. A bare
sequence is therefore *malformed*, not an alternate valid shape. It is no longer
promoted, and it is preserved under `malformed_evidence` for diagnosis instead
of being silently coerced. Object-shaped evidence is unaffected and still
promotes.

**CASE B — empty-evidence `PASSED` writers.** Three writers requested `PASSED`
with `evidence={}`. Real observable evidence existed nearby for all three, so it
is now constructed rather than fabricated:

| Writer | Evidence now recorded |
| --- | --- |
| `file_and_output` | `path`, `exists`, `size`, `expected_output`, `output_matched`, `exit_code` |
| `tool_observation` | `tool`, `output_chars`, `output_excerpt`, `exit_code` — and `PASSED` additionally requires non-empty observed output, because a tool returning success is not itself evidence |
| `debug` | `command`, `working_dir`, `exit_code`, `failure_signature`, `output_excerpt` from the auto-verify run |

A writer with no real evidence available now stays non-`PASSED` rather than
fabricating evidence to preserve an old verdict.

### Persistence

The ledger is **not** a second persistence system. `TaskManager` owns one
in-memory ledger and projects a bounded slice into the existing
`TaskRecord.metadata["lifecycle"]["verification_ledger"]`, alongside the
pre-existing `verification`, `verification_detail`, `verification_evidence` and
`verification_history` fields, all of which are unchanged and still written.

Three bounds exist at three different layers, deliberately not merged:

| Bound | Value | Layer |
| --- | --- | --- |
| `VerificationLedger.MAX_ENTRIES` | 200 | in-memory adjudication ledger, so recovery can ask what the last verdict established |
| `VerificationLedger.PERSISTED_TAIL` | 20 | projection written into the task record, which is re-serialised on every sync |
| `TaskManager.verification_history` | 20 | *invalidations* ("what was true before a later operation invalidated it"), not adjudications |

Records written before P3 have no `verification_ledger` key and load unchanged.
`VerificationEntry.from_dict` / `VerificationLedger.from_dict` are total: a
truncated, hand-edited or older projection degrades field-by-field instead of
raising, unreadable entries are skipped and counted in `dropped`, and a stored
`PASSED` that no longer satisfies the invariant is loaded as `UNVERIFIED` with
the original record retained for audit.

### Still out of scope

`DebugLoop` planning/dedup, `_llm_loop` recovery structure, `Agent.resume()`,
`RecoveryPolicy` and `FailureObservation` are untouched by Phase 2.

---

## M2.12 P3 Phase 3 — FailureObservation and unified RecoveryPolicy

Introduced in `core/recovery.py`.

### What a `FailureObservation` is

One immutable, redacted record of what went wrong and what is known about it at
the moment recovery is being considered. It captures recovery *decision inputs*
and deliberately does **not** duplicate `TaskRecord` / `TaskRecord.plan`: no
goal, no step listing, no git state, no snapshots. It references the
authoritative ids and carries the bounded facts a decision needs — category,
tool/capability, error code/type, attempt, verification state and evidence,
retryability, cancellation, `recovery_hint`, `expected_result`, durable-plan step
state.

Every free-text and structured field is passed through
`core.logging_setup.redact` **on construction**, so a token, password or
credential in an error message or in verification evidence is `<redacted>`
before the observation can be stored or logged.

`FailureCategory` reuses the ratified `CapabilityStatus` / `StepStatus` meanings
(`cancelled`, `permission_denied`, `invalid_request`, `unavailable`,
`not_executed`, `verification_failed`, `execution_failed`, `unknown`) rather
than inventing a parallel taxonomy. Classification reads structured status
fields, never error-message wording.

### Recovery precedence

Evaluated in order, first match wins:

1. **Cancellation dominates.** Always `NONE`. No retry, no escalation, no
   verification rerun, no skip into another side effect, no replay of an
   interrupted step. Matches the S2.9 rule that `CANCELLED` is terminal.
2. **Terminal plan state with no legal retry edge.** `DONE`, `CANCELLED`,
   `SKIPPED` -> `NONE`; `BLOCKED` and `RUNNING` -> `ABORT`; `PENDING` -> `NONE`.
3. **Interrupted step** -> `ABORT`. Checked *before* the retryable test, so no
   budget can authorise a silent replay.
4. **Non-retryable category** -> `ABORT`.
5. **Verification failure with recorded evidence** -> `RERUN_VERIFY`, within the
   bounded `max_rerun_verify`.
6. **Retry** -> `RETRY_STEP`, within the bounded `max_retries`, through the only
   legal `FAILED -> PENDING` edge. Preferred over skipping and escalation
   because it is the cheapest correct action.
7. **Legal skip** -> `SKIP_STEP`, only once retries are spent and only when
   `allow_skip` is explicitly enabled. Off by default: the durable plan cannot
   know whether a skipped step was required by the goal.
8. **Escalate, then abort** -> `ESCALATE_MODEL` while the escalation budget
   lasts, else `ABORT`.

`recovery_hint` and `expected_result` are **inputs, not commands**. A hint
saying "retry" never overrides a limit, a terminal state, a non-retryable
category or cancellation, and an `expected_result` is never verification
evidence — only the ledger adjudicates a verdict.

### What the policy is not

`RecoveryPolicy` is pure and side-effect free: it inspects an observation and
returns a `RecoveryDecision`. It dispatches nothing, mutates nothing, cannot
recurse, and never returns a step status — the existing durable-plan state
machine remains the only thing that enforces transitions. It cannot grant
permission or widen workspace roots, branch policy or destructive-command
restrictions. Every retry re-enters the normal dispatch path, so the whole
safety stack re-applies on each attempt.

`ESCALATE_MODEL` is a *signal*, not a model choice. Model selection remains with
`_select_model` / `ModelRouter`, which keeps its own `models_max_escalations`
bound; the policy applies a second, independent ceiling that is the stricter of
the two. There is no second model router.

`PlanStepRecord.is_replayable` deliberately does **not** veto an in-request
retry. It answers the resume question ("is re-dispatching free of side
effects?"), which is right for `Agent.resume()` and wrong for a step that ran to
completion and reported a definite failure. What must never be replayed is an
*interrupted* step, and that is checked unconditionally.

### Unified call sites

All three previously independent recovery authorities now route their **final**
decision through the one policy:

| Site | Previously | Now |
| --- | --- | --- |
| `Agent._record_execution` | inline `recovery_attempts + 1 >= models_max_attempts_before_escalate` | `_apply_recovery_policy(..., phase="escalation")`; only `ESCALATE_MODEL` flips `_router_verify_ok` |
| `Agent._llm_loop` | inline `recovery_attempts >= max_recovery_attempts` | `_apply_recovery_policy(..., phase="llm_loop")`; branch wording and behaviour preserved |
| `DebugLoop` | inline `recovery_used >= max_recovery` | `self._recovery_policy().decide(...)`; signature dedup and `max_failures` remain unchanged internal diagnostics |

Limits are reused, not replaced: `max_recovery_attempts` still bounds retries,
`models_max_escalations` still bounds escalation. In `DebugLoop` the auto-verify
command *is* the verification mechanism, so another round necessarily re-runs
verification too; `RERUN_VERIFY` and `RETRY_STEP` therefore both mean "one more
bounded round" there.

Every decision is logged and emitted as a `recovery_decided` lifecycle event
with the action, reason, category, step, tool and counters.

### Known limitation, explicitly not fixed here

Some S2.8 workflow calls cannot interrupt an already-running Blender child
because supervision is not bound directly to the task token. Phase 3 does not
fix that. It guarantees only that where interruption *is* possible, cancellation
dominates every recovery action.

### Still out of scope

`Agent.resume()`, cross-turn cumulative plans, concurrency > 1, a full
`DebugLoop` durable-plan redesign, and `TaskState`/`TaskRecord` status
unification are later work and are not started here.

---

## M2.12 post-P3 composition audit

A read-only audit traced the real production call paths rather than judging the
architecture by naming, then hardened two confirmed defects. Traced entry point
for every path is `Agent.respond()`.

### Composition facts established by tracing

* **Identity.** `task_id` and `run_id` are minted once per request in
  `TaskManager.begin_task`; `step_id` is deterministic (`step-N`, or the tool
  call id) and stable across a retry because the retry re-plans into a *new*
  plan record that retires the failed one into `superseded`. Ledger entries
  carry the authoritative `task_id`/`run_id` copied from the `TaskManager` and
  never mint their own.
* **The `FAILED -> PENDING` edge is never actually taken.** The state machine
  permits it, but the LLM loop retries by re-planning, so a retry never mutates
  a FAILED step in place. This is consistent with the ratified P2 design
  ("the LLM loop re-plans on every reasoning turn") and with `superseded`
  existing to preserve the failed record, so it is intentional rather than a gap.
  It does mean the edge is currently unused.
* **Permissions are re-evaluated on every retry.** A retry re-enters
  `Executor.run`, which calls `PermissionManager.evaluate` for each dispatch, so
  confirmation, workspace roots, branch policy and destructive-command blocking
  all re-apply. Recovery cannot bypass them.
* **Escalation has no second router.** `ESCALATE_MODEL` only flips
  `_router_verify_ok`; `_select_model` / `ModelRouter` remain the sole place a
  model is chosen and the only place `_escalations_used` increments.
* **`RERUN_VERIFY` is unreachable from `_llm_loop`.** A failed verification
  short-circuits to "Task stopped because verification failed" before the
  recovery branch, so the policy is never consulted for that category.
  `DebugLoop`, which owns no durable plan, is the only caller that can produce
  the action. The asymmetry is real but not currently harmful, and is now
  pinned by a test.

### Defect 1 — status inferred from error-message wording (fixed, `ffff890`)

`Agent._step_status` classified a step as `CANCELLED` when the reason text
contained `"Denied by user"`, contradicting the rule above that status must not
be inferred from error-message wording. A real denial already sets
`cancelled=True` and `status="cancelled"` in `Executor`, so the text match was
redundant in production; its only real effect was to let an unrelated blocked
operation whose message happened to read "Denied by user" be reported as
`CANCELLED`, inverting the terminal task state. Pre-existing since `36fdee3`, not
a P3 regression.

### Defect 2 — terminal verdicts bypassing the ledger (fixed, `59eaa53`)

Four sites in `Agent._finish_tracking` called `set_verification` directly,
despite Phase 2 documenting `record_verification` as the single funnel. A task
could therefore end with an authoritative verification of `FAILED` and an empty
ledger, leaving the persisted projection unable to explain its own state:

```
before:  task=failed verification=failed ledger=0 (not_run)
after:   task=failed verification=failed ledger=1 (failed)
```

This was an auditability and contract defect, not a safety hole: all four sites
could only move verification to `FAILED`, so nothing was ever promoted to
`PASSED` outside the ledger invariant.

### Accepted, not changed

* **`RECOVERING -> COMPLETED` is an illegal edge that is attempted and
  swallowed.** After a recovery, a turn returning no tool calls makes
  `_llm_loop` attempt this transition; `Agent._transition` deliberately does not
  raise from cleanup paths, and `_respond` immediately re-evaluates from step
  statuses and reaches the correct terminal state. The durable record status is
  always recomputed from steps, so the swallowed edge cannot produce a wrong
  outcome. Pinned by a test.
* **`invalidate_verification` sets state without a ledger entry.** Correct: a
  retraction is not an adjudication, and it is preserved in the separate,
  bounded `verification_history` layer.
* **`_reconcile_interrupted_tasks` writes lifecycle metadata directly.** Correct
  for a historical record: there is no live ledger in memory for an old task.

### Known deferred limitations

* `ExecutionPlanRecord.summary()` drops `recovery_hint` (and `expected_result`,
  `detail`, `interrupted`, `verification_state`) when a plan is superseded, so
  the hint derived from an observed failure does not carry into the replacement
  step. It survives only in the superseded summary's `status`/`attempts`. This
  matters only once `Agent.resume()` exists.
* The fast-intent path (`_run_intent`: hello-python, open-app, system-info) has
  no recovery at all. It handles three fixed intents and never had retry
  semantics, so this is a scope boundary rather than a defect.
* Ledger entries written by the intent-path writers carry no `step_id`, and
  durable plan steps often have an empty `verification_state`, so a verification
  cannot always be correlated to a specific plan step.
* The ledger is a write-only projection in production;
  `VerificationLedger.from_dict` is not called on any production path.
* The S2.8 limitation where a running Blender child cannot be interrupted is
  unchanged and is not claimed as fixed.

---

## M2.14 Phase 10A - Terminal verdict invariant

### The contradiction this resolves

Two live tests in `tests/test_recovery_p10_real_model.py` asserted opposite
things about the same situation, and both could not be right:

* `..._unverified_task_is_labelled_unverified_not_complete_as_verified`
  (line 333) treated `COMPLETED` + non-`PASSED` as **permitted and honest**;
* `..._unrepairable_project_stays_bounded_and_honest` (line 619) required
  verification to be `PASSED` whenever the task was `COMPLETED`.

The second was passing only because its `if state == "completed"` guard never
fired for the model class it was written against, so **it was validating
nothing**. A qwen3:14b run reached `COMPLETED` on an unrepairable project, the
guard fired, and the stale assumption surfaced as a failure.

### The invariant, as ratified

```
TaskState       answers: did the request terminate, and how?
VerificationState answers: was the result independently checked?

COMPLETED asserts the first fact and never the second.
```

These are two independent facts and the architecture keeps them apart on
purpose. **A task can and does enter `COMPLETED` with a non-`PASSED` verdict.**
That is an honest outcome, not a false one. What is forbidden is the *opposite*
disagreement.

Allowed terminal pairs:

| `TaskState` | `VerificationState` | meaning |
|---|---|---|
| `COMPLETED` | `PASSED` | verified completion |
| `COMPLETED` | `UNVERIFIED` | finished, never independently checked |
| `COMPLETED` | `NOT_REQUIRED` | finished, nothing executed to verify |
| `COMPLETED` | `NOT_RUN` | finished before any verification phase; transient, closed by `synchronize_verification()` |
| `FAILED` | `FAILED` | the ordinary failure |
| `FAILED` | `UNVERIFIED` / `NOT_REQUIRED` / `NOT_RUN` | failed without an adjudicated verdict |
| `CANCELLED` | any | cancellation dominates; the verdict is invalidated and never load-bearing |

Forbidden, each for a safety reason rather than tidiness:

* **`COMPLETED` + `FAILED`** - reports a failure as a finished request. Already
  unreachable by construction, because `Agent._record_verification` transitions
  the task to `FAILED` on a `FAILED` verdict. Now also refused at the state
  machine, so a future writer cannot undo it.
* **`FAILED` + `PASSED`** - a passing verdict surviving an execution failure.
  This is the false pass the whole architecture exists to prevent.

### Answers to the questions the decision had to settle

1. **Can a task enter `COMPLETED` without successful verification?** Yes. That
   is the case where no independent verifier existed.
2. **Verification unavailable?** `UNVERIFIED` when steps executed, because work
   was attempted and not checked. `NOT_REQUIRED` when nothing executed, because
   there is nothing to verify. Both are existing states; no new one was added.
3. **Execution succeeded but verification failed?** `FAILED` verdict, and the
   task is `FAILED`. Never `COMPLETED`.
4. **Inherently unverifiable operation?** `NOT_REQUIRED`, carried in the ledger
   with `subject_kind=TASK`, `method_kind=EXECUTION_OUTCOME` and a detail
   saying why. Not a silent omission: the ledger always contains an entry
   explaining the final verdict.
5. **What the ledger contains?** At least one entry per terminal task whose
   `state` equals the authoritative `TaskManager.verification`. A `PASSED`
   entry additionally carries non-empty observed evidence, or
   `VerificationLedger.append` refuses it.
6. **What durable TaskStore state contains?** `TaskRecord.status` (the task
   lifecycle), `metadata["lifecycle"]["state"]`, `["verification"]` and the
   bounded `["verification_ledger"]` projection. Both the live task and a
   reloaded record are checked by the same `terminal_invariant_violation`, so a
   restart cannot present a pair the live path would have refused. A persisted
   `PASSED` that no longer satisfies the invariant is downgraded to
   `UNVERIFIED` on load rather than trusted.
7. **How RecoveryPolicy reads it?** It does not, and must not. The policy is
   step-scoped: it reads `FailureObservation.verification_state` only to decide
   `RERUN_VERIFY`, and only when the verdict is `FAILED` *with* evidence.
   `UNVERIFIED` is not mistaken for `FAILED`. Task terminality is not a
   recovery input, so no recovery decision can be driven by wording.
8. **How should a future `Agent.resume()` read it?** Through
   `terminal_verdict(state, verification)`, never by inferring from
   `status == "completed"`. A reloaded `COMPLETED` + `UNVERIFIED` means the work
   was never checked, so it must never be resumed as though it were done.
   `Agent.resume()` does not exist yet; this is a DEFERRED item with its rule
   now fixed.

### Single authority

`core/task_manager.py` is the only place the rule is written down:

* `terminal_verdict(state, verification) -> TerminalVerdict` - the structured
  classification. Pure and total, and tolerant of raw strings so a reloaded
  record is classified exactly as a live task is.
* `terminal_invariant_violation(state, verification) -> str` - `""` when
  admissible, else the reason. Reported rather than raised, so a corrupt
  persisted record cannot abort a task load.
* `TaskManager.terminal_violation()` / `assert_terminal_invariant()` -
  post-hoc forms, for a task built by other means rather than driven through
  `transition`.
* `TaskManager.transition()` refuses a terminal edge that would violate the
  invariant, so the forbidden pair cannot exist even transiently.
* `TaskManager.synchronize_verification()` settles a terminal `NOT_RUN`
  explicitly instead of leaving it implicit. Idempotent.
* `TaskManager.task_succeeded()` is unchanged in signature and behaviour: it is
  now the projection of `terminal_verdict(...).succeeded`.

`TerminalVerdict` adds no task state and no verification state. It exists so no
caller has to re-derive the conjunction itself, which is precisely how the two
tests came to disagree.

### Reporting success

```
True   COMPLETED and the ledger adjudicated PASSED with observed evidence
False  FAILED state, or an adjudicated FAILED verdict
None   finished (or cancelled) with no adjudicated verdict
```

`None` means *no verdict was reached*. It is not `False` and is never reported
as either. This is the semantics Phase 9's handover already described as settled
design and that the 14B run contradicted; it is now enforced rather than
asserted in two places.

---

## Verification

* S1 invariants: `tests/test_cross_layer_contracts.py`
* P3 Phase 1 ledger schema: `tests/test_verification_ledger.py`
* P3 Phase 2 writer migration: `tests/test_p3_verification_migration.py`
* P3 Phase 3 recovery: `tests/test_p3_recovery.py`
* Post-P3 composition audit: `tests/test_p3_composition_audit.py`
* Step-status structured-signal contract: `tests/test_step_status_contract.py`
* Terminal verdict invariant (Phase 10A): `tests/test_terminal_state_invariant.py`
* Pre-existing boundary coverage, unchanged:
  `tests/test_capability_provenance.py` (identity/provenance),
  `tests/test_capability_context_contract.py` (context propagation),
  `tests/test_s2_7_contract.py` (wire contract),
  `tests/test_request_context.py` (P1 request state)
