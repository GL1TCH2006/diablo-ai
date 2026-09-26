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

## Verification

* S1 invariants: `tests/test_cross_layer_contracts.py`
* Pre-existing boundary coverage, unchanged:
  `tests/test_capability_provenance.py` (identity/provenance),
  `tests/test_capability_context_contract.py` (context propagation),
  `tests/test_s2_7_contract.py` (wire contract),
  `tests/test_request_context.py` (P1 request state)
