from __future__ import annotations

import time
import uuid
from pathlib import Path

from core.logging_setup import get_logger

from .sanitizer import SanitizeDecision, sanitize_memory_text, render_untrusted_block
from .storage import load_json, save_json

log = get_logger("memory_store")

_CATEGORIES = {
    "facts": "Things Diablo already verified or directly observed.",
    "preferences": "How the user wants Diablo to behave (UI, style, defaults).",
    "tasks": "Fields: what the user is actively working on.",
    "notes": "Small durable notes the user explicitly asked to keep.",
    "projects": "Anything about a specific project (paths, constraints, status).",
}

_MAX_KEY = 120
_MAX_VALUE = 20000
_MAX_NOTE = 500


class MemoryWriteRefused(ValueError):
    """A memory write was refused by the content or write policy.

    A distinct type so the tool layer can turn it into an honest tool error
    rather than a generic failure. The message is written for the user and never
    contains the offending value.
    """

    def __init__(self, message: str, *, reason: str = "refused") -> None:
        super().__init__(message)
        self.reason = reason


class MemoryStore:
    """Long-term key-value memory.

    Writes pass the content policy in :mod:`memory.sanitizer` before they are
    persisted, and persistence is atomic (write to a temporary file in the same
    directory, then replace), so an interrupted write can never leave a
    half-written JSON document that would be read back as authoritative on the
    next start.
    """

    def __init__(self, path: Path):
        self._file = Path(path)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if not self._file.is_file():
            return {"entries": [], "categories": dict(_CATEGORIES)}
        data = load_json(self._file, default=None)
        if not isinstance(data, dict):
            # load_json swallows both a missing file and unreadable JSON. A
            # corrupt document is reported once and treated as empty rather than
            # raising, so a bad file cannot stop Diablo from starting.
            if self._file.is_file():
                log.warning("memory store unreadable, starting fresh")
            return {"entries": [], "categories": dict(_CATEGORIES)}
        entries = data.get("entries")
        data["entries"] = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        categories = data.get("categories")
        data["categories"] = {**dict(_CATEGORIES), **(categories if isinstance(categories, dict) else {})}
        return data

    def _save(self) -> None:
        """Persist atomically.

        Raises on failure: a memory write that cannot be persisted must not be
        reported as remembered, and swallowing the error here is what previously
        let the caller believe a write had landed when it had not.
        """
        save_json(self._file, self._data)

    def categories(self) -> dict[str, str]:
        return dict(self._data["categories"])

    def remember(self, key: str, value: str, category: str = "notes", note: str = "") -> dict:
        if not key or not key.strip():
            raise ValueError("empty key")
        if category not in self.categories():
            raise ValueError(f"unknown category: {category}")
        clean_key = _safe(key, _MAX_KEY)
        clean_value, clean_note = _sanitize_fields(value, note)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for entry in self._data["entries"]:
            if entry.get("key") == clean_key:
                entry.update({"value": clean_value, "note": clean_note, "updated_at": stamp})
                self._save()
                return entry
        entry = {
            "id": uuid.uuid4().hex[:12],
            "key": clean_key,
            "value": clean_value,
            "category": category,
            "note": clean_note,
            "created_at": stamp,
            "updated_at": stamp,
        }
        self._data["entries"].append(entry)
        self._save()
        return entry

    def get(self, key: str) -> dict | None:
        for entry in self._data["entries"]:
            if entry.get("key") == key:
                return dict(entry)
        return None

    def search(self, query: str, category: str = "", limit: int = 15) -> list[dict]:
        q = query.lower()
        hits = []
        for entry in self._data["entries"]:
            if category and entry.get("category") != category:
                continue
            haystack = f"{entry.get('key')} {entry.get('value')} {entry.get('note')}".lower()
            if not q or q in haystack:
                hits.append(dict(entry))
            if len(hits) >= limit:
                break
        return hits

    def list(self, category: str = "") -> list[dict]:
        return [dict(e) for e in self._data["entries"] if (not category or e.get("category") == category)]

    def update(self, key: str, value: str) -> dict | None:
        clean_value, _ = _sanitize_fields(value, None)
        for entry in self._data["entries"]:
            if entry.get("key") == key:
                entry["value"] = clean_value
                entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._save()
                return dict(entry)
        return None

    def delete(self, key: str) -> bool:
        before = len(self._data["entries"])
        self._data["entries"] = [e for e in self._data["entries"] if e.get("key") != key]
        changed = len(self._data["entries"]) != before
        if changed:
            self._save()
        return changed

    def clear(self) -> None:
        self._data["entries"] = []
        self._save()

    def render_active(self, limit_chars: int = 3000) -> str:
        """Render stored memory as DATA for injection into the system prompt.

        Every line is passed through the content policy again on the way out, not
        only on the way in. That covers entries written before this module
        existed, and entries edited directly into the JSON file.
        """
        if not self._data["entries"]:
            return ""
        chunks: list[str] = []
        used = 0
        for entry in self._data["entries"]:
            raw = str(entry.get("value") or "")[:400]
            rendered = sanitize_memory_text(raw)
            if rendered.decision is SanitizeDecision.REJECT:
                # A credential that somehow reached the file (hand edit, older
                # build) is not injected at all.
                continue
            value = rendered.text
            block = f"- [{entry.get('category')}] {entry.get('key')}: {value}"
            if used + len(block) > limit_chars:
                chunks.append("... (memory truncated)")
                break
            chunks.append(block)
            used += len(block)
        if not chunks:
            return ""
        return render_untrusted_block("\n".join(chunks))


def _sanitize_fields(value: str, note: str | None) -> tuple[str, str]:
    """Apply the content policy to a value, then to an optional note."""
    result = sanitize_memory_text(value)
    if result.decision is SanitizeDecision.REJECT:
        raise MemoryWriteRefused(
            "Refused: this looks like a credential "
            f"({result.secret_summary()}). Long-term memory is written to disk "
            "in plain text and re-read into every later request, so it is not a "
            "place to keep secrets. Put it in an environment variable or a .env "
            "file instead.",
            reason="credential_detected",
        )
    clean_value = result.text
    if note is None:
        return clean_value, ""
    note_result = sanitize_memory_text(note)
    if note_result.decision is SanitizeDecision.REJECT:
        # The note is optional; refusing the whole write over an optional note
        # would lose the value. Drop the note and say so via the log.
        log.warning("dropped memory note: credential detected")
        return clean_value, ""
    return clean_value, _safe(note_result.text, _MAX_NOTE)


def _safe(text: str, max_chars: int) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch not in "\0\x01").strip()
    return cleaned[:max_chars]
