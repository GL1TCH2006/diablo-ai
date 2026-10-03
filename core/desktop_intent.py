"""M2.16.UI.1 - desktop intent families and model-facing tool routing.

Why this module exists
======================

The M2.16.UI audit reported that real-model desktop-tool selection was "0/3". That
number was real but its *explanation* was wrong, and correcting it is the reason
this module is here.

What actually happened
----------------------

All three audited runs never consulted a model. ``Planner.fast_intent`` matches
``^(?:open|launch|start)\\s+(.+)$`` and short-circuits to ``applications.open``
*before* any LLM call. Two consequences:

1. "The model prefers ``applications.open``" was not a model preference at all -
   a regex chose it. The audit also reported 8b passing the whole sentence as the
   application name; that was the same regex being greedy, not a model defect.
2. Because nothing was ever routed, the deeper fault stayed invisible: under the
   default configuration ``desktop.open``/``find``/``interact``/``read``/``verify``
   are **never presented to the model at all**. ``Agent._requested_capabilities``
   could only ever emit ``debugging``, ``coding`` or ``version_control``, so the
   ``desktop`` capability group in ``core.tool_context`` was unreachable. A
   tool the model cannot see is a tool it cannot select.

So the gap is a *reachability* problem before it is a preference problem, and this
module is what makes the desktop capability family reachable.

What this is, and is not
========================

A bounded, table-driven classifier over compiled patterns - **not** an NLP system,
and not a replacement for the planner. It answers exactly one question: *which
desktop intent family does this request belong to?* Everything downstream (which
tools to present, whether the model's choice was a mismatch) is derived from that
one answer.

Design constraints, all deliberate:

* **Structured signal, not text routing inside the selector.** The classifier runs
  here, outside ``ToolContextSelector.select``, and hands it a resolved signal.
  ``select`` keeps receiving symbols, never user prose - an existing architectural
  test asserts it never inspects user text itself, and that property is worth
  keeping.
* **Uncertainty widens.** A request that matches nothing confidently returns
  ``NONE`` with low confidence, and the caller then presents the whole desktop
  family rather than a narrow guess.
* **A family is an expectation, not a permission.** Mapping a request to
  ``UI_ACTION`` does not authorise anything; it only decides which tools the model
  is *shown*. Permission, cancellation and verification remain where they were.
* **No handle, no fabrication.** Nothing here produces or accepts a native handle,
  a PID or a runtime id, and no target identity is ever synthesised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "DesktopIntentFamily",
    "DesktopIntent",
    "DesktopRoutingState",
    "classify_desktop_intent",
    "expected_tool_names",
    "tool_intent_families",
    "detect_tool_mismatch",
    "ToolSelectionMismatch",
    "DESKTOP_TOOL_NAMES",
]


class DesktopIntentFamily(str, Enum):
    """The bounded set of desktop request shapes.

    Deliberately small. A larger taxonomy would be a taxonomy nobody maintains,
    and every extra family widens the tool set for requests that did not need it.
    """

    NONE = "none"
    #: Start a program and nothing else.
    APPLICATION_LAUNCH = "application_launch"
    #: Act on the window itself: focus, minimise, resize, switch to.
    WINDOW_MANAGEMENT = "window_management"
    #: Locate a window or a named control.
    UI_DISCOVERY = "ui_discovery"
    #: Act on a control: click, press, set, toggle, select.
    UI_ACTION = "ui_action"
    #: Send keys or type text.
    KEYBOARD_INTERACTION = "keyboard_interaction"
    #: Move or click the pointer.
    POINTER_INTERACTION = "pointer_interaction"
    #: Capture or read the screen / display state.
    SCREEN_DISPLAY = "screen_display"
    #: A launch plus something else, i.e. more than one phase of work.
    COMPOUND_DESKTOP_TASK = "compound_desktop_task"

    @property
    def is_desktop(self) -> bool:
        return self is not DesktopIntentFamily.NONE


#: Every tool that can reach the desktop. Kept here so routing, mismatch detection
#: and tests all agree on one list rather than three.
DESKTOP_TOOL_NAMES: tuple[str, ...] = (
    "applications.open",
    "desktop.open",
    "desktop.find",
    "desktop.interact",
    "desktop.read",
    "desktop.verify",
)

#: Intent family -> the tools that can satisfy it, most specific first.
#:
#: Kept small on purpose. A family that lists every desktop tool is no better than
#: presenting them all, which is the thing this milestone exists to avoid.
_FAMILY_TOOLS: dict[DesktopIntentFamily, tuple[str, ...]] = {
    DesktopIntentFamily.APPLICATION_LAUNCH: ("applications.open", "desktop.open"),
    DesktopIntentFamily.WINDOW_MANAGEMENT: (
        "desktop.find", "desktop.interact", "desktop.verify", "desktop.read",
    ),
    DesktopIntentFamily.UI_DISCOVERY: ("desktop.find", "desktop.read"),
    DesktopIntentFamily.UI_ACTION: (
        "desktop.interact", "desktop.find", "desktop.read", "desktop.verify",
    ),
    DesktopIntentFamily.KEYBOARD_INTERACTION: (
        "desktop.interact", "desktop.find",
    ),
    DesktopIntentFamily.POINTER_INTERACTION: (
        "desktop.interact", "desktop.find",
    ),
    DesktopIntentFamily.SCREEN_DISPLAY: (
        "desktop.read", "desktop.find", "capability.execute",
    ),
    DesktopIntentFamily.COMPOUND_DESKTOP_TASK: (
        "desktop.open", "desktop.find", "desktop.interact",
        "desktop.read", "desktop.verify", "applications.open",
    ),
    DesktopIntentFamily.NONE: DESKTOP_TOOL_NAMES,
}

#: The reverse map: which families a given tool can legitimately serve.
#:
#: ``applications.open`` is deliberately absent from every family except launch
#: and compound. It cannot click, type or read, so treating it as a valid answer
#: to "click Save in Notepad" is precisely the mismatch this detects.
_TOOL_TO_FAMILIES: dict[str, frozenset[DesktopIntentFamily]] = {
    "applications.open": frozenset({
        DesktopIntentFamily.APPLICATION_LAUNCH,
        DesktopIntentFamily.COMPOUND_DESKTOP_TASK,
    }),
    "desktop.open": frozenset({
        DesktopIntentFamily.APPLICATION_LAUNCH,
        DesktopIntentFamily.COMPOUND_DESKTOP_TASK,
    }),
    "desktop.find": frozenset({
        DesktopIntentFamily.WINDOW_MANAGEMENT,
        DesktopIntentFamily.UI_DISCOVERY,
        DesktopIntentFamily.UI_ACTION,
        DesktopIntentFamily.KEYBOARD_INTERACTION,
        DesktopIntentFamily.POINTER_INTERACTION,
        DesktopIntentFamily.SCREEN_DISPLAY,
        DesktopIntentFamily.COMPOUND_DESKTOP_TASK,
    }),
    "desktop.interact": frozenset({
        DesktopIntentFamily.WINDOW_MANAGEMENT,
        DesktopIntentFamily.UI_ACTION,
        DesktopIntentFamily.KEYBOARD_INTERACTION,
        DesktopIntentFamily.POINTER_INTERACTION,
        DesktopIntentFamily.COMPOUND_DESKTOP_TASK,
    }),
    "desktop.read": frozenset({
        DesktopIntentFamily.WINDOW_MANAGEMENT,
        DesktopIntentFamily.UI_DISCOVERY,
        DesktopIntentFamily.UI_ACTION,
        DesktopIntentFamily.SCREEN_DISPLAY,
        DesktopIntentFamily.COMPOUND_DESKTOP_TASK,
    }),
    "desktop.verify": frozenset({
        DesktopIntentFamily.WINDOW_MANAGEMENT,
        DesktopIntentFamily.UI_ACTION,
        DesktopIntentFamily.COMPOUND_DESKTOP_TASK,
    }),
    "capability.execute": frozenset({DesktopIntentFamily.SCREEN_DISPLAY}),
}

#: The verbs that start a program. Deliberately narrower than the general English
#: sense of "run": ``Planner.fast_intent`` has always short-circuited on
#: open/launch/start only, and widening this set would route "run my test suite" to
#: an application launcher - a regression introduced by being more thorough here.
_LAUNCH_VERB = r"(?:open|launch|start)"
_ACTION_VERB = (
    r"(?:click|press|select|toggle|check|uncheck|type|enter|fill|set|"
    r"double[\s-]?click|right[\s-]?click|hit|activate)"
)
_DISCOVERY_VERB = r"(?:find|locate|search\s+for|look\s+for|identify|spot)"
_WINDOW_VERB = (
    r"(?:focus|activate|minimi[sz]e|maximi[sz]e|resize|move|close|switch\s+to|"
    r"bring\s+up|foreground)"
)
_KEY_VERB = r"(?:type|press|hit|enter\s+text|keyboard|shortcut|hotkey)"
_POINTER_VERB = r"(?:move\s+(?:the\s+)?(?:mouse|cursor|pointer)|double[\s-]?click|right[\s-]?click)"
_SCREEN_VERB = r"(?:screenshot|screen[\s-]?shot|capture|capture\s+the\s+screen|resolution|display)"

#: A trailing clause means the request is more than "start this program".
_TRAILING_CLAUSE = re.compile(
    r"(?:\band\s+(?:then|also)?|\bthen\b|,\s*(?:then|and|after)\b|"
    r"\bafter\s+that\b|\bfollowed\s+by\b)",
    re.I,
)

#: Punctuation that ends a request. A capture that spans one of these is not an
#: application name.
_CLAUSE_BREAK = re.compile(r"[.;!?]|,\s|\b(?:and|then|after|but|so)\b", re.I)

#: Sentence-final punctuation, stripped before the clause test. Without this,
#: "Open Calculator." looked like a launch *plus a clause*, because the closing
#: full stop matched ``_CLAUSE_BREAK`` - which classified every punctuated request
#: as compound.
_TERMINAL_PUNCTUATION = re.compile(r"[\s.!?;:]+$")

#: The verbs that start a program. Deliberately narrower than the general English
#: sense of "run": ``Planner.fast_intent`` has always short-circuited on
#: open/launch/start only, and widening this set would route "run my test suite" to
#: an application launcher - a regression introduced by being more thorough here.
_LAUNCH_VERBS = r"(?:open|launch|start)"


@dataclass(frozen=True)
class DesktopIntent:
    """The classified intent, plus enough evidence to explain the decision."""

    family: DesktopIntentFamily = DesktopIntentFamily.NONE
    confidence: float = 0.0
    #: Names of the patterns that fired, in match order. Diagnostic only.
    signals: tuple[str, ...] = ()
    #: Application named by the request, when one was cleanly isolated.
    app_name: str = ""
    #: The whole request was more than one phase of work.
    compound: bool = False
    #: Tools this intent expects to be able to use.
    expected_tools: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_desktop(self) -> bool:
        return self.family.is_desktop

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "confidence": round(float(self.confidence), 3),
            "signals": list(self.signals),
            "app_name": self.app_name,
            "compound": self.compound,
            "expected_tools": list(self.expected_tools),
        }

    def __bool__(self) -> bool:
        """True when a desktop family was recognised.

        Lets callers write ``if classify_desktop_intent(text):`` without caring
        that the enum has a NONE member, which is not "no intent" but "unclassified".
        """
        return self.family is not DesktopIntentFamily.NONE


# ═════════════════════════════════════════════════════ patterns

#: Each entry is (family, compiled pattern, base confidence). Ordered most specific
#: first: the first family that fires wins, and ties are resolved toward the earlier
#: entry rather than by scoring, so classification is deterministic.
_PATTERNS: tuple[tuple[DesktopIntentFamily, re.Pattern[str], float], ...] = (
    # A window verb is more specific than a generic action verb, so it is tested
    # first; "close Notepad" is window management, not "interact with a control".
    (DesktopIntentFamily.WINDOW_MANAGEMENT, re.compile(
        rf"\b{_WINDOW_VERB}\b.*\b(?:window|app|application|notepad|calculator|"
        rf"explorer|terminal|editor)\b", re.I), 0.75),
    # Keyboard shortcuts: an explicit key token is a strong, unambiguous signal.
    (DesktopIntentFamily.KEYBOARD_INTERACTION, re.compile(
        r"\b(?:press|hit|type|send)\b[^.]{0,24}\b(?:ctrl|alt|shift|cmd|command|"
        r"win|super|meta|f\d{1,2}|tab|enter|return|escape|esc|backspace|delete|"
        r"home|end|page\s?up|page\s?down)\b", re.I), 0.85),
    (DesktopIntentFamily.KEYBOARD_INTERACTION, re.compile(
        rf"\b{_KEY_VERB}\b", re.I), 0.6),
    (DesktopIntentFamily.POINTER_INTERACTION, re.compile(
        rf"\b{_POINTER_VERB}\b", re.I), 0.7),
    (DesktopIntentFamily.SCREEN_DISPLAY, re.compile(
        rf"\b{_SCREEN_VERB}\b", re.I), 0.7),
    (DesktopIntentFamily.UI_DISCOVERY, re.compile(
        rf"\b{_DISCOVERY_VERB}\b[^.]{{0,40}}\b(?:button|field|edit|checkbox|"
        rf"menu|item|tab|control|window|dialog|element|text|label)\b", re.I), 0.8),
    (DesktopIntentFamily.UI_DISCOVERY, re.compile(
        rf"\b{_DISCOVERY_VERB}\b[^.]{{0,30}}\b(?:in|on|inside|within)\b", re.I), 0.6),
    (DesktopIntentFamily.UI_ACTION, re.compile(
        rf"\b{_ACTION_VERB}\b[^.]{{0,40}}\b(?:button|field|edit|checkbox|"
        rf"menu|item|tab|control|window|dialog|link|icon)\b", re.I), 0.8),
    # "Click Save in Notepad" - the object is named but is not one of the control
    # nouns above ("Save" is a label, not "button"), so the preposition carries the
    # signal instead.
    (DesktopIntentFamily.UI_ACTION, re.compile(
        rf"\b{_ACTION_VERB}\b\s+\w+(?:\s+\w+){{0,2}}\s+(?:in|on|inside|within)\s+"
        rf"(?:the\s+)?[\w' ]+", re.I), 0.8),
    (DesktopIntentFamily.UI_ACTION, re.compile(
        rf"\b{_ACTION_VERB}\b\s+(?:on|in|inside|within)\b", re.I), 0.75),
    (DesktopIntentFamily.UI_ACTION, re.compile(
        rf"\b{_ACTION_VERB}\b\s+the\s+\w+", re.I), 0.6),
)

#: Demonstratives and vague referents. "Open this" cannot name a program, so
#: treating it as a name guarantees a failed launch instead of routing the request
#: to something that can act on "this". These fall through to the model, which can
#: ask, rather than short-circuiting into a doomed launch.
_UNRESOLVABLE_NAMES = frozenset({
    "this", "that", "it", "them", "these", "those", "one",
    "something", "anything", "the app", "the application", "the program",
    "app", "application", "program",
})

_LAUNCH_ONLY = re.compile(rf"^\s*{_LAUNCH_VERB}\s+(?:the\s+)?(?P<app>[^.;!?]+?)\s*[.!]?\s*$", re.I)
_LAUNCH_ANY = re.compile(rf"^\s*{_LAUNCH_VERB}\s+(?:the\s+)?(?P<app>.+)$", re.I)


def _extract_app_name(text: str) -> str:
    """Best-effort isolation of an application name from a launch request.

    Returns "" when the request is not a clean single application name. Refusing to
    guess is the point: the greedy capture that produced the application name
    "Calculator, then verify that it is open" is the defect being fixed.
    """
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    only = _LAUNCH_ONLY.match(clean)
    if only:
        candidate = only.group("app").strip(" \t\"'.,")
    else:
        loose = _LAUNCH_ANY.match(clean)
        if not loose:
            return ""
        candidate = loose.group("app").strip(" \t\"'.,")
    # A candidate containing a clause boundary is not a name. This is the specific
    # guard that stops "Open Calculator, then verify that it is open" becoming an
    # application called "Calculator, then verify that it is open".
    if not candidate:
        return ""
    if _CLAUSE_BREAK.search(candidate):
        head = _CLAUSE_BREAK.split(candidate, maxsplit=1)[0].strip()
        # Only the head counts, and only if it is a plausible single name.
        candidate = head
    if not candidate or len(candidate) > 64:
        return ""
    # More than a handful of words is a sentence, not a program name.
    if len(candidate.split()) > 4:
        return ""
    if candidate.lower() in _UNRESOLVABLE_NAMES:
        return ""
    return candidate


def classify_desktop_intent(text: str) -> DesktopIntent:
    """Classify one request into a desktop intent family.

    Never raises. Returns a ``NONE`` intent for anything unrecognised, which the
    caller must treat as "widen the tool set", never as "this is not a desktop
    request, offer nothing".
    """
    clean = " ".join(str(text or "").split())
    if not clean:
        # Still widen. An empty request must not be answered with an empty tool
        # set, and the early return used to skip expected_tools entirely, which
        # handed the caller an intent carrying no expectation at all.
        return DesktopIntent(
            family=DesktopIntentFamily.NONE,
            signals=("empty",),
            expected_tools=expected_tool_names(DesktopIntentFamily.NONE),
        )

    signals: list[str] = []
    family = DesktopIntentFamily.NONE
    confidence = 0.0

    app_name = _extract_app_name(clean)
    is_launch_shape = bool(_LAUNCH_ANY.match(clean))
    # Sentence-final punctuation is not a clause. Testing the raw string made
    # "Open Calculator." a compound request, because the closing full stop matched
    # the clause-break pattern.
    clause_text = _TERMINAL_PUNCTUATION.sub("", clean)
    has_trailing_clause = bool(_TRAILING_CLAUSE.search(clause_text)) or bool(
        _CLAUSE_BREAK.search(clause_text))

    # Non-launch verbs win over launch shape. "Open Notepad and click Save" is a
    # compound task, not an application launch, and must not be short-circuited.
    for candidate_family, pattern, base in _PATTERNS:
        if pattern.search(clean):
            family = candidate_family
            confidence = base
            signals.append(candidate_family.value)
            break

    if family is DesktopIntentFamily.NONE and is_launch_shape:
        if has_trailing_clause:
            # A launch plus more: one phase of work is not the whole request.
            family = DesktopIntentFamily.COMPOUND_DESKTOP_TASK
            confidence = 0.7
            signals.append("launch_with_trailing_clause")
        elif app_name:
            family = DesktopIntentFamily.APPLICATION_LAUNCH
            confidence = 0.9
            signals.append("launch_only")
        else:
            # "Open it." - a launch shape we cannot resolve to a name. Compound is
            # the honest answer: something is being asked beyond the launch.
            family = DesktopIntentFamily.COMPOUND_DESKTOP_TASK
            confidence = 0.4
            signals.append("launch_unresolved")

    compound = family is DesktopIntentFamily.COMPOUND_DESKTOP_TASK or (
        has_trailing_clause and family.is_desktop)

    if compound and family is not DesktopIntentFamily.COMPOUND_DESKTOP_TASK:
        # Widen: a request that also does something else needs the whole family.
        family = DesktopIntentFamily.COMPOUND_DESKTOP_TASK
        signals.append(f"promoted_from:{signals[0] if signals else 'unknown'}")
        confidence = max(confidence, 0.6)

    expected = expected_tool_names(family)
    if not signals:
        signals.append("unclassified")
    return DesktopIntent(
        family=family,
        confidence=round(confidence, 3),
        signals=tuple(signals),
        app_name=app_name,
        compound=compound,
        expected_tools=expected,
    )


def expected_tool_names(family: DesktopIntentFamily) -> tuple[str, ...]:
    """Tools that can plausibly satisfy ``family``.

    ``NONE`` returns every desktop tool: unclassified must widen, never narrow.
    """
    try:
        key = DesktopIntentFamily(family)
    except ValueError:
        key = DesktopIntentFamily.NONE
    return _FAMILY_TOOLS.get(key, DESKTOP_TOOL_NAMES)


def tool_intent_families(tool_name: str) -> frozenset[DesktopIntentFamily]:
    """Which intent families ``tool_name`` can legitimately serve."""
    return _TOOL_TO_FAMILIES.get(str(tool_name or ""), frozenset())


# ═════════════════════════════════════════════════════ mismatch detection


@dataclass(frozen=True)
class ToolSelectionMismatch:
    """A selected tool that cannot serve the request's intent family.

    Reported, never silently corrected. The bounded recovery path decides what to
    do; this type only states the fact and the evidence.
    """

    requested: DesktopIntentFamily
    selected_tool: str
    expected_tools: tuple[str, ...]
    reason: str = ""

    @property
    def is_mismatch(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested.value,
            "selected_tool": self.selected_tool,
            "expected_tools": list(self.expected_tools),
            "reason": self.reason,
        }


def detect_tool_mismatch(intent: DesktopIntent, selected_tool: str) -> ToolSelectionMismatch | None:
    """Whether ``selected_tool`` cannot serve ``intent``.

    Returns ``None`` when the choice is acceptable, when the intent is
    unclassified, or when the tool is not a desktop tool at all - a non-desktop
    tool is a different kind of wrong and is not this check's business.
    """
    if intent is None or intent.family is DesktopIntentFamily.NONE:
        return None
    name = str(selected_tool or "")
    served = tool_intent_families(name)
    if not served:
        return None
    if intent.family in served:
        return None
    expected = intent.expected_tools or expected_tool_names(intent.family)
    return ToolSelectionMismatch(
        requested=intent.family,
        selected_tool=name,
        expected_tools=expected,
        reason=(
            f"{name} can serve {sorted(f.value for f in served)} "
            f"but the request was classified as {intent.family.value}"
        ),
    )


@dataclass
class DesktopRoutingState:
    """Per-request desktop routing decisions, owned by the Agent.

    Why this is not on ``RequestContext``: that dataclass has a pinned nine-field
    contract (``tests/test_knowledge_p9_architecture.py`` -
    ``test_it_has_exactly_nine_fields``, whose assertion message is "new state
    belongs elsewhere"). The Agent already keeps routing decisions beside
    ``_tool_context_decision`` and ``_active_plan`` rather than on the context,
    and this follows that precedent.

    Reset at the start of every request, so nothing here can leak from one turn
    into the next.
    """

    #: Upper bound on retained mismatch records.
    MAX_MISMATCHES = 20

    intent: DesktopIntent | None = None
    corrections_used: int = 0
    mismatches: list[ToolSelectionMismatch] = field(default_factory=list)

    def record(self, mismatch: ToolSelectionMismatch) -> None:
        self.mismatches.append(mismatch)
        if len(self.mismatches) > self.MAX_MISMATCHES:
            del self.mismatches[: len(self.mismatches) - self.MAX_MISMATCHES]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict() if self.intent else None,
            "corrections_used": int(self.corrections_used),
            "mismatches": [m.to_dict() for m in self.mismatches],
        }

    def reset(self) -> None:
        self.intent = None
        self.corrections_used = 0
        self.mismatches.clear()