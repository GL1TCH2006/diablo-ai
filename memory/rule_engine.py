from __future__ import annotations

from config.permissions import Decision, PermissionLevel

_RANK = {"block": 2, "confirm": 1}


class RuleEngine:
    """Applies rules to permission decisions. RESTRICT-ONLY:

    - a matched BLOCK rule always blocks, even if the base decision allowed the call
    - a matched CONFIRM rule forces confirmation (raises the level at best)
    - rules never lower the permission level, never unblock, never auto-allow
    - most restrictive wins (block > confirm > base)
    """

    def __init__(self, store, project: str = ""):
        self._store = store
        self._project = project
        self.last_applied: list[str] = []

    @property
    def project(self) -> str:
        return self._project

    def set_project(self, project: str) -> None:
        self._project = project

    def apply(self, decision: Decision, tool_name: str, args: dict) -> tuple[Decision, list[str]]:
        applied: list[str] = []
        if self._store is None or decision is None:
            return decision, applied
        if decision.blocked:
            return decision, applied  # never unblock
        rules = self._store.active(self._project)
        if not rules:
            self.last_applied = applied
            return decision, applied

        matched = [r for r in rules if r.matches(tool_name, args or {}, self._project)]
        if not matched:
            self.last_applied = applied
            return decision, applied

        matched.sort(key=lambda r: _RANK.get(r.action, 0), reverse=True)
        for rule in matched:
            if rule.action == "block":
                reason = f"Rule '{rule.name}' blocked this call." + (f" ({rule.reason})" if rule.reason else "")
                applied.append(f"{rule.id}:block")
                self.last_applied = applied
                return Decision(PermissionLevel.BLOCKED, False, False, reason), applied
        for rule in matched:
            if rule.action == "confirm":
                reason = f"Rule '{rule.name}' requires confirmation before this call."
                applied.append(f"{rule.id}:confirm")
                self.last_applied = applied
                return Decision(PermissionLevel.SENSITIVE, False, True, reason), applied
        self.last_applied = applied
        return decision, applied

    def explain(self) -> str:
        if not self.last_applied:
            return "No rule influenced the last permission decision."
        return "; ".join(self.last_applied) + " (restrict-only)"