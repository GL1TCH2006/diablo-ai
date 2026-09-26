from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from core.logging_setup import get_logger

log = get_logger("model_router")

_8B_MIN, _8B_MAX = 6.5, 9.9
_STRONG_MIN = 12.0

_MODE_LABELS = {
    "auto": "AUTO",
    "force8b": "FORCE_8B",
    "force14b": "FORCE_14B",
    "project": "PROJECT",
}


@dataclass
class ModelIdentity:
    name: str
    parameter_b: float = 0.0
    size_bytes: int = 0
    family: str = ""
    capabilities: tuple[str, ...] = ()

    @property
    def supports_tools(self) -> bool:
        return not self.capabilities or "tools" in self.capabilities

    @property
    def is_8b_class(self) -> bool:
        return _8B_MIN <= self.parameter_b <= _8B_MAX if self.parameter_b else False

    @property
    def is_strong_class(self) -> bool:
        return self.parameter_b >= _STRONG_MIN if self.parameter_b else False


@dataclass
class ModelChoice:
    name: str
    mode: str = "auto"
    source: str = "default"
    reason: str = ""
    escalated: bool = False
    #: M2.13.0: how confident the router is in this choice, 0.0-1.0. Additive
    #: with defaults, so every existing construction site is unaffected.
    confidence: float = 1.0
    #: The evidence the decision was based on, for diagnostics.
    signals: tuple = ()

    def to_state_line(self) -> str:
        return f"MODEL: {self.name}  MODE: {self.mode.upper()}  SOURCE: {self.source}" + (f"  ({self.reason})" if self.reason else "")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "source": self.source,
            "reason": self.reason,
            "escalated": self.escalated,
            "confidence": round(float(self.confidence), 3),
            "signals": list(self.signals),
        }


class ModelRouter:
    """Adaptive qwen3 selection.

    Decision rules:
      FORCE_8B  -> always the fast model
      FORCE_14B -> always the strong model
      PROJECT   -> a project preference is used when it names an installed 8B/strong model
      AUTO      -> task override (one-shot) > escalation after failed verification
                   > complexity >= threshold > default fast model

    Identity is always read from Ollama metadata (parameter_size); the router never
    assumes qwen3:latest equals qwen3:8b.
    """

    def __init__(self, settings, catalog: list | None = None, catalog_call=None):
        self._settings = settings
        self.identities: dict[str, ModelIdentity] = {}
        self.catalog_ok = False
        self._catalog_error = ""
        catalog = catalog if catalog is not None else (catalog_call() if catalog_call else [])
        self._load_catalog(catalog)

    def _load_catalog(self, catalog: list) -> None:
        self.catalog_ok = False
        for item in catalog:
            name = getattr(item, "name", None) or ""
            if not name:
                continue
            param_text = getattr(item, "parameter_size", "") or ""
            size = int(getattr(item, "size_bytes", 0) or 0)
            param_b = _parse_billion(param_text)
            family = str(getattr(item, "family", "") or _family_of(name))
            capabilities = tuple(str(value) for value in (getattr(item, "capabilities", ()) or ()))
            self.identities[name] = ModelIdentity(
                name=name,
                parameter_b=param_b,
                size_bytes=size,
                family=family,
                capabilities=capabilities,
            )
            self.catalog_ok = True
        if not self.catalog_ok:
            log.warning("model catalog is empty or unavailable")

    def _fast_name(self) -> str:
        return self._settings.models_fast or "qwen3:8b"

    def _strong_name(self) -> str:
        return self._settings.models_strong or "qwen3:14b"

    def is_fast_identity_verified(self) -> bool:
        ident = self._identity(self._fast_name())
        return bool(ident and ident.is_8b_class)

    def _norm(self, name: str) -> str:
        return name.replace("_", ":").strip()

    def _identity(self, name: str) -> ModelIdentity | None:
        raw = str(name or "").strip()
        return self.identities.get(raw) or self.identities.get(self._norm(raw))

    def is_available(self, name: str) -> bool:
        identity = self._identity(name)
        return bool(identity and identity.supports_tools)

    def _fallback_name(self, exclude: str = "") -> str:
        excluded = self._norm(exclude)
        candidates = [self._fast_name(), self._strong_name(), *self.identities.keys()]
        seen: set[str] = set()
        for candidate in candidates:
            normalized = self._norm(candidate)
            if normalized in seen or normalized == excluded:
                continue
            seen.add(normalized)
            identity = self._identity(candidate)
            if identity is not None and identity.supports_tools:
                return identity.name
        return ""

    def _available_choice(self, name: str, mode: str, source: str, reason: str) -> ModelChoice:
        identity = self._identity(name)
        if identity is not None and identity.supports_tools:
            return ModelChoice(identity.name, mode, source, reason)
        fallback = self._fallback_name(exclude=name)
        if not fallback:
            return ModelChoice(name, mode, "unavailable", f"{reason}; no compatible local model is installed")
        return ModelChoice(fallback, mode, source + "-fallback", f"{reason}; {name} unavailable, using {fallback}")

    def choose(
        self,
        *,
        project_pref: str = "",
        task_override: str = "",
        complexity: float = 0.0,
        last_verify_failed: bool = False,
        escalations_used: int = 0,
    ) -> ModelChoice:
        settings = self._settings
        mode = (settings.models_mode or "auto").lower()
        fast = self._fast_name()
        strong = self._strong_name()

        if mode == "force8b":
            return self._available_choice(fast, "force8b", "forced", "user-override mode FORCE_8B")
        if mode == "force14b":
            choice = self._available_choice(strong, "force14b", "forced", "user-override mode FORCE_14B")
            return self._guard_resource(choice)

        task = str(task_override or "").strip()
        if mode == "project":
            pref = str(project_pref or "").strip()
            pref_identity = self._identity(pref) if pref else None
            if pref_identity is not None and pref_identity.supports_tools:
                return ModelChoice(pref_identity.name, "project", "project-preference", f"project preference {pref_identity.name}")
            return self._available_choice(fast, "project", "fallback", "no valid project preference")

        task_identity = self._identity(task) if task else None
        if task_identity is not None and task_identity.supports_tools:
            return ModelChoice(task_identity.name, "auto", "task", "explicit task override for this request")
        if last_verify_failed and escalations_used < settings.models_max_escalations and self.is_available(strong):
            return self._guard_resource(
                ModelChoice(strong, "auto", "escalation",
                            f"verification failed; escalating to {strong} (attempt {escalations_used + 1})", escalated=True))
        if complexity >= settings.models_complexity_threshold and self.is_available(strong):
            return self._guard_resource(
                ModelChoice(strong, "auto", "auto-complexity",
                            f"task complexity {complexity:.2f} >= threshold {settings.models_complexity_threshold:.2f}"))
        return self._available_choice(fast, "auto", "default", "routine request")

    def _guard_resource(self, choice: ModelChoice) -> ModelChoice:
        """Refuse a model the machine cannot hold.

        M2.13.0 fix. This compared the model's size against *currently
        available* physical memory. That is the wrong quantity: available memory
        measures what happens to be free this instant, and on a busy machine it
        collapses even when a model would fit comfortably once the OS reclaimed
        page cache. The M2.13.0 benchmark hit exactly that - 0.2GiB free of
        15.7GiB - which would have made this guard reject every installed model
        for no reason. Fit is a question about total capacity, so total capacity
        is what is compared. Available memory is still reported, as context.
        """
        if not self._settings.resource_check_enabled:
            return choice
        ident = self.identities.get(choice.name)
        if ident is None:
            return choice
        total_bytes = _total_ram()
        if not total_bytes or not ident.size_bytes:
            return choice
        needed = ident.size_bytes * 1.6
        if needed <= total_bytes:
            return choice
        fallback = self._fallback_name(exclude=choice.name)
        available = _available_ram()
        detail = (
            f"strong model {choice.name} needs ~{int(needed) // (1024 ** 3)}GiB "
            f"of {total_bytes // (1024 ** 3)}GiB total"
        )
        if available:
            detail += f" ({available // (1024 ** 3)}GiB free right now)"
        if fallback and fallback != choice.name:
            return ModelChoice(
                fallback, choice.mode, choice.source + "-resource-fallback",
                f"{detail}; using {fallback} instead", confidence=0.4,
                signals=(f"total_ram={total_bytes}", f"needed={int(needed)}"),
            )
        return ModelChoice(
            choice.name, choice.mode, choice.source + "-resource-warn",
            f"{detail}; no smaller model installed", confidence=0.2,
            signals=(f"total_ram={total_bytes}", f"needed={int(needed)}"),
        )

    def fallback_name(self, exclude: str = "") -> str:
        return self._fallback_name(exclude=exclude)

    def state(self) -> dict:
        return {
            "mode": _MODE_LABELS.get((self._settings.models_mode or "auto").lower(), (self._settings.models_mode or "auto").upper()),
            "fast": self._fast_name(),
            "strong": self._strong_name(),
            "fast_identity_verified": self.is_fast_identity_verified(),
            "fast_available": self.is_available(self._fast_name()),
            "strong_available": self.is_available(self._strong_name()),
            "catalog_ok": self.catalog_ok,
            "models_installed": {
                n: {"param_b": i.parameter_b, "family": i.family, "capabilities": list(i.capabilities)}
                for n, i in self.identities.items()
            },
            "resource_check": self._settings.resource_check_enabled,
        }

    def apply_management_overrides(self, prefs) -> None:
        if prefs is None:
            return
        try:
            mode = prefs.get("models.mode")
            fast = prefs.get("models.fast")
            strong = prefs.get("models.strong")
        except Exception:
            return
        if mode in _MODE_LABELS:
            self._settings.models_mode = mode
        if fast:
            self._settings.models_fast = fast
        if strong:
            self._settings.models_strong = strong


def estimate_complexity(text: str) -> float:
    """Heuristic 0..1 complexity proxy: task breadth, recursion/parallelism, framework size, error depth."""
    lowered = (text or "").lower()
    score = 0.0
    heavy = (
        r"\b(refactor|refactoring)\b", r"\b(architecture|design)\b", r"\b(concurrent|parallel|async|threads?)\b",
        r"\b(recurs)\w*", r"\bframework(s)?\b", r"\b(dependency|monorepo|multi[- ]service)\b",
        r"\b(traceback|stack trace)\b", r"\b(deadlock|race)\b", r"\b(codegen|code gen)\b", r"\bsecurity\b",
    )
    for pattern in heavy:
        if re.search(pattern, lowered):
            score += 0.12
    if len((text or "").split()) > 90:
        score += 0.15
    steps = len(re.findall(r"(and then|after that|next[, ]|then[, ]|followed by)", lowered))
    score += min(steps * 0.08, 0.2)
    return min(round(score, 2), 1.0)


def all_gb(size_bytes: int) -> float:
    return (size_bytes or 0) / (1024 ** 3)


def _total_ram() -> int:
    """Total physical memory in bytes, or 0 if it cannot be determined."""
    return _memory_status()[0]


def _available_ram() -> int:
    """Physical memory free *right now*, or 0 if it cannot be determined.

    Reported for context only. This is a snapshot, not a capacity, so it must
    not be used to decide whether a model can be loaded.
    """
    return _memory_status()[1]


def _parse_billion(text: str) -> float:
    if not text:
        return 0.0
    match = re.search(r"([\d.]+)\s*B", text, re.I)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _family_of(name: str) -> str:
    base = name.split(":")[0].lower()
    for fam in ("qwen", "llama", "mistral", "gemma", "phi", "deepseek"):
        if fam in base:
            return fam
    return base


def _memory_status() -> tuple[int, int]:
    """(total_physical, available_physical) in bytes; (0, 0) if unknown.

    M2.13.0 fix. The previous implementation declared ``MEMORYSTATUSEX``
    without its two leading ``DWORD`` fields (``dwLength``, ``dwMemoryLoad``).
    ctypes then wrote and read every subsequent field at the wrong offset, the
    call reported failure, and the function returned 0 - so the resource guard
    silently did nothing on every machine. Setting ``dwLength`` is not
    optional: the API uses it to validate the structure size.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        status = kernel32.GlobalMemoryStatusEx
        status.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        status.restype = wintypes.BOOL
        info = MEMORYSTATUSEX()
        info.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if status(ctypes.byref(info)):
            return int(info.ullTotalPhys), int(info.ullAvailPhys)
    except Exception:
        pass
    return 0, 0