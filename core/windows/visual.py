"""The visual-target contract: what a future Vision system will hand to M2.16.

M2.16.5. This module is a **data model and a safety contract, not a detector**.
There is no image analysis here, no model, no OCR, and no provider. What exists is
the shape a detector's output has to take before anything downstream is allowed to
treat it as a place on this machine - and the refusal that applies while the
detector that would produce one is not installed.

Why this exists now rather than with the Vision milestone
---------------------------------------------------------
The dangerous property of "click what the model saw" is not the vision; it is the
gap between *a number from an image* and *an authorised location on the desktop*.
That gap is where provenance is lost. By defining the target shape and the
revalidation rules while the only real producer is ``geometry``, the contract gets
tested against something that exists today, and a future detector inherits rules
that are already proven rather than rules written alongside it.

A visual target is **not** an actionable target
-----------------------------------------------
:func:`VisualTarget.revalidate` is the whole of that distinction. It answers
"is this still a coherent description of a place on this machine?" and never "you
may now click here". Producing an actionable point is the job of the existing
M2.16.2 pointer path, which requires an anchored target, re-resolves it and
verifies foreground. A ``VisualTarget`` is deliberately not accepted anywhere that
dispatches input, and :func:`assert_not_dispatchable` exists to make that a test
rather than a convention.

Sources are declared, not implied
---------------------------------
:data:`SOURCES` marks which producers exist. ``geometry`` and ``uia`` do.
``future_vision`` and ``future_ocr`` are **declared and unavailable**, so a
descriptor claiming to have come from a detector is rejected rather than quietly
trusted - the failure mode being that a placeholder name starts being treated as an
installed provider.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .coordinates import Rect, WindowPoint, resolve_point
from .errors import (
    WindowsInvalidGeometryError,
    WindowsInvalidRequestError,
    WindowsProviderUnavailableError,
    WindowsTargetStaleError,
    WindowsWindowIdentityMismatchError,
    WindowsWindowNotFoundError,
)
from .window_models import WindowIdentity

__all__ = ["VisualTarget", "SOURCES", "COORDS_CONFIDENCE_RANGE"]


#: Declared producers and whether one is installed *right now*.
#:
#: A name being present here is not a claim that it works. ``installed`` is the
#: only field that grants anything, and it is ``False`` for every future producer.
SOURCES: dict[str, dict[str, Any]] = {
    "geometry": {
        "installed": True,
        "kind": "deterministic",
        "description": "Computed from the display topology in M2.16.4.",
    },
    "uia": {
        "installed": True,
        "kind": "semantic",
        "description": "Reported by UI Automation in M2.16.5.",
    },
    "future_vision": {
        "installed": False,
        "kind": "model",
        "description": ("Reserved name for M2.18 Vision Foundation. Declared so "
                        "the shape is pinned early and explicitly NOT a working "
                        "provider."),
    },
    "future_ocr": {
        "installed": False,
        "kind": "model",
        "description": ("Reserved name for M2.18 OCR. Declared and unavailable."),
    },
    "unknown": {
        "installed": False,
        "kind": "unspecified",
        "description": "Unspecified provenance; never actionable.",
    },
}

#: Confidence is a unit interval. Out-of-range is refused rather than clamped,
#: because a 1.4 "confidence" means the producer is broken, and clamping hides that.
COORDS_CONFIDENCE_RANGE = (0.0, 1.0)


@dataclass(frozen=True)
class VisualTarget:
    """A claimed region on this desktop, with the provenance needed to trust it.

    Holds no image, no model output and no native handle. Everything here is a
    value that can be logged and re-checked later.
    """

    bounds: Rect
    source: str = "unknown"
    display_id: str = ""
    window_key: str = ""
    confidence: float | None = None
    target_id: str = ""
    captured_at: str = ""
    provenance: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.provenance is None:
            object.__setattr__(self, "provenance", {})
        if not self.target_id:
            object.__setattr__(self, "target_id", self._derive_id())
        if not self.captured_at:
            object.__setattr__(
                self, "captured_at",
                datetime.now(timezone.utc).isoformat())

    def _derive_id(self) -> str:
        """Deterministic, so the same claim twice is recognisably the same claim."""
        parts = [self.source, self.display_id, self.window_key,
                 str(self.bounds.left), str(self.bounds.top),
                 str(self.bounds.right), str(self.bounds.bottom)]
        return "vt-" + hashlib.sha256(
            "\x1f".join(parts).encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------ validation

    @classmethod
    def from_dict(cls, data: Any) -> "VisualTarget":
        if isinstance(data, VisualTarget):
            return data
        if not isinstance(data, dict):
            raise WindowsInvalidRequestError(
                "a visual target must be an object with bounds and a source.")
        bounds = Rect.from_dict(data.get("bounds"))
        if bounds is None:
            raise WindowsInvalidRequestError(
                "a visual target requires integer bounds "
                "{left, top, right, bottom}.")
        confidence = data.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise WindowsInvalidRequestError(
                    "confidence must be a number or absent.") from exc
            low, high = COORDS_CONFIDENCE_RANGE
            if not (low <= confidence <= high):
                raise WindowsInvalidRequestError(
                    f"confidence {confidence} is outside the unit interval "
                    f"[{low}, {high}]; a value outside it means the producer is "
                    "broken and is not clamped into range.",
                    diagnostics={"confidence": confidence})
        provenance = data.get("provenance")
        return cls(
            bounds=bounds,
            source=str(data.get("source") or "unknown"),
            display_id=str(data.get("display_id") or ""),
            window_key=str(data.get("window_key") or ""),
            confidence=confidence,
            target_id=str(data.get("target_id") or ""),
            captured_at=str(data.get("captured_at") or ""),
            provenance=dict(provenance) if isinstance(provenance, dict) else {},
        )

    def validate(self) -> dict[str, Any]:
        """Structural checks. Returns findings; raises on what cannot be judged.

        Split from :meth:`revalidate` on purpose: this answers "is this a
        well-formed claim" and needs no display state, so it can be tested without a
        desktop at all.
        """
        if self.source not in SOURCES:
            raise WindowsInvalidRequestError(
                f"unknown visual target source {self.source!r}; declared sources "
                f"are {sorted(SOURCES)}.",
                diagnostics={"source": self.source})
        if self.bounds.is_empty or not self.bounds.is_valid:
            raise WindowsInvalidGeometryError(
                f"the visual target bounds {self.bounds.to_dict()} describe no "
                "area. right and bottom are exclusive and are never clamped.",
                diagnostics={"bounds": self.bounds.to_dict()})
        if not self.display_id and not self.window_key:
            raise WindowsInvalidRequestError(
                "a visual target must carry either a display_id or a window_key; "
                "an unanchored region is a position, not a target.",
                diagnostics=self.to_dict())
        findings = {
            "source": self.source,
            "source_installed": bool(SOURCES[self.source]["installed"]),
            "source_kind": SOURCES[self.source]["kind"],
            "anchored": bool(self.display_id or self.window_key),
            "bounds_valid": True,
            "confidence": self.confidence,
            "exhaustive": True,
        }
        if not SOURCES[self.source]["installed"]:
            findings["blocking"] = (
                f"source {self.source!r} is a declared placeholder with no "
                "installed producer; the claim cannot be corroborated")
        return findings

    # ------------------------------------------------------------ revalidate

    def revalidate(self, display_layer: Any = None, window_layer: Any = None,
                   *, cancelled=None) -> dict[str, Any]:
        """Check the claim against *current* desktop state.

        Deliberately returns a verdict and never a coordinate. Turning a validated
        claim into a dispatchable point is the existing pointer layer's job, and it
        will re-resolve and re-check foreground at that moment. Keeping this method
        from producing a point is what makes "a visual target is not an actionable
        target" structural rather than a naming convention.
        """
        findings = self.validate()
        checks: dict[str, Any] = {"source": findings["source"],
                                  "source_installed": findings["source_installed"]}

        if self.display_id and display_layer is not None:
            try:
                info = display_layer.get_display({"display_id": self.display_id},
                                                 cancelled=cancelled)
                checks["display_resolved"] = True
                checks["display_id"] = info.display_id
                if not info.bounds.contains(self.bounds.left, self.bounds.top):
                    checks["within_display"] = False
                    raise WindowsInvalidGeometryError(
                        f"the claimed origin ({self.bounds.left}, "
                        f"{self.bounds.top}) is not inside display "
                        f"{self.display_id!r} {info.bounds.to_dict()}, so the "
                        "provenance cannot be corroborated.",
                        diagnostics={"bounds": self.bounds.to_dict(),
                                     "display_bounds": info.bounds.to_dict()})
                checks["within_display"] = True
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, WindowsInvalidGeometryError):
                    raise
                raise WindowsTargetStaleError(
                    f"the display this target was claimed on is no longer "
                    f"resolvable: {type(exc).__name__}.",
                    diagnostics={"display_id": self.display_id}) from exc
        elif self.display_id:
            checks["display_resolved"] = None
            checks["reason"] = "no display layer was supplied to check against"

        if self.window_key and window_layer is not None:
            try:
                # From the key, not from a ``{"window_key": ...}`` dict: the window
                # layer accepts a ``WindowIdentity`` or its full dict form, and a
                # key string is neither. Passing the dict form anyway made every
                # revalidation fail as "stale" for a window that was alive - an
                # unresolvable identity reported as a dead one.
                info = window_layer.get_window(
                    WindowIdentity.from_key(self.window_key),
                    cancelled=cancelled)
                checks["window_resolved"] = True
                # ``WindowInfo.rect`` is a plain tuple by design, so this is where
                # it becomes a rectangle. Calling ``.contains()`` on the tuple
                # raised ``AttributeError``, which the handler below reported as
                # "the window is no longer valid" - so every revalidation of a
                # perfectly live window failed, and looked like a stale target.
                window_rect = Rect(*info.rect)
                # The whole claim has to sit inside the window it is attributed
                # to. Checking only the origin would accept a rectangle that
                # starts in the window and runs off the edge of it.
                if not (window_rect.contains(self.bounds.left, self.bounds.top)
                        and window_rect.contains(self.bounds.right - 1,
                                                self.bounds.bottom - 1)):
                    checks["within_window"] = False
                    raise WindowsInvalidGeometryError(
                        f"the claim {self.bounds.to_dict()} is not wholly inside "
                        f"window {self.window_key!r} {list(info.rect)}, so the "
                        "target is not inside the window it was attributed to.",
                        diagnostics={"bounds": self.bounds.to_dict(),
                                     "window_rect": list(info.rect)})
                checks["within_window"] = True
            except WindowsInvalidGeometryError:
                raise
            except (WindowsInvalidRequestError, WindowsWindowNotFoundError,
                    WindowsWindowIdentityMismatchError) as exc:
                # Only a *window verification* failure means the window is gone.
                # Catching ``Exception`` here once turned an ``AttributeError``
                # from the code above into "the window this target belonged to is
                # no longer valid", which sent the investigation at the window
                # layer while the bug was three lines below it. A programming error
                # must not be reported as a stale target.
                raise WindowsTargetStaleError(
                    f"the window this target belonged to is no longer valid: "
                    f"{type(exc).__name__}.",
                    diagnostics={"window_key": self.window_key}) from exc
        elif self.window_key:
            checks["window_resolved"] = None
            checks["reason"] = "no window layer was supplied to check against"

        verdict = {
            "target_id": self.target_id,
            "verdict": "VALID" if findings["source_installed"] else "UNCORROBORATED",
            "actionable": False,
            "actionable_reason": (
                "a visual target is never actionable on its own; it must go "
                "through the anchored pointer path, which re-resolves the target "
                "and verifies foreground at dispatch time"),
            "checks": checks,
            "findings": findings,
            "provenance": dict(self.provenance),
            "captured_at": self.captured_at,
            "untrusted": True,
        }
        return verdict

    def to_anchored_point(self, window_layer: Any, *, cancelled=None,
                          prefer: str = "centre") -> dict[str, Any]:
        """Convert to a window-relative point **through the existing resolver**.

        Provided so the conversion a future Vision integration needs exists and is
        testable now. It requires an explicit window layer and goes through
        :func:`resolve_point`, the same anchoring rule M2.16.2 uses, so a visual
        claim cannot acquire authority by taking a different route.
        """
        if window_layer is None:
            raise WindowsInvalidRequestError(
                "converting a visual target requires a window layer so the result "
                "can be anchored and verified.")
        if not self.window_key:
            raise WindowsInvalidRequestError(
                "a visual target with no window_key cannot be anchored; use "
                "target_point on a UI element instead.")
        self.validate()
        info = window_layer.get_window(WindowIdentity.from_key(self.window_key),
                                       cancelled=cancelled)
        identity = info.identity
        left, top, right, bottom = (self.bounds.left, self.bounds.top,
                                    self.bounds.right, self.bounds.bottom)
        x = max(left, min(right - 1, (left + right) // 2))
        y = max(top, min(bottom - 1, (top + bottom) // 2))
        point = resolve_point(
            WindowPoint(identity=WindowIdentity.from_dict(identity.to_dict()),
                        relative_x=x - int(info.rect[0]),
                        relative_y=y - int(info.rect[1])),
            reader=window_layer.get_window, virtual_screen=None,
            require_foreground=False,
        )
        return {
            "target_id": self.target_id,
            "window_key": identity.key,
            "point": point.to_dict(),
            "anchored": point.verified_target,
            "provenance": f"visual:{self.source}",
            "visual_target_id": self.target_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "bounds": self.bounds.to_dict(),
            "source": self.source,
            "source_installed": bool(SOURCES.get(self.source, {}).get(
                "installed", False)),
            "display_id": self.display_id,
            "window_key": self.window_key,
            "confidence": self.confidence,
            "captured_at": self.captured_at,
            "provenance": dict(self.provenance or {}),
            "actionable": False,
        }

    def __repr__(self) -> str:                       # pragma: no cover - debug aid
        return (f"VisualTarget(target_id={self.target_id!r}, "
                f"source={self.source!r}, bounds={self.bounds.to_dict()})")


def assert_not_dispatchable(candidate: Any) -> None:
    """Raise if anything could hand ``candidate`` straight to an input verb.

    Used by tests to prove the safety boundary rather than assert it in prose: a
    ``VisualTarget`` must not be usable anywhere ``SendInput`` is reachable.
    """
    if isinstance(candidate, VisualTarget):
        raise WindowsInvalidRequestError(
            "a VisualTarget is not dispatchable; it must be revalidated and "
            "converted through the anchored pointer path first.")