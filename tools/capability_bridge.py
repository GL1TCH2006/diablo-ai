from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from config.permissions import PermissionLevel, path_is_within
from core.verification import VerificationResult, VerificationState
from integration import (
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CoreEventBusPublisher,
)
from tools.registry import ToolExecutionContext, ToolResult, ToolSpec

_MUTATING_WORDS = (
    "create",
    "delete",
    "modify",
    "transform",
    "assign",
    "configure",
    "save",
    "export",
    "render",
    "execute_script",
)
_READ_ONLY_WORDS = (
    "inspect",
    "list",
    "validate",
    "describe",
    "detect",
    "compare",
    "verify",
)
_PATH_KEYS = ("path", "image_path", "reference_image", "reference_path", "render_path")

# Context keys Core owns. Anything a model supplies under these names is
# untrusted and is dropped before the request reaches the capability layer.
UNTRUSTED_PROVENANCE_KEYS = frozenset({"source", "run_id", "task_id", "request_id"})


def trusted_model_context(embedded: Any) -> dict:
    """Return model-supplied context with Core-owned provenance keys removed.

    MODEL CONTEXT      -> untrusted
    CORE REQUEST IDENTITY -> trusted
    """
    if not isinstance(embedded, dict):
        return {}
    return {key: value for key, value in embedded.items()
            if key not in UNTRUSTED_PROVENANCE_KEYS}


# ---------------------------------------------------------------------------
# Context dispatch contract.
#
# Trusted provenance lives on CapabilityRequest.context and is delivered by the
# capability adapters that support it. Model-supplied context travels separately
# in params["context"] and may only be forwarded to operations whose dispatch
# contract actually declares a ``context`` parameter; otherwise the layer raises
# TypeError (or, where a layer takes ``**extra``, silently swallows the payload).
#
# This is a statement of the cross-layer interface, not of S2 internals.
# ---------------------------------------------------------------------------

# Capabilities on which no operation declares a context parameter. The S2.1
# Blender layer takes no context on any operation and the plain layer adapter
# forwards params verbatim, so a context kwarg is always unexpected there.
CONTEXT_FREE_CAPABILITIES = frozenset({"blender"})

# Individual operations on otherwise context-aware capabilities that declare no
# context parameter.
CONTEXT_FREE_OPERATIONS = frozenset({
    ("vision", "validate_image"),
    ("vision", "inspect"),
})


def operation_accepts_context(capability: str, operation: str) -> bool:
    """Whether the dispatch contract for this operation accepts ``context``."""
    if str(capability) in CONTEXT_FREE_CAPABILITIES:
        return False
    return (str(capability), str(operation)) not in CONTEXT_FREE_OPERATIONS


def dispatch_params(capability: str, operation: str, params: dict) -> dict:
    """Return operation params with the model context payload applied per contract.

    Unrelated params are never altered. When the operation does not accept a
    context payload the key is removed entirely rather than forwarded blindly.
    """
    if "context" not in params:
        return params
    if not isinstance(params.get("context"), dict):
        return params
    if operation_accepts_context(capability, operation):
        params["context"] = trusted_model_context(params["context"])
    else:
        params.pop("context", None)
    return params


# ═══════════════════════════════════════════════════ M2.16 window verification
#
# Verification for the window layer follows one rule: a native call returning
# without error is not evidence that anything happened. `ShowWindow` returns
# before the window has moved, `SetForegroundWindow` can be refused by Windows
# while reporting success, and `PostMessage(WM_CLOSE)` only queues a message -
# the receiving application decides when, or whether, to close. Each verifier
# therefore checks the specific observable property its verb was asked to change,
# and a `PASSED` requires that property to hold in the state the layer re-read
# *after* the call.


def _window_evidence(bridge: "CapabilityBridge", common: dict, data: dict,
                     window: dict | None, extra: dict | None = None) -> dict:
    payload = {
        **common,
        "window_key": data.get("window_key"),
        "hwnd": data.get("hwnd") or (window or {}).get("hwnd"),
        "pid": (window or {}).get("pid"),
        "identity_created_at": (window or {}).get("identity_created_at"),
        "class_name": (window or {}).get("class_name"),
        "requested": data.get("requested") is True,
        "confirmed": data.get("confirmed") is True,
    }
    if extra:
        payload.update(extra)
    return bridge._json_safe(payload)


def _window_read_verification(self: "CapabilityBridge", common: dict,
                              data: dict, operation: str) -> VerificationResult:
    """Structural validity of a window read.

    A window row is only well-formed if it carries a *usable identity*: a positive
    handle, a class name, and - critically - a process creation time. Without the
    creation time the row cannot be verified before it is acted on, so treating it
    as valid would hand the model a window reference that is unsafe to quote back.
    """
    rows = data.get("windows")
    if operation == "get_window":
        rows = [data] if data.get("hwnd") is not None else []
    if not isinstance(rows, list):
        rows = []

    def _usable(row: object) -> bool:
        if not isinstance(row, dict):
            return False
        try:
            if int(row.get("hwnd") or 0) <= 0:
                return False
            if int(row.get("pid") or -1) < 0:
                return False
        except (TypeError, ValueError):
            return False
        return bool(row.get("identity_created_at")) and bool(row.get("class_name"))

    well_formed = all(_usable(row) for row in rows) and bool(rows)
    evidence = self._json_safe({
        **common,
        "count": data.get("count", len(rows)),
        "returned": len(rows),
        "anchored": sum(1 for row in rows if isinstance(row, dict)
                        and row.get("anchored")),
        "well_formed": well_formed,
    })
    return VerificationResult(
        VerificationState.PASSED if well_formed else VerificationState.UNVERIFIED,
        "Window metadata was structurally valid and every window carried a "
        "process-anchored identity."
        if well_formed else
        "No well-formed, process-anchored window metadata was returned.",
        "tool_observation",
        evidence,
    )


def _verify_window_focus(self: "CapabilityBridge", common: dict,
                         data: dict) -> VerificationResult:
    """Focus passes only when the window is actually the foreground window.

    Windows may refuse activation for a process that is not already foreground.
    That refusal is not a failure of the request - the user is in another
    application - so it is reported as not-confirmed with the observed foreground
    window named, rather than as a pass and not as an error.
    """
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    foreground = window.get("foreground") is True
    evidence = _window_evidence(self, common, data, window,
                                {"foreground_observed": foreground})
    if not data.get("requested"):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The focus request was refused by Windows; no activation occurred.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED if foreground else VerificationState.FAILED,
        "The target window became the foreground window."
        if foreground else
        "Activation was requested but the target window is not the foreground "
        "window. Windows commonly refuses activation while the user is "
        "interacting with another application.",
        "capability_execution", evidence,
    )


def _verify_window_minimize(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """Minimize passes only when the re-read window reports minimized."""
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    minimized = window.get("minimized") is True
    evidence = _window_evidence(self, common, data, window,
                                {"minimized_observed": minimized})
    return VerificationResult(
        VerificationState.PASSED if minimized else VerificationState.FAILED,
        "The target window is minimized."
        if minimized else
        "The minimize was requested but the window is not minimized.",
        "capability_execution", evidence,
    )


def _verify_window_restore(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """Restore passes only when the window is no longer minimized and visible."""
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    visible = window.get("visible") is True
    not_minimized = window.get("minimized") is False
    evidence = _window_evidence(self, common, data, window, {
        "visible_observed": visible, "minimized_observed": not_minimized,
    })
    return VerificationResult(
        VerificationState.PASSED if (visible and not_minimized)
        else VerificationState.FAILED,
        "The target window is visible and not minimized."
        if (visible and not_minimized) else
        "The restore was requested but the window is still minimized or hidden.",
        "capability_execution", evidence,
    )


def _verify_window_close(self: "CapabilityBridge", common: dict,
                         data: dict) -> VerificationResult:
    """Close passes only when the window is gone.

    ``PostMessage(WM_CLOSE)`` merely queues a message. An application can defer
    it, prompt the user, or ignore it, and the handle can survive while the window
    is no longer visible - which is not closure. So this checks disappearance,
    not visibility.
    """
    confirmed = data.get("confirmed") is True
    requested = data.get("requested") is True
    window = data.get("window") if isinstance(data.get("window"), dict) else None
    evidence = _window_evidence(self, common, data, window, {"window_present": window is not None})
    if not requested:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "No close message was posted; nothing to verify.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED if confirmed else VerificationState.FAILED,
        "The window was destroyed."
        if confirmed else
        "A close request was posted but the window still exists. The "
        "application may have deferred or refused the close.",
        "capability_execution", evidence,
    )


_WindowLifecycleVerifier = Callable[["CapabilityBridge", dict, dict], VerificationResult]

#: Dispatch table, so the verifier for a verb is chosen by declared membership
#: rather than by an ``if`` chain that could drift from
#: :data:`_MUTATING_OPERATIONS`.
_WINDOW_LIFECYCLE_VERIFIERS: dict[str, _WindowLifecycleVerifier] = {
    "window_focus": _verify_window_focus,
    "window_minimize": _verify_window_minimize,
    "window_restore": _verify_window_restore,
    "window_close": _verify_window_close,
}


class CapabilityBridge:
    def __init__(
        self,
        registry: CapabilityRegistry,
        events: Any | None = None,
        allowed_roots: list[Path] | tuple[Path, ...] = (),
    ) -> None:
        self._registry = registry
        self._events = events
        self._allowed_roots = tuple(Path(root) for root in allowed_roots)

    def validate_args(self, args: dict) -> None:
        if not isinstance(args, dict):
            raise ValueError("capability arguments must be an object")
        extra = set(args) - {"capability", "operation", "params"}
        if extra:
            raise ValueError(f"unsupported capability arguments: {', '.join(sorted(extra))}")
        capability_name = args.get("capability")
        operation = args.get("operation")
        if not isinstance(capability_name, str) or not capability_name.strip():
            raise ValueError("capability must be a non-empty string")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-empty string")
        if not isinstance(args.get("params", {}), dict):
            raise ValueError("params must be an object")
        capability = self._registry.get(capability_name)
        if capability is None:
            raise ValueError(f"unknown capability: {capability_name}")
        if not capability.can_execute(operation):
            raise ValueError(f"unsupported operation: {capability_name}.{operation}")

    def permission_level(self, args: dict) -> PermissionLevel:
        capability_name = str(args.get("capability") or "")
        operation = str(args.get("operation") or "")
        capability = self._registry.get(capability_name)
        if capability is None:
            return PermissionLevel.BLOCKED
        if operation == "execute_script":
            return PermissionLevel.BLOCKED
        # M2.15: an adapter that declares which of its operations mutate state is
        # authoritative for that decision. The native Windows layer is the first:
        # `terminate` must not be SAFE just because its name does not contain a
        # mutating keyword, and a query must not be SENSITIVE just because it
        # lives beside one.
        is_mutating = getattr(capability, "is_mutating", None)
        if callable(is_mutating):
            try:
                if is_mutating(operation):
                    return PermissionLevel.SENSITIVE
            except Exception:  # noqa: BLE001 - a bad probe must not widen access
                return PermissionLevel.SENSITIVE
            if self._has_external_path(args.get("params") or {}):
                return PermissionLevel.SENSITIVE
            return PermissionLevel.SAFE
        if capability_name == "mock" and operation == "execute":
            return PermissionLevel.SENSITIVE if self._has_external_path(args.get("params") or {}) else PermissionLevel.SAFE
        if operation.startswith(_READ_ONLY_WORDS):
            if self._has_external_path(args.get("params") or {}):
                return PermissionLevel.SENSITIVE
            return PermissionLevel.SAFE
        if any(word in operation for word in _MUTATING_WORDS):
            return PermissionLevel.SENSITIVE
        return PermissionLevel.SENSITIVE

    def describe(self, args: dict) -> str:
        return f"Execute capability {args.get('capability', '')}.{args.get('operation', '')}"

    def execute(
        self,
        args: dict,
        _tool_context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate_args(args)
        context = _tool_context or ToolExecutionContext("", None, None)
        capability_name = str(args["capability"])
        operation = str(args["operation"])
        params = dict(args.get("params") or {})
        if isinstance(params.get("context"), dict):
            params = dispatch_params(capability_name, operation, params)
        request = CapabilityRequest(
            capability=capability_name,
            operation=operation,
            params=params,
            context={
                "source": "s1_core",
                "run_id": context.run_id,
                "task_id": context.task_id or "",
            },
            task_id=context.task_id or "",
            cancellation_token=context.cancellation_token,
        )
        # The registry owns CapabilityRequest.request_id; publish that same
        # trusted value so no downstream consumer can read a model-supplied one.
        request.context["request_id"] = request.request_id
        publisher = CoreEventBusPublisher(self._events) if self._events is not None else None
        result = self._registry.execute(request, event_publisher=publisher)
        payload = self._json_safe(result.to_dict())
        error = result.error or {}
        message = str(error.get("message") or error.get("error") or f"Capability returned {result.status.value}.")
        output = json.dumps(payload, ensure_ascii=False, default=str)
        verification = self._verification(result, request)
        return ToolResult(
            success=result.success,
            output=output,
            error="" if result.success else message,
            data={"capability_result": payload},
            cancelled=result.cancelled,
            verification=verification,
        )

    def _has_external_path(self, params: dict) -> bool:
        if not self._allowed_roots:
            return False
        for key in _PATH_KEYS:
            value = params.get(key)
            if value in (None, ""):
                continue
            try:
                path = Path(str(value))
            except (TypeError, ValueError):
                return True
            if not path_is_within(path, list(self._allowed_roots)):
                return True
        return False

    @staticmethod
    def _json_safe(value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _result_path(result: CapabilityResult, request: CapabilityRequest) -> str:
        data = result.data or {}
        render = data.get("render") if isinstance(data.get("render"), dict) else {}
        return str(
            render.get("path")
            or data.get("render_path")
            or data.get("path")
            or request.params.get("path")
            or ""
        )

    def _verification(
        self,
        result: CapabilityResult,
        request: CapabilityRequest,
    ) -> VerificationResult | None:
        if not result.success:
            return None
        data = result.data or {}
        operation = result.operation
        capability = result.capability
        if capability == "blender_vision_workflow":
            if operation in {"run", "render_and_verify", "compare_reference"}:
                return self._workflow_verification(result, request, data, operation)
            if operation == "analyze_reference":
                return VerificationResult(
                    VerificationState.UNVERIFIED,
                    "Reference analysis completed without render verification.",
                    "capability_analysis",
                    self._json_safe({
                        "reference_path": data.get("reference_path"),
                        "backend": data.get("backend"),
                        "evidence": data.get("evidence"),
                    }),
                )
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Workflow operation completed without an independent render verification contract.",
                "capability_execution",
                self._json_safe({"capability": capability, "operation": operation, "data": data}),
            )
        if capability == "blender" and operation == "render":
            path = self._result_path(result, request)
            exists = bool(path and Path(path).is_file())
            evidence = self._json_safe({"path": path, "exists": exists})
            if data.get("valid") is False:
                return VerificationResult(VerificationState.FAILED, "Blender reported an invalid render.", "render_file", evidence)
            return VerificationResult(
                VerificationState.PASSED if exists else VerificationState.FAILED,
                "Render file was created and exists." if exists else "Blender reported success but the render file is missing.",
                "render_file",
                evidence,
            )
        if capability in {"blender", "vision"} and operation in {"inspect_render", "validate_image"}:
            path = self._result_path(result, request)
            valid = data.get("valid") is True or data.get("readable") is True
            exists = bool(path and Path(path).is_file())
            passed = valid and exists
            evidence = self._json_safe({"path": path, "valid": valid, "exists": exists})
            return VerificationResult(
                VerificationState.PASSED if passed else VerificationState.FAILED,
                "Image inspection evidence was returned." if passed else "Image inspection did not provide a readable file.",
                "image_inspection",
                evidence,
            )
        if capability == "windows":
            return self._windows_verification(result, request, data, operation)
        if capability == "blender_vision" and operation in {
            "render_and_inspect",
            "render_and_describe",
            "render_and_compare",
            "inspect_render",
            "verify_render",
        }:
            path = self._result_path(result, request)
            inspection = data.get("inspection") if isinstance(data.get("inspection"), dict) else {}
            exists = bool(path and Path(path).is_file())
            valid_present = "valid" in data
            valid = data.get("valid") is True
            inspection_present = "inspection" in data
            inspection_valid = inspection.get("valid") is True
            requires_inspection = operation in {"render_and_inspect", "inspect_render", "verify_render"}
            evidence = self._json_safe({
                "path": path,
                "exists": exists,
                "valid": data.get("valid"),
                "inspection": inspection,
            })
            if data.get("valid") is False or (inspection_present and inspection.get("valid") is False):
                return VerificationResult(
                    VerificationState.FAILED,
                    "Blender-Vision reported invalid render evidence.",
                    "blender_vision_render",
                    evidence,
                )
            if not exists:
                return VerificationResult(
                    VerificationState.FAILED,
                    "Blender-Vision did not provide a readable render file.",
                    "blender_vision_render",
                    evidence,
                )
            if not valid_present or not valid or (requires_inspection and (not inspection_present or not inspection_valid)):
                return VerificationResult(
                    VerificationState.UNVERIFIED,
                    "Blender-Vision did not provide positive verification evidence.",
                    "blender_vision_render",
                    evidence,
                )
            return VerificationResult(
                VerificationState.PASSED,
                "Blender-Vision evidence was returned.",
                "blender_vision_render",
                evidence,
            )
        if data.get("verified") is False:
            return VerificationResult(
                VerificationState.FAILED,
                "Capability explicitly reported failed verification.",
                "capability_evidence",
                self._json_safe({"evidence": data.get("evidence")}),
            )
        raw_evidence = data.get("evidence")
        if data.get("verified") is True and not isinstance(raw_evidence, Mapping) and raw_evidence is not None:
            # M2.12 P3: the evidence contract is object-shaped by design
            # (integration/workflows.py builds dicts and rejects a callback that
            # supplies evidence without a boolean verdict). A bare list/tuple/
            # string is malformed, not an alternate valid shape, so it is not
            # promoted to PASSED. The payload is still preserved for diagnosis.
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Capability reported a malformed evidence contract; evidence must be an object.",
                "capability_execution",
                self._json_safe({
                    "capability": capability,
                    "operation": operation,
                    "malformed_evidence": raw_evidence,
                }),
            )
        if data.get("verified") is True and self._valid_evidence(raw_evidence):
            return VerificationResult(
                VerificationState.PASSED,
                "Capability returned an explicit verification contract.",
                "capability_evidence",
                self._json_safe(raw_evidence),
            )
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Capability completed without an independent S1 verifier.",
            "capability_execution",
            self._json_safe({"capability": capability, "operation": operation, "status": result.status.value, "evidence": data.get("evidence")}),
        )

    def _windows_verification(
        self,
        result: CapabilityResult,
        request: CapabilityRequest,
        data: dict,
        operation: str,
    ) -> VerificationResult:
        """Verification evidence for the M2.15 native Windows layer.

        The rule that matters: a termination that was **requested** but not
        **observed** can never be a ``PASSED``. The layer reports the two as
        separate fields precisely so this function can refuse to conflate them,
        and the ledger's invariant then refuses anything without observed
        evidence anyway.
        """
        request_id = str(request.request_id or "")
        common = {"capability": "windows", "operation": operation,
                  "request_id": request_id}

        if operation == "terminate":
            requested = data.get("requested") is True
            confirmed = data.get("confirmed") is True
            evidence = self._json_safe({
                **common,
                "pid": data.get("pid"),
                "requested": requested,
                "confirmed": confirmed,
                "exit_code": data.get("exit_code"),
                "identity_created_at": data.get("identity_created_at"),
            })
            if requested and confirmed:
                return VerificationResult(
                    VerificationState.PASSED,
                    "Termination was requested and the process exit was observed.",
                    "capability_execution",
                    evidence,
                )
            if requested and not confirmed:
                return VerificationResult(
                    VerificationState.FAILED,
                    "Termination was requested but the exit was not observed; "
                    "this is not a completed termination.",
                    "capability_execution",
                    evidence,
                )
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Termination was not requested; nothing to verify.",
                "capability_execution",
                evidence,
            )

        if operation == "system_info":
            memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
            cpu = data.get("cpu") if isinstance(data.get("cpu"), dict) else {}
            total = float(memory.get("total_physical_mb") or 0.0)
            available = float(memory.get("available_physical_mb") or 0.0)
            # Internally consistent: available cannot exceed total, and a
            # machine reporting zero physical memory is not reporting a system.
            consistent = bool(total > 0.0 and 0.0 <= available <= total
                              and data.get("os_caption"))
            evidence = self._json_safe({
                **common,
                "os_caption": data.get("os_caption"),
                "backend": data.get("backend"),
                "total_physical_mb": total,
                "available_physical_mb": available,
                "logical_cores": cpu.get("logical_cores"),
            })
            return VerificationResult(
                VerificationState.PASSED if consistent else VerificationState.FAILED,
                "Native system snapshot was internally consistent."
                if consistent else
                "Native system snapshot was missing or inconsistent fields.",
                "tool_observation",
                evidence,
            )

        # M2.16 window reads: structural validity of the returned rows.
        if operation in ("list_windows", "get_window"):
            return _window_read_verification(self, common, data, operation)

        # M2.16 window lifecycle mutations. Verification is of *observed state*,
        # never of an API return code: `ShowWindow` returns before the window has
        # moved, and `SetForegroundWindow` can be refused outright while still
        # reporting success. Each verb therefore checks the specific property it
        # was asked to change.
        if operation in _WINDOW_LIFECYCLE_VERIFIERS:
            return _WINDOW_LIFECYCLE_VERIFIERS[operation](self, common, data)

        # list / get / find: structural validity of the returned rows.
        rows = data.get("processes")
        if operation == "get":
            rows = [data] if data.get("pid") is not None else []
        if not isinstance(rows, list):
            rows = []
        well_formed = all(
            isinstance(row, dict) and isinstance(row.get("pid"), int) and row.get("pid") > 0
            for row in rows
        )
        evidence = self._json_safe({
            **common,
            "count": data.get("count", len(rows)),
            "returned": len(rows),
            "well_formed": well_formed,
        })
        return VerificationResult(
            VerificationState.PASSED if well_formed and rows else VerificationState.UNVERIFIED,
            "Process metadata was structurally valid."
            if well_formed and rows else
            "No well-formed process metadata was returned.",
            "tool_observation",
            evidence,
        )

    def _workflow_verification(
        self,
        result: CapabilityResult,
        request: CapabilityRequest,
        data: dict,
        operation: str,
    ) -> VerificationResult:
        path = self._result_path(result, request)
        evidence = {
            "path": path,
            "reference_path": data.get("reference_path"),
            "verified": data.get("verified"),
            "attempts": data.get("attempts"),
            "iterations": data.get("iterations"),
            "render": data.get("render"),
            "state": data.get("state"),
            "comparison": data.get("comparison"),
        }
        if data.get("verified") is False:
            return VerificationResult(
                VerificationState.FAILED,
                "Workflow reported failed verification.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        if data.get("verified") is not True:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Workflow did not return an explicit verification result.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        attempts = data.get("attempts")
        iterations = data.get("iterations")
        render = data.get("render")
        comparison = data.get("comparison")
        if operation == "compare_reference":
            valid_shape = bool(path) and isinstance(comparison, dict) and bool(comparison)
            if valid_shape:
                try:
                    threshold = float(data.get("threshold", request.params.get("threshold", 0.01)))
                except (TypeError, ValueError):
                    threshold = -1.0
                diff_ratio = comparison.get("diff_ratio")
                positive = (
                    comparison.get("success") is not False
                    and (
                        comparison.get("identical") is True
                        or (isinstance(diff_ratio, (int, float)) and not isinstance(diff_ratio, bool) and 0.0 <= threshold <= 1.0 and diff_ratio <= threshold)
                        or (comparison.get("identical") is None and diff_ratio is None and comparison.get("success") is True)
                    )
                )
                valid_shape = valid_shape and positive
        else:
            valid_shape = bool(path) and isinstance(attempts, list) and bool(attempts)
            valid_shape = valid_shape and isinstance(iterations, int) and not isinstance(iterations, bool) and iterations >= 1
            valid_shape = valid_shape and isinstance(render, dict) and bool(render)
            valid_shape = valid_shape and attempts[-1].get("valid") is True
            valid_shape = valid_shape and render.get("valid") is True
        if not valid_shape:
            return VerificationResult(
                VerificationState.FAILED,
                "Workflow verification evidence is malformed.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        if not Path(path).is_file():
            return VerificationResult(
                VerificationState.FAILED,
                "Workflow reported verification but the render path is missing.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        if operation == "compare_reference":
            reference_path = str(data.get("reference_path") or "")
            if not reference_path or not Path(reference_path).is_file():
                return VerificationResult(
                    VerificationState.FAILED,
                    "Workflow comparison evidence is missing its reference file.",
                    "workflow_verification",
                    self._json_safe(evidence),
                )
        return VerificationResult(
            VerificationState.PASSED,
            "Workflow returned valid deterministic verification evidence.",
            "workflow_verification",
            self._json_safe(evidence),
        )

    @staticmethod
    def _valid_evidence(value: Any) -> bool:
        # Mapping-only since M2.12 P3. The evidence contract is object-shaped;
        # a bare sequence carries no defined semantics and cannot be persisted
        # as `verification_evidence`. Kept as a method so the contract stays in
        # one place.
        return isinstance(value, Mapping) and bool(value)


def build_capability_tool(
    registry: CapabilityRegistry,
    events: Any | None = None,
    allowed_roots: list[Path] | tuple[Path, ...] = (),
) -> ToolSpec:
    bridge = CapabilityBridge(registry, events, allowed_roots)

    def description():
        reports = registry.capabilities()
        available = []
        for report in reports:
            name = str(report.get("name") or "")
            operations = ", ".join(str(value) for value in report.get("operations") or [])
            if name:
                available.append(f"{name}({operations})")
        suffix = "; ".join(available) if available else "none"
        return (
            "Execute one registered capability through the Core Agent. "
            "The capability and operation are selected explicitly and permission is decided by Core. "
            f"Registered capabilities: {suffix}."
        )

    def execute_tool(**kwargs):
        tool_context = kwargs.pop("_tool_context", None)
        return bridge.execute(kwargs, tool_context)

    return ToolSpec(
        name="capability.execute",
        description=description(),
        # M2.13.0: this one tool reaches Blender, Vision and the S2.8 workflows
        # without being named after any of them, so its capability is declared
        # explicitly rather than derived from the "capability" namespace.
        capabilities=("capability", "blender", "vision"),
        parameters={
            "type": "object",
            "properties": {
                "capability": {"type": "string"},
                "operation": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["capability", "operation"],
            "additionalProperties": False,
        },
        required=["capability", "operation"],
        level=PermissionLevel.NORMAL,
        level_for=bridge.permission_level,
        fn=execute_tool,
        describe=bridge.describe,
        wants_context=True,
        validate_args=bridge.validate_args,
        persist_data=True,
    )
