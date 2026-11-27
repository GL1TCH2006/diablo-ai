"""M2.16.UI - the narrow, redacted event stream from backend to companion (Part 10).

The backend already speaks a rich event language. ``ui.bridge.CoreEventBusBridge``
emits ``AgentEvent`` objects whose ``metadata`` carries the *entire* raw source
event from the ``EventBus`` - tool payloads, capability detail, whatever the
producing subsystem put there.

That metadata is exactly what Part 10 forbids handing to a presentation layer:

* raw model prompts,
* native handles,
* credentials,
* arbitrary application text that a UI might mistake for instructions.

So this module is a narrowing boundary. It converts whatever the backend produced
into the small closed vocabulary in :mod:`ui.companion_state`, and it is the only
place that decides that text is safe to put on screen.

Three jobs
==========

**Recognise.** Backend event names are accepted from three sources at once, because
all three are real in this repository: the conceptual names the milestone prompt
lists, the ``AgentEvent.type`` values the existing bridge actually emits, and the
``UIStatus`` values that ride along on them.

**Narrow.** Anything without a mapping is dropped rather than passed through. An
unrecognised event is not rendered, which is what stops the UI from growing into a
log viewer.

**Redact.** :func:`sanitize_text` is applied to every string that reaches a
companion event, and metadata is never forwarded at all.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from typing import Any

from ui.companion_state import CompanionEvent, CompanionEventKind

__all__ = [
    "map_backend_event",
    "sanitize_text",
    "summarise",
    "MAX_MESSAGE_CHARS",
    "MAX_SUMMARY_CHARS",
]

#: A progress line longer than this is a log, not a status. Part 11 asks for
#: concise progress, so the limit is enforced here rather than trusted to callers.
MAX_MESSAGE_CHARS = 240

#: Summaries sit in a one-line scannable list, so they are much shorter.
MAX_SUMMARY_CHARS = 60


# ═════════════════════════════════════════════════════════ redaction

#: Keys whose *values* are credentials. Matched case-insensitively against
#: ``key=value`` and ``key: value`` shapes in free text.
#:
#: The value pattern deliberately absorbs an optional leading ``Bearer``. Without
#: that, ``Authorization: Bearer <token>`` matched on the word "Bearer" alone and
#: replaced it, leaving the actual token sitting in the message.
#: The value pattern deliberately absorbs an optional leading ``Bearer``, and the
#: separator accepts a copula as well as punctuation. Without either, prose like
#: ``Authorization: Bearer <token>`` matched on the word "Bearer" and
#: ``my api key is <value>`` matched on the word "is" - both redacting the wrong
#: token and leaving the secret in place.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_\- ]?key|apikey|access[_\- ]?key|secret|token|password|passwd|"
    r"passphrase|credential|bearer|authorization|auth[_\- ]?token|session[_\- ]?key)\b"
    r"(\s*[:=]\s*|\s+(?:is|was|are|were)\s+|\s+)"
    r"((?:bearer\s+)?\S+)"
)

#: Provider-style key material that appears without a label. GitHub and Slack
#: prefixes use an underscore where OpenAI-style prefixes use a hyphen, so both
#: separators are accepted.
_BARE_SECRET = re.compile(r"\b(?:sk|rk|pk|ghp|gho|ghu|ghs|ghr|glpat|xox[baprs])[_-][A-Za-z0-9._-]{8,}")

#: ``Authorization: Bearer eyJ...`` where the label itself is not a known word.
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

#: Native handle and process identity values. These are meaningless to a person
#: and are the Part 10 "no raw native handles" rule expressed as text.
#:
#: Group 1 is the label and group 2 the separator. The substitution uses group 1 on
#: purpose so the line still reads "hwnd [handle]" - dropping the label too left a
#: bare "= [handle]", which tells a reader nothing.
_HANDLE_VALUE = re.compile(
    r"(?i)\b(hwnd|handle|runtime[_-]?id|process[_-]?id|thread[_-]?id|pid|"
    r"window[_-]?id|desktop[_-]?id)\b(\s*[:=]\s*|\s+)(0x[0-9a-f]+|\d{3,})"
)

#: Any bare hexadecimal address, which is what a handle looks like in a log line.
_BARE_HANDLE = re.compile(r"\b0x[0-9A-Fa-f]{6,}\b")

#: Control characters, including the newlines that make a log line spill across
#: several UI rows. Tab and newline both become a single space.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")

#: Values substituted in. Deliberately uniform so a redacted line is obvious.
_REDACTED = "[redacted]"
_HANDLE = "[handle]"


def sanitize_text(value: Any, *, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Return ``value`` as a single-line, redacted, length-capped string.

    Safe to call on anything. Never raises, and never returns an empty string for
    input that had visible characters - over-redaction is preferred to leaking, but
    the common path keeps the text a person needs.
    """
    try:
        text = value if isinstance(value, str) else ("" if value is None else str(value))
    except Exception:  # noqa: BLE001 - a broken __str__ must not break rendering
        text = ""
    if not text:
        return ""
    text = _CONTROL.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    # Order matters: labelled secrets before bare ones, so the label survives as a
    # hint and the value is what gets removed.
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)
    text = _BEARER_TOKEN.sub(f"Bearer {_REDACTED}", text)
    text = _BARE_SECRET.sub(_REDACTED, text)
    text = _HANDLE_VALUE.sub(lambda m: f"{m.group(1)} {_HANDLE}", text)
    text = _BARE_HANDLE.sub(_HANDLE, text)
    cap = max(1, int(limit))
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "\u2026"
    return text


# ═════════════════════════════════════════════════════════ recognition

#: Conceptual Part 10 names, plus the spellings a caller is likely to use.
_NAME_MAP: dict[str, CompanionEventKind] = {
    # request / task admission
    "request_accepted": CompanionEventKind.TASK_ACCEPTED,
    "request_received": CompanionEventKind.TASK_ACCEPTED,
    "task_created": CompanionEventKind.TASK_ACCEPTED,
    "task_accepted": CompanionEventKind.TASK_ACCEPTED,
    # planning
    "planning": CompanionEventKind.THINKING,
    "plan_created": CompanionEventKind.THINKING,
    "thinking": CompanionEventKind.THINKING,
    "tool_selected": CompanionEventKind.THINKING,
    # execution
    "step_started": CompanionEventKind.STEP,
    "step_completed": CompanionEventKind.STEP,
    "step": CompanionEventKind.STEP,
    "executing": CompanionEventKind.EXECUTING,
    "tool_started": CompanionEventKind.STEP,
    "tool_completed": CompanionEventKind.STEP,
    # verification
    "verification_started": CompanionEventKind.VERIFICATION,
    "verification_completed": CompanionEventKind.VERIFICATION,
    "verification": CompanionEventKind.VERIFICATION,
    "verifying": CompanionEventKind.VERIFICATION,
    # recovery
    "recovery_started": CompanionEventKind.RECOVERY,
    "recovery": CompanionEventKind.RECOVERY,
    # permission
    "permission_required": CompanionEventKind.PERMISSION,
    "permission": CompanionEventKind.PERMISSION,
    "awaiting_permission": CompanionEventKind.PERMISSION,
    # cancellation
    "cancellation_requested": CompanionEventKind.CANCELLATION_REQUESTED,
    "cancel_requested": CompanionEventKind.CANCELLATION_REQUESTED,
    "canceling": CompanionEventKind.CANCELLATION_REQUESTED,
    # terminal
    "task_completed": CompanionEventKind.TASK_COMPLETED,
    "task_succeeded": CompanionEventKind.TASK_COMPLETED,
    "task_done": CompanionEventKind.TASK_COMPLETED,
    "task_failed": CompanionEventKind.TASK_FAILED,
    "task_error": CompanionEventKind.TASK_FAILED,
    "error": CompanionEventKind.ERROR,
    "task_cancelled": CompanionEventKind.TASK_CANCELLED,
    "task_canceled": CompanionEventKind.TASK_CANCELLED,
    "cancelled": CompanionEventKind.TASK_CANCELLED,
    "canceled": CompanionEventKind.TASK_CANCELLED,
    # lifecycle
    "activated": CompanionEventKind.ACTIVATED,
    "retracting": CompanionEventKind.RETRACTING,
    "sleeping": CompanionEventKind.SLEEPING,
}

#: ``UIStatus`` values that carry enough meaning to render. ``ready``/``idle``/
#: ``closed`` are absent on purpose: they say nothing a user needs to see, and
#: mapping them would make the progress list flicker on ordinary heartbeats.
_STATUS_MAP: dict[str, CompanionEventKind] = {
    "thinking": CompanionEventKind.THINKING,
    "executing": CompanionEventKind.EXECUTING,
    "verifying": CompanionEventKind.VERIFICATION,
    "success": CompanionEventKind.TASK_COMPLETED,
    "cancelled": CompanionEventKind.TASK_CANCELLED,
    "canceled": CompanionEventKind.TASK_CANCELLED,
    "error": CompanionEventKind.ERROR,
}

#: Tool-level status, as produced by the existing ``tool`` channel. A denied or
#: awaiting-confirmation tool is the one thing the user must act on.
_TOOL_STATUS_MAP: dict[str, CompanionEventKind] = {
    "pending_confirmation": CompanionEventKind.PERMISSION,
    "denied": CompanionEventKind.PERMISSION,
    "blocked": CompanionEventKind.PERMISSION,
    "awaiting_permission": CompanionEventKind.PERMISSION,
    "error": CompanionEventKind.ERROR,
    "failed": CompanionEventKind.ERROR,
    "running": CompanionEventKind.STEP,
    "ok": CompanionEventKind.STEP,
    "completed": CompanionEventKind.STEP,
    "done": CompanionEventKind.STEP,
    # A cancelled *tool* is deliberately unmapped. The task may still be running,
    # and treating one cancelled tool as a terminal task state would make the
    # companion claim an outcome the backend has not reported.
}


def _normalise(name: Any) -> str:
    return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")


#: The *only* metadata keys this module is willing to read.
#:
#: ``AgentEvent.metadata`` carries the backend's entire raw source event - tool
#: payloads, capability detail, whatever the producer put there. Forwarding that to
#: a presentation layer is exactly the Part 10 violation. But three scalar keys are
#: genuinely needed to render honestly: ``tool_status`` is what distinguishes a
#: permission prompt from ordinary progress, and ``detail`` is what lets a line say
#: "Opening Calculator" instead of "desktop.open: running".
#:
#: An explicit allowlist is used rather than a denylist because a denylist silently
#: forwards whatever nobody thought to forbid. Everything else - including
#: ``source_event`` - is never read.
_METADATA_ALLOWLIST = ("tool", "tool_status", "detail")


def _allowed_metadata(payload: Any) -> dict[str, Any]:
    """Extract only the allowlisted metadata keys from a payload."""
    metadata = getattr(payload, "metadata", None)
    if not isinstance(metadata, Mapping):
        return {}
    return {key: metadata[key] for key in _METADATA_ALLOWLIST if key in metadata}


def _fields(payload: Any) -> dict[str, Any]:
    """Pull the display-relevant fields out of any supported payload shape.

    Accepts an ``AgentEvent``, a mapping, or anything else exposing the same
    attributes. Only the allowlisted metadata is consulted; the rest of the raw
    backend payload is left untouched.
    """
    if isinstance(payload, Mapping):
        base = {
            "type": payload.get("type") or payload.get("kind") or payload.get("event")
                    or payload.get("name") or "",
            "status": payload.get("status") or payload.get("state") or "",
            "message": payload.get("message") or payload.get("detail") or payload.get("text") or "",
            "task_id": payload.get("task_id") or payload.get("run_id") or payload.get("id") or "",
            "summary": payload.get("summary") or payload.get("title") or "",
            "timestamp": payload.get("timestamp"),
            "tool": payload.get("tool") or payload.get("tool_name") or "",
            "tool_status": payload.get("tool_status") or "",
            "detail": payload.get("detail") or "",
        }
        for key, value in _allowed_metadata(payload).items():
            base.setdefault(key, "")
            if not base.get(key):
                base[key] = value
        return base
    # Duck-typed object (``AgentEvent`` and anything shaped like it).
    if hasattr(payload, "type") or hasattr(payload, "message"):
        base = {
            "type": getattr(payload, "type", "") or getattr(payload, "kind", "") or "",
            "status": getattr(payload, "status", "") or "",
            "message": getattr(payload, "message", "") or "",
            "task_id": getattr(payload, "task_id", "") or "",
            "summary": getattr(payload, "summary", "") or "",
            "timestamp": getattr(payload, "timestamp", None),
            "tool": getattr(payload, "tool", "") or "",
            "tool_status": getattr(payload, "tool_status", "") or "",
            "detail": getattr(payload, "detail", "") or "",
        }
        for key, value in _allowed_metadata(payload).items():
            if not base.get(key):
                base[key] = value
        return base
    return {}


def _resolve(fields: dict[str, Any]) -> CompanionEventKind | None:
    """Decide which companion event, if any, a backend payload maps to."""
    name = _normalise(fields.get("type"))

    # An explicit tool status is the most specific signal available, so it wins.
    # It also wins when it means "nothing to show": a tool that reports ``cancelled``
    # has decided its own outcome, and the task-level terminal event is still to
    # come. Treating it as a generic step would render a cancellation the backend
    # never claimed at task level.
    tool_status = _normalise(fields.get("tool_status"))
    if tool_status:
        return _TOOL_STATUS_MAP.get(tool_status)

    if name in _NAME_MAP:
        return _NAME_MAP[name]

    # Generic ``status``/``activity`` envelopes carry the meaning in ``status``.
    status = _normalise(fields.get("status"))
    if status and status in _STATUS_MAP:
        return _STATUS_MAP[status]

    # A tool named on a generic envelope implies a step.
    if fields.get("tool") and name in ("activity", "tool", "tool_update", ""):
        return CompanionEventKind.STEP

    return None


def summarise(kind: CompanionEventKind, fields: dict[str, Any]) -> str:
    """Build the short label shown in the scannable progress list.

    Prefers a real subject (a tool name plus its detail) over a generic verb,
    because "desktop.open: Opening Calculator" tells a user something and
    "Working..." does not.

    The bridge already formats ``message`` as ``"<tool>: <status>"``, so the tool
    name is frequently *already* the prefix of the message. Re-prepending it
    produced "desktop.open: desktop.open: running", hence the startswith guards.
    """
    explicit = sanitize_text(fields.get("summary"), limit=MAX_SUMMARY_CHARS)
    if explicit:
        return explicit
    tool = sanitize_text(fields.get("tool"), limit=28)
    detail = sanitize_text(fields.get("detail"), limit=MAX_SUMMARY_CHARS)
    message = sanitize_text(fields.get("message"), limit=MAX_SUMMARY_CHARS)

    subject = detail or message
    if not subject:
        return _DEFAULT_SUMMARY[kind]
    if not tool or subject.startswith(tool):
        return subject[:MAX_SUMMARY_CHARS]
    combined = f"{tool}: {subject}"
    if len(combined) <= MAX_SUMMARY_CHARS:
        return combined
    return combined[: MAX_SUMMARY_CHARS - 1].rstrip() + "\u2026"


_DEFAULT_SUMMARY: dict[CompanionEventKind, str] = {
    CompanionEventKind.ACTIVATED: "Awakened",
    CompanionEventKind.TASK_ACCEPTED: "Task accepted",
    CompanionEventKind.THINKING: "Planning",
    CompanionEventKind.EXECUTING: "Working",
    CompanionEventKind.STEP: "Working",
    CompanionEventKind.VERIFICATION: "Verifying",
    CompanionEventKind.RECOVERY: "Recovering",
    CompanionEventKind.PERMISSION: "Needs permission",
    CompanionEventKind.CANCELLATION_REQUESTED: "Stopping",
    CompanionEventKind.TASK_COMPLETED: "Done",
    CompanionEventKind.TASK_FAILED: "Failed",
    CompanionEventKind.TASK_CANCELLED: "Stopped",
    CompanionEventKind.ERROR: "Failed",
    CompanionEventKind.RETRACTING: "Retracting",
    CompanionEventKind.SLEEPING: "Sleeping",
}

#: Statuses that read as failures get a plain, non-alarming terminal line rather
#: than whatever long text the backend attached.
_TERMINAL_DEFAULT = {
    CompanionEventKind.TASK_COMPLETED: "Done.",
    CompanionEventKind.TASK_FAILED: "I couldn't complete that.",
    CompanionEventKind.TASK_CANCELLED: "Stopped.",
    CompanionEventKind.ERROR: "I couldn't complete that.",
}


def map_backend_event(payload: Any) -> CompanionEvent | None:
    """Convert one backend event into a redacted companion event.

    Returns ``None`` when the payload has no displayable meaning. That is the
    filter which keeps internal logs out of the companion, so callers should treat
    ``None`` as "nothing to show" rather than as an error.
    """
    if payload is None:
        return None

    # Already a companion event: re-sanitise rather than trust it blindly.
    if isinstance(payload, CompanionEvent):
        return CompanionEvent(
            kind=payload.kind,
            message=sanitize_text(payload.message),
            task_id=sanitize_text(payload.task_id, limit=128),
            summary=sanitize_text(payload.summary, limit=MAX_SUMMARY_CHARS),
            timestamp=payload.timestamp,
        )

    fields = _fields(payload)
    if not fields:
        return None
    kind = _resolve(fields)
    if kind is None:
        return None

    message = sanitize_text(fields.get("detail")) or sanitize_text(fields.get("message"))
    if not message:
        message = _TERMINAL_DEFAULT.get(kind, "")

    task_id = sanitize_text(fields.get("task_id"), limit=128)
    # A task id is identity, not prose: keep it machine-clean even if the backend
    # sent something odd, because the controller compares it for staleness.
    task_id = "".join(ch for ch in task_id if ch.isalnum() or ch in "-_")[:128]

    try:
        timestamp = float(fields.get("timestamp")) if fields.get("timestamp") else 0.0
    except (TypeError, ValueError):
        timestamp = 0.0
    # ``isfinite`` rather than ``> 0``: NaN compares False against everything, so a
    # NaN timestamp would sail through a positivity check and reach the view.
    if not math.isfinite(timestamp) or timestamp <= 0.0:
        timestamp = time.time()

    return CompanionEvent(
        kind=kind,
        message=message,
        task_id=task_id,
        summary=summarise(kind, fields),
        timestamp=timestamp,
    )