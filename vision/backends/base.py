from __future__ import annotations

"""Base interface for vision backends.

All vision model backends must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Any

from vision.protocol import VisionRequest, VisionResult


class VisionBackend(ABC):
    """Abstract base class for vision backends.

    Backends encapsulate a specific model or inference engine.
    They are stateless and thread-safe (per-request).
    """

    # Unique identifier for this backend (e.g., "llava", "moondream", "fallback")
    name: str = "base"

    # Human-readable description
    description: str = "Base vision backend"

    # Operations this backend supports
    supported_operations: frozenset[str] = frozenset()

    # Whether this backend requires GPU
    requires_gpu: bool = False

    # Minimum VRAM required in MB (0 = unknown/no requirement)
    min_vram_mb: int = 0

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._available: bool | None = None

    @abstractmethod
    def check_availability(self) -> tuple[bool, str]:
        """Check if this backend is available.

        Returns:
            (available: bool, reason: str)
        """
        ...

    @abstractmethod
    def inference(self, request: VisionRequest) -> VisionResult:
        """Run vision inference for a single request.

        Args:
            request: Validated VisionRequest

        Returns:
            VisionResult with success=True/False
        """
        ...

    def is_available(self) -> bool:
        """Cached availability check."""
        if self._available is None:
            self._available, _ = self.check_availability()
        return self._available

    def supports(self, operation: str) -> bool:
        """Check if backend supports an operation."""
        return operation in self.supported_operations

    def get_info(self) -> dict[str, Any]:
        """Return backend metadata for capability reporting."""
        available, reason = self.check_availability()
        return {
            "name": self.name,
            "description": self.description,
            "available": available,
            "reason": reason,
            "supported_operations": sorted(self.supported_operations),
            "requires_gpu": self.requires_gpu,
            "min_vram_mb": self.min_vram_mb,
            "config": self.config,
        }


class BackendRegistry:
    """Registry of available vision backends.

    Maintains priority order for backend selection.
    """

    def __init__(self) -> None:
        self._backends: list[VisionBackend] = []

    def register(self, backend: VisionBackend, priority: int = 0) -> None:
        """Register a backend with optional priority (higher = preferred)."""
        self._backends.append((priority, backend))
        self._backends.sort(key=lambda x: -x[0])

    def get_backend(self, name: str) -> VisionBackend | None:
        """Get a specific backend by name."""
        for _, backend in self._backends:
            if backend.name == name:
                return backend
        return None

    def get_available_backends(self) -> list[VisionBackend]:
        """Return all available backends in priority order."""
        return [b for _, b in self._backends if b.is_available()]

    def get_best_backend(self, operation: str) -> VisionBackend | None:
        """Get the highest-priority available backend supporting an operation."""
        for _, backend in self._backends:
            if backend.is_available() and backend.supports(operation):
                return backend
        return None

    def list_all(self) -> list[dict[str, Any]]:
        """List all registered backends with availability info."""
        return [b.get_info() for _, b in self._backends]


# Global registry instance
registry = BackendRegistry()