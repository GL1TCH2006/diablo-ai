from __future__ import annotations

"""Diablo Vision Intelligence Subsystem.

Provides modular visual understanding capabilities with pluggable backends
and a deterministic fallback that requires no ML models.
"""

from vision.capabilities import VisionCapabilityLayer, create_vision_layer
from vision.errors import (
    VisionBackendError,
    VisionBackendNotFoundError,
    VisionError,
    VisionImageError,
    VisionModelError,
    VisionUnsupportedOperationError,
    condense,
)
from vision.inspect import compare_images, inspect_image
from vision.loader import load_image_array, load_image_info, validate_image_path
from vision.protocol import (
    SUPPORTED_OPERATIONS,
    VisionRequest,
    VisionResult,
    build_request,
    parse_result,
    to_json_safe,
)

__all__ = [
    # Capabilities
    "VisionCapabilityLayer",
    "create_vision_layer",
    # Errors
    "VisionError",
    "VisionBackendNotFoundError",
    "VisionBackendError",
    "VisionImageError",
    "VisionModelError",
    "VisionUnsupportedOperationError",
    "condense",
    # Protocol
    "SUPPORTED_OPERATIONS",
    "VisionRequest",
    "VisionResult",
    "build_request",
    "parse_result",
    "to_json_safe",
    # Inspection (deterministic)
    "inspect_image",
    "compare_images",
    # Loading
    "load_image_info",
    "load_image_array",
    "validate_image_path",
]