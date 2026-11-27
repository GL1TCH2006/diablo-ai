from __future__ import annotations

"""Composition layer connecting S2.1 Blender and S2.2 Vision capabilities."""

import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from blender.capabilities import BlenderCapabilityLayer
from blender.errors import BlenderNotFoundError
from core.logging_setup import get_logger
from vision.capabilities import VisionCapabilityLayer
from vision.errors import (
    VisionBackendNotFoundError,
    VisionImageError,
    VisionUnsupportedOperationError,
)
from vision.protocol import to_json_safe

from .errors import (
    BlenderUnavailableError,
    BlenderVisionError,
    ComparisonError,
    InvalidRenderError,
    RenderFailureError,
    VisionBackendUnavailableError,
    VisionOperationUnsupportedError,
    VisionStageError,
)
from .protocol import (
    BlenderLayerProtocol,
    IntegrationResult,
    VisionLayerProtocol,
    elapsed_ms,
)

log = get_logger("integration.blender_vision")


class BlenderVisionIntegration:
    """Coordinate Blender rendering and Vision analysis without owning either layer's logic.

    The integration owns only orchestration, correlation IDs, stage boundaries, and
    normalized results. Callers retain direct access to the injected Blender and
    Vision layers through the ``blender`` and ``vision`` properties.
    """

    def __init__(
        self,
        blender: BlenderLayerProtocol | None = None,
        vision: VisionLayerProtocol | None = None,
        *,
        blender_exe: str | Path | None = None,
        work_dir: str | Path | None = None,
        blender_timeout: float = 120.0,
        vision_backend: str | None = None,
    ) -> None:
        self._owns_blender = blender is None
        self._blender = blender or BlenderCapabilityLayer(
            exe=blender_exe,
            work_dir=work_dir,
            timeout=blender_timeout,
        )
        self._vision = vision or VisionCapabilityLayer(preferred_backend=vision_backend)
        self._last_error: dict | None = None
        self._closed = False

    @property
    def blender(self) -> BlenderLayerProtocol:
        return self._blender

    @property
    def vision(self) -> VisionLayerProtocol:
        return self._vision

    @property
    def closed(self) -> bool:
        return self._closed

    def last_error(self) -> dict | None:
        return self._last_error

    def capabilities(self) -> dict[str, Any]:
        """Return non-invasive Blender and Vision capability information."""
        blender_info: Any = None
        detector = getattr(self._blender, "detect", None)
        if callable(detector):
            try:
                blender_info = detector()
            except Exception as exc:  # noqa: BLE001 - capability report is non-invasive
                blender_info = {"available": False, "error": str(exc)}
        else:
            blender_info = {"available": True, "reason": "detection method unavailable"}

        vision_info: Any = None
        detector = getattr(self._vision, "detect_capabilities", None)
        if callable(detector):
            try:
                vision_info = detector()
            except Exception as exc:  # noqa: BLE001 - capability report is non-invasive
                vision_info = {"vision_available": False, "error": str(exc)}
        else:
            vision_info = {"vision_available": None, "reason": "detection method unavailable"}

        if hasattr(blender_info, "__dataclass_fields__"):
            blender_info = asdict(blender_info)
        return to_json_safe(
            {
                "integration": "blender_vision",
                "blender": blender_info,
                "vision": vision_info,
            }
        )

    def _context(self, context: dict | None, operation: str, request_id: str) -> dict:
        """Build the context this operation hands to Vision.

        ``request_id`` is the integration's own per-call correlation id, produced
        by :meth:`_start` for the ``IntegrationResult`` envelope. It is published
        only as ``s2_internal_request_id``.

        The trusted request identity is owned by S1, which pins it onto
        ``CapabilityRequest.context`` before dispatch. S2 must never author or
        replace that value, so an incoming ``request_id`` is carried through
        untouched and the internal id is kept in its own field. ``run_id`` and
        ``task_id`` are likewise left exactly as the caller supplied them.
        """
        value = dict(context or {})
        value.setdefault("source", "blender_vision_integration")
        value["integration_operation"] = operation
        value["s2_internal_request_id"] = request_id
        return value

    def _start(self, operation: str) -> tuple[str, float]:
        return uuid.uuid4().hex, time.perf_counter()

    def _store_error(self, error: BlenderVisionError) -> None:
        self._last_error = error.to_dict()

    @staticmethod
    def _exception_payload(exc: BaseException) -> dict:
        if isinstance(exc, dict):
            return dict(exc)
        to_dict = getattr(exc, "to_dict", None)
        if callable(to_dict):
            try:
                value = to_dict()
            except Exception:  # noqa: BLE001 - preserve the original failure
                value = None
            if isinstance(value, dict):
                return dict(value)
        return {"error": type(exc).__name__, "message": str(exc)}

    @staticmethod
    def _layer_last_error(layer: Any) -> dict | None:
        getter = getattr(layer, "last_error", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - diagnostics must not mask the failure
            return None
        return dict(value) if isinstance(value, dict) else None

    def _raise_blender_error(
        self,
        exc: BaseException,
        operation: str,
        path: str | Path,
    ) -> None:
        source = self._exception_payload(exc)
        if isinstance(exc, BlenderNotFoundError):
            error_type = BlenderUnavailableError
            code = "blender_unavailable"
        else:
            error_type = RenderFailureError
            code = "blender_render_failed"
        error = error_type(
            operation=operation,
            message=source.get("message") or str(exc),
            stage="blender_render",
            source_error=source,
            source_last_error=self._layer_last_error(self._blender),
            details={"render_path": str(path)},
            cause=exc,
            code=code,
        )
        self._store_error(error)
        raise error from exc

    def _raise_invalid_render(
        self,
        operation: str,
        stage: str,
        path: str | Path,
        message: str,
        *,
        source_error: dict | None = None,
        details: dict | None = None,
        cause: BaseException | None = None,
    ) -> None:
        error = InvalidRenderError(
            operation=operation,
            message=message,
            stage=stage,
            source_error=source_error or {"error": "invalid_render", "path": str(path)},
            source_last_error=self._layer_last_error(self._vision),
            details={**(details or {}), "render_path": str(path)},
            cause=cause,
        )
        self._store_error(error)
        raise error from cause

    def _vision_error(
        self,
        operation: str,
        stage: str,
        source: dict,
        *,
        failure_type: type[VisionStageError],
        failure_code: str,
        cause: BaseException | None = None,
    ) -> BlenderVisionError:
        source_code = str(source.get("error") or source.get("code") or "")
        if source_code in {"vision_backend_not_found", "backend_not_found", "backend_unavailable"}:
            error_type = VisionBackendUnavailableError
            code = "vision_backend_unavailable"
        elif source_code in {"vision_unsupported_operation", "unsupported_operation"}:
            error_type = VisionOperationUnsupportedError
            code = "vision_operation_unsupported"
        elif source_code in {
            "vision_image_error",
            "not_found",
            "not_a_file",
            "unsupported_format",
            "empty_file",
            "unreadable",
            "dimension_exceeded",
            "pixel_count_exceeded",
        } or isinstance(cause, VisionImageError):
            error_type = InvalidRenderError
            code = "invalid_render"
        else:
            error_type = failure_type
            code = failure_code
        return error_type(
            operation=operation,
            message=source.get("message") or code,
            stage=stage,
            source_error=source,
            source_last_error=self._layer_last_error(self._vision),
            details={"source_operation": operation},
            cause=cause,
            code=code,
        )

    def _raise_vision_error(
        self,
        exc: BaseException,
        operation: str,
        stage: str,
        *,
        failure_type: type[VisionStageError],
        failure_code: str,
    ) -> None:
        source = self._exception_payload(exc)
        if isinstance(exc, VisionBackendNotFoundError):
            source.setdefault("error", "vision_backend_not_found")
        elif isinstance(exc, VisionUnsupportedOperationError):
            source.setdefault("error", "vision_unsupported_operation")
        elif isinstance(exc, VisionImageError):
            source.setdefault("error", exc.code)
        error = self._vision_error(
            operation,
            stage,
            source,
            failure_type=failure_type,
            failure_code=failure_code,
            cause=exc,
        )
        self._store_error(error)
        raise error from exc

    def _call_vision(
        self,
        operation: str,
        stage: str,
        callback: Callable[[], Any],
        *,
        failure_type: type[VisionStageError],
        failure_code: str,
    ) -> dict:
        try:
            value = callback()
        except Exception as exc:  # noqa: BLE001 - normalize backend boundary failures
            self._raise_vision_error(
                exc,
                operation,
                stage,
                failure_type=failure_type,
                failure_code=failure_code,
            )
        if not isinstance(value, dict):
            source = {"error": failure_code, "message": "Vision operation returned a non-object result"}
            error = self._vision_error(
                operation,
                stage,
                source,
                failure_type=failure_type,
                failure_code=failure_code,
            )
            self._store_error(error)
            raise error
        if value.get("success") is False:
            source = value.get("error")
            if not isinstance(source, dict):
                source = {"error": failure_code, "message": str(source or "Vision operation failed")}
            error = self._vision_error(
                operation,
                stage,
                source,
                failure_type=failure_type,
                failure_code=failure_code,
            )
            self._store_error(error)
            raise error
        return to_json_safe(value)

    def _render(
        self,
        path: str | Path,
        *,
        resolution_x: int | None,
        resolution_y: int | None,
        img_format: str,
        engine: str | None,
        use_gpu: bool | None,
    ) -> tuple[dict, Path]:
        if not path:
            self._raise_invalid_render("render", "render_validation", path, "render path is required")
        options = {
            "resolution_x": resolution_x,
            "resolution_y": resolution_y,
            "img_format": img_format or "PNG",
            "engine": engine,
            "use_gpu": use_gpu,
        }
        try:
            value = self._blender.render(str(path), **options)
        except Exception as exc:  # noqa: BLE001 - preserve the Blender stage boundary
            self._raise_blender_error(exc, "render", path)
        if not isinstance(value, dict):
            source = {"error": "blender_render_failed", "message": "Blender returned a non-object result"}
            error = RenderFailureError(
                operation="render",
                message=source["message"],
                stage="blender_render",
                source_error=source,
                source_last_error=self._layer_last_error(self._blender),
                details={"render_path": str(path)},
            )
            self._store_error(error)
            raise error
        candidate = value.get("path") or str(path)
        try:
            target = Path(candidate)
        except (TypeError, ValueError) as exc:
            self._raise_blender_error(exc, "render", path)
        if not target.is_file() or value.get("exists") is False:
            self._raise_invalid_render(
                "render",
                "render_validation",
                target,
                "Blender did not produce a render file",
                source_error={
                    "error": "render_file_missing",
                    "path": str(target),
                    "render_result": value,
                },
                details={"render_result": value},
            )
        return to_json_safe(value), target

    def _validate_image(self, path: Path, operation: str, request_id: str) -> dict:
        if not path.is_file():
            self._raise_invalid_render(
                operation,
                "vision_validate",
                path,
                "render file is missing",
                source_error={"error": "not_found", "path": str(path)},
            )
        validator = getattr(self._vision, "validate_image", None)
        if not callable(validator):
            return {"path": str(path), "readable": True}
        try:
            value = validator(path)
        except Exception as exc:  # noqa: BLE001 - classify image and backend failures
            self._raise_vision_error(
                exc,
                "validate_image",
                "vision_validate",
                failure_type=InvalidRenderError,
                failure_code="invalid_render",
            )
        if not isinstance(value, dict):
            source = {"error": "invalid_render", "message": "Vision image validation returned a non-object result"}
            error = InvalidRenderError(
                operation="validate_image",
                message=source["message"],
                stage="vision_validate",
                source_error=source,
                source_last_error=self._layer_last_error(self._vision),
                details={"render_path": str(path), "request_id": request_id},
            )
            self._store_error(error)
            raise error
        return to_json_safe(value)

    def _inspect_existing(
        self,
        path: Path,
        context: dict,
        request_id: str,
        operation: str,
    ) -> dict:
        image_info = self._validate_image(path, operation, request_id)
        inspection = self._call_vision(
            "inspect_render",
            "vision_inspect",
            lambda: self._vision.inspect_render(str(path), context=context),
            failure_type=VisionStageError,
            failure_code="vision_inspection_failed",
        )
        if inspection.get("valid") is not True:
            self._raise_invalid_render(
                operation,
                "vision_inspect",
                path,
                "render failed structural Vision inspection",
                source_error={
                    "error": "invalid_render",
                    "path": str(path),
                    "checks": inspection.get("checks", {}),
                },
                details={"inspection": inspection},
            )
        return {
            "render_path": str(path),
            "image": image_info,
            "inspection": inspection,
            "valid": True,
        }

    def _describe_existing(
        self,
        path: Path,
        context: dict,
        request_id: str,
        operation: str,
        detail_level: str,
        focus: str | None,
    ) -> dict:
        image_info = self._validate_image(path, operation, request_id)
        description = self._call_vision(
            "describe",
            "vision_describe",
            lambda: self._vision.describe(
                str(path),
                detail_level=detail_level,
                focus=focus,
                context=context,
            ),
            failure_type=VisionStageError,
            failure_code="vision_description_failed",
        )
        return {
            "render_path": str(path),
            "image": image_info,
            "description": description,
        }

    def _compare_existing(
        self,
        path: Path,
        reference_path: Path,
        context: dict,
        request_id: str,
        operation: str,
        threshold: float,
    ) -> dict:
        image_info = self._validate_image(path, operation, request_id)
        reference_info = self._validate_image(reference_path, operation, request_id)
        comparison = self._call_vision(
            "compare",
            "vision_compare",
            lambda: self._vision.compare(
                str(path),
                str(reference_path),
                threshold=threshold,
                context=context,
            ),
            failure_type=ComparisonError,
            failure_code="comparison_failed",
        )
        return {
            "render_path": str(path),
            "reference_path": str(reference_path),
            "image": image_info,
            "reference_image": reference_info,
            "comparison": comparison,
        }

    def _success(self, operation: str, data: dict, request_id: str, started: float) -> IntegrationResult:
        self._last_error = None
        return IntegrationResult.success_result(
            operation,
            data,
            request_id=request_id,
            duration_ms=elapsed_ms(started),
        )

    def render_and_inspect(
        self,
        path: str | Path,
        *,
        resolution_x: int | None = None,
        resolution_y: int | None = None,
        img_format: str = "PNG",
        engine: str | None = None,
        use_gpu: bool | None = None,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("render_and_inspect")
        operation = "render_and_inspect"
        render, target = self._render(
            path,
            resolution_x=resolution_x,
            resolution_y=resolution_y,
            img_format=img_format,
            engine=engine,
            use_gpu=use_gpu,
        )
        visual = self._inspect_existing(
            target,
            self._context(context, operation, request_id),
            request_id,
            operation,
        )
        return self._success(
            operation,
            {"render": render, **visual},
            request_id,
            started,
        )

    def render_and_describe(
        self,
        path: str | Path,
        *,
        resolution_x: int | None = None,
        resolution_y: int | None = None,
        img_format: str = "PNG",
        engine: str | None = None,
        use_gpu: bool | None = None,
        detail_level: str = "medium",
        focus: str | None = None,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("render_and_describe")
        operation = "render_and_describe"
        render, target = self._render(
            path,
            resolution_x=resolution_x,
            resolution_y=resolution_y,
            img_format=img_format,
            engine=engine,
            use_gpu=use_gpu,
        )
        visual = self._describe_existing(
            target,
            self._context(context, operation, request_id),
            request_id,
            operation,
            detail_level,
            focus,
        )
        return self._success(
            operation,
            {"render": render, **visual},
            request_id,
            started,
        )

    def render_and_compare(
        self,
        path: str | Path,
        reference_path: str | Path,
        *,
        resolution_x: int | None = None,
        resolution_y: int | None = None,
        img_format: str = "PNG",
        engine: str | None = None,
        use_gpu: bool | None = None,
        threshold: float = 0.01,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("render_and_compare")
        operation = "render_and_compare"
        if not reference_path:
            self._raise_invalid_render(
                operation,
                "vision_compare",
                reference_path,
                "reference image path is required",
                source_error={"error": "reference_missing"},
            )
        render, target = self._render(
            path,
            resolution_x=resolution_x,
            resolution_y=resolution_y,
            img_format=img_format,
            engine=engine,
            use_gpu=use_gpu,
        )
        visual = self._compare_existing(
            target,
            Path(reference_path),
            self._context(context, operation, request_id),
            request_id,
            operation,
            threshold,
        )
        return self._success(
            operation,
            {"render": render, **visual},
            request_id,
            started,
        )

    def inspect_render(
        self,
        path: str | Path,
        *,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("inspect_render")
        operation = "inspect_render"
        data = self._inspect_existing(
            Path(path),
            self._context(context, operation, request_id),
            request_id,
            operation,
        )
        return self._success(operation, data, request_id, started)

    def verify_render(
        self,
        path: str | Path,
        *,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("verify_render")
        operation = "verify_render"
        data = self._inspect_existing(
            Path(path),
            self._context(context, operation, request_id),
            request_id,
            operation,
        )
        return self._success(operation, data, request_id, started)

    def describe_render(
        self,
        path: str | Path,
        *,
        detail_level: str = "medium",
        focus: str | None = None,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("describe_render")
        operation = "describe_render"
        data = self._describe_existing(
            Path(path),
            self._context(context, operation, request_id),
            request_id,
            operation,
            detail_level,
            focus,
        )
        return self._success(operation, data, request_id, started)

    def compare_renders(
        self,
        path: str | Path,
        reference_path: str | Path,
        *,
        threshold: float = 0.01,
        context: dict | None = None,
    ) -> IntegrationResult:
        request_id, started = self._start("compare_renders")
        operation = "compare_renders"
        if not reference_path:
            self._raise_invalid_render(
                operation,
                "vision_compare",
                reference_path,
                "reference image path is required",
                source_error={"error": "reference_missing"},
            )
        data = self._compare_existing(
            Path(path),
            Path(reference_path),
            self._context(context, operation, request_id),
            request_id,
            operation,
            threshold,
        )
        return self._success(operation, data, request_id, started)

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._owns_blender:
                self._blender.close()
        finally:
            self._closed = True

    def __enter__(self) -> "BlenderVisionIntegration":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - destructor must not raise
            pass


def create_blender_vision_integration(
    blender: BlenderLayerProtocol | None = None,
    vision: VisionLayerProtocol | None = None,
    **kwargs: Any,
) -> BlenderVisionIntegration:
    """Construct a Blender–Vision composition layer."""
    return BlenderVisionIntegration(blender=blender, vision=vision, **kwargs)
