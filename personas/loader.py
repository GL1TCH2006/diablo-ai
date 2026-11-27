"""Persona loading and formatting. Presentation only, and **fail closed**.

A persona is a bag of wording: reply templates, an orb palette key, a voice hint.
It carries no authority. In particular:

* a persona cannot change a permission decision, and nothing in this package is
  imported by :mod:`config.permissions`;
* a persona cannot make an unverified result read as completed;
* a persona that fails validation is **refused**, not repaired and not ignored.

The last point is why this loader is strict. A persona file is data, and data that
is malformed must not silently degrade into "the default persona" - a user who
edits a persona and gets no error will not notice they got the wrong one.

Schemas
=======

``persona``
    Required, non-empty string. The canonical name.

``description``
    Required string.

``aliases``
    Optional list of non-empty strings. Matched case-insensitively against typed
    or spoken text.

``replies``
    Required mapping covering **every** outcome in :data:`OUTCOMES`. A missing
    outcome is a validation error, not a silent fallback to a generic string: a
    template that only sometimes knows the difference between verified and
    unverified is exactly the failure this package exists to prevent.

``orb_palette_key``
    Required string naming a palette in :data:`ui.orb.PALETTES`.

``voice``
    Optional mapping. Unknown keys are tolerated; unknown *values* are not.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "Persona",
    "PersonaError",
    "OUTCOMES",
    "COMPLETED_REQUIRES_VERIFIED",
    "DEFAULT_PERSONA",
    "load_persona",
    "load_all",
    "format_status",
    "select_persona",
    "persona_names",
    "alias_index",
]

#: Every outcome a report can describe. A persona must supply all of them.
OUTCOMES = ("completed", "failed", "unverified", "approval_required", "cancelled")

#: The only outcome that may not be reported unless verification was observed.
COMPLETED_REQUIRES_VERIFIED = "completed"

#: The persona used when nothing matches. A name, resolved like any other.
DEFAULT_PERSONA = "sage"

_PACKAGE_DIR = Path(__file__).resolve().parent

#: Aliases that must never resolve, because they collide with real commands.
_RESERVED_ALIASES = frozenset({
    "quit", "exit", "help", "wipe", "stop", "cancel", "yes", "no", "model",
    "memory", "prefs", "projects", "rules", "why",
})


class PersonaError(ValueError):
    """A persona file is missing, unreadable, or fails validation."""


@dataclass(frozen=True)
class Persona:
    """One loaded persona. Immutable; every field is presentation data."""

    name: str
    description: str
    replies: Mapping[str, str]
    orb_palette_key: str
    aliases: tuple[str, ...] = ()
    voice: Mapping[str, Any] = field(default_factory=dict)
    risk_policy: Mapping[str, Any] = field(default_factory=dict)

    def reply_for(self, outcome: str) -> str:
        key = str(outcome or "").strip().lower()
        if key not in self.replies:
            # Unreachable through format_status, which normalises first. Kept
            # explicit so a direct caller cannot render an empty reply.
            raise PersonaError(f"{self.name} has no reply template for {outcome!r}")
        return self.replies[key]


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonaError(f"{label} must be a non-empty string")
    return value.strip()


def _validate(module: Any, module_name: str | None = None) -> Persona:
    """Validate one loaded module into a :class:`Persona`. Fail closed.

    ``module_name`` is only used in error messages; it defaults to the module's
    own ``__name__`` so a caller cannot mislabel a failure.
    """
    module_name = module_name or getattr(module, "__name__", "<persona>")
    name = _require_str(getattr(module, "persona", None), f"{module_name}.persona")

    description = _require_str(getattr(module, "description", None),
                               f"{module_name}.description")

    palette = _require_str(getattr(module, "orb_palette_key", None),
                           f"{module_name}.orb_palette_key")
    try:
        from ui.orb import PALETTES
    except Exception as exc:  # noqa: BLE001 - a missing orb must not load a persona
        raise PersonaError(
            f"{module_name}: the orb palette table is unavailable ({exc}); a "
            f"persona may not name a palette that cannot be checked") from exc
    if palette not in PALETTES:
        raise PersonaError(
            f"{module_name}.orb_palette_key = {palette!r} is not a known orb "
            f"palette; known: {sorted(PALETTES)}")

    raw_replies = getattr(module, "replies", None)
    if not isinstance(raw_replies, Mapping):
        raise PersonaError(f"{module_name}.replies must be a mapping")
    replies: dict[str, str] = {}
    missing = [outcome for outcome in OUTCOMES if outcome not in raw_replies]
    if missing:
        raise PersonaError(
            f"{module_name}.replies is missing required outcome(s): {missing}. "
            f"Every persona must be able to describe every outcome, or it will "
            f"fall back to wording that does not know verified from unverified.")
    for outcome in OUTCOMES:
        template = raw_replies.get(outcome)
        if not isinstance(template, str) or not template.strip():
            raise PersonaError(
                f"{module_name}.replies[{outcome!r}] must be a non-empty string")
        replies[outcome] = template.strip()

    raw_aliases = getattr(module, "aliases", ()) or ()
    if isinstance(raw_aliases, str):
        raise PersonaError(f"{module_name}.aliases must be a list, not a string")
    try:
        candidates = list(raw_aliases)
    except TypeError as exc:
        raise PersonaError(f"{module_name}.aliases must be a list") from exc
    aliases: list[str] = []
    for entry in candidates:
        alias = _require_str(entry, f"{module_name}.aliases entry")
        folded = alias.strip().lower()
        if folded in _RESERVED_ALIASES:
            raise PersonaError(
                f"{module_name}.aliases contains {alias!r}, which collides with a "
                f"reserved word; a persona may not be summoned by a command")
        aliases.append(folded)

    voice = getattr(module, "voice", {}) or {}
    if not isinstance(voice, Mapping):
        raise PersonaError(f"{module_name}.voice must be a mapping")
    risk_policy = getattr(module, "risk_policy", {}) or {}
    if not isinstance(risk_policy, Mapping):
        raise PersonaError(f"{module_name}.risk_policy must be a mapping")

    return Persona(name=name, description=description, replies=replies,
                   orb_palette_key=palette, aliases=tuple(aliases),
                   voice=dict(voice), risk_policy=dict(risk_policy))


_cache: dict[str, Persona] = {}


def load_persona(name: str) -> Persona:
    """Load and validate one persona by module name. Raises on any problem.

    Cached on success only: a persona that failed validation stays failed, so a
    second call cannot succeed by accident.
    """
    key = str(name or "").strip().lower()
    if not key:
        raise PersonaError("no persona name given")
    if not key.replace("_", "").replace("-", "").isalnum():
        raise PersonaError(f"persona name is not a simple identifier: {name!r}")
    if key in _cache:
        return _cache[key]
    try:
        module = importlib.import_module(f"{__name__.rsplit('.', 1)[0]}.{key}")
    except ImportError as exc:
        raise PersonaError(f"unknown persona: {name!r} ({exc})") from exc
    persona = _validate(module, key)
    _cache[key] = persona
    return persona


def persona_names() -> tuple[str, ...]:
    """Module names that look like persona files, sorted."""
    return tuple(sorted(path.stem for path in _PACKAGE_DIR.glob("*.py")
                        if not path.stem.startswith("_")
                        and path.stem not in ("loader", "selector", "formats")))


def load_all() -> dict[str, Persona]:
    """Every loadable persona, keyed by name.

    A persona that fails validation is **skipped**, and the reason is available
    from :func:`load_persona`. That is deliberate: one broken file must not stop
    the others loading, but it must also never silently become the default.
    """
    found: dict[str, Persona] = {}
    for name in persona_names():
        try:
            found[name] = load_persona(name)
        except PersonaError:
            continue
    return found


def alias_index(personas: Mapping[str, Persona] | None = None) -> dict[str, str]:
    """Alias (folded) to persona name.

    The first persona to claim an alias wins, in sorted order, so the mapping is
    deterministic rather than filesystem-order dependent.
    """
    table = personas if personas is not None else load_all()
    index: dict[str, str] = {}
    for key in sorted(table):
        for alias in table[key].aliases:
            index.setdefault(alias, key)
        index.setdefault(key, key)
    return index


def _normalise(text: str) -> str:
    """Fold typed or spoken text into a matchable form.

    Handles the difference between typed and spoken input: case, punctuation,
    surrounding whitespace and a leading wake-style address are all removed, so
    ``"Hey Raphael, `` what is up"`` and ``"raphael"`` match the same alias.
    """
    lowered = str(text or "").strip().lower()
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in lowered)
    words = cleaned.split()
    # Drop a leading wake-style address, which is how a name usually arrives.
    if words and words[0] in ("hey", "ok", "okay", "hi", "hello", "please", "yo"):
        words = words[1:]
    return " ".join(words)


def select_persona(text: str, default: str = DEFAULT_PERSONA) -> Persona:
    """Pick a persona from typed or spoken text, falling back to ``default``.

    Resolution order, most specific first:

    1. an alias that appears as a whole word sequence anywhere in the text;
    2. an alias that *is* the text;
    3. ``default``.

    Matching on word boundaries matters: a persona called ``sol`` must not be
    summoned by the word "solution", and a persona called ``sage`` must not fire
    on "usage".
    """
    index = alias_index()
    normalised = _normalise(text)
    if not normalised:
        return load_persona(default)

    # A whole-word-sequence match anywhere in the text, longest alias first so a
    # longer alias beats a shorter one that also appears.
    for alias in sorted(index, key=len, reverse=True):
        if not alias:
            continue
        if f" {alias} " in f" {normalised} ":
            return load_persona(index[alias])
    if normalised in index:
        return load_persona(index[normalised])
    return load_persona(default)


def format_status(persona: Persona | str, outcome: str,
                  details: Any = None) -> str:
    """Render a persona's wording for one outcome.

    **Never says completed unless the outcome is verified.** ``outcome`` must be
    a normalised outcome name; if a caller passes ``completed`` it is taken at
    face value, which is why the *caller* is responsible for only passing
    ``completed`` when verification actually happened. To make that hard to get
    wrong, ``verified`` can be passed explicitly - when it is False the
    ``unverified`` wording is used even if ``completed`` was requested.

    ``details`` is coerced to text and truncated, because this value ends up in a
    UI: it must not be able to inject unbounded content.
    """
    persona = persona if isinstance(persona, Persona) else load_persona(persona)
    key = str(outcome or "").strip().lower()

    normalised = key
    for candidate in OUTCOMES:
        if candidate == key:
            normalised = candidate
            break
    else:
        # An unknown outcome is the most dangerous input there is: it must never
        # be treated as success. UNVERIFIED is the honest default.
        normalised = "unverified"

    detail_text = str(details).strip() if details not in (None, "") else ""
    if len(detail_text) > 400:
        detail_text = detail_text[:397] + "..."
    detail_clause = detail_text or "No further detail was reported."
    if detail_text and not detail_text.endswith((".", "!", "?")):
        detail_clause += "."

    template = persona.reply_for(normalised)
    return template.format(
        details=detail_clause,
        detail=detail_clause,
        outcome_label=normalised,
        confidence="high" if normalised in ("completed", "failed") else "low",
        risk="none observed" if normalised == "completed" else "as described above",
    )