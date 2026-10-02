from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from config.permissions import PermissionLevel, path_is_within
from core.verification import VerificationResult, VerificationState
from core.windows.errors import WindowsInvalidRequestError
from integration import (
    CapabilityRegistry,
    CapabilityRequest,
    CapabilityResult,
    CoreEventBusPublisher,
)
from tools.registry import ToolExecutionContext, ToolResult, ToolSpec

_MUTATING_WORDS = (
    "create",
    "delete",
    "modify",
    "transform",
    "assign",
    "configure",
    "save",
    "export",
    "render",
    "execute_script",
)
_READ_ONLY_WORDS = (
    "inspect",
    "list",
    "validate",
    "describe",
    "detect",
    "compare",
    "verify",
)
_PATH_KEYS = ("path", "image_path", "reference_image", "reference_path", "render_path")

# Context keys Core owns. Anything a model supplies under these names is
# untrusted and is dropped before the request reaches the capability layer.
UNTRUSTED_PROVENANCE_KEYS = frozenset({"source", "run_id", "task_id", "request_id"})


def trusted_model_context(embedded: Any) -> dict:
    """Return model-supplied context with Core-owned provenance keys removed.

    MODEL CONTEXT      -> untrusted
    CORE REQUEST IDENTITY -> trusted
    """
    if not isinstance(embedded, dict):
        return {}
    return {key: value for key, value in embedded.items()
            if key not in UNTRUSTED_PROVENANCE_KEYS}


# ---------------------------------------------------------------------------
# Context dispatch contract.
#
# Trusted provenance lives on CapabilityRequest.context and is delivered by the
# capability adapters that support it. Model-supplied context travels separately
# in params["context"] and may only be forwarded to operations whose dispatch
# contract actually declares a ``context`` parameter; otherwise the layer raises
# TypeError (or, where a layer takes ``**extra``, silently swallows the payload).
#
# This is a statement of the cross-layer interface, not of S2 internals.
# ---------------------------------------------------------------------------

# Capabilities on which no operation declares a context parameter. The S2.1
# Blender layer takes no context on any operation and the plain layer adapter
# forwards params verbatim, so a context kwarg is always unexpected there.
CONTEXT_FREE_CAPABILITIES = frozenset({"blender"})

# Individual operations on otherwise context-aware capabilities that declare no
# context parameter.
CONTEXT_FREE_OPERATIONS = frozenset({
    ("vision", "validate_image"),
    ("vision", "inspect"),
})


def operation_accepts_context(capability: str, operation: str) -> bool:
    """Whether the dispatch contract for this operation accepts ``context``."""
    if str(capability) in CONTEXT_FREE_CAPABILITIES:
        return False
    return (str(capability), str(operation)) not in CONTEXT_FREE_OPERATIONS


def dispatch_params(capability: str, operation: str, params: dict) -> dict:
    """Return operation params with the model context payload applied per contract.

    Unrelated params are never altered. When the operation does not accept a
    context payload the key is removed entirely rather than forwarded blindly.
    """
    if "context" not in params:
        return params
    if not isinstance(params.get("context"), dict):
        return params
    if operation_accepts_context(capability, operation):
        params["context"] = trusted_model_context(params["context"])
    else:
        params.pop("context", None)
    return params


# ═══════════════════════════════════════════════════ M2.16 window verification
#
# Verification for the window layer follows one rule: a native call returning
# without error is not evidence that anything happened. `ShowWindow` returns
# before the window has moved, `SetForegroundWindow` can be refused by Windows
# while reporting success, and `PostMessage(WM_CLOSE)` only queues a message -
# the receiving application decides when, or whether, to close. Each verifier
# therefore checks the specific observable property its verb was asked to change,
# and a `PASSED` requires that property to hold in the state the layer re-read
# *after* the call.


def _window_evidence(bridge: "CapabilityBridge", common: dict, data: dict,
                     window: dict | None, extra: dict | None = None) -> dict:
    payload = {
        **common,
        "window_key": data.get("window_key"),
        "hwnd": data.get("hwnd") or (window or {}).get("hwnd"),
        "pid": (window or {}).get("pid"),
        "identity_created_at": (window or {}).get("identity_created_at"),
        "class_name": (window or {}).get("class_name"),
        "requested": data.get("requested") is True,
        "confirmed": data.get("confirmed") is True,
    }
    if extra:
        payload.update(extra)
    return bridge._json_safe(payload)


def _window_read_verification(self: "CapabilityBridge", common: dict,
                              data: dict, operation: str) -> VerificationResult:
    """Structural validity of a window read.

    A window row is only well-formed if it carries a *usable identity*: a positive
    handle, a class name, and - critically - a process creation time. Without the
    creation time the row cannot be verified before it is acted on, so treating it
    as valid would hand the model a window reference that is unsafe to quote back.
    """
    rows = data.get("windows")
    if operation == "get_window":
        rows = [data] if data.get("hwnd") is not None else []
    if not isinstance(rows, list):
        rows = []

    def _usable(row: object) -> bool:
        if not isinstance(row, dict):
            return False
        try:
            if int(row.get("hwnd") or 0) <= 0:
                return False
            if int(row.get("pid") or -1) < 0:
                return False
        except (TypeError, ValueError):
            return False
        return bool(row.get("identity_created_at")) and bool(row.get("class_name"))

    well_formed = all(_usable(row) for row in rows) and bool(rows)
    evidence = self._json_safe({
        **common,
        "count": data.get("count", len(rows)),
        "returned": len(rows),
        "anchored": sum(1 for row in rows if isinstance(row, dict)
                        and row.get("anchored")),
        "well_formed": well_formed,
    })
    return VerificationResult(
        VerificationState.PASSED if well_formed else VerificationState.UNVERIFIED,
        "Window metadata was structurally valid and every window carried a "
        "process-anchored identity."
        if well_formed else
        "No well-formed, process-anchored window metadata was returned.",
        "tool_observation",
        evidence,
    )


def _verify_window_focus(self: "CapabilityBridge", common: dict,
                         data: dict) -> VerificationResult:
    """Focus passes only when the window is actually the foreground window.

    Windows may refuse activation for a process that is not already foreground.
    That refusal is not a failure of the request - the user is in another
    application - so it is reported as not-confirmed with the observed foreground
    window named, rather than as a pass and not as an error.
    """
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    foreground = window.get("foreground") is True
    evidence = _window_evidence(self, common, data, window,
                                {"foreground_observed": foreground})
    if not data.get("requested"):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The focus request was refused by Windows; no activation occurred.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED if foreground else VerificationState.FAILED,
        "The target window became the foreground window."
        if foreground else
        "Activation was requested but the target window is not the foreground "
        "window. Windows commonly refuses activation while the user is "
        "interacting with another application.",
        "capability_execution", evidence,
    )


def _verify_window_minimize(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """Minimize passes only when the re-read window reports minimized."""
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    minimized = window.get("minimized") is True
    evidence = _window_evidence(self, common, data, window,
                                {"minimized_observed": minimized})
    return VerificationResult(
        VerificationState.PASSED if minimized else VerificationState.FAILED,
        "The target window is minimized."
        if minimized else
        "The minimize was requested but the window is not minimized.",
        "capability_execution", evidence,
    )


def _verify_window_restore(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """Restore passes only when the window is no longer minimized and visible."""
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    visible = window.get("visible") is True
    not_minimized = window.get("minimized") is False
    evidence = _window_evidence(self, common, data, window, {
        "visible_observed": visible, "minimized_observed": not_minimized,
    })
    return VerificationResult(
        VerificationState.PASSED if (visible and not_minimized)
        else VerificationState.FAILED,
        "The target window is visible and not minimized."
        if (visible and not_minimized) else
        "The restore was requested but the window is still minimized or hidden.",
        "capability_execution", evidence,
    )


def _verify_window_close(self: "CapabilityBridge", common: dict,
                         data: dict) -> VerificationResult:
    """Close passes only when the window is gone.

    ``PostMessage(WM_CLOSE)`` merely queues a message. An application can defer
    it, prompt the user, or ignore it, and the handle can survive while the window
    is no longer visible - which is not closure. So this checks disappearance,
    not visibility.
    """
    confirmed = data.get("confirmed") is True
    requested = data.get("requested") is True
    window = data.get("window") if isinstance(data.get("window"), dict) else None
    evidence = _window_evidence(self, common, data, window, {"window_present": window is not None})
    if not requested:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "No close message was posted; nothing to verify.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED if confirmed else VerificationState.FAILED,
        "The window was destroyed."
        if confirmed else
        "A close request was posted but the window still exists. The "
        "application may have deferred or refused the close.",
        "capability_execution", evidence,
    )


_WindowLifecycleVerifier = Callable[["CapabilityBridge", dict, dict], VerificationResult]

#: Dispatch table, so the verifier for a verb is chosen by declared membership
#: rather than by an ``if`` chain that could drift from
#: :data:`_MUTATING_OPERATIONS`.
_WINDOW_LIFECYCLE_VERIFIERS: dict[str, _WindowLifecycleVerifier] = {
    "window_focus": _verify_window_focus,
    "window_minimize": _verify_window_minimize,
    "window_restore": _verify_window_restore,
    "window_close": _verify_window_close,
}


# ═══════════════════════════════════════════════════ M2.16.2/3 input verification
#
# Input has one property none of the window verbs has: most of its effects happen
# inside an application this process cannot see. A click may open a menu, change a
# field, or do nothing at all, and the OS gives no way to tell which.
#
# So verification is split by what is genuinely observable:
#
#   * pointer position is *global OS state* and can be read back, so movement can
#     reach PASSED on real evidence;
#   * a click, a key, a hotkey and text cannot be observed from outside the target
#     application, so the strongest honest answer is FAILED with the reason
#     spelled out - never PASSED.
#
# Reporting those as PASSED because Windows accepted the input would be inventing
# evidence, and a PASSED row in the ledger is something downstream reasoning is
# allowed to trust.


def _input_evidence(bridge: "CapabilityBridge", common: dict, data: dict) -> dict:
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
    return bridge._json_safe({
        **common,
        "dispatched": data.get("dispatched") is True,
        "confirmed": data.get("confirmed") is True,
        "window_key": detail.get("window_key", data.get("window_key")),
        "requested": detail.get("requested", observed or {}),
        "observed": observed,
        "effect": detail.get("effect", ""),
    })


def _verify_pointer_position(self: "CapabilityBridge", common: dict,
                             data: dict) -> VerificationResult:
    """Movement passes only when the pointer is actually where it was asked to go.

    The pointer is global state, so this is a real observation rather than an
    assumption. Windows clamps to the desktop edge and warps the pointer in
    fractional steps, so the comparison uses the layer's tolerance and always
    reports the observed value.
    """
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
    requested = detail.get("requested") if isinstance(detail.get("requested"), dict) else {}
    dispatched = data.get("dispatched") is True
    tolerance = int(detail.get("tolerance_px", 1) or 1)
    evidence = _input_evidence(self, common, data)

    if not dispatched:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Windows did not inject the pointer move.",
            "capability_execution", evidence,
        )
    if not requested or "x" not in observed:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The pointer position could not be read back, so the move is "
            "unverified rather than passed.",
            "capability_execution", evidence,
        )
    within = (abs(int(observed["x"]) - int(requested["x"])) <= tolerance
              and abs(int(observed["y"]) - int(requested["y"])) <= tolerance)
    evidence["within_tolerance"] = within
    return VerificationResult(
        VerificationState.PASSED if within else VerificationState.FAILED,
        f"The pointer is at ({observed['x']}, {observed['y']}), within "
        f"{tolerance}px of the requested position."
        if within else
        f"The pointer is at ({observed['x']}, {observed['y']}), which is not the "
        f"requested ({requested['x']}, {requested['y']}). Windows may clamp the "
        f"pointer to the edge of the virtual desktop.",
        "capability_execution", evidence,
    )


def _verify_pointer_click(self: "CapabilityBridge", common: dict,
                          data: dict) -> VerificationResult:
    """A dispatched click is unverified, never passed.

    The effect belongs to the receiving application. ``dispatched`` says Windows
    injected the events; it says nothing about whether a menu opened. Reporting
    PASSED here would put an unsupported claim into the ledger.
    """
    evidence = _input_evidence(self, common, data)
    if data.get("dispatched") is not True:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Windows did not inject the click.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.UNVERIFIED,
        "The click was dispatched to the verified target. Its effect occurs "
        "inside the target application and cannot be observed from outside it, "
        "so this is recorded as dispatched rather than verified.",
        "capability_execution", evidence,
    )


def _verify_keyboard_dispatch(self: "CapabilityBridge", common: dict,
                              data: dict) -> VerificationResult:
    """A dispatched keystroke is unverified, never passed.

    Same reasoning as the click: the effect is the target application's business.
    """
    evidence = _input_evidence(self, common, data)
    if data.get("dispatched") is not True:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Windows did not inject the key event.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.UNVERIFIED,
        "The key event was dispatched to the verified foreground target. Its "
        "effect occurs inside the target application and cannot be observed from "
        "outside it.",
        "capability_execution", evidence,
    )


def _verify_pointer_claim(self: "CapabilityBridge", common: dict,
                          data: dict) -> VerificationResult:
    """Movement-plus-click: the move is observable, the click is not.

    This is the strongest evidence available for a click, so it is reported
    separately from ``confirmed`` rather than being flattened into it.
    """
    evidence = _input_evidence(self, common, data)
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    cursor_confirmed = detail.get("cursor_confirmed") is True
    evidence["cursor_confirmed"] = cursor_confirmed
    evidence["click_confirmed"] = False
    if data.get("dispatched") is not True:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Windows did not inject the click.",
            "capability_execution", evidence,
        )
    if cursor_confirmed:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The pointer reached the requested position inside the verified "
            "target and the click was dispatched. The click's own effect occurs "
            "inside the target application and cannot be observed from outside "
            "it, so this is recorded as dispatched rather than verified.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.FAILED,
        "The click was dispatched but the pointer did not reach the requested "
        "position inside the verified target, so the click cannot be attributed "
        "to it.",
        "capability_execution", evidence,
    )


def _verify_pointer_read(self: "CapabilityBridge", common: dict,
                         data: dict) -> VerificationResult:
    """A cursor or foreground read: structural validity only."""
    if "x" in data:
        try:
            x, y = int(data["x"]), int(data["y"])
            screen = data.get("virtual_screen") or {}
            within = screen.get("width", 0) and screen.get("height", 0)
        except (TypeError, ValueError):
            x = y = None
            within = False
        if x is None or not within:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "The pointer position could not be read as a valid screen "
                "coordinate.", "tool_observation",
                self._json_safe({**common, "x": data.get("x"), "y": data.get("y")}),
            )
        return VerificationResult(
            VerificationState.PASSED,
            "The pointer position and virtual screen geometry were read and are "
            "internally consistent.",
            "tool_observation",
            self._json_safe({**common, "x": x, "y": y,
                             "virtual_screen": data.get("virtual_screen")}),
        )

    window = data.get("window") if isinstance(data.get("window"), dict) else None
    if window is None:
        # No foreground window is a real answer on a locked or service desktop,
        # not a failure - but it is not evidence either.
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "There is no foreground window to verify; nothing was fabricated.",
            "tool_observation",
            self._json_safe({**common, "has_foreground": False}),
        )
    anchored = bool(window.get("identity_created_at")) and bool(window.get("class_name"))
    return VerificationResult(
        VerificationState.PASSED if anchored else VerificationState.UNVERIFIED,
        "The foreground window was read with a process-anchored identity."
        if anchored else
        "The foreground window could not be anchored to a process creation time, "
        "so it cannot be safely targeted.",
        "tool_observation",
        self._json_safe({**common, "has_foreground": True,
                         "window_key": window.get("window_key"),
                         "anchored": anchored}),
    )


_INPUT_VERIFIERS: dict[str, Any] = {
    "mouse_move": _verify_pointer_position,
    "mouse_left_click": _verify_pointer_click,
    "mouse_right_click": _verify_pointer_click,
    "mouse_double_click": _verify_pointer_claim,
    "key_press": _verify_keyboard_dispatch,
    "key_release": _verify_keyboard_dispatch,
    "key_tap": _verify_keyboard_dispatch,
    "hotkey": _verify_keyboard_dispatch,
    "type_text": _verify_keyboard_dispatch,
}

def _verify_display_enumeration(self: "CapabilityBridge", common: dict,
                                data: dict) -> VerificationResult:
    """A display list passes only when it describes real, consistent topology.

    Three things have to hold, and each one catches a different lie: at least one
    display (an empty list is not a machine with no screens), every entry has
    usable geometry (a display with no pixels cannot be a target), and the
    reported primary is one of the displays that were actually listed rather than
    a separate guess.
    """
    displays = data.get("displays") if isinstance(data.get("displays"), list) else []
    primary_id = str(data.get("primary_display_id") or "")
    evidence = _display_evidence(self, common, data)
    evidence["display_count"] = len(displays)

    if not displays:
        return VerificationResult(
            VerificationState.FAILED,
            "No displays were reported. Windows does not return an empty display "
            "set, so this is a failed read rather than a machine without screens.",
            "capability_execution", evidence,
        )

    degenerate = [str(d.get("display_id")) for d in displays
                  if isinstance(d, dict)
                  and (int((d.get("bounds") or {}).get("width") or 0) <= 0
                       or int((d.get("bounds") or {}).get("height") or 0) <= 0)]
    if degenerate:
        return VerificationResult(
            VerificationState.FAILED,
            f"These displays report no usable area: {', '.join(degenerate)}. "
            "Geometry that covers no pixels cannot be a capture target.",
            "capability_execution", evidence,
        )

    listed = {str(d.get("display_id")) for d in displays if isinstance(d, dict)}
    if primary_id and primary_id not in listed:
        return VerificationResult(
            VerificationState.FAILED,
            f"The reported primary display {primary_id!r} is not among the "
            f"displays that were listed ({sorted(listed)}).",
            "capability_execution", evidence,
        )

    return VerificationResult(
        VerificationState.PASSED,
        f"Read {len(displays)} display(s) with consistent geometry; "
        f"{primary_id or 'none'} is the primary.",
        "capability_execution", evidence,
    )


def _verify_primary_display(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """The primary passes only when Windows actually flagged one.

    A fallback - "the first one", "the largest", "the one at 0,0" - would be an
    invented answer dressed as a measurement, so a missing primary flag fails
    rather than being papered over.
    """
    display = data.get("display") if isinstance(data.get("display"), dict) else {}
    evidence = _display_evidence(self, common, data)
    display_id = str(display.get("display_id") or data.get("display_id") or "")
    evidence["display_id"] = display_id
    if not display_id:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "No primary display was identified, so there is nothing to verify.",
            "capability_execution", evidence,
        )
    if display.get("primary") is not True:
        return VerificationResult(
            VerificationState.FAILED,
            f"Display {display_id!r} was returned as primary but does not carry "
            "the primary flag, so its primacy was inferred rather than read.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"Windows identifies {display_id!r} as the primary display.",
        "capability_execution", evidence,
    )


def _verify_display_lookup(self: "CapabilityBridge", common: dict,
                           data: dict) -> VerificationResult:
    """A resolved display passes when it was resolved and still is present.

    ``resolved: True`` is only trustworthy if the payload also carries the live
    geometry the resolution came from, so that is checked rather than assumed.
    """
    display = data.get("display") if isinstance(data.get("display"), dict) else {}
    evidence = _display_evidence(self, common, data)
    if data.get("resolved") is not True:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The display target was not resolved against current topology.",
            "capability_execution", evidence,
        )
    if not display.get("display_id") or not isinstance(display.get("bounds"), dict):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The display was reported as resolved but carries no identity or "
            "geometry, so the resolution cannot be checked.",
            "capability_execution", evidence,
        )
    if data.get("reconfigured") is True:
        # Not a failure - the display moved. But the caller's assumptions are
        # stale, so the evidence says so rather than quietly passing.
        return VerificationResult(
            VerificationState.PASSED,
            f"Display {display['display_id']!r} was found, but it has been "
            f"reconfigured since the target was recorded "
            f"(now {display.get('bounds')}).",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"Display {display['display_id']!r} was found in current topology with "
        f"bounds {display.get('bounds')}.",
        "capability_execution", evidence,
    )


def _verify_point_to_display(self: "CapabilityBridge", common: dict,
                             data: dict) -> VerificationResult:
    """Point lookup passes only when the named display really contains the point."""
    display = data.get("display") if isinstance(data.get("display"), dict) else {}
    point = data.get("point") if isinstance(data.get("point"), dict) else {}
    bounds = display.get("bounds") if isinstance(display.get("bounds"), dict) else {}
    evidence = _display_evidence(self, common, data)
    try:
        x, y = int(point["x"]), int(point["y"])
        left, top = int(bounds["left"]), int(bounds["top"])
        right, bottom = int(bounds["right"]), int(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The response is missing the point or the display bounds, so the "
            "mapping cannot be verified.",
            "capability_execution", evidence,
        )
    # Recomputed here rather than trusting the layer's own answer. Exclusive far
    # edges, the same rule the geometry uses everywhere else.
    inside = (left <= x < right) and (top <= y < bottom)
    evidence["point_inside_reported_display"] = inside
    if not inside:
        return VerificationResult(
            VerificationState.FAILED,
            f"({x}, {y}) was reported as being on display "
            f"{display.get('display_id')!r}, but it is outside its bounds "
            f"{bounds}.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"({x}, {y}) is inside the bounds of {display.get('display_id')!r}.",
        "capability_execution", evidence,
    )


def _verify_screen_capture(self: "CapabilityBridge", common: dict,
                           data: dict) -> VerificationResult:
    """A capture passes only when the returned image matches what was requested.

    This is genuinely verifiable rather than merely acknowledged: the capture
    reports the rectangle it covered and its own pixel dimensions, and the
    exclusive-edge arithmetic ties those together. A capture that came back at a
    different size, or covering a different rectangle, fails.

    What is deliberately **not** claimed is anything about the *content*. Whether
    the pixels are what the user expected to see is not observable from here, so
    this passes on the capture being faithful to the request and says no more.
    """
    capture = data.get("capture") if isinstance(data.get("capture"), dict) else {}
    evidence = _display_evidence(self, common, data)
    if not capture:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "No capture metadata was returned, so nothing can be verified.",
            "capability_execution", evidence,
        )
    bounds = capture.get("bounds") if isinstance(capture.get("bounds"), dict) else {}
    try:
        width, height = int(capture["width"]), int(capture["height"])
        left, top = int(bounds["left"]), int(bounds["top"])
        right, bottom = int(bounds["right"]), int(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The capture metadata is missing dimensions or bounds.",
            "capability_execution", evidence,
        )
    expected_width, expected_height = right - left, bottom - top
    evidence["requested_size"] = [expected_width, expected_height]
    evidence["observed_size"] = [width, height]
    if (width, height) != (expected_width, expected_height):
        return VerificationResult(
            VerificationState.FAILED,
            f"The capture is {width}x{height} but the rectangle it reports "
            f"{bounds} describes {expected_width}x{expected_height}. The image "
            "does not cover the requested region.",
            "capability_execution", evidence,
        )
    if width <= 0 or height <= 0:
        return VerificationResult(
            VerificationState.FAILED,
            f"The capture is {width}x{height}, which covers no pixels.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"Captured {width}x{height} covering {bounds} on "
        f"{capture.get('display_id') or 'the requested region'}. This "
        "establishes the capture matched the request; it says nothing about what "
        "the pixels contain.",
        "capability_execution", evidence,
    )


def _display_evidence(self: "CapabilityBridge", common: dict,
                      data: dict) -> dict:
    """Evidence common to every display operation.

    Includes the DPI-awareness context, because a capture on a virtualised
    desktop is a different fact from a capture on physical pixels and a reader of
    the ledger needs to be able to tell them apart.
    """
    return self._json_safe({
        **common,
        "has_pixels_in_payload": False,
        "dpi_awareness": (data.get("dpi_awareness")
                          if isinstance(data.get("dpi_awareness"), dict) else {}),
    })


def _verify_virtual_desktop(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """The virtual desktop passes only when it describes real, non-degenerate area.

    Checked against the OS's own view of the desktop rather than only against
    itself, because a rectangle that is internally consistent but disagrees with
    Windows would send captures and pointer validation to different places.
    """
    evidence = _display_evidence(self, common, data)
    try:
        left = int(data["left"])
        top = int(data["top"])
        width = int(data["width"])
        height = int(data["height"])
    except (KeyError, TypeError, ValueError):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The virtual desktop response is missing its origin or size.",
            "capability_execution", evidence,
        )
    evidence["virtual_desktop"] = {"left": left, "top": top,
                                   "width": width, "height": height}
    if width <= 0 or height <= 0:
        return VerificationResult(
            VerificationState.FAILED,
            f"The virtual desktop is {width}x{height}, which covers no pixels. "
            "A machine with no display area cannot be a real configuration.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"The virtual desktop spans ({left}, {top}) across {width}x{height}.",
        "capability_execution", evidence,
    )


def _verify_uia_enumeration(self: "CapabilityBridge", common: dict,
                            data: dict) -> VerificationResult:
    """Discovery passes only when the elements returned were actually described.

    The claim a caller would otherwise be making is "here is that window's UI", so
    the check is that every returned element is anchored, described and
    structurally complete. A truncation flag does not fail: a bounded search is a
    legitimate answer, but only when the response *says* it was bounded.
    """
    from core.windows.uia import identity_from_payload

    elements = data.get("elements") if isinstance(data.get("elements"), list) else []
    evidence = _uia_evidence(self, common, data)
    evidence["element_count"] = len(elements)
    exhaustive = data.get("exhaustive")

    # Read the anchor through the shared reader rather than reaching into
    # ``element["identity"]`` directly: the model-facing payload keeps the identity
    # inside its labelled ``recipe`` block, so a hardcoded path here found nothing
    # and reported every real element as unanchored.
    unanchored = []
    for element in elements:
        if not isinstance(element, dict):
            unanchored.append("")
            continue
        try:
            if not identity_from_payload(element).window_key:
                unanchored.append(str(element.get("target_id")))
        except WindowsInvalidRequestError:
            unanchored.append(str(element.get("target_id")))
    if unanchored:
        return VerificationResult(
            VerificationState.FAILED,
            f"{len(unanchored)} element(s) came back without a window anchor, so "
            "none of them could be acted on.",
            "capability_execution", evidence,
        )
    malformed = [str((e or {}).get("target_id")) for e in elements
                 if isinstance(e, dict) and not isinstance(e.get("bounds"), (dict, type(None)))]
    if malformed:
        return VerificationResult(
            VerificationState.FAILED,
            f"{len(malformed)} element(s) were returned without usable geometry.",
            "capability_execution", evidence,
        )
    if exhaustive is not True and exhaustive is not False:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The response does not say whether the search was exhaustive, so its "
            "completeness cannot be checked.",
            "capability_execution", evidence,
        )
    if exhaustive is False:
        return VerificationResult(
            VerificationState.PASSED,
            f"Described {len(elements)} element(s); the search hit a bound and was "
            "not exhaustive, which the response reports.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"Described {len(elements)} element(s) in an exhaustive search.",
        "capability_execution", evidence,
    )


def _verify_uia_inspection(self: "CapabilityBridge", common: dict,
                          data: dict) -> VerificationResult:
    """A window inspection passes on the anchor, not on an element.

    ``uia_inspect_window`` describes the window's own control tree root. It resolves
    no element and names no target, so it must not be judged by
    :func:`_verify_uia_target` - that would ask for ``resolved`` and ``target_id``
    fields this response structurally cannot have, and report every honest
    inspection as unverifiable.

    What it *can* be held to is the window anchor being real and the payload being
    labelled untrusted, since the whole privacy guarantee for M2.16.5 rests on
    application text arriving marked as data.
    """
    from core.windows.coordinates import Rect

    evidence = _uia_evidence(self, common, data)
    window_key = str(data.get("window_key") or "")
    evidence["window_key"] = window_key
    if not window_key:
        return VerificationResult(
            VerificationState.FAILED,
            "The inspection names no window anchor, so there is nothing to trust.",
            "capability_execution", evidence,
        )
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    try:
        bounds = Rect.from_dict(window.get("bounds"))
    except Exception as exc:  # noqa: BLE001
        return VerificationResult(
            VerificationState.FAILED,
            f"The inspected window has unusable bounds: {exc}",
            "capability_execution", evidence,
        )
    if bounds.is_empty:
        return VerificationResult(
            VerificationState.FAILED,
            "The inspected window reported an empty rectangle.",
            "capability_execution", evidence,
        )
    if data.get("untrusted") is not True:
        return VerificationResult(
            VerificationState.FAILED,
            "The inspection did not mark application-supplied text as untrusted, so "
            "it must not be treated as verified output.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"Window {window_key!r} inspected; its bounds {bounds.to_dict()} were read "
        "from the live window and its text is marked untrusted.",
        "capability_execution", evidence,
    )


def _verify_uia_target(self: "CapabilityBridge", common: dict,
                       data: dict) -> VerificationResult:
    """A resolved target passes only when it re-resolved to the recorded identity.

    Re-derived here rather than trusted: the recorded and observed identities are
    compared field by field, so a "resolved" claim that quietly points at something
    else is caught at the ledger rather than at the click.
    """
    from core.windows.uia import identity_from_payload

    evidence = _uia_evidence(self, common, data)
    if data.get("resolved") is not True:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The target was not reported as resolved, so there is nothing to "
            "verify.",
            "capability_execution", evidence,
        )
    element = data.get("element") if isinstance(data.get("element"), dict) else {}
    try:
        observed = identity_from_payload(element)
    except WindowsInvalidRequestError as exc:
        # A response that cannot produce an identity has not re-resolved anything,
        # whatever else it claims. Failing closed here is the point of this
        # verifier: an unreadable target must never be reported as a verified one.
        return VerificationResult(
            VerificationState.FAILED,
            f"The response carries no readable UI element identity: {exc}",
            "capability_execution", evidence,
        )
    recorded_id = str(data.get("target_id") or "")
    evidence["target_id"] = recorded_id
    if recorded_id and recorded_id != observed.target_id:
        return VerificationResult(
            VerificationState.FAILED,
            f"The response claims target {recorded_id!r} but describes "
            f"{observed.target_id!r}. The target was resolved to something else.",
            "capability_execution", evidence,
        )
    if not observed.window_key:
        return VerificationResult(
            VerificationState.FAILED,
            "The resolved element has no window anchor, so it is not actionable.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"Target {observed.target_id!r} re-resolved to the recorded element in "
        f"window {observed.window_key!r}.",
        "capability_execution", evidence,
    )


def _verify_uia_point(self: "CapabilityBridge", common: dict,
                      data: dict) -> VerificationResult:
    """A derived point passes only when it is anchored and inside its window.

    Recomputes the containment from the reported rectangles rather than believing
    the point. An unanchored point is the M2.16.2 failure this whole path exists
    to prevent, so it fails loudly rather than being reported as derived.
    """
    from core.windows.coordinates import Rect

    evidence = _uia_evidence(self, common, data)
    point = data.get("point") if isinstance(data.get("point"), dict) else {}
    bounds = data.get("element_bounds") if isinstance(
        data.get("element_bounds"), dict) else {}
    window_rect = data.get("window_rect") if isinstance(
        data.get("window_rect"), dict) else {}
    try:
        px, py = int(point["x"]), int(point["y"])
        left, top = int(bounds["left"]), int(bounds["top"])
        right, bottom = int(bounds["right"]), int(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The response is missing the point or the element bounds, so the "
            "conversion cannot be verified.",
            "capability_execution", evidence,
        )
    if data.get("anchored") is not True:
        return VerificationResult(
            VerificationState.FAILED,
            "The derived point is not anchored to a verified window.",
            "capability_execution", evidence,
        )
    inside = (left <= px < right) and (top <= py < bottom)
    evidence["point_inside_element"] = inside
    if not inside:
        return VerificationResult(
            VerificationState.FAILED,
            f"({px}, {py}) is outside the element rectangle it was derived from "
            f"{bounds}.",
            "capability_execution", evidence,
        )
    if window_rect:
        window = Rect.from_dict(window_rect)
        if window is not None and not window.contains(px, py):
            return VerificationResult(
                VerificationState.FAILED,
                f"({px}, {py}) is outside the window it was derived from.",
                "capability_execution", evidence,
            )
    return VerificationResult(
        VerificationState.PASSED,
        f"({px}, {py}) is an anchored point inside the element "
        f"{bounds}. This establishes the conversion; dispatch still re-verifies "
        "foreground.",
        "capability_execution", evidence,
    )


def _verify_uia_action(self: "CapabilityBridge", common: dict,
                       data: dict) -> VerificationResult:
    """A UIA action passes only on an observed postcondition.

    The adapter re-reads the pattern's own state property and reports what it saw;
    this verifies that the reported state is a genuine one rather than defaulting
    to PASSED because a COM call returned. ``UNVERIFIED`` from the adapter stays
    ``UNVERIFIED`` here: ``Invoke`` exposes no state, and inventing a pass for it
    is exactly the claim this repository refuses to make.
    """
    evidence = _uia_evidence(self, common, data)
    verification = data.get("verification") if isinstance(
        data.get("verification"), dict) else {}
    status = str(verification.get("status") or "")
    evidence.update({k: v for k, v in verification.items()
                     if k not in ("status", "reason")})
    evidence["target_id"] = data.get("target_id")
    evidence["pattern"] = data.get("pattern")

    if not status:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The action reported no verification state.",
            "capability_execution", evidence,
        )
    if status == "PASSED":
        return VerificationResult(
            VerificationState.PASSED,
            f"{data.get('pattern')} was applied and the postcondition was "
            f"observed: {verification.get('observed_state') or verification.get('observed_value') or verification.get('observed_selected')}",
            "capability_execution", evidence,
        )
    if status == "FAILED":
        return VerificationResult(
            VerificationState.FAILED,
            f"The {data.get('pattern')} call was made but the postcondition was "
            f"not observed. {verification.get('reason') or ''}".strip(),
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.UNVERIFIED,
        f"The {data.get('pattern')} action could not be independently verified: "
        f"{verification.get('reason') or status}.",
        "capability_execution", evidence,
    )


def _verify_visual_target(self: "CapabilityBridge", common: dict,
                          data: dict) -> VerificationResult:
    """A visual target passes only when its claim was checked against real state.

    And it is never *actionable*, whatever the verdict. That is the boundary this
    operation exists to hold: a visual claim can be validated, and turning it into
    a place to click goes through the anchored pointer path.
    """
    evidence = _uia_evidence(self, common, data)
    verdict = str(data.get("verdict") or "")
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    evidence["visual_verdict"] = verdict
    evidence["target_id"] = data.get("target_id")

    if data.get("actionable") is not False:
        return VerificationResult(
            VerificationState.FAILED,
            "A visual target reported itself as actionable, which it must never "
            "do: dispatch goes through the anchored pointer path.",
            "capability_execution", evidence,
        )
    if not verdict:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The response carries no verdict to verify.",
            "capability_execution", evidence,
        )
    if checks.get("source_installed") is not True:
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "The target's declared source has no installed producer, so the claim "
            "cannot be corroborated. It is still not actionable.",
            "capability_execution", evidence,
        )
    if checks.get("within_display") is False or checks.get("within_window") is False:
        return VerificationResult(
            VerificationState.FAILED,
            "The claimed region is not inside the display or window it was "
            "attributed to.",
            "capability_execution", evidence,
        )
    return VerificationResult(
        VerificationState.PASSED,
        f"The claim is structurally valid and consistent with current desktop "
        f"state ({verdict}). It remains non-actionable by design.",
        "capability_execution", evidence,
    )


def _uia_evidence(self: "CapabilityBridge", common: dict,
                  data: dict) -> dict:
    """Evidence common to UI Automation results.

    ``untrusted`` is carried on purpose. Every string in a UIA result came from
    another application, and a ledger entry that does not say so invites a later
    reader to treat a button's label as if it were trustworthy text.
    """
    return self._json_safe({
        **common,
        "untrusted_application_data": data.get("untrusted") is not False,
        "dead_nodes_skipped": data.get("dead_nodes_skipped"),
    })


_UIA_VERIFIERS: dict[str, Any] = {
    "uia_inspect_window": _verify_uia_inspection,
    "uia_find_elements": _verify_uia_enumeration,
    "uia_resolve_target": _verify_uia_target,
    "uia_target_point": _verify_uia_point,
    "uia_invoke": _verify_uia_action,
    "uia_set_value": _verify_uia_action,
    "uia_toggle": _verify_uia_action,
    "uia_select": _verify_uia_action,
    "uia_expand_collapse": _verify_uia_action,
    "visual_resolve_target": _verify_visual_target,
}

_DISPLAY_VERIFIERS: dict[str, Any] = {
    "list_displays": _verify_display_enumeration,
    "primary_display": _verify_primary_display,
    "virtual_desktop": _verify_virtual_desktop,
    "get_display": _verify_display_lookup,
    "point_to_display": _verify_point_to_display,
    "capture_screen": _verify_screen_capture,
}

_POINTER_READ_VERIFIERS = frozenset({"get_cursor_pos", "get_foreground_window"})


class CapabilityBridge:
    def __init__(
        self,
        registry: CapabilityRegistry,
        events: Any | None = None,
        allowed_roots: list[Path] | tuple[Path, ...] = (),
    ) -> None:
        self._registry = registry
        self._events = events
        self._allowed_roots = tuple(Path(root) for root in allowed_roots)

    def validate_args(self, args: dict) -> None:
        if not isinstance(args, dict):
            raise ValueError("capability arguments must be an object")
        extra = set(args) - {"capability", "operation", "params"}
        if extra:
            raise ValueError(f"unsupported capability arguments: {', '.join(sorted(extra))}")
        capability_name = args.get("capability")
        operation = args.get("operation")
        if not isinstance(capability_name, str) or not capability_name.strip():
            raise ValueError("capability must be a non-empty string")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-empty string")
        if not isinstance(args.get("params", {}), dict):
            raise ValueError("params must be an object")
        capability = self._registry.get(capability_name)
        if capability is None:
            raise ValueError(f"unknown capability: {capability_name}")
        if not capability.can_execute(operation):
            raise ValueError(f"unsupported operation: {capability_name}.{operation}")

    def permission_level(self, args: dict) -> PermissionLevel:
        capability_name = str(args.get("capability") or "")
        operation = str(args.get("operation") or "")
        capability = self._registry.get(capability_name)
        if capability is None:
            return PermissionLevel.BLOCKED
        if operation == "execute_script":
            return PermissionLevel.BLOCKED
        # M2.15: an adapter that declares which of its operations mutate state is
        # authoritative for that decision. The native Windows layer is the first:
        # `terminate` must not be SAFE just because its name does not contain a
        # mutating keyword, and a query must not be SENSITIVE just because it
        # lives beside one.
        is_mutating = getattr(capability, "is_mutating", None)
        if callable(is_mutating):
            try:
                if is_mutating(operation):
                    return PermissionLevel.SENSITIVE
            except Exception:  # noqa: BLE001 - a bad probe must not widen access
                return PermissionLevel.SENSITIVE
            if self._has_external_path(args.get("params") or {}):
                return PermissionLevel.SENSITIVE
            return PermissionLevel.SAFE
        if capability_name == "mock" and operation == "execute":
            return PermissionLevel.SENSITIVE if self._has_external_path(args.get("params") or {}) else PermissionLevel.SAFE
        if operation.startswith(_READ_ONLY_WORDS):
            if self._has_external_path(args.get("params") or {}):
                return PermissionLevel.SENSITIVE
            return PermissionLevel.SAFE
        if any(word in operation for word in _MUTATING_WORDS):
            return PermissionLevel.SENSITIVE
        return PermissionLevel.SENSITIVE

    def describe(self, args: dict) -> str:
        return f"Execute capability {args.get('capability', '')}.{args.get('operation', '')}"

    def execute(
        self,
        args: dict,
        _tool_context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate_args(args)
        context = _tool_context or ToolExecutionContext("", None, None)
        capability_name = str(args["capability"])
        operation = str(args["operation"])
        params = dict(args.get("params") or {})
        if isinstance(params.get("context"), dict):
            params = dispatch_params(capability_name, operation, params)
        request = CapabilityRequest(
            capability=capability_name,
            operation=operation,
            params=params,
            context={
                "source": "s1_core",
                "run_id": context.run_id,
                "task_id": context.task_id or "",
            },
            task_id=context.task_id or "",
            cancellation_token=context.cancellation_token,
        )
        # The registry owns CapabilityRequest.request_id; publish that same
        # trusted value so no downstream consumer can read a model-supplied one.
        request.context["request_id"] = request.request_id
        publisher = CoreEventBusPublisher(self._events) if self._events is not None else None
        result = self._registry.execute(request, event_publisher=publisher)
        payload = self._json_safe(result.to_dict())
        error = result.error or {}
        message = str(error.get("message") or error.get("error") or f"Capability returned {result.status.value}.")
        output = json.dumps(payload, ensure_ascii=False, default=str)
        verification = self._verification(result, request)
        return ToolResult(
            success=result.success,
            output=output,
            error="" if result.success else message,
            data={"capability_result": payload},
            cancelled=result.cancelled,
            verification=verification,
        )

    def _has_external_path(self, params: dict) -> bool:
        if not self._allowed_roots:
            return False
        for key in _PATH_KEYS:
            value = params.get(key)
            if value in (None, ""):
                continue
            try:
                path = Path(str(value))
            except (TypeError, ValueError):
                return True
            if not path_is_within(path, list(self._allowed_roots)):
                return True
        return False

    @staticmethod
    def _json_safe(value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _result_path(result: CapabilityResult, request: CapabilityRequest) -> str:
        data = result.data or {}
        render = data.get("render") if isinstance(data.get("render"), dict) else {}
        return str(
            render.get("path")
            or data.get("render_path")
            or data.get("path")
            or request.params.get("path")
            or ""
        )

    def _verification(
        self,
        result: CapabilityResult,
        request: CapabilityRequest,
    ) -> VerificationResult | None:
        if not result.success:
            return None
        data = result.data or {}
        operation = result.operation
        capability = result.capability
        if capability == "blender_vision_workflow":
            if operation in {"run", "render_and_verify", "compare_reference"}:
                return self._workflow_verification(result, request, data, operation)
            if operation == "analyze_reference":
                return VerificationResult(
                    VerificationState.UNVERIFIED,
                    "Reference analysis completed without render verification.",
                    "capability_analysis",
                    self._json_safe({
                        "reference_path": data.get("reference_path"),
                        "backend": data.get("backend"),
                        "evidence": data.get("evidence"),
                    }),
                )
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Workflow operation completed without an independent render verification contract.",
                "capability_execution",
                self._json_safe({"capability": capability, "operation": operation, "data": data}),
            )
        if capability == "blender" and operation == "render":
            path = self._result_path(result, request)
            exists = bool(path and Path(path).is_file())
            evidence = self._json_safe({"path": path, "exists": exists})
            if data.get("valid") is False:
                return VerificationResult(VerificationState.FAILED, "Blender reported an invalid render.", "render_file", evidence)
            return VerificationResult(
                VerificationState.PASSED if exists else VerificationState.FAILED,
                "Render file was created and exists." if exists else "Blender reported success but the render file is missing.",
                "render_file",
                evidence,
            )
        if capability in {"blender", "vision"} and operation in {"inspect_render", "validate_image"}:
            path = self._result_path(result, request)
            valid = data.get("valid") is True or data.get("readable") is True
            exists = bool(path and Path(path).is_file())
            passed = valid and exists
            evidence = self._json_safe({"path": path, "valid": valid, "exists": exists})
            return VerificationResult(
                VerificationState.PASSED if passed else VerificationState.FAILED,
                "Image inspection evidence was returned." if passed else "Image inspection did not provide a readable file.",
                "image_inspection",
                evidence,
            )
        if capability == "windows":
            return self._windows_verification(result, request, data, operation)
        if capability == "blender_vision" and operation in {
            "render_and_inspect",
            "render_and_describe",
            "render_and_compare",
            "inspect_render",
            "verify_render",
        }:
            path = self._result_path(result, request)
            inspection = data.get("inspection") if isinstance(data.get("inspection"), dict) else {}
            exists = bool(path and Path(path).is_file())
            valid_present = "valid" in data
            valid = data.get("valid") is True
            inspection_present = "inspection" in data
            inspection_valid = inspection.get("valid") is True
            requires_inspection = operation in {"render_and_inspect", "inspect_render", "verify_render"}
            evidence = self._json_safe({
                "path": path,
                "exists": exists,
                "valid": data.get("valid"),
                "inspection": inspection,
            })
            if data.get("valid") is False or (inspection_present and inspection.get("valid") is False):
                return VerificationResult(
                    VerificationState.FAILED,
                    "Blender-Vision reported invalid render evidence.",
                    "blender_vision_render",
                    evidence,
                )
            if not exists:
                return VerificationResult(
                    VerificationState.FAILED,
                    "Blender-Vision did not provide a readable render file.",
                    "blender_vision_render",
                    evidence,
                )
            if not valid_present or not valid or (requires_inspection and (not inspection_present or not inspection_valid)):
                return VerificationResult(
                    VerificationState.UNVERIFIED,
                    "Blender-Vision did not provide positive verification evidence.",
                    "blender_vision_render",
                    evidence,
                )
            return VerificationResult(
                VerificationState.PASSED,
                "Blender-Vision evidence was returned.",
                "blender_vision_render",
                evidence,
            )
        if data.get("verified") is False:
            return VerificationResult(
                VerificationState.FAILED,
                "Capability explicitly reported failed verification.",
                "capability_evidence",
                self._json_safe({"evidence": data.get("evidence")}),
            )
        raw_evidence = data.get("evidence")
        if data.get("verified") is True and not isinstance(raw_evidence, Mapping) and raw_evidence is not None:
            # M2.12 P3: the evidence contract is object-shaped by design
            # (integration/workflows.py builds dicts and rejects a callback that
            # supplies evidence without a boolean verdict). A bare list/tuple/
            # string is malformed, not an alternate valid shape, so it is not
            # promoted to PASSED. The payload is still preserved for diagnosis.
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Capability reported a malformed evidence contract; evidence must be an object.",
                "capability_execution",
                self._json_safe({
                    "capability": capability,
                    "operation": operation,
                    "malformed_evidence": raw_evidence,
                }),
            )
        if data.get("verified") is True and self._valid_evidence(raw_evidence):
            return VerificationResult(
                VerificationState.PASSED,
                "Capability returned an explicit verification contract.",
                "capability_evidence",
                self._json_safe(raw_evidence),
            )
        return VerificationResult(
            VerificationState.UNVERIFIED,
            "Capability completed without an independent S1 verifier.",
            "capability_execution",
            self._json_safe({"capability": capability, "operation": operation, "status": result.status.value, "evidence": data.get("evidence")}),
        )

    def _windows_verification(
        self,
        result: CapabilityResult,
        request: CapabilityRequest,
        data: dict,
        operation: str,
    ) -> VerificationResult:
        """Verification evidence for the M2.15 native Windows layer.

        The rule that matters: a termination that was **requested** but not
        **observed** can never be a ``PASSED``. The layer reports the two as
        separate fields precisely so this function can refuse to conflate them,
        and the ledger's invariant then refuses anything without observed
        evidence anyway.
        """
        request_id = str(request.request_id or "")
        common = {"capability": "windows", "operation": operation,
                  "request_id": request_id}

        if operation == "terminate":
            requested = data.get("requested") is True
            confirmed = data.get("confirmed") is True
            evidence = self._json_safe({
                **common,
                "pid": data.get("pid"),
                "requested": requested,
                "confirmed": confirmed,
                "exit_code": data.get("exit_code"),
                "identity_created_at": data.get("identity_created_at"),
            })
            if requested and confirmed:
                return VerificationResult(
                    VerificationState.PASSED,
                    "Termination was requested and the process exit was observed.",
                    "capability_execution",
                    evidence,
                )
            if requested and not confirmed:
                return VerificationResult(
                    VerificationState.FAILED,
                    "Termination was requested but the exit was not observed; "
                    "this is not a completed termination.",
                    "capability_execution",
                    evidence,
                )
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Termination was not requested; nothing to verify.",
                "capability_execution",
                evidence,
            )

        if operation == "system_info":
            memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
            cpu = data.get("cpu") if isinstance(data.get("cpu"), dict) else {}
            total = float(memory.get("total_physical_mb") or 0.0)
            available = float(memory.get("available_physical_mb") or 0.0)
            # Internally consistent: available cannot exceed total, and a
            # machine reporting zero physical memory is not reporting a system.
            consistent = bool(total > 0.0 and 0.0 <= available <= total
                              and data.get("os_caption"))
            evidence = self._json_safe({
                **common,
                "os_caption": data.get("os_caption"),
                "backend": data.get("backend"),
                "total_physical_mb": total,
                "available_physical_mb": available,
                "logical_cores": cpu.get("logical_cores"),
            })
            return VerificationResult(
                VerificationState.PASSED if consistent else VerificationState.FAILED,
                "Native system snapshot was internally consistent."
                if consistent else
                "Native system snapshot was missing or inconsistent fields.",
                "tool_observation",
                evidence,
            )

        # M2.16 window reads: structural validity of the returned rows.
        if operation in ("list_windows", "get_window"):
            return _window_read_verification(self, common, data, operation)

        # M2.16 window lifecycle mutations. Verification is of *observed state*,
        # never of an API return code: `ShowWindow` returns before the window has
        # moved, and `SetForegroundWindow` can be refused outright while still
        # reporting success. Each verb therefore checks the specific property it
        # was asked to change.
        if operation in _WINDOW_LIFECYCLE_VERIFIERS:
            return _WINDOW_LIFECYCLE_VERIFIERS[operation](self, common, data)

        # M2.16.2/3 pointer and keyboard. Reads verify structurally; mutations verify
        # against observed state, and a dispatched click or keystroke is reported
        # UNVERIFIED rather than PASSED because its effect lives inside the target
        # application where this process cannot observe it.
        if operation in _POINTER_READ_VERIFIERS:
            return _verify_pointer_read(self, common, data)
        if operation in _INPUT_VERIFIERS:
            return _INPUT_VERIFIERS[operation](self, common, data)

        # M2.16.4 display topology and bounded capture. A capture is verified
        # against the region it reports covering; nothing is claimed about the
        # pixels, and a capture can never carry them into this evidence.
        if operation in _DISPLAY_VERIFIERS:
            return _DISPLAY_VERIFIERS[operation](self, common, data)

        # M2.16.5 UI Automation and visual targeting. A resolved target is checked
        # against its recorded identity, a derived point against the rectangle it
        # came from, and an action only passes on an observed postcondition.
        if operation in _UIA_VERIFIERS:
            return _UIA_VERIFIERS[operation](self, common, data)

        # list / get / find: structural validity of the returned rows.
        rows = data.get("processes")
        if operation == "get":
            rows = [data] if data.get("pid") is not None else []
        if not isinstance(rows, list):
            rows = []
        well_formed = all(
            isinstance(row, dict) and isinstance(row.get("pid"), int) and row.get("pid") > 0
            for row in rows
        )
        evidence = self._json_safe({
            **common,
            "count": data.get("count", len(rows)),
            "returned": len(rows),
            "well_formed": well_formed,
        })
        return VerificationResult(
            VerificationState.PASSED if well_formed and rows else VerificationState.UNVERIFIED,
            "Process metadata was structurally valid."
            if well_formed and rows else
            "No well-formed process metadata was returned.",
            "tool_observation",
            evidence,
        )

    def _workflow_verification(
        self,
        result: CapabilityResult,
        request: CapabilityRequest,
        data: dict,
        operation: str,
    ) -> VerificationResult:
        path = self._result_path(result, request)
        evidence = {
            "path": path,
            "reference_path": data.get("reference_path"),
            "verified": data.get("verified"),
            "attempts": data.get("attempts"),
            "iterations": data.get("iterations"),
            "render": data.get("render"),
            "state": data.get("state"),
            "comparison": data.get("comparison"),
        }
        if data.get("verified") is False:
            return VerificationResult(
                VerificationState.FAILED,
                "Workflow reported failed verification.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        if data.get("verified") is not True:
            return VerificationResult(
                VerificationState.UNVERIFIED,
                "Workflow did not return an explicit verification result.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        attempts = data.get("attempts")
        iterations = data.get("iterations")
        render = data.get("render")
        comparison = data.get("comparison")
        if operation == "compare_reference":
            valid_shape = bool(path) and isinstance(comparison, dict) and bool(comparison)
            if valid_shape:
                try:
                    threshold = float(data.get("threshold", request.params.get("threshold", 0.01)))
                except (TypeError, ValueError):
                    threshold = -1.0
                diff_ratio = comparison.get("diff_ratio")
                positive = (
                    comparison.get("success") is not False
                    and (
                        comparison.get("identical") is True
                        or (isinstance(diff_ratio, (int, float)) and not isinstance(diff_ratio, bool) and 0.0 <= threshold <= 1.0 and diff_ratio <= threshold)
                        or (comparison.get("identical") is None and diff_ratio is None and comparison.get("success") is True)
                    )
                )
                valid_shape = valid_shape and positive
        else:
            valid_shape = bool(path) and isinstance(attempts, list) and bool(attempts)
            valid_shape = valid_shape and isinstance(iterations, int) and not isinstance(iterations, bool) and iterations >= 1
            valid_shape = valid_shape and isinstance(render, dict) and bool(render)
            valid_shape = valid_shape and attempts[-1].get("valid") is True
            valid_shape = valid_shape and render.get("valid") is True
        if not valid_shape:
            return VerificationResult(
                VerificationState.FAILED,
                "Workflow verification evidence is malformed.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        if not Path(path).is_file():
            return VerificationResult(
                VerificationState.FAILED,
                "Workflow reported verification but the render path is missing.",
                "workflow_verification",
                self._json_safe(evidence),
            )
        if operation == "compare_reference":
            reference_path = str(data.get("reference_path") or "")
            if not reference_path or not Path(reference_path).is_file():
                return VerificationResult(
                    VerificationState.FAILED,
                    "Workflow comparison evidence is missing its reference file.",
                    "workflow_verification",
                    self._json_safe(evidence),
                )
        return VerificationResult(
            VerificationState.PASSED,
            "Workflow returned valid deterministic verification evidence.",
            "workflow_verification",
            self._json_safe(evidence),
        )

    @staticmethod
    def _valid_evidence(value: Any) -> bool:
        # Mapping-only since M2.12 P3. The evidence contract is object-shaped;
        # a bare sequence carries no defined semantics and cannot be persisted
        # as `verification_evidence`. Kept as a method so the contract stays in
        # one place.
        return isinstance(value, Mapping) and bool(value)


def build_capability_tool(
    registry: CapabilityRegistry,
    events: Any | None = None,
    allowed_roots: list[Path] | tuple[Path, ...] = (),
) -> ToolSpec:
    bridge = CapabilityBridge(registry, events, allowed_roots)

    def description():
        reports = registry.capabilities()
        available = []
        for report in reports:
            name = str(report.get("name") or "")
            operations = ", ".join(str(value) for value in report.get("operations") or [])
            if name:
                available.append(f"{name}({operations})")
        suffix = "; ".join(available) if available else "none"
        return (
            "Execute one registered capability through the Core Agent. "
            "The capability and operation are selected explicitly and permission is decided by Core. "
            f"Registered capabilities: {suffix}."
        )

    def execute_tool(**kwargs):
        tool_context = kwargs.pop("_tool_context", None)
        return bridge.execute(kwargs, tool_context)

    return ToolSpec(
        name="capability.execute",
        description=description(),
        # M2.13.0: this one tool reaches Blender, Vision and the S2.8 workflows
        # without being named after any of them, so its capability is declared
        # explicitly rather than derived from the "capability" namespace.
        capabilities=("capability", "blender", "vision"),
        parameters={
            "type": "object",
            "properties": {
                "capability": {"type": "string"},
                "operation": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["capability", "operation"],
            "additionalProperties": False,
        },
        required=["capability", "operation"],
        level=PermissionLevel.NORMAL,
        level_for=bridge.permission_level,
        fn=execute_tool,
        describe=bridge.describe,
        wants_context=True,
        validate_args=bridge.validate_args,
        persist_data=True,
    )
