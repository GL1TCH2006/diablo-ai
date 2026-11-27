from __future__ import annotations

import json

from config.settings import Settings

from .registry import ToolResult

_MODES = ("auto", "force8b", "force14b", "project")


def _runtime():
    try:
        from tools import runtime_context
        return runtime_context
    except Exception:
        return type("R", (), {"router": None, "prefs": None})


def models_status(settings: Settings | None = None) -> ToolResult:
    ctx = _runtime()
    router = getattr(ctx, "router", None)
    if router is None:
        return ToolResult(True, output="Model router not initialised (backend unavailable?).")
    state = router.state()
    fast_verified = "verified (8B class per Ollama metadata)" if state["fast_identity_verified"] else "NOT verified - identity unknown"
    if state["resource_check"]:
        resource = "enabled (gate before 14B selection)"
    else:
        resource = "disabled (documented extension)"
    lines = [
        f"MODE: {state['mode']}",
        f"FAST: {state['fast']} ({fast_verified})",
        f"STRONG: {state['strong']}",
        f"RESOURCE CHECK: {resource}",
    ]
    if state["models_installed"]:
        lines.append("INSTALLED:")
        for name, meta in sorted(state["models_installed"].items()):
            lines.append(f"  {name} (param={meta['param_b']}B family={meta['family']})")
    return ToolResult(True, output="\n".join(lines))


def models_set_mode(mode: str, fast: str = "", strong: str = "", settings: Settings | None = None) -> ToolResult:
    mode = (mode or "").strip().lower()
    if mode not in _MODES:
        return ToolResult(False, error=f"mode must be one of: {', '.join(_MODES)}")
    ctx = _runtime()
    prefs = getattr(ctx, "prefs", None)
    if prefs is not None and hasattr(prefs, "set"):
        try:
            prefs.set("models.mode", mode)
            if fast:
                prefs.set("models.fast", fast)
            if strong:
                prefs.set("models.strong", strong)
        except Exception as exc:
            return ToolResult(False, error=f"could not persist preference: {exc}")
    router = getattr(ctx, "router", None)
    if router is not None:
        router.apply_management_overrides(prefs)
    return ToolResult(True, output=f"model mode -> {mode}" + (f" (fast={fast})" if fast else "") + (f" (strong={strong})" if strong else ""))