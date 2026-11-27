from __future__ import annotations

"""Deterministic image inspection without ML models.

Provides structured visual metadata that can be computed directly from
image files: dimensions, format, color statistics, entropy, etc.
"""

from pathlib import Path
from typing import Any

from core.logging_setup import get_logger

from .loader import load_image_info
from .protocol import to_json_safe

log = get_logger("vision.inspect")


def inspect_image(path: str | Path) -> dict[str, Any]:
    """Comprehensive deterministic image inspection.

    Returns structured metadata that requires no ML model:
    - basic file info
    - dimensions, format, mode
    - color statistics (if Pillow available)
    - entropy/uniformity estimates
    - Blender render validation hints
    """
    info = load_image_info(path)

    result: dict[str, Any] = {
        "success": True,
        "image": {
            "path": info["path"],
            "size_bytes": info["size_bytes"],
            "format": info["format"],
            "mode": info["mode"],
            "width": info["width"],
            "height": info["height"],
            "aspect_ratio": round(info["width"] / info["height"], 3) if info["height"] else None,
            "megapixels": round((info["width"] * info["height"]) / 1_000_000, 2) if info["width"] and info["height"] else None,
        },
        "analysis": {},
        "render_hints": {},
    }

    # Deeper analysis if Pillow available
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore

        with Image.open(path) as img:
            # Convert to RGB for analysis
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            arr = np.array(img)

            # Basic color statistics
            if arr.ndim == 3:
                channels = arr.shape[2]
                result["analysis"]["channels"] = channels
                result["analysis"]["mean_color"] = [float(arr[:, :, c].mean()) for c in range(min(channels, 3))]
                result["analysis"]["std_color"] = [float(arr[:, :, c].std()) for c in range(min(channels, 3))]
                result["analysis"]["min_color"] = [int(arr[:, :, c].min()) for c in range(min(channels, 3))]
                result["analysis"]["max_color"] = [int(arr[:, :, c].max()) for c in range(min(channels, 3))]

                # Dominant colors (simple histogram)
                if channels >= 3:
                    result["analysis"]["dominant_colors"] = _dominant_colors(arr)

            # Entropy / complexity estimate
            result["analysis"]["entropy"] = _estimate_entropy(arr)

            # Uniformity check (solid color regions)
            result["analysis"]["uniformity"] = _check_uniformity(arr)

            # Edge density (simple gradient magnitude)
            result["analysis"]["edge_density"] = _edge_density(arr)

            # Blender render specific hints
            result["render_hints"] = _blender_render_hints(arr, info)

    except ImportError:
        result["analysis"]["note"] = "Pillow/numpy not available; limited analysis"
    except Exception as exc:
        log.warning("Deep inspection failed for %s: %s", path, exc)
        result["analysis"]["error"] = str(exc)

    return to_json_safe(result)


def _dominant_colors(arr: "np.ndarray", n: int = 5) -> list[dict[str, Any]]:
    """Simple dominant color extraction via histogram binning."""
    try:
        import numpy as np

        # Reshape to (pixels, channels)
        pixels = arr.reshape(-1, arr.shape[2])[:, :3]  # RGB only
        # Quantize to 32 levels per channel
        quantized = (pixels // 8).astype(np.uint8)
        # Pack to single integer for unique counting
        packed = (quantized[:, 0] << 10) | (quantized[:, 1] << 5) | quantized[:, 2]
        unique, counts = np.unique(packed, return_counts=True)
        top_idx = np.argsort(counts)[-n:][::-1]

        colors = []
        for idx in top_idx:
            val = unique[idx]
            r = (val >> 10) & 0x1F
            g = (val >> 5) & 0x1F
            b = val & 0x1F
            colors.append({
                "rgb": [int(r * 8), int(g * 8), int(b * 8)],
                "count": int(counts[idx]),
                "percentage": round(counts[idx] / len(pixels) * 100, 1),
            })
        return colors
    except Exception:
        return []


def _estimate_entropy(arr: "np.ndarray") -> float:
    """Estimate image entropy (complexity measure)."""
    try:
        import numpy as np

        # Use grayscale for entropy
        if arr.ndim == 3:
            gray = arr.mean(axis=2).astype(np.uint8)
        else:
            gray = arr.astype(np.uint8)

        hist, _ = np.histogram(gray, bins=256, range=(0, 255), density=True)
        hist = hist[hist > 0]
        entropy = float(-(hist * np.log2(hist)).sum())
        return round(entropy, 3)
    except Exception:
        return 0.0


def _check_uniformity(arr: "np.ndarray") -> dict[str, Any]:
    """Check for large uniform regions (solid colors)."""
    try:
        import numpy as np

        if arr.ndim == 3:
            # Check per channel
            uniform_channels = []
            for c in range(min(3, arr.shape[2])):
                channel = arr[:, :, c]
                unique_ratio = len(np.unique(channel)) / channel.size
                uniform_channels.append(round(unique_ratio, 4))
            return {
                "per_channel_unique_ratio": uniform_channels,
                "likely_solid": all(r < 0.01 for r in uniform_channels),
            }
        else:
            unique_ratio = len(np.unique(arr)) / arr.size
            return {
                "unique_ratio": round(unique_ratio, 4),
                "likely_solid": unique_ratio < 0.01,
            }
    except Exception:
        return {"error": "uniformity_check_failed"}


def _edge_density(arr: "np.ndarray") -> float:
    """Simple edge density via gradient magnitude."""
    try:
        import numpy as np

        if arr.ndim == 3:
            gray = arr.mean(axis=2)
        else:
            gray = arr.astype(np.float32)

        # Simple Sobel-like gradient
        gy, gx = np.gradient(gray)
        grad_mag = np.sqrt(gx**2 + gy**2)
        # Normalize by max possible gradient
        density = float(grad_mag.mean() / 255.0)
        return round(density, 4)
    except Exception:
        return 0.0


def _blender_render_hints(arr: "np.ndarray", info: dict[str, Any]) -> dict[str, Any]:
    """Heuristics to detect Blender render characteristics."""
    hints = {
        "likely_blender_render": False,
        "has_alpha": info.get("mode") in ("RGBA", "LA"),
        "transparent_background": False,
        "simple_scene": False,
    }

    try:
        import numpy as np

        if arr.ndim == 3 and arr.shape[2] >= 4:
            alpha = arr[:, :, 3]
            # Check if alpha has transparent regions
            transparent_pixels = np.sum(alpha < 128)
            total_pixels = alpha.size
            hints["transparent_background"] = bool(transparent_pixels > total_pixels * 0.1)
            hints["alpha_coverage"] = round(float(1 - transparent_pixels / total_pixels), 3)

        # Simple scene heuristic: low entropy + low edge density = simple
        entropy = _estimate_entropy(arr)
        edges = _edge_density(arr)
        hints["simple_scene"] = entropy < 4.0 and edges < 0.05

        # Blender renders often have specific characteristics
        hints["likely_blender_render"] = (
            hints["has_alpha"]
            or hints["simple_scene"]
            or (info.get("format") == "PNG" and info.get("mode") == "RGBA")
        )

    except Exception:
        pass

    return hints


def compare_images(path_a: str | Path, path_b: str | Path, threshold: float = 0.01) -> dict[str, Any]:
    """Deterministic image comparison (pixel-wise difference).

    Returns structured difference metrics without ML.
    Requires Pillow + numpy.

    ``diff_ratio`` is the fraction of **pixels** whose maximum per-channel
    deviation exceeds ``threshold``, so it is always in ``[0.0, 1.0]``.

    The previous implementation counted array *elements* above the threshold and
    divided by the pixel count. For an RGB image that made the numerator up to
    three times the denominator, so ``diff_ratio`` ranged to 3.0 while callers
    gated on ``diff_ratio <= threshold`` with a default of 0.01 - a threshold
    that was unreachable for any non-identical image. The metric is now defined
    per pixel, which is what a caller comparing two renders means.
    """
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return {
            "success": False,
            "error": "Pillow and numpy required for comparison",
            "code": "missing_dependency",
        }

    try:
        with Image.open(path_a) as img_a, Image.open(path_b) as img_b:
            if img_a.size != img_b.size:
                return {
                    "success": False,
                    "error": f"dimension mismatch: {img_a.size} vs {img_b.size}",
                    "code": "dimension_mismatch",
                }

            # Convert to same mode
            if img_a.mode != img_b.mode:
                img_a = img_a.convert("RGBA")
                img_b = img_b.convert("RGBA")

            arr_a = np.array(img_a, dtype=np.float32)
            arr_b = np.array(img_b, dtype=np.float32)

            # Normalize to 0-1
            arr_a /= 255.0
            arr_b /= 255.0

            diff = np.abs(arr_a - arr_b)
            max_diff = float(diff.max())
            mean_diff = float(diff.mean())
            rmse = float(np.sqrt((diff**2).mean()))

            # Collapse channels first, so one pixel counts once however many
            # channels it has. This is what makes the ratio dimensionless.
            per_pixel = diff.max(axis=2) if diff.ndim == 3 else diff
            diff_pixels = int(np.sum(per_pixel > threshold))
            total_pixels = int(per_pixel.size)
            diff_ratio = float(diff_pixels / total_pixels) if total_pixels else 0.0
            if diff_ratio > 1.0:  # unreachable by construction; asserted by test
                diff_ratio = 1.0

            return to_json_safe({
                "success": True,
                "identical": max_diff == 0.0,
                "max_pixel_diff": round(max_diff, 4),
                "mean_pixel_diff": round(mean_diff, 4),
                "rmse": round(rmse, 4),
                "channels": int(arr_a.shape[2]) if arr_a.ndim == 3 else 1,
                "pixels_above_threshold": diff_pixels,
                "total_pixels": total_pixels,
                "diff_ratio": round(diff_ratio, 4),
                "threshold": threshold,
                "similar": diff_ratio <= threshold,
                "dimensions": list(img_a.size),
            })

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "code": "comparison_failed",
        }
