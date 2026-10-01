from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_setup import get_logger

from .storage import load_json, save_json

log = get_logger("rules_store")

SCOPES = ("global", "project", "tool", "task")

#: The scope a rule applies when nothing narrower was given. Deliberately
#: ``global``, because that is the honest answer for a rule that names no
#: narrower scope - and it is a restriction, so applying it broadly can only
#: ever narrow authority. What must never happen is a rule declared ``tool`` or
#: ``task`` quietly behaving as ``global``; see :meth:`Rule.matches`.
DEFAULT_SCOPE = "global"


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

    def matches(self, tool_name: str, args: dict, project: str = "",
                task_id: str = "") -> bool:
        """Whether this rule applies to one call.

        Scope is now enforced, not merely recorded:

        ``project``
            Only when the rule's project matches, or the rule names none.
        ``tool``
            Only when the rule's ``if_tool`` matches the call's tool. A tool
            rule that names no tool is refused rather than treated as global,
            because the whole point of the scope is to narrow it.
        ``task``
            Only when the rule's ``extra["task_id"]`` matches this call's task.
            A task rule with no task id is refused for the same reason.

        The previous behaviour let both of those fall through to "matches
        everything", so a rule the user believed was scoped to one tool
        silently applied to all of them. Narrowing a rule can only reduce what
        it blocks, but it was reporting a restriction it was not applying, so
        the fix is to make the scope real.
        """
        scope = str(self.scope or DEFAULT_SCOPE)

        if scope == "project":
            want = self.extra.get("project")
            if want not in (None, "") and want != project:
                return False

        if scope == "tool":
            want_tool = str(self.if_tool or "").strip()
            if not want_tool:
                # A tool rule that names no tool cannot be a tool rule.
                return False
            if not _tool_names_match(want_tool, tool_name):
                return False

        if scope == "task":
            want_task = str(self.extra.get("task_id") or "").strip()
            if not want_task:
                return False
            if not task_id or want_task != str(task_id):
                return False

        if self.if_tool:
            if not _tool_names_match(str(self.if_tool), tool_name):
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
        data = load_json(self._file, default=None)
        if not isinstance(data, dict):
            log.warning("rules store unreadable, keeping no rules")
            return
        try:
            self._mtime = self._file.stat().st_mtime_ns
        except OSError:
            self._mtime = 0
        raw_rules = data.get("rules")
        self._rules = [
            Rule.from_dict(r)
            for r in (raw_rules if isinstance(raw_rules, list) else [])
            if isinstance(r, dict)
        ]

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
        """Persist atomically.

        Raises on failure. Swallowing it here let ``create`` return a rule that
        was never written, so the caller believed a restriction was in force
        when no restart would ever load it.
        """
        save_json(self._file, {"rules": [r.as_dict() for r in self._rules]})

    def create(self, name: str, action: str = "block", description: str = "", scope: str = "global",
               priority: int = 50, if_tool: str = "", if_content: str = "", reason: str = "",
               project: str = "", source: str = "user", task_id: str = "") -> Rule:
        if not name or not name.strip():
            raise ValueError("empty rule name")
        if action not in ("block", "confirm"):
            raise ValueError("action must be 'block' or 'confirm'")
        scope = _normalise_scope(scope)
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        if scope == "tool" and not str(if_tool or "").strip():
            # Rejected rather than silently downgraded to global. A user who
            # asked for a tool-scoped rule and omitted the tool has made a
            # mistake; widening it to everything is the one interpretation that
            # cannot be what they meant.
            raise ValueError("scope 'tool' requires if_tool to name the tool")
        if scope == "task" and not str(task_id or "").strip():
            raise ValueError("scope 'task' requires task_id")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        extra: dict = {}
        if project:
            extra["project"] = project
        if scope == "task" and task_id:
            extra["task_id"] = str(task_id)
        rule = Rule(
            id=uuid.uuid4().hex[:12], name=_clean(name, 120), action=action,
            description=_clean(description, 1000), scope=scope, priority=max(1, min(100, int(priority))),
            if_tool=_clean(if_tool, 120), if_content=_clean(if_content, 500),
            reason=_clean(reason, 500), source=source, created_at=stamp, updated_at=stamp,
            extra=extra,
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

    def active(self, project: str = "", task_id: str = "") -> list[Rule]:
        """Enabled rules, filtered by the scopes this call can be judged under.

        A ``task`` rule is only offered when its task matches, and a ``project``
        rule only when its project matches. Filtering here as well as in
        :meth:`Rule.matches` keeps the rendered prompt honest: the model is only
        shown rules that actually apply to the call it is making.
        """
        self._sync()
        out: list[Rule] = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            if rule.scope == "project":
                want = rule.extra.get("project")
                if want not in (None, "") and want != project:
                    continue
            if rule.scope == "task":
                want_task = str(rule.extra.get("task_id") or "")
                if not want_task or not task_id or want_task != str(task_id):
                    continue
            if rule.scope == "tool" and not str(rule.if_tool or "").strip():
                continue
            out.append(rule)
        return out

    def update(self, rule_id: str, **changes) -> Rule | None:
        rule = self.get(rule_id)
        if rule is None:
            return None
        # ``enabled`` is deliberately not settable here. Toggling a restriction
        # off is a widening, and it goes through ``enable`` (and the
        # explicit_confirmation on ``rules.enable``) so it can never be an
        # unconfirmed side effect of editing some other field.
        mutable = {"name", "description", "scope", "priority", "action",
                   "if_tool", "if_content", "reason", "source"}
        for key, value in changes.items():
            if key in mutable:
                setattr(rule, key, value)
        new_scope = _normalise_scope(str(rule.scope or DEFAULT_SCOPE))
        if new_scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        if new_scope == "tool" and not str(rule.if_tool or "").strip():
            raise ValueError("scope 'tool' requires if_tool to name the tool")
        if new_scope == "task" and not str(rule.extra.get("task_id") or "").strip():
            raise ValueError("scope 'task' requires task_id")
        rule.scope = new_scope
        rule.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save()
        return rule

    def bind_task(self, rule_id: str, task_id: str) -> Rule | None:
        """Attach a task id to a task-scoped rule that does not have one yet.

        Kept separate from :meth:`update` so the tool layer cannot reach it by
        accident through the field whitelist.
        """
        rule = self.get(rule_id)
        if rule is None:
            return None
        rule.extra = {**(rule.extra or {}), "task_id": str(task_id)}
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
        """Conflict: same trigger but opposite action, enforced by a higher-priority rule.

        Every rule is considered, not only those active under some other
        project. Previously this called ``active()`` with no project, which
        silently excluded every project-scoped rule from conflict detection.
        """
        for rule in self._rules:
            if not rule.enabled or rule.id == candidate.id:
                continue
            if rule.scope != candidate.scope:
                continue
            if rule.extra.get("project") != candidate.extra.get("project"):
                continue
            if rule.extra.get("task_id") != candidate.extra.get("task_id"):
                continue
            if not _tool_names_match(rule.if_tool or "", candidate.if_tool or "", prefix_only=True):
                continue
            if (rule.if_content or "").lower() != (candidate.if_content or "").lower():
                continue
            if rule.action != candidate.action:
                if rule.priority > candidate.priority:
                    return (f"conflict: rule '{candidate.name}' would be overridden by "
                            f"higher-priority rule '{rule.name}' ({rule.action}).")
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

    def render_active(self, project: str = "", limit: int = 0, task_id: str = "") -> str:
        rules = self.active(project, task_id)
        if limit:
            rules = rules[:limit]
        if not rules:
            return ""
        lines = ["#ACTIVE RULES (RESTRICT-ONLY; must be obeyed; never overridden for convenience)"]
        for rule in sorted(rules, key=lambda r: (-_rank(r.priority), r.name)):
            head = f"- [{rule.action.upper()}] {rule.name} (priority {rule.priority}, scope {rule.scope})"
            extra = []
            if rule.if_tool:
                extra.append(f"tool←{rule.if_tool}")
            if rule.if_content:
                extra.append(f"content←{rule.if_content}")
            if rule.scope == "task":
                extra.append(f"task←{rule.extra.get('task_id', '')}")
            if rule.scope == "project":
                extra.append(f"project←{rule.extra.get('project', '')}")
            if extra:
                head += " | " + ", ".join(extra)
            lines.append(head)
            if rule.description:
                lines.append(f"    {rule.description}")
        return "\n".join(lines)


def _normalise_scope(scope: str) -> str:
    """Accept only a known scope, case- and space-insensitively.

    An unrecognised scope resolves to the default rather than to anything that
    widens the rule, and :func:`RuleStore.create` then rejects it outright.
    """
    value = str(scope or "").strip().lower().replace("-", "_")
    aliases = {"": DEFAULT_SCOPE, "all": "global", "any": "global"}
    return aliases.get(value, value)


def _tool_names_match(want: str, got: str, prefix_only: bool = False) -> bool:
    """Whether a rule's tool pattern covers the called tool.

    Matching is by exact name, or by dotted-prefix, so ``git`` covers
    ``git.push`` and ``git.push`` covers ``git``. An empty pattern matches
    everything, which is what a ``global`` rule with no tool means.
    """
    want = str(want or "").strip().lower()
    got = str(got or "").strip().lower()
    if not want or not got:
        return True
    if want == got:
        return True
    if prefix_only:
        return want.startswith(got + ".") or got.startswith(want + ".")
    return want.startswith(got + ".") or got.startswith(want + ".")


def _rank(priority: int) -> int:
    return priority


def _clean(text: str, max_chars: int) -> str:
    return "".join(ch for ch in (text or "") if ch not in "\0\x01").strip()[:max_chars]