from __future__ import annotations

"""Image loading utilities for vision subsystem.

Provides safe, validated image loading with format detection and basic
validation. Does not perform any inference.
"""

from pathlib import Path
from typing import Any

from core.logging_setup import get_logger

from .errors import VisionImageError

log = get_logger("vision.loader")

# Supported image formats (by extension)
SUPPORTED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp", ".gif"})

# Maximum image dimensions for safety (can be overridden)
DEFAULT_MAX_DIMENSION = 8192
DEFAULT_MAX_PIXELS = 50_000_000  # 50 megapixels


def validate_image_path(path: str | Path) -> Path:
    """Validate that an image path exists and has a supported extension."""
    target = Path(path)
    if not target.exists():
        raise VisionImageError(
            operation="load",
            message=f"image not found: {path}",
            image_path=str(target),
            code="not_found",
        )
    if not target.is_file():
        raise VisionImageError(
            operation="load",
            message=f"path is not a file: {path}",
            image_path=str(target),
            code="not_a_file",
        )
    if target.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise VisionImageError(
            operation="load",
            message=f"unsupported image format: {target.suffix}",
            image_path=str(target),
            code="unsupported_format",
            details={"supported": sorted(SUPPORTED_EXTENSIONS)},
        )
    return target


def load_image_info(path: str | Path, max_dimension: int = DEFAULT_MAX_DIMENSION) -> dict[str, Any]:
    """Load basic image information without decoding full pixel data.

    Uses Pillow if available, falls back to stdlib PNG header parsing.
    Returns a JSON-safe dict with metadata only.
    """
    target = validate_image_path(path)

    size = target.stat().st_size
    if size == 0:
        raise VisionImageError(
            operation="load",
            message="image file is empty",
            image_path=str(target),
            code="empty_file",
        )

    info: dict[str, Any] = {
        "path": str(target),
        "size_bytes": size,
        "extension": target.suffix.lower(),
        "readable": False,
        "width": None,
        "height": None,
        "mode": None,
        "format": None,
    }

    # Try Pillow first
    try:
        from PIL import Image  # type: ignore

        with Image.open(target) as img:
            info["readable"] = True
            info["width"], info["height"] = img.size
            info["mode"] = img.mode
            info["format"] = img.format
            # Image.open() is lazy and only parses the header, so a truncated
            # file still opens cleanly. verify() proves the container is
            # complete before the image is called readable.
            img.verify()
            # Check dimensions
            if info["width"] > max_dimension or info["height"] > max_dimension:
                raise VisionImageError(
                    operation="load",
                    message=f"image dimensions exceed maximum ({max_dimension}px)",
                    image_path=str(target),
                    code="dimension_exceeded",
                    details={"width": info["width"], "height": info["height"], "max": max_dimension},
                )

            total_pixels = info["width"] * info["height"]
            if total_pixels > DEFAULT_MAX_PIXELS:
                raise VisionImageError(
                    operation="load",
                    message=f"image exceeds maximum pixel count ({DEFAULT_MAX_PIXELS:,})",
                    image_path=str(target),
                    code="pixel_count_exceeded",
                    details={"pixels": total_pixels, "max": DEFAULT_MAX_PIXELS},
                )

            return info

    except VisionImageError:
        raise
    except ImportError:
        pass  # Pillow not present; use stdlib fallback
    except Exception as exc:
        log.warning("Pillow failed to read %s: %s", target, exc)

    # Stdlib PNG fallback
    try:
        if info["extension"] == ".png":
            raw = target.read_bytes()
            if not _png_carries_image_data(raw):
                raise ValueError("PNG contains no image data (missing IDAT/IEND)")
            width, height = _png_dimensions(raw)
            info["readable"] = True
            info["width"] = width
            info["height"] = height
            info["format"] = "PNG"
            info["mode"] = "RGBA"  # assumed for PNG
            return info
    except Exception as exc:
        log.warning("PNG header parse failed for %s: %s", target, exc)

    raise VisionImageError(
        operation="load",
        message="image could not be read (corrupt or unsupported)",
        image_path=str(target),
        code="unreadable",
    )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Minimal stdlib PNG dimension read (signature + IHDR), no Pillow needed."""
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG container")
    if data[12:16] != b"IHDR":
        raise ValueError("PNG missing IHDR chunk")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _png_carries_image_data(data: bytes) -> bool:
    """Confirm a PNG holds actual image data, not just a header.

    A truncated or hand-crafted file can carry a valid signature and IHDR while
    containing no pixels at all. Header dimensions alone must never be trusted
    as proof of a readable render, so require at least one IDAT chunk and a
    terminating IEND chunk before accepting the stdlib fallback.
    """
    return b"IDAT" in data and b"IEND" in data


def load_image_array(path: str | Path) -> Any:
    """Load image as numpy array (requires Pillow + numpy).

    Returns (array, info_dict) where array is HxWxC uint8.
    Raises VisionImageError if dependencies unavailable or image invalid.
    """
    target = validate_image_path(path)
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise VisionImageError(
            operation="load_array",
            message="Pillow and numpy required for array loading",
            image_path=str(target),
            code="missing_dependency",
            details={"missing": str(exc)},
        ) from exc

    with Image.open(target) as img:
        if img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGB")
        array = np.array(img)
        info = load_image_info(target)
        return array, info