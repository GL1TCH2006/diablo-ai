"""Persona profiles: presentation data, deliberately not wired into the agent.

    >>> from personas import select_persona, format_status
    >>> p = select_persona("hey raphael, what is up")
    >>> p.name
    'raphael'
    >>> format_status(p, "unverified", "the file was written but nothing checked it")
    'This finished, but it was not independently verified, ...'

What this package is
====================

Two personas (``raphael``, ``sage``) with reply templates per outcome, an orb
palette key and an optional voice hint, plus a strict loader and a selector.

What this package is **not**
===========================

It is **not wired into the agent, the Executor or ``main.py``**, and nothing in
``config/permissions.py`` imports it. A persona cannot change what a tool is
allowed to do - ``tests/test_personas.py`` proves that by running identical
permission decisions under every persona and asserting they come out identical.

Recommended wiring points, when this is wanted
==============================================

1. **Voice of the reply only.** In ``core/agent.py``, where the final text is
   assembled for the user (``_respond`` / the ``return`` at
   ``Agent.respond``'s end), wrap the reply text in ``format_status`` rather than
   post-processing it afterwards. Doing it at the end means the *agent's* own
   wording still drives behaviour, and only the surface text is replaced.

2. **Selection.** In ``ui/terminal.py`` (``:model``-style command handling) and
   ``ui/companion_state.py`` (``submit_task``), resolve
   ``select_persona(text)`` once per request and pass the persona down as
   presentation data. Do not store it on the controller as authority.

3. **Orb palette.** In the orb state mapper (``ui/orb/state_map.py``), after it
   has chosen a visual state, map ``persona.orb_palette_key`` onto
   ``ui.orb.PALETTES``. The visual state must still be chosen by the projection;
   the persona may only choose the palette.

4. **Never** in ``config/permissions.py``, ``core/executor.py`` or
   ``core/verification_ledger.py``. Those are the boundaries a persona must not
   reach.

NEEDS HUMAN DECISION
====================

**The persona names are not decided.** ``raphael`` and ``sage`` are working
placeholders chosen to be short and unambiguous. The alias lists in each profile
file are the configuration point: edit ``aliases`` and nothing else changes.
The following are open questions for a human:

* the canonical names, and whether they should be single words or two;
* whether an alias may be a multi-word phrase (``"the formal one"``) - currently
  allowed, and matched on word boundaries;
* whether an unmatched request should fall back to a fixed persona or to the last
  persona explicitly chosen by the user;
* whether ``sage`` should be the default, given ``DEFAULT_PERSONA = "sage"``.
"""

from __future__ import annotations

from .loader import (
    COMPLETED_REQUIRES_VERIFIED,
    DEFAULT_PERSONA,
    OUTCOMES,
    Persona,
    PersonaError,
    alias_index,
    format_status,
    load_all,
    load_persona,
    persona_names,
    select_persona,
)

__all__ = [
    "COMPLETED_REQUIRES_VERIFIED",
    "DEFAULT_PERSONA",
    "OUTCOMES",
    "Persona",
    "PersonaError",
    "alias_index",
    "format_status",
    "load_all",
    "load_persona",
    "persona_names",
    "select_persona",
]