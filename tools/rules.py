from __future__ import annotations

from config.settings import Settings

from .registry import ToolResult


def _store(settings: Settings):
    from memory.rules_store import RuleStore
    return RuleStore(settings.memory_dir / "rules.json")


def rules_create(name: str, action: str = "block", description: str = "", scope: str = "global",
                 priority: int = 50, if_tool: str = "", if_content: str = "", reason: str = "",
                 project: str = "", task_id: str = "", settings: Settings | None = None) -> ToolResult:
    from memory.rules_store import RuleConflict
    try:
        rule = _store(settings).create(name, action=action, description=description, scope=scope,
                                       priority=priority, if_tool=if_tool, if_content=if_content,
                                       reason=reason, project=project, task_id=task_id)
    except RuleConflict as exc:
        return ToolResult(False, error=str(exc))
    except ValueError as exc:
        return ToolResult(False, error=str(exc))
    return ToolResult(True, output=f"rule created: {rule.id} {rule.name} ({rule.action}, scope {rule.scope})")


def rules_list(settings: Settings | None = None) -> ToolResult:
    rules = _store(settings).list()
    if not rules:
        return ToolResult(True, output="(no rules)")
    lines = []
    for r in sorted(rules, key=lambda x: (-x.priority, x.name)):
        state = "on" if r.enabled else "off"
        lines.append(f"{r.id} [{state}] {r.name} -> {r.action} pri={r.priority} scope={r.scope}")
        if r.scope == "task":
            lines[-1] += f" (task={r.extra.get('task_id') or 'UNBOUND-not-enforced'})"
        elif r.scope == "tool" and not r.if_tool:
            lines[-1] += " (UNBOUND-not-enforced: no if_tool)"
        if r.if_tool or r.if_content:
            lines[-1] += f" (tool={r.if_tool or '-'}, content={r.if_content or '-'})"
    return ToolResult(True, output="\n".join(lines))


def rules_check(rule_id: str, settings: Settings | None = None) -> ToolResult:
    """Report whether a rule is enforced, and under what scope.

    A rule whose scope cannot be enforced - a tool rule with no tool, a task rule
    with no task - is reported as not enforced. That was the whole defect: such a
    rule was previously reported as active while behaving as global.
    """
    rule = _store(settings).get(rule_id)
    if rule is None:
        return ToolResult(False, error=f"no rule: {rule_id}")
    if not rule.enabled:
        return ToolResult(True, output=f"rule '{rule.name}' is DISABLED - not enforced.")
    if rule.scope == "tool" and not str(rule.if_tool or "").strip():
        return ToolResult(True, output=(
            f"rule '{rule.name}' is ENABLED but scope 'tool' names no tool - "
            "NOT enforced. Set if_tool, or change the scope."))
    if rule.scope == "task" and not str(rule.extra.get("task_id") or "").strip():
        return ToolResult(True, output=(
            f"rule '{rule.name}' is ENABLED but scope 'task' has no task id - "
            "NOT enforced. Recreate it with a task_id."))
    return ToolResult(True, output=(
        f"rule '{rule.name}' is ENABLED -> {rule.action} (restrict-only, scope {rule.scope})"))


def rules_get(rule_id: str, settings: Settings | None = None) -> ToolResult:
    rule = _store(settings).get(rule_id)
    if rule is None:
        return ToolResult(False, error=f"no rule: {rule_id}")
    return ToolResult(True, output=str(rule))


def rules_update(rule_id: str, name: str = "", action: str = "", description: str = "", priority: int = -1,
                 if_tool: str = "", if_content: str = "", reason: str = "", settings: Settings | None = None) -> ToolResult:
    changes = {}
    if name:
        changes["name"] = name
    if action:
        if action not in ("block", "confirm"):
            return ToolResult(False, error="action must be 'block' or 'confirm'")
        changes["action"] = action
    if description:
        changes["description"] = description
    if priority >= 0:
        changes["priority"] = priority
    if if_tool:
        changes["if_tool"] = if_tool
    if if_content:
        changes["if_content"] = if_content
    if reason:
        changes["reason"] = reason
    try:
        updated = _store(settings).update(rule_id, **changes)
    except Exception as exc:
        return ToolResult(False, error=str(exc))
    if updated is None:
        return ToolResult(False, error=f"no rule: {rule_id}")
    return ToolResult(True, output=f"updated rule {rule_id}")


def rules_enable(rule_id: str, enabled: bool = True, settings: Settings | None = None) -> ToolResult:
    ok = _store(settings).enable(rule_id, enabled)
    if not ok:
        return ToolResult(False, error=f"no rule: {rule_id}")
    return ToolResult(True, output=f"rule {'enabled' if enabled else 'disabled'}: {rule_id}")


def rules_delete(rule_id: str, settings: Settings | None = None) -> ToolResult:
    ok = _store(settings).delete(rule_id)
    if not ok:
        return ToolResult(False, error=f"no rule: {rule_id}")
    return ToolResult(True, output=f"rule deleted: {rule_id}")


def rules_conflicts(settings: Settings | None = None) -> ToolResult:
    store = _store(settings)
    issues = []
    for rule in store.list():
        issue = store.detect_conflict(rule)
        if issue:
            issues.append(f"{rule.id}: {issue}")
    if not issues:
        return ToolResult(True, output="(no conflicts detected)")
    return ToolResult(True, output="\n".join(issues))