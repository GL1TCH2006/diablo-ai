from __future__ import annotations

"""Vision backends package.

Registers built-in backends. Optional backends can be registered
by importing them and calling registry.register().
"""

from vision.backends.base import BackendRegistry, VisionBackend, registry
from vision.backends.fallback import FallbackBackend

# Register the fallback backend (always available, lowest priority)
registry.register(FallbackBackend(), priority=-100)

__all__ = [
    "VisionBackend",
    "BackendRegistry",
    "registry",
    "FallbackBackend",
]