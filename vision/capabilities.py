from __future__ import annotations

"""Vision capability detection and high-level API.

Provides a clean interface for the rest of Diablo to consume vision
functionality without knowing about backends directly.
"""

from pathlib import Path
from typing import Any

from core.logging_setup import get_logger

from vision.backends import registry
from vision.errors import (
    VisionBackendError,
    VisionError,
    VisionUnsupportedOperationError,
)
from vision.loader import load_image_info
from vision.protocol import VisionRequest, build_request, to_json_safe

log = get_logger("vision.capabilities")


class VisionCapabilityLayer:
    """High-level vision capabilities for Diablo.

    Composes backend registry with image loading and validation.
    Provides structured results for all operations.
    """

    def __init__(self, preferred_backend: str | None = None) -> None:
        self.preferred_backend = preferred_backend
        self._last_error: dict | None = None

    @property
    def available_backends(self) -> list[dict[str, Any]]:
        """List all backends with availability status."""
        return registry.list_all()

    def get_backend_info(self, name: str) -> dict[str, Any] | None:
        """Get info for a specific backend."""
        backend = registry.get_backend(name)
        if backend:
            return backend.get_info()
        return None

    def detect_capabilities(self) -> dict[str, Any]:
        """Return comprehensive capability report."""
        backends = registry.list_all()
        available = [b for b in backends if b["available"]]
        operations = set()
        for b in available:
            operations.update(b["supported_operations"])

        return {
            "vision_available": len(available) > 0,
            "backends": backends,
            "available_backends": [b["name"] for b in available],
            "supported_operations": sorted(operations),
            "preferred_backend": self.preferred_backend,
            "fallback_available": any(b["name"] == "fallback" for b in available),
        }

    def is_operation_supported(self, operation: str) -> bool:
        """Check if any available backend supports an operation."""
        return any(b.supports(operation) for b in registry.get_available_backends())

    def _select_backend(self, operation: str) -> Any:
        """Select the best backend for an operation."""
        if self.preferred_backend:
            backend = registry.get_backend(self.preferred_backend)
            if backend and backend.is_available() and backend.supports(operation):
                return backend
        return registry.get_best_backend(operation)

    def _run(self, operation: str, image_path: str, params: dict | None = None, context: dict | None = None) -> dict:
        """Execute a vision operation with error handling."""
        request = build_request(operation, image_path, params, context)
        validated_request = VisionRequest.from_dict(request)

        backend = self._select_backend(operation)
        if backend is None:
            available_ops = set()
            for b in registry.get_available_backends():
                available_ops.update(b.supported_operations)
            exc = VisionUnsupportedOperationError(
                operation=operation,
                message=f"no available backend supports '{operation}' (available: {sorted(available_ops)})",
                image_path=image_path,
                details={"available_operations": sorted(available_ops)},
            )
            self._last_error = exc.to_dict()
            raise exc

        try:
            result = backend.inference(validated_request)
        except VisionError as exc:
            self._last_error = exc.to_dict()
            raise
        except Exception as exc:
            wrapped = VisionBackendError(
                operation=operation,
                message=str(exc),
                image_path=image_path,
                backend=backend.name,
            )
            self._last_error = wrapped.to_dict()
            raise wrapped from exc

        if not result.success:
            self._last_error = result.error
            # Convert to appropriate exception
            error_code = result.error.get("code", "vision_backend_error")
            if error_code == "unsupported_operation":
                raise VisionUnsupportedOperationError(
                    operation=operation,
                    message=result.error.get("message", "unsupported"),
                    image_path=image_path,
                    backend=backend.name,
                )
            else:
                raise VisionError(
                    operation=operation,
                    message=result.error.get("message", "backend failed"),
                    image_path=image_path,
                    backend=backend.name,
                    code=error_code,
                    details=result.error.get("details", {}),
                )

        self._last_error = None
        return result.data if result.data is not None else {}

    def last_error(self) -> dict | None:
        """Most recent error as JSON-safe dict, or None after a success."""
        return self._last_error

    # ---------------------------------------------------- Public API

    def validate_image(self, image_path: str | Path) -> dict[str, Any]:
        """Validate an image file and return basic info (no backend needed)."""
        info = load_image_info(image_path)
        return to_json_safe(info)

    def inspect(self, image_path: str | Path) -> dict[str, Any]:
        """Full deterministic inspection (uses fallback backend directly)."""
        from vision.inspect import inspect_image
        return inspect_image(image_path)

    def describe(
        self,
        image_path: str | Path,
        detail_level: str = "medium",
        focus: str | None = None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Describe an image using the best available backend."""
        params = {"detail_level": detail_level}
        if focus:
            params["focus"] = focus
        return self._run("describe", str(image_path), params, context)

    def detect_objects(
        self,
        image_path: str | Path,
        confidence_threshold: float = 0.5,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Detect objects in an image (requires ML backend)."""
        params = {"confidence_threshold": confidence_threshold}
        return self._run("detect_objects", str(image_path), params, context)

    def compare(
        self,
        image_path: str | Path,
        reference_image: str | Path,
        threshold: float = 0.01,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Compare two images (deterministic, uses fallback)."""
        params = {"reference_image": str(reference_image), "threshold": threshold}
        return self._run("compare", str(image_path), params, context)

    def extract_text(
        self,
        image_path: str | Path,
        language: str | None = None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Extract text from an image (requires ML backend)."""
        params = {}
        if language:
            params["language"] = language
        return self._run("extract_text", str(image_path), params, context)

    def analyze_ui(
        self,
        image_path: str | Path,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Analyze UI elements in a screenshot (requires ML backend)."""
        return self._run("analyze_ui", str(image_path), {}, context)

    def inspect_render(
        self,
        image_path: str | Path,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Inspect a Blender render for technical validity."""
        return self._run("inspect_render", str(image_path), {}, context)

    def __del__(self) -> None:
        pass


def create_vision_layer(preferred_backend: str | None = None) -> VisionCapabilityLayer:
    """Factory function to create a vision capability layer."""
    return VisionCapabilityLayer(preferred_backend=preferred_backend)