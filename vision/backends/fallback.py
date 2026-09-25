from __future__ import annotations

"""Deterministic fallback vision backend.

Provides structured visual analysis without any ML model.
Always available, no dependencies beyond Pillow/numpy (optional).
"""

import time
from typing import Any

from vision.backends.base import VisionBackend
from vision.inspect import compare_images, inspect_image
from vision.protocol import VisionRequest, VisionResult


class FallbackBackend(VisionBackend):
    """Deterministic vision backend using image inspection heuristics.

    Supports:
    - describe: basic scene description from metadata
    - detect_objects: not supported (returns empty)
    - compare: deterministic pixel comparison
    - extract_text: not supported
    - analyze_ui: not supported
    - inspect_render: Blender render validation
    """

    name = "fallback"
    description = "Deterministic image inspection (no ML model)"
    supported_operations = frozenset({"describe", "compare", "inspect_render"})
    requires_gpu = False
    min_vram_mb = 0

    def check_availability(self) -> tuple[bool, str]:
        return True, "always available (no dependencies required)"

    def inference(self, request: VisionRequest) -> VisionResult:
        start = time.perf_counter()
        operation = request.operation

        try:
            if operation == "describe":
                data = self._describe(request)
            elif operation == "compare":
                data = self._compare(request)
            elif operation == "inspect_render":
                data = self._inspect_render(request)
            else:
                return VisionResult.failure_result(
                    operation=operation,
                    error={"code": "unsupported_operation", "message": f"fallback does not support {operation}"},
                    request_id=request.request_id,
                    backend=self.name,
                    duration_ms=(time.perf_counter() - start) * 1000,
                )

            return VisionResult.success_result(
                operation=operation,
                data=data,
                request_id=request.request_id,
                backend=self.name,
                duration_ms=(time.perf_counter() - start) * 1000,
            )

        except Exception as exc:
            return VisionResult.failure_result(
                operation=operation,
                error={"code": "backend_error", "message": str(exc)},
                request_id=request.request_id,
                backend=self.name,
                duration_ms=(time.perf_counter() - start) * 1000,
            )

    def _describe(self, request: VisionRequest) -> dict[str, Any]:
        """Generate a basic description from image metadata."""
        info = inspect_image(request.image_path)
        img = info["image"]
        analysis = info.get("analysis", {})
        render_hints = info.get("render_hints", {})

        # Build a structured description
        parts = []
        parts.append(f"{img['width']}x{img['height']} {img['format']} image")

        if img.get("megapixels"):
            parts.append(f"({img['megapixels']} MP)")

        if analysis.get("dominant_colors"):
            colors = analysis["dominant_colors"][:3]
            color_names = [f"rgb({c['rgb'][0]},{c['rgb'][1]},{c['rgb'][2]})" for c in colors]
            parts.append(f"dominant colors: {', '.join(color_names)}")

        if render_hints.get("transparent_background"):
            parts.append("has transparency")

        if render_hints.get("simple_scene"):
            parts.append("appears to be a simple scene")

        entropy = analysis.get("entropy", 0)
        if entropy > 6:
            parts.append("high detail/complexity")
        elif entropy < 3:
            parts.append("low detail/simple")

        description = "; ".join(parts)

        return {
            "description": description,
            "metadata": {
                "width": img["width"],
                "height": img["height"],
                "format": img["format"],
                "mode": img["mode"],
                "size_bytes": img["size_bytes"],
            },
            "analysis": analysis,
            "render_hints": render_hints,
        }

    def _compare(self, request: VisionRequest) -> dict[str, Any]:
        """Compare two images (requires reference_image in params)."""
        reference = request.params.get("reference_image")
        if not reference:
            raise ValueError("compare operation requires 'reference_image' in params")

        threshold = request.params.get("threshold", 0.01)
        result = compare_images(request.image_path, reference, threshold)

        if not result.get("success"):
            raise ValueError(result.get("error", "comparison failed"))

        return result

    def _inspect_render(self, request: VisionRequest) -> dict[str, Any]:
        """Inspect a Blender render for technical validity."""
        info = inspect_image(request.image_path)
        img = info["image"]
        render_hints = info.get("render_hints", {})

        # Validation checks (mandatory structural properties)
        checks = {
            "valid_format": img["format"] in ("PNG", "JPEG", "WEBP", "BMP", "TIFF"),
            "has_dimensions": img["width"] is not None and img["height"] is not None,
            "non_zero_size": img["size_bytes"] > 0,
            "readable": bool(info.get("success")),
            "reasonable_resolution": (img["width"] or 0) >= 64 and (img["height"] or 0) >= 64,
        }
        notes = {
            "has_alpha": render_hints.get("has_alpha", False),
            "transparent_background": render_hints.get("transparent_background", False),
            "likely_blender_render": render_hints.get("likely_blender_render", False),
        }

        passed = sum(1 for v in checks.values() if v)
        total = len(checks)

        return {
            "valid": passed == total,
            "checks": checks,
            "notes": notes,
            "passed": passed,
            "total": total,
            "image": {
                "width": img["width"],
                "height": img["height"],
                "format": img["format"],
                "mode": img["mode"],
                "size_bytes": img["size_bytes"],
            },
            "render_hints": render_hints,
        }