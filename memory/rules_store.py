from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_setup import get_logger

log = get_logger("rules_store")

SCOPES = ("global", "project", "tool", "task")


@dataclass
class Rule:
    id: str
    name: str
    description: str = ""
    scope: str = "global"
    priority: int = 50
    enabled: bool = True
    action: str = "block"  # block | confirm
    if_tool: str = ""
    if_content: str = ""
    reason: str = ""
    source: str = "user"
    created_at: str = ""
    updated_at: str = ""
    extra: dict = field(default_factory=dict)

    def matches(self, tool_name: str, args: dict, project: str = "") -> bool:
        if self.scope == "project" and (not project or self.extra.get("project") not in (None, "", project)):
            return False
        if self.if_tool:
            if tool_name != self.if_tool and not tool_name.startswith(self.if_tool + ".") and not self.if_tool.startswith(tool_name + "."):
                if not (tool_name.startswith(self.if_tool) or self.if_tool.startswith(tool_name)):
                    return False
        if self.if_content:
            import json as _json
            try:
                haystack = _json.dumps(args, ensure_ascii=False).lower()
            except (TypeError, ValueError):
                haystack = " ".join(str(v) for v in (args or {}).values()).lower()
            if self.if_content.lower() not in haystack:
                return False
        return True

    def as_dict(self) -> dict:
        data = {k: getattr(self, k) for k in (
            "id", "name", "description", "scope", "priority", "enabled", "action",
            "if_tool", "if_content", "reason", "source", "created_at", "updated_at", "extra",
        )}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        supported = {k for k in cls.__dataclass_fields__}
        clean = {k: v for k, v in (data or {}).items() if k in supported}
        return cls(**clean)


class RuleConflict(Exception):
    pass


class RuleStore:
    def __init__(self, path: Path, max_active_in_prompt: int = 10):
        self._file = Path(path)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._max_active = max_active_in_prompt
        self._rules: list[Rule] = []
        self._mtime = 0
        self._load()

    def _load(self) -> None:
        if not self._file.is_file():
            self._mtime = 0
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._mtime = self._file.stat().st_mtime_ns
        except (ValueError, OSError) as exc:
            log.warning("rules store unreadable (%s)", exc)
            return
        self._rules = [Rule.from_dict(r) for r in data.get("rules", [])]

    def _sync(self) -> None:
        """Reload rules if another process/instance changed the file since our last read."""
        if not self._file.is_file():
            self._mtime = 0
            self._rules = []
            return
        try:
            mtime = self._file.stat().st_mtime_ns
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    def _save(self) -> None:
        try:
            self._file.write_text(json.dumps({"rules": [r.as_dict() for r in self._rules]},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save rules store: %s", exc)

    def create(self, name: str, action: str = "block", description: str = "", scope: str = "global",
               priority: int = 50, if_tool: str = "", if_content: str = "", reason: str = "",
               project: str = "", source: str = "user") -> Rule:
        if not name or not name.strip():
            raise ValueError("empty rule name")
        if action not in ("block", "confirm"):
            raise ValueError("action must be 'block' or 'confirm'")
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        rule = Rule(
            id=uuid.uuid4().hex[:12], name=_clean(name, 120), action=action,
            description=_clean(description, 1000), scope=scope, priority=max(1, min(100, int(priority))),
            if_tool=_clean(if_tool, 120), if_content=_clean(if_content, 500),
            reason=_clean(reason, 500), source=source, created_at=stamp, updated_at=stamp,
            extra={"project": project} if project else {},
        )
        conflicts = self.detect_conflict(rule)
        if conflicts:
            raise RuleConflict(conflicts)
        self._rules.append(rule)
        self._save()
        return rule

    def get(self, rule_id: str) -> Rule | None:
        self._sync()
        return next((r for r in self._rules if r.id == rule_id), None)

    def list(self, include_disabled: bool = True) -> list[Rule]:
        self._sync()
        return [r for r in self._rules if include_disabled or r.enabled]

    def active(self, project: str = "") -> list[Rule]:
        self._sync()
        return [r for r in self._rules if r.enabled and (r.scope != "project" or r.extra.get("project") == project)]

    def update(self, rule_id: str, **changes) -> Rule | None:
        rule = self.get(rule_id)
        if rule is None:
            return None
        for key, value in changes.items():
            if hasattr(rule, key) and key not in ("id", "created_at"):
                setattr(rule, key, value)
        rule.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        return rule

    def enable(self, rule_id: str, enabled: bool = True) -> bool:
        rule = self.get(rule_id)
        if rule is None:
            return False
        rule.enabled = enabled
        rule.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        return True

    def delete(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.id != rule_id]
        changed = len(self._rules) != before
        if changed:
            self._save()
        return changed

    def clear(self) -> None:
        self._rules = []
        self._save()

    def detect_conflict(self, candidate: Rule) -> str:
        """Conflict: same trigger (scope+tool+content) but opposite action enforced by a higher-priority rule."""
        for rule in self.active():
            if rule.id == candidate.id:
                continue
            if rule.if_tool != candidate.if_tool and not _overlap_tool(rule.if_tool, candidate.if_tool):
                continue
            if (rule.if_content or "").lower() != (candidate.if_content or "").lower():
                continue
            if rule.extra.get("project") != candidate.extra.get("project"):
                continue
            if rule.action != candidate.action:
                if rule.priority > candidate.priority:
                    return f"conflict: rule '{candidate.name}' would be overridden by higher-priority rule '{rule.name}' ({rule.action})."
                return ""  # candidate wins (higher/equal priority), fully deterministic
            return ""  # same action, harmless overlap
        return ""

    def detect_conflicts_for_all(self) -> str:
        issues = []
        for rule in self._rules:
            issue = self.detect_conflict(rule)
            if issue:
                issues.append(f"{rule.id}: {issue}")
        return "\n".join(issues)[:2000]

    def render_active(self, project: str = "", limit: int = 0) -> str:
        rules = self.active(project)
        if limit:
            rules = rules[:limit]
        if not rules:
            return ""
        lines = ["#ACTIVE RULES (RESTRICT-ONLY; must be obeyed; never overridden for convenience)"]
        for rule in sorted(rules, key=lambda r: (-_rank(r.priority), r.name)):
            head = f"- [{rule.action.upper()}] {rule.name} (priority {rule.priority})"
            extra = []
            if rule.if_tool:
                extra.append(f"tool←{rule.if_tool}")
            if rule.if_content:
                extra.append(f"content←{rule.if_content}")
            if extra:
                head += " | " + ", ".join(extra)
            lines.append(head)
            if rule.description:
                lines.append(f"    {rule.description}")
        return "\n".join(lines)


def _overlap_tool(a: str, b: str) -> bool:
    if not a or not b:
        return True
    return a == b or a.startswith(b) or b.startswith(a)


def _rank(priority: int) -> int:
    return priority


def _clean(text: str, max_chars: int) -> str:
    return "".join(ch for ch in (text or "") if ch not in "\0\x01").strip()[:max_chars]