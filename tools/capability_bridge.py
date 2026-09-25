from __future__ import annotations

import json
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
        if data.get("verified") is True and self._valid_evidence(data.get("evidence")):
            return VerificationResult(
                VerificationState.PASSED,
                "Capability returned an explicit verification contract.",
                "capability_evidence",
                self._json_safe(data["evidence"]),
            )
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Capability completed without an independent S1 verifier.",
            "capability_execution",
            self._json_safe({"capability": capability, "operation": operation, "status": result.status.value, "evidence": data.get("evidence")}),
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
        return isinstance(value, (dict, list, tuple)) and bool(value)


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
