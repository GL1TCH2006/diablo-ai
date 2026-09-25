from __future__ import annotations

"""Basic technical validation of rendered image output.

This is *not* the S2.2 vision system. It only verifies that a render operation
produced a real, non-empty, readable image file (dimensions, format, bytes).
"""

from pathlib import Path

from core.logging_setup import get_logger

log = get_logger("blender.render_inspect")


def inspect_render(path: str | Path) -> dict:
    """Validate a rendered image file. Returns a JSON-safe dict, never raises."""
    target = Path(path)
    if not target.exists():
        return {
            "success": False,
            "path": str(target),
            "reason": "not_found",
            "size_bytes": None,
            "readable": False,
            "width": None,
            "height": None,
            "image_format": None,
            "mode": None,
        }
    if not target.is_file():
        return {
            "success": False,
            "path": str(target),
            "reason": "not_a_file",
            "size_bytes": None,
            "readable": False,
            "width": None,
            "height": None,
            "image_format": None,
            "mode": None,
        }

    size = target.stat().st_size
    result = {
        "success": size > 0,
        "path": str(target),
        "reason": None if size > 0 else "empty_file",
        "size_bytes": size,
        "readable": False,
        "width": None,
        "height": None,
        "image_format": None,
        "mode": None,
    }
    if size == 0:
        return result

    data: bytes | None = None
    try:
        from PIL import Image

        with Image.open(target) as img:
            result["readable"] = True
            result["width"], result["height"] = img.size
            result["mode"] = img.mode
            result["image_format"] = img.format
            # Image.open() is lazy and only parses the header, so a truncated
            # file still opens cleanly. verify() proves the container is
            # complete before the render is called readable.
            img.verify()
            return result
    except ImportError:
        pass  # Pillow not present; use the stdlib PNG fallback below
    except Exception as exc:  # noqa: BLE001 - fall back to PNG header parsing
        log.warning("render inspection: Pillow failed on %s: %s", target, exc)

    try:
        if data is None:
            data = target.read_bytes()
        if not _png_carries_image_data(data):
            raise ValueError("PNG contains no image data (missing IDAT/IEND)")
        width, height = _png_dimensions(data)
        result["readable"] = True
        result["width"] = width
        result["height"] = height
        result["image_format"] = "PNG"
        result["mode"] = None
        result["reason"] = None
    except Exception as exc:  # noqa: BLE001 - validation result, not a crash
        log.warning("render inspection: unreadable image %s: %s", target, exc)
        result["readable"] = False
        result["reason"] = f"unreadable_image: {exc}"
    return result


def _png_carries_image_data(data: bytes) -> bool:
    """Confirm a PNG holds actual image data, not just a header.

    A truncated or hand-crafted file can carry a valid signature and IHDR while
    containing no pixels at all. Header dimensions alone must never be trusted
    as proof of a readable render, so require at least one IDAT chunk and a
    terminating IEND chunk before accepting the stdlib fallback.
    """
    return b"IDAT" in data and b"IEND" in data


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Minimal stdlib PNG dimension read (signature + IHDR), no Pillow needed."""
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG container")
    if data[12:16] != b"IHDR":
        raise ValueError("PNG missing IHDR chunk")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height